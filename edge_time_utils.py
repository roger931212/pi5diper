from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def _resolve_taipei_tz():
    try:
        return ZoneInfo("Asia/Taipei")
    except Exception:
        # Fallback for environments without tzdata (e.g. stripped CI images).
        return timezone(timedelta(hours=8), name="Asia/Taipei")


TAIPEI_TZ = _resolve_taipei_tz()


def now_iso_taipei() -> str:
    return datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")


def _parse_iso_datetime(raw: str) -> datetime | None:
    text = raw.strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        if " " in text and "T" not in text:
            try:
                dt = datetime.fromisoformat(text.replace(" ", "T", 1))
            except ValueError:
                return None
        else:
            return None

    if dt.tzinfo is None:
        # Legacy cloud records stored UTC timestamps without an explicit offset.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_cloud_created_at(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    dt = _parse_iso_datetime(text)
    if dt is None:
        return text
    return dt.astimezone(TAIPEI_TZ).isoformat(timespec="seconds")
