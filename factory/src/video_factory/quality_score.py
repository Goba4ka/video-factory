from __future__ import annotations

from typing import Any

from .errors import ValidationError


EDITORIAL_SCORE_FIELDS = {
    "visual_relevance": 10.0,
    "narrative_turn": 8.0,
    "opening_truthfulness": 5.0,
    "payoff": 7.0,
}

HARD_BOOLEAN_GATES = (
    "factual_review_passed",
    "freshness_review_passed",
    "rights_manifest_passed",
    "caption_review_passed",
    "technical_qc_passed",
    "visual_provenance_passed",
    "human_editor_approved",
)


def _bounded_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number from 0 to 1")
    number = float(value)
    if not 0 <= number <= 1:
        raise ValidationError(f"{field} must be a number from 0 to 1")
    return number


def evaluate_quality(
    *,
    preflight: dict[str, Any],
    editorial: dict[str, Any],
    originality: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate reference-quality readiness without hiding hard blockers.

    The score helps rank revisions. It never overrides factual, rights,
    technical, caption, originality, or human-approval gates.
    """

    if not isinstance(preflight, dict) or not isinstance(editorial, dict):
        raise ValidationError("preflight and editorial must be objects")
    if not isinstance(originality, dict):
        raise ValidationError("originality must be an object")

    checks = preflight.get("checks")
    if not isinstance(checks, dict) or not checks:
        raise ValidationError("preflight.checks must be a non-empty object")
    if any(not isinstance(value, bool) for value in checks.values()):
        raise ValidationError("every preflight check must be boolean")

    components: dict[str, float] = {}
    preflight_ratio = sum(checks.values()) / len(checks)
    components["reference_profile"] = round(preflight_ratio * 25.0, 2)

    editorial_points = 0.0
    for field, weight in EDITORIAL_SCORE_FIELDS.items():
        editorial_points += _bounded_number(editorial.get(field), field) * weight
    components["editorial"] = round(editorial_points, 2)

    gate_points = 0.0
    gate_weight = 25.0 / len(HARD_BOOLEAN_GATES)
    blockers: list[str] = []
    for field in HARD_BOOLEAN_GATES:
        value = editorial.get(field)
        if not isinstance(value, bool):
            raise ValidationError(f"{field} must be boolean")
        if value:
            gate_points += gate_weight
        else:
            blockers.append(field)
    components["hard_gate_evidence"] = round(gate_points, 2)

    decision = originality.get("decision")
    if decision not in {"allow", "review", "block"}:
        raise ValidationError("originality.decision must be allow, review, or block")
    similarity = _bounded_number(originality.get("similarity"), "originality.similarity")
    if decision == "allow":
        originality_points = 20.0 * (1.0 - 0.5 * similarity)
    elif decision == "review":
        originality_points = 8.0 * (1.0 - similarity)
        blockers.append("originality_review_required")
    else:
        originality_points = 0.0
        blockers.append("originality_blocked")
    components["originality"] = round(max(0.0, originality_points), 2)

    failed_preflight = sorted(key for key, passed in checks.items() if not passed)
    if failed_preflight:
        blockers.append("preflight_failed:" + ",".join(failed_preflight))

    score = round(sum(components.values()), 2)
    threshold = 85.0
    reference_quality = score >= threshold and not blockers
    return {
        "score": score,
        "threshold": threshold,
        "components": components,
        "blockers": blockers,
        "reference_quality": reference_quality,
        "publish_ready": reference_quality,
        "note": (
            "The score ranks revision quality; hard gates remain authoritative "
            "and view performance requires post-publication evidence."
        ),
    }
