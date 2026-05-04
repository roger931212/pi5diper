import logging
import os

from fastapi import HTTPException

from database import db_lock, get_conn
from edge_time_utils import now_iso_taipei
from line_client import send_line_push_with_retry_result
from review_message import build_review_line_message

logger = logging.getLogger(__name__)
MAX_AI_SUGGESTION_CHARS = int(os.getenv("MAX_AI_SUGGESTION_CHARS", "1200"))
MAX_NOTE_CHARS = int(os.getenv("MAX_NOTE_CHARS", "800"))


def _now_iso() -> str:
    return now_iso_taipei()


def submit_review_workflow(case_id: str, level: int, note: str) -> dict:
    note = (note or "").strip()
    if len(note) > MAX_NOTE_CHARS:
        note = note[:MAX_NOTE_CHARS]

    try:
        level = int(level)
    except Exception:
        level = 0
    if level < 0:
        level = 0
    if level > 2:
        level = 2

    with db_lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Case not found")

            current_status = (row["status"] or "").strip().lower()
            if current_status != "pending":
                raise HTTPException(
                    status_code=409,
                    detail=f"Case is already processed (status={current_status or 'unknown'})",
                )

            cur = conn.execute(
                """
                UPDATE cases
                SET status='reviewed',
                    reviewed_level=?,
                    reviewed_note=?,
                    reviewed_at=?
                WHERE id=? AND status='pending'
                """,
                (level, note, _now_iso(), case_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise HTTPException(status_code=409, detail="Case already reviewed")
            conn.commit()

            line_user_id = row["line_user_id"]
            name = row["name"] or ""
            ai_suggestion = (row["ai_suggestion"] or "")[:MAX_AI_SUGGESTION_CHARS]
        finally:
            conn.close()

    msg_text = build_review_line_message(
        name=name,
        reviewed_level=level,
        reviewed_note=note,
        ai_suggestion=ai_suggestion,
        case_id=case_id,
    )

    if not line_user_id:
        logger.warning(f"[LINE] 跳過推播: 案件 {case_id} 無 line_user_id")
        push_result = {
            "ok": False,
            "attempted_at": _now_iso(),
            "attempt_count": 0,
            "last_http_status": None,
            "last_error": "missing_line_user_id",
        }
    else:
        push_result = send_line_push_with_retry_result(line_user_id, msg_text)

    ok = bool(push_result.get("ok"))
    attempted_at = str(push_result.get("attempted_at") or _now_iso()).strip() or _now_iso()
    try:
        attempt_count = int(push_result.get("attempt_count") or 0)
    except Exception:
        attempt_count = 0
    last_http_status_raw = push_result.get("last_http_status")
    try:
        last_http_status = int(last_http_status_raw) if last_http_status_raw is not None else None
    except Exception:
        last_http_status = None
    last_error = str(push_result.get("last_error") or "").strip()[:400]
    if ok:
        last_error = ""

    with db_lock:
        conn = get_conn()
        try:
            conn.execute(
                """
                UPDATE cases
                SET line_send_status=?,
                    line_sent_at=?,
                    line_retry_count=?,
                    line_last_attempt_at=?,
                    line_last_http_status=?,
                    line_last_error=?
                WHERE id=?
                """,
                (
                    "ok" if ok else "failed",
                    attempted_at if ok else None,
                    max(0, attempt_count),
                    attempted_at,
                    last_http_status,
                    last_error or None,
                    case_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return {"status": "ok", "message": "Review submitted", "line_sent": ok}
