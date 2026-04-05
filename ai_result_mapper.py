def normalize_done_level(value) -> int:
    try:
        n = int(value)
    except Exception:
        return 0
    if n < 0:
        return 0
    if n > 2:
        return 2
    return n


def normalize_ai_result_for_db(case_id: str, ai_result: dict, max_ai_suggestion_chars: int) -> dict:
    status = str(ai_result.get("status") or "error").strip().lower()
    if status not in {"done", "error"}:
        status = "error"

    payload = {
        "case_id": case_id,
        "status": status,
        "message": str(ai_result.get("message") or "").strip(),
        "ai_level": None,
        "ai_prob": ai_result.get("ai_prob"),
        "ai_suggestion": str(ai_result.get("ai_suggestion") or "")[:max_ai_suggestion_chars],
        "stage1": ai_result.get("stage1") or {},
        "stage2": ai_result.get("stage2") or {},
        "stage3": ai_result.get("stage3") or {},
        "aggregation": ai_result.get("aggregation") or {},
    }

    if status == "done":
        payload["ai_level"] = normalize_done_level(ai_result.get("ai_level"))
    return payload


def summary_payload(ai_payload: dict, max_ai_suggestion_chars: int) -> dict:
    status = ai_payload.get("status")
    if status not in {"pending", "processing", "done", "error", "expired"}:
        status = "error"
    return {
        "status": status,
        "message": str(ai_payload.get("message") or "")[:500],
        "ai_level": ai_payload.get("ai_level"),
        "ai_suggestion": str(ai_payload.get("ai_suggestion") or "")[:max_ai_suggestion_chars],
    }


def build_ai_payload_from_db_row(row, max_ai_suggestion_chars: int, json_module) -> dict:
    raw_json = row["ai_result_json"]
    if raw_json:
        try:
            data = json_module.loads(raw_json)
            if isinstance(data, dict):
                return summary_payload(data, max_ai_suggestion_chars=max_ai_suggestion_chars)
        except Exception:
            pass
    return summary_payload(
        {
            "status": row["ai_status"] or "error",
            "message": row["ai_message"] or "",
            "ai_level": row["ai_level"],
            "ai_suggestion": row["ai_suggestion"] or "",
        },
        max_ai_suggestion_chars=max_ai_suggestion_chars,
    )
