"""Deterministic, fail-closed policy analyzer for rendered masters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, TextIO

from ._qc_analyzer_common import (
    artifact_sha256,
    common_task_context,
    emit_main,
    parse_completed_at,
    persist_report,
    upstream_artifact,
    validate_master,
)
from .contracts import REQUIRED_SAFETY_GATES
from .errors import ValidationError


_SAFETY_ROLES = {
    "war_history": "sensitivity_review",
    "celebrity_news": "privacy_review",
    "chinese_medicine": "medical_review",
    "health": "medical_review",
}
_COMMON_RULES = frozenset(
    {
        "content_integrity",
        "dangerous_content",
        "synthetic_media_disclosure",
    }
)
_LANE_RULES = {
    "war_history": "war_sensitivity",
    "celebrity_news": "privacy_defamation",
    "motivation": "harmful_extremes",
    "chinese_medicine": "medical_safety",
    "health": "medical_safety",
}
def _unique_string_ids(value: Any, field: str) -> set[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValidationError(f"{field} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise ValidationError(f"{field} must not contain duplicates")
    return set(value)


def handle_task(task: Mapping[str, Any]) -> dict[str, Any]:
    payload, job_id, lane_id = common_task_context(task, "policy_analyzer")
    if lane_id not in _LANE_RULES:
        raise ValidationError(f"unsupported policy lane {lane_id!r}")
    research = upstream_artifact(
        task, roles=("research",), contract="claim_ledger"
    )
    script_upstream = upstream_artifact(
        task, roles=("script",), contract="script_package"
    )
    render_upstream = upstream_artifact(
        task, roles=("render",), contract="render_manifest"
    )
    assert research is not None
    assert script_upstream is not None
    assert render_upstream is not None
    _, claim_ledger = research
    _, script = script_upstream
    render_result, render = render_upstream
    _, render_sha256, render_id = validate_master(render_result, render, job_id)

    if script.get("job_id") != job_id or script.get("lane_id") != lane_id:
        raise ValidationError("script_package is not bound to this job/lane")
    if script.get("idea_id") != claim_ledger.get("idea_id"):
        raise ValidationError("policy analyzer upstream artifacts cross the idea boundary")
    ledger_decision = claim_ledger["decision"]
    if (
        ledger_decision["passed"] is not True
        or ledger_decision["needs_human_review"] is not False
        or ledger_decision["review_notes"] != []
    ):
        raise ValidationError("claim_ledger has not passed its hard gate")
    script_decision = script["decision"]
    if (
        script_decision["passed"] is not True
        or script_decision["needs_human_review"] is not False
        or script_decision["review_notes"] != []
    ):
        raise ValidationError("script_package has not passed its hard gate")

    source_ids = _unique_string_ids(
        [item["source_id"] for item in claim_ledger["sources"]],
        "claim_ledger source ids",
    )
    claim_ids = _unique_string_ids(
        [item["claim_id"] for item in claim_ledger["claims"]],
        "claim_ledger claim ids",
    )
    for claim in claim_ledger["claims"]:
        referenced_sources = _unique_string_ids(
            claim["source_ids"], f"claim {claim['claim_id']}.source_ids"
        )
        unknown_sources = sorted(referenced_sources - source_ids)
        if unknown_sources:
            raise ValidationError(
                f"claim {claim['claim_id']} references unknown sources: "
                + ", ".join(unknown_sources)
            )
    used_claim_ids: set[str] = set()
    for index, segment in enumerate(script["segments"]):
        used_claim_ids.update(
            _unique_string_ids(
                segment["claim_ids"],
                f"script_package.segments[{index}].claim_ids",
            )
        )
    unknown_claims = sorted(used_claim_ids - claim_ids)
    if unknown_claims:
        raise ValidationError(
            "script references unknown claims: " + ", ".join(unknown_claims)
        )

    required_gate = REQUIRED_SAFETY_GATES.get(lane_id)
    safety_upstream = upstream_artifact(
        task,
        roles=(
            (_SAFETY_ROLES[lane_id],)
            if required_gate is not None
            else (
                "sensitivity_review",
                "privacy_review",
                "medical_review",
                "editorial_safety",
            )
        ),
        contract="safety_gate_report",
        required=required_gate is not None,
    )
    safety: dict[str, Any] | None = None
    if safety_upstream is not None:
        _, safety = safety_upstream
        expected_gate = required_gate or "editorial_safety"
        if (
            safety.get("job_id") != job_id
            or safety.get("idea_id") != script.get("idea_id")
            or safety.get("lane") != lane_id
            or safety.get("gate_type") != expected_gate
        ):
            raise ValidationError("safety_gate_report is not bound to this job/lane/gate")
        decision = safety["decision"]
        if (
            decision["passed"] is not True
            or decision["needs_human_review"] is not False
            or decision["review_notes"] != []
            or safety["findings"] != []
        ):
            raise ValidationError("safety_gate_report has not passed its clean hard gate")
        checked_sources = _unique_string_ids(
            safety["source_ids_checked"], "safety_gate_report.source_ids_checked"
        )
        unknown_sources = sorted(checked_sources - source_ids)
        if unknown_sources:
            raise ValidationError(
                "safety gate references unknown sources: " + ", ".join(unknown_sources)
            )
        missing_sources = sorted(source_ids - checked_sources)
        if missing_sources:
            raise ValidationError(
                "safety gate source coverage is incomplete: " + ", ".join(missing_sources)
            )

    bindings = {
        "output_sha256": render_sha256,
        "render_manifest_sha256": artifact_sha256(render),
        "claim_ledger_sha256": artifact_sha256(claim_ledger),
        "script_package_sha256": artifact_sha256(script),
    }
    if safety is not None:
        bindings["safety_gate_report_sha256"] = artifact_sha256(safety)

    required_rules = set(_COMMON_RULES) | {_LANE_RULES[lane_id]}
    completed_at = (
        parse_completed_at(render["created_at"], "render_manifest.created_at")
        if render.get("created_at")
        else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    run_id = "policy-" + artifact_sha256(bindings)[:24]
    report = {
        "schema_version": "1.0.0",
        "category": "policy",
        "job_id": job_id,
        "lane_id": lane_id,
        "render_id": render_id,
        "render_sha256": render_sha256,
        "status": "pass",
        "needs_human_review": False,
        "warnings": [],
        "findings": [],
        "checker": {
            "name": "video_factory.policy_analyzer",
            "version": "1.0.0",
            "run_id": run_id,
        },
        "completed_at": completed_at,
        "bindings": bindings,
        "metrics": {
            "required_rules_total": len(required_rules),
            "rules_verified": len(required_rules),
            "research_sources_total": len(source_ids),
            "used_claims_total": len(used_claim_ids),
            "safety_gate_required": required_gate is not None,
            "safety_sources_verified": len(safety["source_ids_checked"])
            if safety is not None
            else 0,
        },
    }
    return {"artifact": report, "evidence": persist_report(report)}


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    return emit_main(handle_task, stdin=stdin, stdout=stdout)


if __name__ == "__main__":
    raise SystemExit(main())
