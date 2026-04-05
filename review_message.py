def build_review_line_message(
    name: str,
    reviewed_level: int,
    reviewed_note: str,
    ai_suggestion: str,
    case_id: str = "",
) -> str:
    caregiver_name = (name or "").strip()
    note = (reviewed_note or "").strip()
    ai_text = (ai_suggestion or "").strip()
    advice = note or ai_text or "請保持患部清潔乾燥，若症狀惡化請盡速就醫。"
    safe_case_ref = (case_id or "").replace("-", "")[:8]

    line1 = f"親愛的 {caregiver_name} 家長您好，" if caregiver_name else "家長您好，"
    msg_text = (
        "【尿布疹最終確認結果】\n"
        f"{line1}這是醫護人員的最終確認結果。\n"
        f"最終等級：Level {reviewed_level}\n"
        f"照護建議：{advice}\n"
    )
    if safe_case_ref:
        msg_text += f"案件參考碼：{safe_case_ref}\n"

    return msg_text[:4800]
