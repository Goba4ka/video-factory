from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .errors import ValidationError


DEFAULT_TTL_HOURS: dict[str, float] = {
    "celebrity_news": 2.0,
    "health": 24.0,
    "chinese_medicine": 24.0,
    "war_history": 168.0,
    "motivation": 720.0,
}


def _aware_datetime(value: str | datetime, field: str) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} must be an ISO-8601 datetime") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValidationError(f"{field} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def evaluate_freshness(
    *,
    lane: str,
    checked_at: str | datetime,
    now: str | datetime | None = None,
    ttl_hours: float | None = None,
) -> dict[str, Any]:
    """Fail closed when a lane's fact check is older than its publication TTL."""

    if lane not in DEFAULT_TTL_HOURS and ttl_hours is None:
        allowed = ", ".join(sorted(DEFAULT_TTL_HOURS))
        raise ValidationError(f"unknown lane {lane!r}; expected one of: {allowed}")
    ttl = DEFAULT_TTL_HOURS.get(lane) if ttl_hours is None else ttl_hours
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        raise ValidationError("ttl_hours must be a positive number")

    checked = _aware_datetime(checked_at, "checked_at")
    current = (
        datetime.now(timezone.utc)
        if now is None
        else _aware_datetime(now, "now")
    )
    age_seconds = max(0.0, (current - checked).total_seconds())
    age_hours = age_seconds / 3600.0
    passed = age_hours <= float(ttl)
    return {
        "ok": passed,
        "command": "freshness-gate",
        "lane": lane,
        "checked_at": checked.isoformat().replace("+00:00", "Z"),
        "evaluated_at": current.isoformat().replace("+00:00", "Z"),
        "age_hours": round(age_hours, 3),
        "ttl_hours": float(ttl),
        "decision": "pass" if passed else "hold",
        "blockers": [] if passed else ["fact_check_stale"],
    }
