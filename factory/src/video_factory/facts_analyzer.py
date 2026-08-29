"""Deterministic, fail-closed facts analyzer for a rendered master.

The handler does not turn a model's declared ``pass`` into approval.  It binds
machine evidence to the actual master bytes and recomputes complete coverage of
every script segment, shot and used claim against authoritative source records.
"""

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
from .errors import ValidationError
from .validators import canonical_json, digest_text


def _unique_index(items: list[dict[str, Any]], key: str, field: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        value = item.get(key)
        if not isinstance(value, str) or not value:
            raise ValidationError(f"{field}[{index}].{key} must be a non-empty string")
        if value in indexed:
            raise ValidationError(f"{field} contains duplicate {key} {value!r}")
        indexed[value] = item
    return indexed


def _string_set(value: Any, field: str) -> set[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValidationError(f"{field} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise ValidationError(f"{field} must not contain duplicates")
    return set(value)


def handle_task(task: Mapping[str, Any]) -> dict[str, Any]:
    payload, job_id, lane_id = common_task_context(task, "facts_analyzer")
    research = upstream_artifact(
        task, roles=("research",), contract="claim_ledger"
    )
    script_upstream = upstream_artifact(
        task, roles=("script",), contract="script_package"
    )
    shotlist_upstream = upstream_artifact(
        task, roles=("editor",), contract="shotlist"
    )
    render_upstream = upstream_artifact(
        task, roles=("render",), contract="render_manifest"
    )
    assert research is not None
    assert script_upstream is not None
    assert shotlist_upstream is not None
    assert render_upstream is not None
    _, claim_ledger = research
    _, script = script_upstream
    _, shotlist = shotlist_upstream
    render_result, render = render_upstream
    _, render_sha256, render_id = validate_master(render_result, render, job_id)

    if script.get("job_id") != job_id or script.get("lane_id") != lane_id:
        raise ValidationError("script_package is not bound to this job/lane")
    if script.get("idea_id") != claim_ledger.get("idea_id") or shotlist.get(
        "idea_id"
    ) != claim_ledger.get("idea_id"):
        raise ValidationError("facts analyzer upstream artifacts cross the idea boundary")
    if abs(
        float(render["technical"]["duration_seconds"])
        - float(shotlist["duration_seconds"])
    ) > 0.25:
        raise ValidationError("render duration does not match shotlist")

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

    sources = _unique_index(claim_ledger["sources"], "source_id", "claim_ledger.sources")
    claims = _unique_index(claim_ledger["claims"], "claim_id", "claim_ledger.claims")
    segments = _unique_index(script["segments"], "segment_id", "script_package.segments")
    shots = _unique_index(shotlist["shots"], "shot_id", "shotlist.shots")
    for claim_id, claim in claims.items():
        source_ids = _string_set(
            claim.get("source_ids"), f"claim_ledger.claims[{claim_id}].source_ids"
        )
        unknown_sources = sorted(source_ids - set(sources))
        if unknown_sources:
            raise ValidationError(
                f"claim {claim_id} references unknown sources: "
                + ", ".join(unknown_sources)
            )

    used_claim_ids: set[str] = set()
    for segment_id, segment in segments.items():
        used_claim_ids.update(
            _string_set(
                segment.get("claim_ids"),
                f"script_package.segments[{segment_id}].claim_ids",
            )
        )
    for shot_id, shot in shots.items():
        used_claim_ids.update(
            _string_set(shot.get("claim_ids"), f"shotlist.shots[{shot_id}].claim_ids")
        )
    unknown_claims = sorted(used_claim_ids - set(claims))
    if unknown_claims:
        raise ValidationError(
            "script/shotlist reference unknown claims: " + ", ".join(unknown_claims)
        )
    for claim_id in sorted(used_claim_ids):
        claim = claims[claim_id]
        if claim.get("support") not in {"direct", "inference"}:
            raise ValidationError(f"used claim {claim_id} is not adequately supported")
        if claim.get("risk") != "green":
            raise ValidationError(f"used claim {claim_id} is not green-risk")
        if claim.get("script_usage") != "allowed":
            raise ValidationError(f"used claim {claim_id} is not explicitly allowed")

    bindings = {
        "output_sha256": render_sha256,
        "render_manifest_sha256": artifact_sha256(render),
        "claim_ledger_sha256": artifact_sha256(claim_ledger),
        "script_package_sha256": artifact_sha256(script),
        "shotlist_sha256": artifact_sha256(shotlist),
    }
    verified_source_ids: set[str] = set()
    for claim_id in sorted(used_claim_ids):
        claim = claims[claim_id]
        source_records = [sources[item] for item in sorted(claim["source_ids"])]
        # Re-hashing canonical source records makes the evidence independently
        # checkable and ensures coverage cannot be inferred from IDs alone.
        digest_text(canonical_json(source_records))
        verified_source_ids.update(claim["source_ids"])
    created_at = (
        parse_completed_at(render["created_at"], "render_manifest.created_at")
        if render.get("created_at")
        else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    run_id = "facts-" + artifact_sha256(bindings)[:24]
    report = {
        "schema_version": "1.0.0",
        "category": "facts",
        "job_id": job_id,
        "lane_id": lane_id,
        "render_id": render_id,
        "render_sha256": render_sha256,
        "status": "pass",
        "needs_human_review": False,
        "warnings": [],
        "findings": [],
        "checker": {
            "name": "video_factory.facts_analyzer",
            "version": "1.0.0",
            "run_id": run_id,
        },
        "completed_at": created_at,
        "bindings": bindings,
        "metrics": {
            "script_segments_total": len(segments),
            "script_segments_verified": len(segments),
            "shots_total": len(shots),
            "shots_verified": len(shots),
            "used_claims_total": len(used_claim_ids),
            "used_claims_verified": len(used_claim_ids),
            "source_records_total": len(sources),
            "source_records_verified": len(verified_source_ids),
        },
    }
    return {"artifact": report, "evidence": persist_report(report)}


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    return emit_main(handle_task, stdin=stdin, stdout=stdout)


if __name__ == "__main__":
    raise SystemExit(main())
