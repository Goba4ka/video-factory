"""Fail-closed semantic QC handler for checksum-bound analyzer evidence.

This module does not claim to perform semantic analysis.  It reruns the
existing deterministic media QC and accepts semantic decisions only from eight
explicit, immutable-by-checksum evidence files.  Missing, stale, warning or
human-review evidence is a task failure, never a synthetic pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from .contracts import QC_REQUIRED_CATEGORIES, validate_artifact
from .errors import FactoryError, ValidationError
from .media_freeze import MediaFreezeError, verify_frozen_media_manifest
from .media_qc import QC_PROFILES, run_media_qc
from .validators import canonical_json, digest_text, require_nonempty_string


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_EVIDENCE_FIELDS = frozenset({"path", "sha256"})
_COMMON_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "category",
        "job_id",
        "lane_id",
        "render_id",
        "render_sha256",
        "status",
        "needs_human_review",
        "warnings",
        "findings",
        "checker",
        "completed_at",
        "bindings",
        "metrics",
    }
)
_REQUIRED_BINDINGS: dict[str, frozenset[str]] = {
    "technical": frozenset({"output_sha256", "render_manifest_sha256"}),
    "audio": frozenset({"output_sha256", "render_manifest_sha256"}),
    "captions": frozenset(
        {
            "output_sha256",
            "render_manifest_sha256",
            "script_package_sha256",
            "machine_evidence_sha256",
        }
    ),
    "facts": frozenset(
        {
            "output_sha256",
            "render_manifest_sha256",
            "claim_ledger_sha256",
            "script_package_sha256",
            "shotlist_sha256",
        }
    ),
    "rights": frozenset(
        {
            "output_sha256",
            "render_manifest_sha256",
            "rights_manifest_sha256",
            "frozen_media_manifest_sha256",
            "shotlist_sha256",
        }
    ),
    "dedup": frozenset(
        {"output_sha256", "render_manifest_sha256", "corpus_snapshot_sha256"}
    ),
    "policy": frozenset(
        {
            "output_sha256",
            "render_manifest_sha256",
            "claim_ledger_sha256",
            "script_package_sha256",
        }
    ),
    "visual": frozenset(
        {
            "output_sha256",
            "render_manifest_sha256",
            "shotlist_sha256",
            "contact_sheet_sha256",
        }
    ),
}


def _safe_id(value: Any, field: str) -> str:
    normalized = require_nonempty_string(value, field)
    if not _SAFE_ID.fullmatch(normalized) or ".." in normalized:
        raise ValidationError(f"{field} contains unsafe characters")
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ValidationError(f"cannot hash evidence file {path}: {exc}") from exc
    return digest.hexdigest()


def _artifact_sha256(value: Mapping[str, Any]) -> str:
    return digest_text(canonical_json(dict(value)))


def _configured_evidence_root() -> Path:
    raw = os.environ.get("VIDEO_FACTORY_QC_EVIDENCE_ROOT")
    if not raw:
        raise ValidationError("VIDEO_FACTORY_QC_EVIDENCE_ROOT must be configured")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ValidationError("VIDEO_FACTORY_QC_EVIDENCE_ROOT must exist")
    return root


def _contained_file(value: Any, field: str, root: Path) -> Path:
    text = require_nonempty_string(value, field)
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValidationError(f"{field} must be absolute")
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{field} escapes VIDEO_FACTORY_QC_EVIDENCE_ROOT") from exc
    if not path.is_file():
        raise ValidationError(f"{field} does not exist: {path}")
    return path


def _descriptor(
    raw: Any, field: str, root: Path, *, require_json: bool
) -> tuple[Path, str]:
    if not isinstance(raw, Mapping):
        raise ValidationError(f"{field} must be an object")
    if set(raw) != _EVIDENCE_FIELDS:
        raise ValidationError(f"{field} must contain exactly path and sha256")
    path = _contained_file(raw.get("path"), f"{field}.path", root)
    if require_json and path.suffix.lower() != ".json":
        raise ValidationError(f"{field}.path must name a JSON file")
    expected = require_nonempty_string(raw.get("sha256"), f"{field}.sha256")
    if not _SHA256.fullmatch(expected):
        raise ValidationError(f"{field}.sha256 must be lowercase SHA-256")
    actual = _sha256_file(path)
    if actual != expected:
        raise ValidationError(f"{field} checksum does not match actual bytes")
    return path, expected


def _load_json_evidence(path: Path, field: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValidationError(f"{field} exceeds the 4 MiB evidence limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{field} is not readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must contain one JSON object")
    return value


def _upstream(
    task: Mapping[str, Any], role: str, contract: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    upstream = task.get("upstream_results")
    if not isinstance(upstream, list):
        raise ValidationError("task.upstream_results must be an array")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in upstream:
        if not isinstance(entry, Mapping) or entry.get("role") != role:
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(result, dict) and isinstance(artifact, dict):
            matches.append((result, artifact))
    if len(matches) != 1:
        raise ValidationError(
            f"qc task requires exactly one upstream {contract} from role={role!r}"
        )
    validate_artifact(contract, matches[0][1])
    return matches[0]


def _optional_upstream(
    task: Mapping[str, Any], role: str, contract: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    upstream = task.get("upstream_results")
    if not isinstance(upstream, list):
        raise ValidationError("task.upstream_results must be an array")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in upstream:
        if not isinstance(entry, Mapping) or entry.get("role") != role:
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(result, dict) and isinstance(artifact, dict):
            matches.append((result, artifact))
    if len(matches) > 1:
        raise ValidationError(f"qc task has duplicate upstream role={role!r}")
    if not matches:
        return None
    validate_artifact(contract, matches[0][1])
    return matches[0]


def _parse_completed_at(value: Any, field: str) -> datetime:
    text = require_nonempty_string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_checker(value: Any, field: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {"name", "version", "run_id"}:
        raise ValidationError(f"{field} must contain name, version and run_id")
    for key in ("name", "version", "run_id"):
        require_nonempty_string(value.get(key), f"{field}.{key}")


def _validate_report(
    report: Mapping[str, Any],
    *,
    category: str,
    job_id: str,
    lane_id: str,
    render_id: str,
    render_sha256: str,
    expected_bindings: Mapping[str, str],
) -> datetime:
    unknown = set(report) - _COMMON_REPORT_FIELDS
    missing = _COMMON_REPORT_FIELDS - set(report)
    if unknown or missing:
        details = []
        if missing:
            details.append("missing=" + ",".join(sorted(missing)))
        if unknown:
            details.append("unknown=" + ",".join(sorted(unknown)))
        raise ValidationError(f"{category} evidence fields are invalid: {'; '.join(details)}")
    if report.get("schema_version") != "1.0.0":
        raise ValidationError(f"{category} evidence schema_version must be 1.0.0")
    if report.get("category") != category:
        raise ValidationError(f"{category} evidence category is not bound")
    if (
        report.get("job_id") != job_id
        or report.get("lane_id") != lane_id
        or report.get("render_id") != render_id
    ):
        raise ValidationError(f"{category} evidence is not bound to this job/render")
    if report.get("render_sha256") != render_sha256:
        raise ValidationError(f"{category} evidence is stale for this render")
    if report.get("status") != "pass":
        raise ValidationError(f"{category} evidence status is not pass")
    if report.get("needs_human_review") is not False:
        raise ValidationError(f"{category} evidence needs human review")
    if report.get("warnings") != []:
        raise ValidationError(f"{category} evidence contains warnings")
    if report.get("findings") != []:
        raise ValidationError(f"{category} evidence contains findings")
    _validate_checker(report.get("checker"), f"{category}.checker")
    bindings = report.get("bindings")
    if not isinstance(bindings, Mapping):
        raise ValidationError(f"{category}.bindings must be an object")
    required = set(_REQUIRED_BINDINGS[category])
    if category == "policy" and "safety_gate_report_sha256" in expected_bindings:
        required.add("safety_gate_report_sha256")
    if set(bindings) != required:
        raise ValidationError(
            f"{category}.bindings must contain exactly: {', '.join(sorted(required))}"
        )
    for key in required:
        value = bindings.get(key)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ValidationError(f"{category}.bindings.{key} must be SHA-256")
        expected = expected_bindings.get(key)
        if expected is not None and value != expected:
            raise ValidationError(f"{category}.bindings.{key} does not match")
    if not isinstance(report.get("metrics"), Mapping) or not report["metrics"]:
        raise ValidationError(f"{category}.metrics must contain machine observations")
    return _parse_completed_at(report.get("completed_at"), f"{category}.completed_at")


def _validate_technical_result(
    result: Any, *, output: Path, render: Mapping[str, Any]
) -> str:
    if not isinstance(result, Mapping):
        raise ValidationError("technical media QC returned no report")
    if result.get("level") != "full" or result.get("technical_pass") is not True:
        raise ValidationError("full technical media QC did not pass")
    if result.get("failures") != [] or result.get("warnings") != []:
        raise ValidationError("technical media QC has failures or warnings")
    if Path(str(result.get("source", ""))).expanduser().resolve() != output:
        raise ValidationError("technical media QC is not bound to render output")
    media = result.get("media")
    video = media.get("video") if isinstance(media, Mapping) else None
    audio = media.get("audio") if isinstance(media, Mapping) else None
    technical = render["technical"]
    if not isinstance(video, Mapping) or not isinstance(audio, Mapping):
        raise ValidationError("technical media QC lacks audio/video stream evidence")
    expected_video = {
        "width": technical["width"],
        "height": technical["height"],
        "fps": float(technical["fps"]),
    }
    actual_video = {
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": float(video.get("fps", -1)),
    }
    if actual_video != expected_video:
        raise ValidationError("technical media QC does not match render video metadata")
    if audio.get("sample_rate_hz") != technical["audio_sample_rate_hz"]:
        raise ValidationError("technical media QC does not match render audio metadata")
    cache = result.get("cache")
    report_path = cache.get("report_path") if isinstance(cache, Mapping) else None
    if not isinstance(report_path, str) or not Path(report_path).is_file():
        raise ValidationError("technical media QC lacks its immutable cached report")
    return _sha256_file(Path(report_path).resolve())


def handle_task(
    task: Mapping[str, Any],
    *,
    media_qc_runner: Callable[..., dict[str, Any]] = run_media_qc,
) -> dict[str, Any]:
    if task.get("role") != "qc":
        raise ValidationError("semantic_qc_handler accepts only role='qc'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "qc_report":
        raise ValidationError("qc task must require qc_report")
    job_id = _safe_id(task.get("job_id") or payload.get("job_id"), "task.job_id")
    if payload.get("job_id") != job_id:
        raise ValidationError("payload.job_id is not bound to task.job_id")
    lane = require_nonempty_string(payload.get("lane_id"), "payload.lane_id")
    if task.get("pod") != lane:
        raise ValidationError("payload.lane_id is not bound to task.pod")

    render_result, render = _upstream(task, "render", "render_manifest")
    _, claim_ledger = _upstream(task, "research", "claim_ledger")
    _, script = _upstream(task, "script", "script_package")
    _, shotlist = _upstream(task, "editor", "shotlist")
    _, rights = _upstream(task, "rights", "rights_manifest")
    _, frozen = _upstream(task, "media", "frozen_media_manifest")
    safety_role = {
        "war_history": "sensitivity_review",
        "celebrity_news": "privacy_review",
        "chinese_medicine": "medical_review",
        "health": "medical_review",
    }.get(lane)
    safety = (
        _upstream(task, safety_role, "safety_gate_report")[1]
        if safety_role is not None
        else None
    )
    if render["job_id"] != job_id or script["job_id"] != job_id:
        raise ValidationError("render/script artifacts are not bound to task.job_id")
    if frozen["job_id"] != job_id:
        raise ValidationError("frozen media is not bound to task.job_id")
    if script["lane_id"] != lane:
        raise ValidationError("script lane does not match QC lane")
    idea_ids = {
        claim_ledger["idea_id"],
        script["idea_id"],
        shotlist["idea_id"],
        rights["idea_id"],
        frozen["idea_id"],
        *( [safety["idea_id"]] if safety is not None else [] ),
    }
    if len(idea_ids) != 1:
        raise ValidationError("upstream artifacts cross the idea boundary")
    if abs(float(render["technical"]["duration_seconds"]) - float(shotlist["duration_seconds"])) > 0.25:
        raise ValidationError("render duration does not match shotlist")

    rights_decision = rights["decision"]
    if (
        rights_decision["passed"] is not True
        or rights_decision["needs_human_review"] is not False
        or rights_decision["missing_asset_ids"]
    ):
        raise ValidationError("rights manifest has not passed its hard gate")
    try:
        verify_frozen_media_manifest(frozen, rights_manifest=rights, expected_job_id=job_id)
    except MediaFreezeError as exc:
        raise ValidationError(f"frozen media verification failed: {exc}") from exc
    frozen_ids = {item["asset_id"] for item in frozen["assets"]}
    missing_assets = sorted({shot["asset_id"] for shot in shotlist["shots"]} - frozen_ids)
    if missing_assets:
        raise ValidationError("shotlist assets are not frozen: " + ", ".join(missing_assets))

    output_value = render_result.get("output_path")
    if not isinstance(output_value, str) or not output_value.strip():
        raise ValidationError("render result requires output_path")
    output = Path(output_value).expanduser().resolve()
    if not output.is_file():
        raise ValidationError(f"render output does not exist: {output}")
    output_sha256 = _sha256_file(output)
    if output_sha256 != render["output_sha256"]:
        raise ValidationError("render output checksum does not match render_manifest")

    profile_name = require_nonempty_string(
        payload.get("technical_profile"), "payload.technical_profile"
    )
    profile = QC_PROFILES.get(profile_name)
    if not isinstance(profile, Mapping) or profile.get("exact_resolution") != [1080, 1920]:
        raise ValidationError("technical_profile must be a 1080x1920 final-master profile")
    if not all(
        profile.get(key) is True
        for key in ("require_h264_aac", "require_yuv420p")
    ):
        raise ValidationError("technical_profile must enforce final codecs and pixel format")
    technical_result = media_qc_runner(
        output,
        level="full",
        profile_name=profile_name,
        cache_root=os.environ.get("VIDEO_FACTORY_QC_CACHE_ROOT"),
    )
    technical_report_sha256 = _validate_technical_result(
        technical_result, output=output, render=render
    )

    root = _configured_evidence_root()
    evidence_bundle_entry = _optional_upstream(
        task, "qc_evidence_gate", "qc_evidence_bundle"
    )
    if evidence_bundle_entry is not None:
        _, evidence_bundle = evidence_bundle_entry
        if any(
            evidence_bundle[field] != expected
            for field, expected in {
                "job_id": job_id,
                "lane_id": lane,
                "render_id": render["render_id"],
                "render_sha256": output_sha256,
            }.items()
        ):
            raise ValidationError("qc_evidence_bundle is stale or cross-job")
        bundle_decision = evidence_bundle["decision"]
        if (
            bundle_decision["passed"] is not True
            or bundle_decision["needs_human_review"] is not False
            or bundle_decision["blocking_categories"]
        ):
            raise ValidationError("qc_evidence_bundle has not passed its hard gate")
        raw_evidence = {
            row["category"]: row["evidence"]
            for row in evidence_bundle["reports"]
        }
        bundle_artifact_hashes = {
            row["category"]: row["artifact_sha256"]
            for row in evidence_bundle["reports"]
        }
        contact_descriptor = evidence_bundle["contact_sheet"]
    else:
        raw_evidence = payload.get("evidence")
        bundle_artifact_hashes = None
        contact_descriptor = payload.get("visual_contact_sheet")
    if not isinstance(raw_evidence, Mapping) or set(raw_evidence) != QC_REQUIRED_CATEGORIES:
        raise ValidationError(
            "QC evidence must contain exactly the eight categories"
        )
    script_sha256 = _artifact_sha256(script)
    shotlist_sha256 = _artifact_sha256(shotlist)
    rights_sha256 = _artifact_sha256(rights)
    frozen_sha256 = _artifact_sha256(frozen)
    claim_ledger_sha256 = _artifact_sha256(claim_ledger)
    render_manifest_sha256 = _artifact_sha256(render)
    known_bindings = {
        "output_sha256": output_sha256,
        "render_manifest_sha256": render_manifest_sha256,
        "claim_ledger_sha256": claim_ledger_sha256,
        "script_package_sha256": script_sha256,
        "shotlist_sha256": shotlist_sha256,
        "rights_manifest_sha256": rights_sha256,
        "frozen_media_manifest_sha256": frozen_sha256,
    }
    if safety is not None:
        known_bindings["safety_gate_report_sha256"] = _artifact_sha256(safety)
    evidence_rows: dict[str, tuple[Path, str, dict[str, Any]]] = {}
    completed: list[datetime] = []
    for category in sorted(QC_REQUIRED_CATEGORIES):
        path, sha256 = _descriptor(
            raw_evidence[category], f"payload.evidence.{category}", root, require_json=True
        )
        report = _load_json_evidence(path, f"payload.evidence.{category}")
        if (
            bundle_artifact_hashes is not None
            and _artifact_sha256(report) != bundle_artifact_hashes[category]
        ):
            raise ValidationError(
                f"{category} evidence artifact hash differs from qc_evidence_bundle"
            )
        completed.append(
            _validate_report(
                report,
                category=category,
                job_id=job_id,
                lane_id=lane,
                render_id=render["render_id"],
                render_sha256=output_sha256,
                expected_bindings=known_bindings,
            )
        )
        evidence_rows[category] = (path, sha256, report)

    visual = evidence_rows["visual"][2]
    # Contact sheet is deliberately outside the generic report fields, so a
    # visual report must bind it through the descriptor encoded in bindings.
    # Accept it in bindings as an adjacent sidecar descriptor file instead.
    contact_path, contact_sha256 = _descriptor(
        contact_descriptor, "payload.visual_contact_sheet", root, require_json=False
    )
    if visual["bindings"]["contact_sheet_sha256"] != contact_sha256:
        raise ValidationError("visual evidence is not bound to the contact sheet")

    checks: list[dict[str, Any]] = []
    for category in (
        "technical",
        "audio",
        "captions",
        "facts",
        "rights",
        "dedup",
        "policy",
        "visual",
    ):
        path, sha256, _ = evidence_rows[category]
        suffix = (
            f"; media_qc_sha256={technical_report_sha256}"
            if category in {"technical", "audio"}
            else f"; contact_sheet={contact_path}#sha256={contact_sha256}"
            if category == "visual"
            else ""
        )
        checks.append(
            {
                "check_id": f"{category}_evidence",
                "category": category,
                "status": "pass",
                "evidence": f"{path}#sha256={sha256}{suffix}",
                "artifact": str(path),
            }
        )
    created_at = max(completed).isoformat().replace("+00:00", "Z")
    artifact = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "render_id": render["render_id"],
        "technical": {
            "audio_sample_rate_hz": render["technical"]["audio_sample_rate_hz"]
        },
        "checks": checks,
        "decision": {
            "passed": True,
            "needs_human_review": False,
            "blocking_check_ids": [],
            "review_notes": [],
        },
        "created_at": created_at,
    }
    validate_artifact("qc_report", artifact)
    return {
        "artifact": artifact,
        "render_output_path": str(output),
        "evidence_sha256": {
            category: row[1] for category, row in sorted(evidence_rows.items())
        },
        "visual_contact_sheet_sha256": contact_sha256,
        "technical_media_qc_sha256": technical_report_sha256,
    }


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    try:
        task = json.load(source)
        if not isinstance(task, dict):
            raise ValidationError("handler stdin must contain one JSON object")
        result = handle_task(task)
    except (
        FactoryError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        sys.stderr.write(f"semantic_qc_handler_error:{type(exc).__name__}:{exc}\n")
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
