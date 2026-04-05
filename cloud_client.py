import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)
EXTERNAL_BASE = os.getenv("EXTERNAL_BASE", "https://your-app.zeabur.app").rstrip("/")
EXTERNAL_API_KEY = os.getenv("EXTERNAL_API_KEY", "").strip()
EXTERNAL_SIGNING_SECRET = os.getenv("EXTERNAL_SIGNING_SECRET", "").strip()
MAX_AI_SUGGESTION_CHARS = int(os.getenv("MAX_AI_SUGGESTION_CHARS", "1200"))
SEVERITY_SUGGESTIONS = {
    0: "目前未發現明顯尿布疹症狀，皮膚狀態正常。請繼續保持臀部清潔乾燥，勤換尿布。",
    1: "輕度尿布疹，皮膚出現局部發紅。建議加強清潔、保持乾燥，可使用含氧化鋅的護臀膏，並增加換尿布頻率。若 2-3 天未改善請就醫。",
    2: "中重度尿布疹，皮膚出現明顯紅腫或破損。建議盡快就醫，依醫師處方使用藥膏治療。在就醫前請保持患部乾燥並避免摩擦。",
}


_tls = threading.local()


def create_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.headers.update({"X-API-KEY": EXTERNAL_API_KEY})
    return s


def get_http() -> requests.Session:
    s = getattr(_tls, "http", None)
    if s is None:
        s = create_session()
        _tls.http = s
    return s


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _build_internal_signed_headers(raw_body: bytes) -> dict:
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    message = ts.encode("utf-8") + b"." + nonce.encode("utf-8") + b"." + (raw_body or b"")
    signature = hmac.new(
        EXTERNAL_SIGNING_SECRET.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Internal-Timestamp": ts,
        "X-Internal-Nonce": nonce,
        "X-Internal-Signature": signature,
    }


def _post_signed_json(path: str, payload: dict, timeout: int = 10):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(_build_internal_signed_headers(body))
    return get_http().post(f"{EXTERNAL_BASE}{path}", data=body, headers=headers, timeout=timeout)


def _normalize_ai_result_payload(ai_result: Dict[str, Any]) -> Dict[str, Any]:
    status = str(ai_result.get("status") or "error").strip().lower()
    if status not in {"pending", "processing", "done", "error", "expired"}:
        status = "error"

    message = str(ai_result.get("message") or "").strip()[:500]
    ai_level = ai_result.get("ai_level")
    ai_suggestion = str(ai_result.get("ai_suggestion") or "").strip()[:MAX_AI_SUGGESTION_CHARS]

    payload = {
        "status": status,
        "message": message,
        "ai_level": None,
        "ai_suggestion": "",
    }
    if status == "done":
        try:
            level = int(ai_level)
        except Exception:
            level = 0
        level = max(0, min(level, 2))
        payload["ai_level"] = level
        payload["ai_suggestion"] = ai_suggestion or SEVERITY_SUGGESTIONS.get(level, SEVERITY_SUGGESTIONS[2])
    return payload


def abort_case_with_retry(case_id: str, receipt: str) -> bool:
    payload = {"case_id": case_id, "receipt": receipt}
    for i in range(3):
        try:
            r = _post_signed_json("/abort_case", payload, timeout=10)
            if r.status_code == 200:
                logger.warning(f"[ABORT] case returned to pending: {case_id}")
                return True
            logger.warning(f"[ABORT] failed ({i+1}/3): status={r.status_code}")
        except Exception as e:
            logger.warning(f"[ABORT] exception ({i+1}/3): {e}")
        time.sleep(2)
    return False


def confirm_case_with_retry(case_id: str, receipt: str) -> bool:
    payload = {"case_id": case_id, "receipt": receipt}
    for i in range(3):
        try:
            r = _post_signed_json("/confirm_case", payload, timeout=10)
            if r.status_code == 200:
                return True
            logger.warning(f"[CONFIRM] failed ({i+1}/3): status={r.status_code}")
        except Exception as e:
            logger.warning(f"[CONFIRM] exception ({i+1}/3): {e}")
        time.sleep(2)
    return False


def heartbeat_case_best_effort(case_id: str, receipt: str) -> bool:
    payload = {"case_id": case_id, "receipt": receipt}
    try:
        r = _post_signed_json("/heartbeat_case", payload, timeout=10)
        if r.status_code == 200:
            return True
        logger.warning(f"[HEARTBEAT] failed: status={r.status_code}")
    except Exception as e:
        logger.warning(f"[HEARTBEAT] exception: {e}")
    return False


def push_ai_result_with_retry(case_id: str, receipt: str, ai_result: Dict[str, Any]) -> bool:
    payload = {"case_id": case_id, "receipt": receipt}
    payload.update(_normalize_ai_result_payload(ai_result))

    for i in range(3):
        try:
            r = _post_signed_json("/update_ai_result", payload, timeout=10)
            if r.status_code == 200:
                return True
            logger.warning(f"[AI_PUSH] failed ({i+1}/3): status={r.status_code}")
        except Exception as e:
            logger.warning(f"[AI_PUSH] exception ({i+1}/3): {e}")
        time.sleep(2)
    return False
