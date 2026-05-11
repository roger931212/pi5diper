import hmac
import ipaddress
import os
import secrets
import threading
import time
import uuid
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse


_auth_failure_lock = threading.Lock()
_auth_failures: dict[str, list[float]] = {}


def normalize_case_id(case_id: str) -> str:
    try:
        return str(uuid.UUID(case_id))
    except Exception:
        raise HTTPException(status_code=404, detail="Case not found")


def resolve_image_path_safe(image_filename: str, upload_folder: str) -> str | None:
    image_filename = (image_filename or "").strip()
    if not image_filename:
        return None
    image_path = os.path.realpath(os.path.join(upload_folder, image_filename))
    if not image_path.startswith(os.path.realpath(upload_folder) + os.sep):
        return None
    return image_path


def _ip_in_entries(ip_str: str, entries: set[str]) -> bool:
    if not entries:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except Exception:
        return False
    for entry in entries:
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except Exception:
            if ip_str == entry:
                return True
    return False


def get_edge_client_ip(
    request: Request,
    *,
    edge_trust_proxy_headers: bool,
    edge_trusted_proxy_ips: set[str],
    edge_trust_x_forwarded_for: bool,
) -> str:
    direct_ip = request.client.host if request.client else "unknown"
    if not edge_trust_proxy_headers:
        return direct_ip
    if not _ip_in_entries(direct_ip, edge_trusted_proxy_ips):
        return direct_ip

    for h in ("cf-connecting-ip", "true-client-ip", "x-real-ip"):
        v = request.headers.get(h)
        if v:
            return v.strip()

    if edge_trust_x_forwarded_for:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return direct_ip


def _auth_rate_limited(client_ip: str, *, limit: int, window_sec: int) -> bool:
    if limit <= 0 or window_sec <= 0:
        return False
    now = time.time()
    cutoff = now - window_sec
    with _auth_failure_lock:
        hits = [ts for ts in _auth_failures.get(client_ip, []) if ts >= cutoff]
        _auth_failures[client_ip] = hits
        return len(hits) >= limit


def _record_auth_failure(client_ip: str, *, window_sec: int) -> None:
    now = time.time()
    cutoff = now - window_sec
    with _auth_failure_lock:
        hits = [ts for ts in _auth_failures.get(client_ip, []) if ts >= cutoff]
        hits.append(now)
        _auth_failures[client_ip] = hits


def _clear_auth_failures(client_ip: str) -> None:
    with _auth_failure_lock:
        _auth_failures.pop(client_ip, None)


def ensure_csp_nonce(request: Request) -> str:
    nonce = getattr(request.state, "csp_nonce", None)
    if not nonce:
        nonce = secrets.token_urlsafe(24)
        request.state.csp_nonce = nonce
    return nonce


async def edge_security_headers(request: Request, call_next, *, edge_cookie_secure: bool):
    nonce = ensure_csp_nonce(request)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    csp = "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net",
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
            "img-src 'self' data:",
            "font-src 'self' https://cdn.jsdelivr.net",
            "connect-src 'self'",
            "media-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "frame-ancestors 'none'",
        ]
    ) + ";"
    resp.headers["Content-Security-Policy"] = csp.replace("\r", " ").replace("\n", " ").strip()
    if edge_cookie_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=15552000; includeSubDomains"
    return resp


def extract_edge_token(request: Request, allow_query: bool = False) -> str:
    token = (request.headers.get("X-Edge-Token") or "").strip()
    if not token:
        auth = (request.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        token = (request.cookies.get("edge_token") or "").strip()
    if allow_query and not token:
        token = (request.query_params.get("edge_token") or "").strip()
    return token


def template_with_auth_cookie(
    request: Request,
    template_name: str,
    *,
    templates,
    edge_auth_token: str,
    edge_cookie_secure: bool,
    edge_cookie_max_age_sec: int,
):
    resp = templates.TemplateResponse(
        request=request,
        name=template_name,
        context={"request": request},
        headers={"Cache-Control": "no-store"},
    )
    query_token = (request.query_params.get("edge_token") or "").strip()
    if query_token:
        keep_params = [(k, v) for k, v in request.query_params.multi_items() if k != "edge_token"]
        clean_url = request.url.path
        if keep_params:
            clean_url = f"{clean_url}?{urlencode(keep_params)}"
        redirect = RedirectResponse(url=clean_url, status_code=303)
        if hmac.compare_digest(query_token, edge_auth_token):
            redirect.set_cookie(
                "edge_token",
                query_token,
                httponly=True,
                secure=edge_cookie_secure,
                samesite="strict",
                max_age=edge_cookie_max_age_sec,
            )
        redirect.headers["Cache-Control"] = "no-store"
        return redirect
    return resp


def verify_edge_access(
    request: Request,
    *,
    edge_allowed_ips: set[str],
    edge_auth_token: str,
    edge_auth_rate_limit: int = 20,
    edge_auth_rate_window_sec: int = 600,
    edge_trust_proxy_headers: bool = False,
    edge_trusted_proxy_ips: set[str] | None = None,
    edge_trust_x_forwarded_for: bool = False,
    logger,
):
    client_ip = get_edge_client_ip(
        request,
        edge_trust_proxy_headers=edge_trust_proxy_headers,
        edge_trusted_proxy_ips=edge_trusted_proxy_ips or set(),
        edge_trust_x_forwarded_for=edge_trust_x_forwarded_for,
    )
    if edge_allowed_ips and not _ip_in_entries(client_ip, edge_allowed_ips):
        logger.warning(f"[SEC] Blocked edge access from non-allowlisted IP: {client_ip}")
        raise HTTPException(status_code=403, detail="Access denied")
    allow_query_bootstrap = request.url.path == "/"
    token = extract_edge_token(request, allow_query=allow_query_bootstrap)
    if not token:
        raise HTTPException(status_code=401, detail="Missing edge token")
    if _auth_rate_limited(client_ip, limit=edge_auth_rate_limit, window_sec=edge_auth_rate_window_sec):
        logger.warning(f"[SEC] Edge auth rate limit exceeded from {client_ip}")
        raise HTTPException(status_code=429, detail="Too many authentication attempts")
    if not hmac.compare_digest(token, edge_auth_token):
        _record_auth_failure(client_ip, window_sec=edge_auth_rate_window_sec)
        logger.warning(f"[SEC] Invalid edge token from {client_ip}")
        raise HTTPException(status_code=401, detail="Unauthorized")
    _clear_auth_failures(client_ip)
    return True
