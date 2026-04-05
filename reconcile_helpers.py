import json


def process_outbox_row(
    row,
    *,
    outbox_max_retry_count: int,
    push_ai_result_with_retry,
    confirm_case_with_retry,
    update_sync_outbox_status,
) -> None:
    case_id = row["case_id"]
    receipt = row["receipt"]
    need_confirm = bool(row["need_confirm"])
    need_push = bool(row["need_push"])
    retry_count = int(row["retry_count"] or 0)
    last_error_parts = []
    try:
        summary_payload = json.loads(row["summary_json"] or "{}")
    except Exception:
        summary_payload = {
            "status": "error",
            "message": "invalid summary payload",
            "ai_level": None,
            "ai_suggestion": "",
        }

    if retry_count >= outbox_max_retry_count:
        update_sync_outbox_status(
            case_id=case_id,
            need_confirm=False,
            need_push=False,
            retry_count_inc=False,
            last_error=f"dead_letter:max_retries={outbox_max_retry_count}",
            dead_lettered=True,
        )
        return

    if need_push:
        ok_push = push_ai_result_with_retry(case_id, receipt, summary_payload)
        need_push = not ok_push
        if need_push:
            last_error_parts.append("push_failed")
    if need_confirm:
        ok_confirm = confirm_case_with_retry(case_id, receipt)
        need_confirm = not ok_confirm
        if need_confirm:
            last_error_parts.append("confirm_failed")

    still_pending = need_confirm or need_push
    if still_pending and (retry_count + 1) >= outbox_max_retry_count:
        update_sync_outbox_status(
            case_id=case_id,
            need_confirm=False,
            need_push=False,
            retry_count_inc=True,
            last_error=f"dead_letter:max_retries={outbox_max_retry_count}," + ",".join(last_error_parts),
            dead_lettered=True,
        )
    else:
        update_sync_outbox_status(
            case_id=case_id,
            need_confirm=need_confirm,
            need_push=need_push,
            retry_count_inc=still_pending,
            last_error=",".join(last_error_parts),
        )
