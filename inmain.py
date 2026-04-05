"""
Legacy entrypoint disabled on purpose.

Use `edge_private/main.py` (`uvicorn main:app`) as the only supported runtime entrypoint.
"""

raise RuntimeError(
    "edge_private/inmain.py is archived and must not be used. "
    "Start edge_private with `uvicorn main:app`."
)
