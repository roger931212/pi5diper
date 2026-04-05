"""
Source-of-truth worker implementation module for edge background jobs.

`workers.py` is kept as a temporary compatibility re-export layer.
New internal imports should prefer this module directly.
"""

import json
import logging
import os
import threading
import time

from database import db_lock, get_conn
from ai_pipeline import now_iso
from ai_result_mapper import (
    build_ai_payload_from_db_row,
)
from cloud_client import (
    confirm_case_with_retry,
    push_ai_result_with_retry,
)
from line_client import send_line_push_with_retry_result
from case_repo import (
    apply_line_retry_result,
    fetch_line_retry_rows,
    fetch_reconcile_batches,
    set_external_ai_push_status,
    set_external_confirm_status,
)
from outbox_repo import update_sync_outbox_status, upsert_sync_outbox
from reconcile_helpers import process_outbox_row
from review_message import build_review_line_message
from sync_flow import sync_once_impl
from worker_loops import run_line_retry_iteration, run_reconcile_iteration

logger = logging.getLogger(__name__)
LINE_MAX_RETRY_COUNT = int(os.getenv("LINE_MAX_RETRY_COUNT", "10"))
MAX_AI_SUGGESTION_CHARS = int(os.getenv("MAX_AI_SUGGESTION_CHARS", "1200"))
RECONCILE_INTERVAL_SEC = float(os.getenv("RECONCILE_INTERVAL_SEC", "30"))
LINE_RETRY_INTERVAL_SEC = float(os.getenv("LINE_RETRY_INTERVAL_SEC", "60"))
SYNC_ERROR_SLEEP_SEC = float(os.getenv("SYNC_ERROR_SLEEP_SEC", "10"))

# ============================
# Background workers
# ============================
stop_event = threading.Event()
OUTBOX_MAX_RETRY_COUNT = 5


def _build_ai_payload_from_db_row(row) -> dict:
    return build_ai_payload_from_db_row(
        row=row,
        max_ai_suggestion_chars=MAX_AI_SUGGESTION_CHARS,
        json_module=json,
    )


def _upsert_sync_outbox(case_id: str, receipt: str, summary_payload: dict, need_confirm: bool, need_push: bool, last_error: str):
    upsert_sync_outbox(
        db_lock=db_lock,
        get_conn=get_conn,
        logger=logger,
        now_iso=now_iso,
        case_id=case_id,
        receipt=receipt,
        summary_payload=summary_payload,
        need_confirm=need_confirm,
        need_push=need_push,
        last_error=last_error,
    )


def _update_sync_outbox_status(
    case_id: str,
    need_confirm: bool,
    need_push: bool,
    retry_count_inc: bool,
    last_error: str,
    dead_lettered: bool = False,
):
    update_sync_outbox_status(
        db_lock=db_lock,
        get_conn=get_conn,
        now_iso=now_iso,
        case_id=case_id,
        need_confirm=need_confirm,
        need_push=need_push,
        retry_count_inc=retry_count_inc,
        last_error=last_error,
        dead_lettered=dead_lettered,
    )


def sync_once():
    sync_once_impl(
        logger=logger,
        db_lock=db_lock,
        get_conn=get_conn,
        upsert_sync_outbox=_upsert_sync_outbox,
    )


def sync_worker():
    logger.info("sync worker started")
    while not stop_event.is_set():
        try:
            sync_once()
        except Exception as e:
            logger.error(f"[SYNC] worker exception: {e}")
            time.sleep(SYNC_ERROR_SLEEP_SEC)


def reconcile_worker():
    logger.info("reconcile worker started")
    while not stop_event.is_set():
        try:
            run_reconcile_iteration(
                db_lock=db_lock,
                get_conn=get_conn,
                outbox_max_retry_count=OUTBOX_MAX_RETRY_COUNT,
                now_iso=now_iso,
                fetch_reconcile_batches=fetch_reconcile_batches,
                confirm_case_with_retry=confirm_case_with_retry,
                set_external_confirm_status=set_external_confirm_status,
                build_ai_payload_from_db_row=_build_ai_payload_from_db_row,
                push_ai_result_with_retry=push_ai_result_with_retry,
                set_external_ai_push_status=set_external_ai_push_status,
                process_outbox_row=process_outbox_row,
                update_sync_outbox_status=_update_sync_outbox_status,
            )
        except Exception as e:
            logger.error(f"[RECONCILE] exception: {e}")

        time.sleep(RECONCILE_INTERVAL_SEC)


def line_retry_worker():
    """Retry failed LINE pushes with max retry count and dead-letter (audit #10)."""
    interval = max(5.0, LINE_RETRY_INTERVAL_SEC)
    logger.info(f"LINE retry worker started (interval={interval}s)")
    while not stop_event.is_set():
        try:
            run_line_retry_iteration(
                db_lock=db_lock,
                get_conn=get_conn,
                line_max_retry_count=LINE_MAX_RETRY_COUNT,
                max_ai_suggestion_chars=MAX_AI_SUGGESTION_CHARS,
                now_iso=now_iso,
                fetch_line_retry_rows=fetch_line_retry_rows,
                build_review_line_message=build_review_line_message,
                send_line_push_with_retry_result=send_line_push_with_retry_result,
                apply_line_retry_result=apply_line_retry_result,
                logger=logger,
            )
        except Exception as e:
            logger.error(f"[LINE-RETRY] exception: {e}")

        time.sleep(interval)
