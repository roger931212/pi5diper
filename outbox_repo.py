import json


def upsert_sync_outbox(
    *,
    db_lock,
    get_conn,
    logger,
    now_iso,
    case_id: str,
    receipt: str,
    summary_payload: dict,
    need_confirm: bool,
    need_push: bool,
    last_error: str,
) -> None:
    """Insert or update an outbox entry for retry.

    Dead-lettered entries are NOT resurrected by this function.
    Retry_count is NOT reset on conflict — it stays monotonically increasing.
    """
    with db_lock:
        conn = get_conn()
        try:
            existing = conn.execute(
                "SELECT dead_lettered FROM sync_outbox WHERE case_id=?",
                (case_id,),
            ).fetchone()
            if existing and existing["dead_lettered"]:
                logger.warning(f"[OUTBOX] skip upsert for dead-lettered case: {case_id}")
                return

            conn.execute(
                """
                INSERT INTO sync_outbox (
                    case_id, receipt, summary_json, need_confirm, need_push,
                    retry_count, dead_lettered, last_error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    receipt=excluded.receipt,
                    summary_json=excluded.summary_json,
                    need_confirm=excluded.need_confirm,
                    need_push=excluded.need_push,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (
                    case_id,
                    receipt,
                    json.dumps(summary_payload, ensure_ascii=False),
                    1 if need_confirm else 0,
                    1 if need_push else 0,
                    (last_error or "")[:500],
                    now_iso(),
                    now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def update_sync_outbox_status(
    *,
    db_lock,
    get_conn,
    now_iso,
    case_id: str,
    need_confirm: bool,
    need_push: bool,
    retry_count_inc: bool,
    last_error: str,
    dead_lettered: bool = False,
) -> None:
    with db_lock:
        conn = get_conn()
        try:
            if dead_lettered:
                conn.execute(
                    """
                    UPDATE sync_outbox
                    SET need_confirm=0, need_push=0, dead_lettered=1,
                        retry_count=retry_count+?, last_error=?, updated_at=?
                    WHERE case_id=?
                    """,
                    (1 if retry_count_inc else 0, (last_error or "")[:500], now_iso(), case_id),
                )
            elif not need_confirm and not need_push:
                conn.execute("DELETE FROM sync_outbox WHERE case_id=?", (case_id,))
            else:
                conn.execute(
                    """
                    UPDATE sync_outbox
                    SET need_confirm=?, need_push=?, dead_lettered=0,
                        retry_count=retry_count+?, last_error=?, updated_at=?
                    WHERE case_id=?
                    """,
                    (
                        1 if need_confirm else 0,
                        1 if need_push else 0,
                        1 if retry_count_inc else 0,
                        (last_error or "")[:500],
                        now_iso(),
                        case_id,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
