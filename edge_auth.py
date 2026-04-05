import hmac
import os
import uuid
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse


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
    logger,
):
    client_ip = request.client.host if request.client else "unknown"
    if edge_allowed_ips and client_ip not in edge_allowed_ips:
        logger.warning(f"[SEC] Blocked edge access from non-allowlisted IP: {client_ip}")
        raise HTTPException(status_code=403, detail="Access denied")
    allow_query_bootstrap = request.url.path == "/"
    token = extract_edge_token(request, allow_query=allow_query_bootstrap)
    if not token:
        raise HTTPException(status_code=401, detail="Missing edge token")
    if not hmac.compare_digest(token, edge_auth_token):
        logger.warning(f"[SEC] Invalid edge token from {client_ip}")
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True
