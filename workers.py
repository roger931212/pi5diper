"""
Compatibility re-export layer for edge worker runtime (temporary).

DEPRECATION NOTE:
- This module is kept to preserve legacy `from workers import ...` imports.
- New code should import runtime symbols from worker_runtime.py directly.
- Planned migration: remove this shim after internal imports are migrated.
"""

from worker_runtime import (
    OUTBOX_MAX_RETRY_COUNT,
    stop_event,
    line_retry_worker,
    reconcile_worker,
    sync_once,
    sync_worker,
)

__all__ = [
    "stop_event",
    "OUTBOX_MAX_RETRY_COUNT",
    "sync_once",
    "sync_worker",
    "reconcile_worker",
    "line_retry_worker",
]
