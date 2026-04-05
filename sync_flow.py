import logging
import os
import threading
import time

from ai_pipeline import now_iso, run_ai_pipeline, sanitize_ext, save_base64_image
from ai_result_mapper import normalize_ai_result_for_db, summary_payload
from case_repo import (
    get_existing_case_receipt,
    insert_formal_case,
    set_external_ai_push_status,
    set_external_confirm_status,
)
from cloud_client import (
    _build_internal_signed_headers,
    abort_case_with_retry,
    confirm_case_with_retry,
    get_http,
    heartbeat_case_best_effort,
    push_ai_result_with_retry,
)

EXTERNAL_BASE = os.getenv("EXTERNAL_BASE", "https://your-app.zeabur.app").rstrip("/")
MAX_AI_SUGGESTION_CHARS = int(os.getenv("MAX_AI_SUGGESTION_CHARS", "1200"))
SYNC_EMPTY_SLEEP_SEC = float(os.getenv("SYNC_EMPTY_SLEEP_SEC", "5"))
SYNC_ERROR_SLEEP_SEC = float(os.getenv("SYNC_ERROR_SLEEP_SEC", "10"))
SYNC_HEARTBEAT_INTERVAL_SEC = float(os.getenv("SYNC_HEARTBEAT_INTERVAL_SEC", "30"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")


def _mask_secret(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return "***"
    if len(s) <= 8:
        return s[:2] + "***"
    return f"{s[:4]}***{s[-4:]}"


def _safe_remove(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


class _ProcessingHeartbeat:
    def __init__(self, case_id: str, receipt: str, logger: logging.Logger):
        self.case_id = case_id
        self.receipt = receipt
        self.logger = logger
        self.interval = max(5.0, SYNC_HEARTBEAT_INTERVAL_SEC)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _send(self):
        if not heartbeat_case_best_effort(self.case_id, self.receipt):
            self.logger.warning(f"[SYNC] heartbeat failed: {self.case_id}")

    def _run(self):
        while not self.stop_event.wait(self.interval):
            self._send()

    def __enter__(self):
        # Send one heartbeat immediately so cleanup sees this case as active.
        self._send()
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop_event.set()
        self.thread.join(timeout=1.0)


def _claim_from_cloud(logger: logging.Logger) -> dict | None:
    try:
        resp = get_http().post(
            f"{EXTERNAL_BASE}/claim_case",
            headers=_build_internal_signed_headers(b""),
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error(f"[SYNC] claim status={resp.status_code}, body={resp.text[:200]}")
            time.sleep(SYNC_ERROR_SLEEP_SEC)
            return None
        return resp.json()
    except Exception as e:
        logger.error(f"[SYNC] claim connection failed: {e}")
        time.sleep(SYNC_ERROR_SLEEP_SEC)
        return None


def _handle_idempotent_case(case_id: str, receipt: str, *, logger, db_lock, get_conn):
    ok = confirm_case_with_retry(case_id, receipt)
    set_external_confirm_status(
        db_lock=db_lock,
        get_conn=get_conn,
        case_id=case_id,
        ok=ok,
        now_iso=now_iso,
    )
    time.sleep(1)


def _handle_stage1_invalid(
    case_id: str,
    receipt: str,
    summary_payload_obj: dict,
    local_image_path: str,
    *,
    upsert_sync_outbox,
):
    ok_push = push_ai_result_with_retry(case_id, receipt, summary_payload_obj)
    ok_confirm = confirm_case_with_retry(case_id, receipt)

    if not ok_confirm or not ok_push:
        last_error = []
        if not ok_confirm:
            last_error.append("confirm_failed")
        if not ok_push:
            last_error.append("push_failed")
        upsert_sync_outbox(
            case_id=case_id,
            receipt=receipt,
            summary_payload=summary_payload_obj,
            need_confirm=not ok_confirm,
            need_push=not ok_push,
            last_error=",".join(last_error),
        )

    _safe_remove(local_image_path)


def _persist_formal_case(case_id: str, receipt: str, rec: dict, local_filename: str, ai_result: dict, *, logger, db_lock, get_conn) -> bool:
    try:
        insert_formal_case(
            db_lock=db_lock,
            get_conn=get_conn,
            case_id=case_id,
            receipt=receipt,
            rec=rec,
            local_filename=local_filename,
            ai_result=ai_result,
            now_iso=now_iso,
        )
        return True
    except Exception as e:
        logger.error(f"[SYNC] DB insert failed: {e}")
        return False


def _sync_formal_case_to_cloud(case_id: str, receipt: str, summary_payload_obj: dict, *, db_lock, get_conn, upsert_sync_outbox):
    ok_push = push_ai_result_with_retry(case_id, receipt, summary_payload_obj)
    set_external_ai_push_status(
        db_lock=db_lock,
        get_conn=get_conn,
        case_id=case_id,
        ok=ok_push,
        now_iso=now_iso,
    )

    ok_confirm = confirm_case_with_retry(case_id, receipt)
    set_external_confirm_status(
        db_lock=db_lock,
        get_conn=get_conn,
        case_id=case_id,
        ok=ok_confirm,
        now_iso=now_iso,
    )

    if not ok_confirm or not ok_push:
        last_error = []
        if not ok_confirm:
            last_error.append("confirm_failed")
        if not ok_push:
            last_error.append("push_failed")
        upsert_sync_outbox(
            case_id=case_id,
            receipt=receipt,
            summary_payload=summary_payload_obj,
            need_confirm=not ok_confirm,
            need_push=not ok_push,
            last_error=",".join(last_error),
        )


def sync_once_impl(*, logger, db_lock, get_conn, upsert_sync_outbox):
    data = _claim_from_cloud(logger)
    if data is None:
        return

    status = data.get("status")
    if status == "empty":
        time.sleep(SYNC_EMPTY_SLEEP_SEC)
        return
    if status != "ok":
        logger.info(f"[SYNC] claim non-ok: status={status}, msg={data.get('message')}")
        time.sleep(SYNC_EMPTY_SLEEP_SEC)
        return

    rec = data.get("data", {}) or {}
    img_b64 = data.get("image_b64")
    img_ext = sanitize_ext(data.get("image_ext", ".jpg"))
    case_id = rec.get("id")
    receipt = rec.get("receipt")

    if not case_id or not receipt:
        logger.error(f"[SYNC] invalid claim payload: id={case_id}, receipt={_mask_secret(receipt)}")
        time.sleep(1)
        return

    logger.info(f"[SYNC] claimed case: {case_id}")

    with _ProcessingHeartbeat(case_id, receipt, logger):
        stored_receipt = get_existing_case_receipt(
            db_lock=db_lock,
            get_conn=get_conn,
            case_id=case_id,
        )
        if stored_receipt is not None:
            if stored_receipt and stored_receipt != receipt:
                logger.warning(
                    f"[SYNC] receipt mismatch local={_mask_secret(stored_receipt)} remote={_mask_secret(receipt)}"
                )
                receipt = stored_receipt
            _handle_idempotent_case(
                case_id,
                receipt,
                logger=logger,
                db_lock=db_lock,
                get_conn=get_conn,
            )
            return

        local_filename = f"{case_id}{img_ext}"
        local_image_path = os.path.join(UPLOAD_FOLDER, local_filename)
        if not save_base64_image(img_b64, local_filename):
            logger.error(f"[SYNC] image save failed: {case_id}, aborting case")
            abort_case_with_retry(case_id, receipt)
            time.sleep(1)
            return

        ai_result = normalize_ai_result_for_db(
            case_id,
            ai_result=run_ai_pipeline(local_image_path, case_id=case_id),
            max_ai_suggestion_chars=MAX_AI_SUGGESTION_CHARS,
        )
        stage1_valid = bool((ai_result.get("stage1") or {}).get("selected_bbox"))
        summary_payload_obj = summary_payload(
            ai_payload=ai_result,
            max_ai_suggestion_chars=MAX_AI_SUGGESTION_CHARS,
        )

        if not stage1_valid:
            _handle_stage1_invalid(
                case_id,
                receipt,
                summary_payload_obj,
                local_image_path,
                upsert_sync_outbox=upsert_sync_outbox,
            )
            return

        if not _persist_formal_case(
            case_id,
            receipt,
            rec,
            local_filename,
            ai_result,
            logger=logger,
            db_lock=db_lock,
            get_conn=get_conn,
        ):
            _safe_remove(local_image_path)
            abort_case_with_retry(case_id, receipt)
            time.sleep(1)
            return

        _sync_formal_case_to_cloud(
            case_id,
            receipt,
            summary_payload_obj,
            db_lock=db_lock,
            get_conn=get_conn,
            upsert_sync_outbox=upsert_sync_outbox,
        )
