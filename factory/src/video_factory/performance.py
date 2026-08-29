from __future__ import annotations

from typing import Any, Iterable

from .errors import ValidationError


WEIGHTS = {
    "hook": 0.35,
    "hold": 0.30,
    "value": 0.20,
    "conversion": 0.15,
}


def _number(value: Any, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be numeric")
    number = float(value)
    if number < minimum:
        raise ValidationError(f"{field} must be at least {minimum}")
    return number


def _signals(snapshot: dict[str, Any]) -> dict[str, float]:
    engaged = _number(snapshot.get("engaged_views"), "engaged_views", minimum=1)
    hook = _number(snapshot.get("stayed_to_watch_rate"), "stayed_to_watch_rate")
    hold_candidates = [
        snapshot.get("average_percentage_viewed"),
        snapshot.get("completion_rate"),
    ]
    hold_values = [
        _number(value, "hold metric") for value in hold_candidates if value is not None
    ]
    if not hold_values:
        raise ValidationError(
            "average_percentage_viewed or completion_rate is required"
        )
    shares = _number(snapshot.get("shares", 0), "shares")
    saves = _number(snapshot.get("saves", 0), "saves")
    follows = _number(snapshot.get("follows", 0), "follows")
    return {
        "hook": hook,
        "hold": sum(hold_values) / len(hold_values),
        "value": (shares + saves) / engaged * 1000,
        "conversion": follows / engaged * 1000,
    }


def _percentile(value: float, cohort: Iterable[float]) -> float:
    values = list(cohort)
    if not values:
        raise ValidationError("cohort must not be empty")
    below = sum(item < value for item in values)
    equal = sum(item == value for item in values)
    return (below + 0.5 * equal) / len(values)


def evaluate_performance(
    candidate: dict[str, Any],
    cohort: list[dict[str, Any]],
    *,
    minimum_cohort: int = 5,
) -> dict[str, Any]:
    """Compare a 72-hour snapshot to a like-for-like channel cohort.

    The caller owns cohort selection (same account, platform, pod, duration band).
    The result changes editorial weights only; it never changes factual or rights
    confidence.
    """

    if len(cohort) < minimum_cohort:
        return {
            "ok": False,
            "status": "insufficient_cohort",
            "required": minimum_cohort,
            "available": len(cohort),
            "winner": False,
        }
    candidate_signals = _signals(candidate)
    cohort_signals = [_signals(item) for item in cohort]
    percentiles = {
        key: _percentile(candidate_signals[key], [item[key] for item in cohort_signals])
        for key in WEIGHTS
    }
    score = sum(percentiles[key] * weight for key, weight in WEIGHTS.items())
    policy_events = candidate.get("policy_events", [])
    if not isinstance(policy_events, list):
        raise ValidationError("policy_events must be an array")
    safety_clear = not any(
        isinstance(event, dict)
        and event.get("status") not in {None, "none", "resolved"}
        for event in policy_events
    )
    winner = (
        percentiles["hook"] > 0.5
        and any(percentiles[key] > 0.5 for key in ("hold", "value", "conversion"))
        and safety_clear
    )
    return {
        "ok": True,
        "status": "evaluated",
        "signals": {key: round(value, 4) for key, value in candidate_signals.items()},
        "percentiles": {key: round(value, 4) for key, value in percentiles.items()},
        "north_star_score": round(score, 4),
        "weights": WEIGHTS,
        "safety_clear": safety_clear,
        "winner": winner,
        "maximum_followups": 2 if winner else 0,
        "note": "Performance updates editorial weights, never factual or rights confidence.",
    }
