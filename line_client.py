import logging
import threading
import time
from datetime import datetime

import requests

from config import LINE_API_TIMEOUT_SEC, LINE_CHANNEL_ACCESS_TOKEN, LINE_PUSH_API

logger = logging.getLogger(__name__)


_tls = threading.local()


def get_line_http() -> requests.Session:
    s = getattr(_tls, "line_http", None)
    if s is None:
        s = requests.Session()
        _tls.line_http = s
    return s


def _mask_id(user_id: str) -> str:
    if not user_id or len(user_id) <= 5:
        return "***"
    return user_id[:5] + "***"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def send_line_push_with_retry_result(to_user_id: str, text: str, max_retries: int = 3) -> dict:
    masked_id = _mask_id(to_user_id)
    result = {
        "ok": False,
        "attempted_at": _now_iso(),
        "attempt_count": 0,
        "last_http_status": None,
        "last_error": "",
    }

    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.error("[LINE] missing LINE_CHANNEL_ACCESS_TOKEN")
        result["last_error"] = "missing_line_channel_access_token"
        return result
    if not to_user_id:
        logger.warning("[LINE] empty target user id")
        result["last_error"] = "missing_line_user_id"
        return result

    for attempt in range(1, max_retries + 1):
        result["attempt_count"] = attempt
        result["attempted_at"] = _now_iso()
        try:
            r = get_line_http().post(
                LINE_PUSH_API,
                json={"to": to_user_id, "messages": [{"type": "text", "text": text}]},
                headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
                timeout=LINE_API_TIMEOUT_SEC,
            )
            result["last_http_status"] = r.status_code
            if 200 <= r.status_code < 300:
                logger.info(f"[LINE] push success: target={masked_id}")
                result["ok"] = True
                result["last_error"] = ""
                return result
            body_excerpt = (r.text or "").strip().replace("\n", " ")[:200]
            result["last_error"] = f"http_{r.status_code}:{body_excerpt}" if body_excerpt else f"http_{r.status_code}"
            logger.warning(f"[LINE] push failed ({attempt}/{max_retries}): target={masked_id}, status={r.status_code}")
        except requests.RequestException as e:
            result["last_error"] = type(e).__name__
            logger.warning(f"[LINE] push exception ({attempt}/{max_retries}): target={masked_id}, error={type(e).__name__}")
        if attempt < max_retries:
            time.sleep(2 * attempt)

    logger.error(f"[LINE] push failed after retries: target={masked_id}")
    return result


def send_line_push_with_retry(to_user_id: str, text: str, max_retries: int = 3) -> bool:
    return bool(send_line_push_with_retry_result(to_user_id, text, max_retries=max_retries).get("ok"))
