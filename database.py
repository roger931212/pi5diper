import os
import sqlite3
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "internal.db")

# ============================
# SQLite（加 Lock 防止併發寫入）
# ============================
db_lock = threading.Lock()

# Whitelist of allowed table names for safe PRAGMA queries (audit #17)
_VALID_TABLES = {"cases", "sync_outbox"}


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _column_exists(conn, table: str, column: str) -> bool:
    # Safe table name validation (audit #17: avoid f-string SQL injection patterns)
    if table not in _VALID_TABLES:
        raise ValueError(f"Invalid table name for PRAGMA: {table}")
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db():
    conn = get_conn()
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cases (
                id TEXT PRIMARY KEY,
                receipt TEXT,
                name TEXT,
                phone TEXT,
                -- line_id column is DEPRECATED: retained for backward compatibility only.
                -- All new code MUST use line_user_id instead.
                line_id TEXT,
                line_user_id TEXT,
                image_filename TEXT,
                created_at TEXT,

                status TEXT DEFAULT 'pending',
                ai_status TEXT,
                ai_message TEXT,
                ai_level INTEGER,
                ai_prob REAL,
                ai_suggestion TEXT,
                ai_result_json TEXT,

                reviewed_level INTEGER,
                reviewed_note TEXT,
                reviewed_at TEXT,

                external_confirm_status TEXT,
                external_confirmed_at TEXT,

                external_ai_push_status TEXT,
                external_ai_pushed_at TEXT,

                line_send_status TEXT,
                line_sent_at TEXT,
                line_retry_count INTEGER NOT NULL DEFAULT 0,
                line_last_attempt_at TEXT,
                line_last_http_status INTEGER,
                line_last_error TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_outbox (
                case_id TEXT PRIMARY KEY,
                receipt TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                need_confirm INTEGER NOT NULL DEFAULT 1,
                need_push INTEGER NOT NULL DEFAULT 1,
                retry_count INTEGER NOT NULL DEFAULT 0,
                dead_lettered INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )

        # Schema migration: add columns that may not exist in older DBs
        # P2-9 fix: Column names and types are whitelisted here (code-defined,
        # not user input) and validated before use in ALTER TABLE to maintain
        # consistency with the PRAGMA whitelist approach above.
        _VALID_CASES_COLUMNS = {
            "external_confirm_status": "TEXT",
            "external_confirmed_at": "TEXT",
            "external_ai_push_status": "TEXT",
            "external_ai_pushed_at": "TEXT",
            "line_send_status": "TEXT",
            "line_sent_at": "TEXT",
            "line_user_id": "TEXT",
            "ai_status": "TEXT",
            "ai_message": "TEXT",
            "ai_result_json": "TEXT",
            "line_retry_count": "INTEGER NOT NULL DEFAULT 0",
            "line_last_attempt_at": "TEXT",
            "line_last_http_status": "INTEGER",
            "line_last_error": "TEXT",
        }
        for col, ctype in _VALID_CASES_COLUMNS.items():
            if not _column_exists(conn, "cases", col):
                # col and ctype come from the hardcoded whitelist above, not user input.
                conn.execute(f"ALTER TABLE cases ADD COLUMN {col} {ctype}")
        if not _column_exists(conn, "sync_outbox", "dead_lettered"):
            conn.execute("ALTER TABLE sync_outbox ADD COLUMN dead_lettered INTEGER NOT NULL DEFAULT 0")

        # Keep legacy records within authoritative severity scale (0, 1, 2).
        conn.execute("UPDATE cases SET ai_level=0 WHERE ai_level IS NOT NULL AND ai_level < 0")
        conn.execute("UPDATE cases SET ai_level=2 WHERE ai_level IS NOT NULL AND ai_level > 2")
        conn.execute("UPDATE cases SET reviewed_level=0 WHERE reviewed_level IS NOT NULL AND reviewed_level < 0")
        conn.execute("UPDATE cases SET reviewed_level=2 WHERE reviewed_level IS NOT NULL AND reviewed_level > 2")

        conn.commit()
    finally:
        conn.close()

# Initialize database on module import
init_db()
