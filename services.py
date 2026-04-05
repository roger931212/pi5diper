"""
Compatibility re-export layer for edge service modules (temporary).

DEPRECATION NOTE:
- This module is kept to preserve legacy `from services import ...` imports.
- New code should import from concrete modules directly:
  - ai_pipeline.py
  - cloud_client.py
  - line_client.py
- Planned migration: remove this shim after internal imports are migrated.
"""

from ai_pipeline import (
    now_iso,
    run_ai_model,
    run_ai_pipeline,
    sanitize_ext,
    save_base64_image,
)
from cloud_client import (
    _build_internal_signed_headers,
    _normalize_ai_result_payload,
    _post_signed_json,
    abort_case_with_retry,
    confirm_case_with_retry,
    create_session,
    get_http,
    push_ai_result_with_retry,
)
from line_client import get_line_http, send_line_push_with_retry, send_line_push_with_retry_result

__all__ = [
    "create_session",
    "get_http",
    "get_line_http",
    "_build_internal_signed_headers",
    "_post_signed_json",
    "now_iso",
    "sanitize_ext",
    "save_base64_image",
    "run_ai_pipeline",
    "run_ai_model",
    "send_line_push_with_retry",
    "send_line_push_with_retry_result",
    "_normalize_ai_result_payload",
    "abort_case_with_retry",
    "confirm_case_with_retry",
    "push_ai_result_with_retry",
]
