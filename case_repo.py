import json


def get_existing_case_receipt(*, db_lock, get_conn, case_id: str):
    with db_lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT id, receipt FROM cases WHERE id=?", (case_id,)).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    return row["receipt"]


def insert_formal_case(
    *,
    db_lock,
    get_conn,
    case_id: str,
    receipt: str,
    rec: dict,
    local_filename: str,
    ai_result: dict,
    now_iso,
) -> None:
    with db_lock:
        conn = get_conn()
        try:
            conn.execute(
                """
                INSERT INTO cases (
                    id, receipt, name, phone, line_user_id, image_filename, created_at, status,
                    ai_status, ai_message, ai_level, ai_prob, ai_suggestion, ai_result_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    receipt,
                    rec.get("name"),
                    rec.get("phone"),
                    rec.get("line_user_id"),
                    local_filename,
                    rec.get("created_at") or now_iso(),
                    ai_result.get("status"),
                    ai_result.get("message"),
                    ai_result.get("ai_level"),
                    ai_result.get("ai_prob"),
                    ai_result.get("ai_suggestion"),
                    json.dumps(ai_result, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def set_external_confirm_status(*, db_lock, get_conn, case_id: str, ok: bool, now_iso) -> None:
    with db_lock:
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE cases SET external_confirm_status=?, external_confirmed_at=? WHERE id=?",
                ("ok" if ok else "failed", now_iso() if ok else None, case_id),
            )
            conn.commit()
        finally:
            conn.close()


def set_external_ai_push_status(*, db_lock, get_conn, case_id: str, ok: bool, now_iso) -> None:
    with db_lock:
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE cases SET external_ai_push_status=?, external_ai_pushed_at=? WHERE id=?",
                ("ok" if ok else "failed", now_iso() if ok else None, case_id),
            )
            conn.commit()
        finally:
            conn.close()


def fetch_reconcile_batches(*, db_lock, get_conn):
    with db_lock:
        conn = get_conn()
        try:
            rows_confirm = conn.execute(
                """
                SELECT id, receipt FROM cases
                WHERE (external_confirm_status IS NULL OR external_confirm_status!='ok')
                  AND datetime(replace(created_at, 'T', ' ')) <= datetime('now', '-60 seconds')
                """
            ).fetchall()
            rows_ai = conn.execute(
                """
                SELECT id, receipt, ai_status, ai_message, ai_level, ai_suggestion, ai_result_json
                FROM cases
                WHERE (ai_status IS NOT NULL OR ai_level IS NOT NULL)
                  AND (external_ai_push_status IS NULL OR external_ai_push_status!='ok')
                  AND status != 'reviewed'
                """
            ).fetchall()
            outbox_rows = conn.execute(
                """
                SELECT case_id, receipt, summary_json, need_confirm, need_push, retry_count
                FROM sync_outbox
                WHERE dead_lettered=0
                ORDER BY created_at ASC
                """
            ).fetchall()
        finally:
            conn.close()
    return rows_confirm, rows_ai, outbox_rows


def fetch_line_retry_rows(*, db_lock, get_conn, line_max_retry_count: int):
    with db_lock:
        conn = get_conn()
        try:
            rows = conn.execute(
                """
                SELECT id, name, line_user_id, reviewed_level, reviewed_note, ai_suggestion, line_retry_count
                FROM cases
                WHERE status='reviewed'
                  AND line_send_status='failed'
                  AND COALESCE(line_user_id, '') != ''
                  AND line_retry_count < ?
                ORDER BY reviewed_at ASC
                """,
                (line_max_retry_count,),
            ).fetchall()
        finally:
            conn.close()
    return rows


def apply_line_retry_result(
    *,
    db_lock,
    get_conn,
    case_id: str,
    ok: bool,
    retry_count: int,
    line_max_retry_count: int,
    attempted_at: str,
    last_http_status,
    last_error: str,
    now_iso,
) -> None:
    with db_lock:
        conn = get_conn()
        try:
            if ok:
                conn.execute(
                    """
                    UPDATE cases
                    SET line_send_status='ok',
                        line_sent_at=?,
                        line_retry_count=?,
                        line_last_attempt_at=?,
                        line_last_http_status=?,
                        line_last_error=NULL
                    WHERE id=?
                    """,
                    (now_iso(), retry_count + 1, attempted_at, last_http_status, case_id),
                )
            else:
                new_count = retry_count + 1
                new_status = "dead_letter" if new_count >= line_max_retry_count else "failed"
                conn.execute(
                    """
                    UPDATE cases
                    SET line_send_status=?,
                        line_retry_count=?,
                        line_last_attempt_at=?,
                        line_last_http_status=?,
                        line_last_error=?
                    WHERE id=?
                    """,
                    (new_status, new_count, attempted_at, last_http_status, (last_error or "")[:400], case_id),
                )
            conn.commit()
        finally:
            conn.close()
