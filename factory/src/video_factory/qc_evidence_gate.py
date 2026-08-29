"""Assemble eight independently produced QC reports into one strict bundle."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO

from ._qc_analyzer_common import (
    artifact_sha256,
    emit_main,
    file_sha256,
    parse_completed_at,
    safe_id,
)
from .contracts import QC_REQUIRED_CATEGORIES, validate_artifact
from .errors import ValidationError
from .validators import canonical_json


_SEMANTIC_ROLES = {
    "captions_analyzer": "captions",
    "facts_analyzer": "facts",
    "policy_analyzer": "policy",
    "dedup_analyzer": "dedup",
    "visual_analyzer": "visual",
}


def _evidence_root() -> Path:
    raw = os.environ.get("VIDEO_FACTORY_QC_EVIDENCE_ROOT")
    if not raw:
        raise ValidationError("VIDEO_FACTORY_QC_EVIDENCE_ROOT must be configured")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValidationError("VIDEO_FACTORY_QC_EVIDENCE_ROOT must be a regular directory")
    return root


def _contained_descriptor(
    raw: Any, *, root: Path, field: str, require_json: bool
) -> tuple[dict[str, str], Path]:
    if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256"}:
        raise ValidationError(f"{field} must contain exactly path and sha256")
    path_value = raw.get("path")
    expected = raw.get("sha256")
    if not isinstance(path_value, str) or not path_value:
        raise ValidationError(f"{field}.path must be a non-empty string")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValidationError(f"{field}.sha256 must be SHA-256")
    path = Path(path_value).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise ValidationError(f"{field}.path must be an absolute regular file")
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{field}.path escapes the evidence root") from exc
    if not path.is_file() or (require_json and path.suffix.lower() != ".json"):
        raise ValidationError(f"{field}.path is not the required evidence file")
    if file_sha256(path) != expected:
        raise ValidationError(f"{field} checksum does not match actual bytes")
    return {"path": str(path), "sha256": expected}, path


def _load_exact_report(path: Path, expected: Mapping[str, Any], field: str) -> None:
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValidationError(f"{field} exceeds the evidence size limit")
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{field} is unreadable JSON: {exc}") from exc
    if stored != dict(expected):
        raise ValidationError(f"{field} bytes do not contain the reported artifact")


def _upstream_entries(task: Mapping[str, Any]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    raw = task.get("upstream_results")
    if not isinstance(raw, list):
        raise ValidationError("task.upstream_results must be an array")
    found: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    roles = {"render", "qc_auto_evidence", *_SEMANTIC_ROLES}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        role = entry.get("role")
        if role not in roles:
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if not isinstance(result, dict) or not isinstance(artifact, dict):
            raise ValidationError(f"upstream role {role!r} has no artifact")
        if role in found:
            raise ValidationError(f"upstream role {role!r} is duplicated")
        found[str(role)] = (result, artifact)
    required = {"render", "qc_auto_evidence", *_SEMANTIC_ROLES}
    if set(found) != required:
        missing = sorted(required - set(found))
        raise ValidationError("qc_evidence_gate is missing roles: " + ", ".join(missing))
    return found


def handle_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if task.get("role") != "qc_evidence_gate":
        raise ValidationError("handler accepts only role='qc_evidence_gate'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "qc_evidence_bundle":
        raise ValidationError("qc_evidence_gate must require qc_evidence_bundle")
    job_id = safe_id(task.get("job_id") or payload.get("job_id"), "task.job_id")
    lane_id = safe_id(payload.get("lane_id"), "payload.lane_id")
    if payload.get("job_id") != job_id or task.get("pod") != lane_id:
        raise ValidationError("qc_evidence_gate task identity is not bound")
    root = _evidence_root()
    upstream = _upstream_entries(task)

    render_result, render = upstream["render"]
    validate_artifact("render_manifest", render)
    if render.get("job_id") != job_id:
        raise ValidationError("RenderManifest is not bound to evidence gate job")
    output_value = render_result.get("output_path")
    if not isinstance(output_value, str) or not output_value.strip():
        raise ValidationError("render result requires output_path")
    output = Path(output_value).expanduser().resolve()
    if not output.is_file() or file_sha256(output) != render["output_sha256"]:
        raise ValidationError("render master bytes changed before evidence gate")
    render_id = safe_id(render["render_id"], "render_manifest.render_id")
    render_sha256 = render["output_sha256"]
    render_manifest_sha256 = artifact_sha256(render)

    auto_result, auto = upstream["qc_auto_evidence"]
    del auto_result
    validate_artifact("qc_auto_evidence_manifest", auto)
    reports: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    for report in auto["reports"]:
        category = report["category"]
        descriptor, path = _contained_descriptor(
            auto["evidence"][category],
            root=root,
            field=f"qc_auto_evidence.{category}",
            require_json=True,
        )
        _load_exact_report(path, report, f"qc_auto_evidence.{category}")
        reports[category] = (report, descriptor)

    visual_contact: dict[str, str] | None = None
    for role, category in _SEMANTIC_ROLES.items():
        result, report = upstream[role]
        validate_artifact("qc_analyzer_report", report)
        if report["category"] != category:
            raise ValidationError(f"{role} returned category={report['category']!r}")
        descriptor, path = _contained_descriptor(
            result.get("evidence"),
            root=root,
            field=f"{role}.evidence",
            require_json=True,
        )
        _load_exact_report(path, report, f"{role}.evidence")
        reports[category] = (report, descriptor)
        if category == "visual":
            visual_contact, _ = _contained_descriptor(
                result.get("contact_sheet"),
                root=root,
                field="visual_analyzer.contact_sheet",
                require_json=False,
            )

    if set(reports) != QC_REQUIRED_CATEGORIES or visual_contact is None:
        raise ValidationError("evidence gate did not receive all eight categories")
    completed: list[str] = []
    blocking: list[str] = []
    rows: list[dict[str, Any]] = []
    for category in sorted(QC_REQUIRED_CATEGORIES):
        report, descriptor = reports[category]
        if any(
            report[field] != expected
            for field, expected in {
                "job_id": job_id,
                "lane_id": lane_id,
                "render_id": render_id,
                "render_sha256": render_sha256,
            }.items()
        ):
            raise ValidationError(f"{category} report identity is stale or cross-job")
        if report["bindings"].get("render_manifest_sha256") != render_manifest_sha256:
            raise ValidationError(f"{category} report is not bound to RenderManifest")
        if (
            report["status"] != "pass"
            or report["needs_human_review"] is not False
            or report["warnings"]
            or report["findings"]
        ):
            blocking.append(category)
        completed.append(parse_completed_at(report["completed_at"], f"{category}.completed_at"))
        rows.append(
            {
                "category": category,
                "artifact_sha256": artifact_sha256(report),
                "evidence": descriptor,
            }
        )
    if reports["visual"][0]["bindings"]["contact_sheet_sha256"] != visual_contact["sha256"]:
        raise ValidationError("visual report is not bound to its contact sheet")
    if blocking:
        raise ValidationError(
            "QC evidence categories did not pass: " + ", ".join(sorted(blocking))
        )

    created_at = max(completed)
    artifact = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "lane_id": lane_id,
        "render_id": render_id,
        "render_sha256": render_sha256,
        "reports": rows,
        "contact_sheet": visual_contact,
        "decision": {
            "passed": True,
            "needs_human_review": False,
            "blocking_categories": [],
        },
        "created_at": created_at,
    }
    validate_artifact("qc_evidence_bundle", artifact)
    return {
        "artifact": artifact,
        "render_output_path": str(output),
        "evidence_sha256": {
            row["category"]: row["evidence"]["sha256"] for row in rows
        },
        "visual_contact_sheet_sha256": visual_contact["sha256"],
    }


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    return emit_main(handle_task, stdin=stdin, stdout=stdout)


if __name__ == "__main__":
    raise SystemExit(main())
