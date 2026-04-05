def run_reconcile_iteration(
    *,
    db_lock,
    get_conn,
    outbox_max_retry_count: int,
    now_iso,
    fetch_reconcile_batches,
    confirm_case_with_retry,
    set_external_confirm_status,
    build_ai_payload_from_db_row,
    push_ai_result_with_retry,
    set_external_ai_push_status,
    process_outbox_row,
    update_sync_outbox_status,
):
    rows_confirm, rows_ai, outbox_rows = fetch_reconcile_batches(
        db_lock=db_lock,
        get_conn=get_conn,
    )

    for r in rows_confirm:
        case_id = r["id"]
        receipt = r["receipt"]
        ok = confirm_case_with_retry(case_id, receipt)
        set_external_confirm_status(
            db_lock=db_lock,
            get_conn=get_conn,
            case_id=case_id,
            ok=ok,
            now_iso=now_iso,
        )

    for r in rows_ai:
        case_id = r["id"]
        receipt = r["receipt"]
        ai_payload = build_ai_payload_from_db_row(r)
        ok = push_ai_result_with_retry(case_id, receipt, ai_payload)
        set_external_ai_push_status(
            db_lock=db_lock,
            get_conn=get_conn,
            case_id=case_id,
            ok=ok,
            now_iso=now_iso,
        )

    for r in outbox_rows:
        process_outbox_row(
            r,
            outbox_max_retry_count=outbox_max_retry_count,
            push_ai_result_with_retry=push_ai_result_with_retry,
            confirm_case_with_retry=confirm_case_with_retry,
            update_sync_outbox_status=update_sync_outbox_status,
        )


def run_line_retry_iteration(
    *,
    db_lock,
    get_conn,
    line_max_retry_count: int,
    max_ai_suggestion_chars: int,
    now_iso,
    fetch_line_retry_rows,
    build_review_line_message,
    send_line_push_with_retry_result,
    apply_line_retry_result,
    logger,
):
    rows = fetch_line_retry_rows(
        db_lock=db_lock,
        get_conn=get_conn,
        line_max_retry_count=line_max_retry_count,
    )

    for r in rows:
        case_id = r["id"]
        retry_count = int(r["line_retry_count"] or 0)
        msg_text = build_review_line_message(
            name=r["name"] or "",
            reviewed_level=int(r["reviewed_level"] or 0),
            reviewed_note=r["reviewed_note"] or "",
            ai_suggestion=(r["ai_suggestion"] or "")[:max_ai_suggestion_chars],
            case_id=case_id,
        )
        push_result = send_line_push_with_retry_result(r["line_user_id"], msg_text)
        ok = bool(push_result.get("ok"))
        attempted_at = str(push_result.get("attempted_at") or now_iso()).strip() or now_iso()
        last_http_status_raw = push_result.get("last_http_status")
        try:
            last_http_status = int(last_http_status_raw) if last_http_status_raw is not None else None
        except Exception:
            last_http_status = None
        last_error = str(push_result.get("last_error") or "").strip()
        apply_line_retry_result(
            db_lock=db_lock,
            get_conn=get_conn,
            case_id=case_id,
            ok=ok,
            retry_count=retry_count,
            line_max_retry_count=line_max_retry_count,
            attempted_at=attempted_at,
            last_http_status=last_http_status,
            last_error=last_error,
            now_iso=now_iso,
        )
        if ok:
            logger.info(f"[LINE-RETRY] success: {case_id}")
        elif retry_count + 1 >= line_max_retry_count:
            logger.warning(f"[LINE-RETRY] dead-lettered: {case_id} after {retry_count + 1} retries")
