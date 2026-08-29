"""Read-only production-evidence gate for a completed daily video batch.

This module never initializes or migrates SQLite, never completes a human gate,
and never invokes a provider, renderer, or publisher.  It evaluates durable
queue state and the exact bytes already produced by the production runtime.
Synthetic queue simulations are deliberately ineligible.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import validate_artifact
from .db import SCHEMA_VERSION
from .errors import ValidationError
from .lanes import load_lane_registry, roles_for_lane
from .validators import canonical_json, digest_text, require_nonempty_string
from .worker import default_resource_lock_path


REPORT_VERSION = "1.0.0"
QC_CATEGORIES = (
    "technical",
    "audio",
    "captions",
    "facts",
    "rights",
    "dedup",
    "policy",
    "visual",
)
AUTO_QC_CATEGORIES = frozenset({"technical", "audio", "rights"})
ANALYZER_ROLES = {
    "captions": "captions_analyzer",
    "facts": "facts_analyzer",
    "dedup": "dedup_analyzer",
    "policy": "policy_analyzer",
    "visual": "visual_analyzer",
}
REQUIRED_TABLE_COLUMNS: Mapping[str, frozenset[str]] = {
    "ideas": frozenset({"id", "payload_json"}),
    "jobs": frozenset({"id", "idea_id", "batch_id", "created_at"}),
    "tasks": frozenset(
        {
            "id",
            "job_id",
            "dependency_task_id",
            "role",
            "pod",
            "kind",
            "payload_json",
            "status",
            "attempt_count",
            "available_at",
            "lease_expires_at",
            "result_json",
            "created_at",
            "completed_at",
        }
    ),
    "task_attempts": frozenset(
        {
            "task_id",
            "attempt_no",
            "status",
            "claimed_at",
            "finished_at",
            "lease_expires_at",
        }
    ),
    "dead_letters": frozenset({"task_id", "status", "cause_code"}),
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _timestamp(value: str | datetime | None, field: str) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} must be an ISO-8601 date-time") from exc
    else:
        raise ValidationError(f"{field} must be an ISO-8601 date-time")
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _number(value: Any, field: str, *, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum or (
        maximum is not None and result > maximum
    ):
        bound = f" through {maximum}" if maximum is not None else " or greater"
        raise ValidationError(f"{field} must be {minimum}{bound}")
    return result


def expected_lane_distribution(
    registry: Mapping[str, Any], target: int
) -> dict[str, int]:
    """Return the registry-order allocation used by the daily runtime."""

    if not _is_int(target) or not 10 <= target <= 15:
        raise ValidationError("target must be an integer from 10 to 15")
    enabled = [lane for lane in registry["lanes"] if lane["enabled"]]
    counts = {lane["id"]: int(lane["daily"]["min"]) for lane in enabled}
    minimum = sum(counts.values())
    maximum = sum(int(lane["daily"]["max"]) for lane in enabled)
    if not minimum <= target <= maximum:
        raise ValidationError(
            f"target {target} is outside registry capacity {minimum}..{maximum}"
        )
    remaining = target - minimum
    for lane in enabled:
        if remaining <= 0:
            break
        lane_id = lane["id"]
        available = int(lane["daily"]["max"]) - counts[lane_id]
        addition = min(available, remaining)
        counts[lane_id] += addition
        remaining -= addition
    if remaining:
        raise ValidationError("registry could not allocate the requested target")
    return counts


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        result = ordered[lower]
    else:
        weight = position - lower
        result = ordered[lower] * (1 - weight) + ordered[upper] * weight
    return round(result, 3)


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "p50_seconds": None, "p95_seconds": None, "max_seconds": None}
    return {
        "count": len(values),
        "p50_seconds": _percentile(values, 0.50),
        "p95_seconds": _percentile(values, 0.95),
        "max_seconds": round(max(values), 3),
    }


def _grouped_metrics(
    observations: Sequence[tuple[str, str, float]]
) -> dict[str, Any]:
    by_role: dict[str, list[float]] = defaultdict(list)
    by_lane: dict[str, list[float]] = defaultdict(list)
    by_role_lane: dict[tuple[str, str], list[float]] = defaultdict(list)
    all_values: list[float] = []
    for role, lane, value in observations:
        all_values.append(value)
        by_role[role].append(value)
        by_lane[lane].append(value)
        by_role_lane[(role, lane)].append(value)
    return {
        "overall": _distribution(all_values),
        "by_role": {key: _distribution(by_role[key]) for key in sorted(by_role)},
        "by_lane": {key: _distribution(by_lane[key]) for key in sorted(by_lane)},
        "by_role_lane": [
            {
                "role": role,
                "lane": lane,
                **_distribution(by_role_lane[(role, lane)]),
            }
            for role, lane in sorted(by_role_lane)
        ],
    }


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} is missing")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{field} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValidationError(f"{field} must contain a JSON object")
    return parsed


def _artifact_result(task: Mapping[str, Any], contract: str) -> tuple[dict[str, Any], dict[str, Any]]:
    result_json = task["result_json"] if "result_json" in set(task.keys()) else None
    result = _json_object(result_json, f"{task['role']}.result_json")
    artifact = result.get("artifact")
    if not isinstance(artifact, dict):
        raise ValidationError(f"{task['role']} result has no artifact object")
    validate_artifact(contract, artifact)
    return result, artifact


def _artifact_sha256(value: Mapping[str, Any]) -> str:
    return digest_text(canonical_json(dict(value)))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class _FileVerifier:
    def __init__(self, base: Path, allowed_roots: Sequence[str | Path] | None = None):
        self.base = base
        configured = allowed_roots if allowed_roots is not None else (base,)
        roots: list[Path] = []
        for value in configured:
            raw = Path(value).expanduser()
            if raw.is_symlink():
                raise ValidationError(f"evidence root must not be a symlink: {raw}")
            root = raw.resolve()
            if not root.is_dir():
                raise ValidationError(f"evidence root is not a directory: {root}")
            roots.append(root)
        if not roots:
            raise ValidationError("at least one evidence root is required")
        self.allowed_roots = tuple(roots)
        self._hashes: dict[Path, str] = {}

    def resolve(self, value: Any, field: str) -> Path:
        text = require_nonempty_string(value, field)
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.base / candidate
        if candidate.is_symlink():
            raise ValidationError(f"{field} must not be a symlink")
        path = candidate.resolve()
        if not any(path.is_relative_to(root) for root in self.allowed_roots):
            raise ValidationError(f"{field} is outside the configured evidence roots")
        return path

    def sha256(self, path: Path) -> str:
        if path not in self._hashes:
            if not path.is_file():
                raise ValidationError(f"evidence file does not exist: {path}")
            if path.stat().st_size < 1:
                raise ValidationError(f"evidence file is empty: {path}")
            self._hashes[path] = _sha256_file(path)
        return self._hashes[path]

    def descriptor(self, value: Any, field: str) -> tuple[Path, str]:
        if not isinstance(value, Mapping):
            raise ValidationError(f"{field} must be a descriptor object")
        path = self.resolve(value.get("path"), f"{field}.path")
        expected = require_nonempty_string(value.get("sha256"), f"{field}.sha256")
        actual = self.sha256(path)
        if expected != actual:
            raise ValidationError(f"{field} checksum does not match bytes")
        return path, actual


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValidationError(f"throughput database does not exist: {path}")
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValidationError(f"cannot open throughput database read-only: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise ValidationError(
                f"database schema version {version} does not match required {SCHEMA_VERSION}"
            )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, required in REQUIRED_TABLE_COLUMNS.items():
            if table not in tables:
                raise ValidationError(f"database is missing required table {table}")
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            missing = sorted(required - columns)
            if missing:
                raise ValidationError(
                    f"database table {table} is missing columns: {', '.join(missing)}"
                )
    except Exception:
        connection.close()
        raise
    return connection


def _issue(
    errors: list[dict[str, Any]],
    code: str,
    message: str,
    **context: Any,
) -> None:
    errors.append({"code": code, "message": message, **context})


def _select_batch(
    connection: sqlite3.Connection, requested: str | None
) -> str | None:
    if requested is not None:
        batch = require_nonempty_string(requested, "batch_id")
        found = connection.execute(
            "SELECT 1 FROM jobs WHERE batch_id = ? LIMIT 1", (batch,)
        ).fetchone()
        return batch if found is not None else None
    row = connection.execute(
        """
        SELECT batch_id, MAX(created_at) AS latest
        FROM jobs GROUP BY batch_id
        ORDER BY latest DESC, batch_id DESC LIMIT 1
        """
    ).fetchone()
    return str(row["batch_id"]) if row is not None else None


def _task_chain(
    rows: Sequence[Mapping[str, Any]], expected_roles: Sequence[str]
) -> tuple[list[Mapping[str, Any]], list[str]]:
    errors: list[str] = []
    if len(rows) != len(expected_roles):
        errors.append(
            f"task count {len(rows)} does not match registry chain {len(expected_roles)}"
        )
    by_id = {str(row["id"]): row for row in rows}
    roots = [row for row in rows if row["dependency_task_id"] is None]
    if len(roots) != 1:
        errors.append(f"registry DAG must have exactly one root, found {len(roots)}")
        return [], errors
    children: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        dependency = row["dependency_task_id"]
        if dependency is not None:
            if str(dependency) not in by_id:
                errors.append(f"task {row['id']} depends on an unknown task")
            children[str(dependency)].append(row)
    ordered: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    current: Mapping[str, Any] | None = roots[0]
    while current is not None:
        task_id = str(current["id"])
        if task_id in seen:
            errors.append("registry DAG contains a cycle")
            break
        seen.add(task_id)
        ordered.append(current)
        next_rows = children.get(task_id, [])
        if len(next_rows) > 1:
            errors.append(f"task {task_id} has {len(next_rows)} downstream branches")
            break
        current = next_rows[0] if next_rows else None
    if len(seen) != len(rows):
        errors.append("registry DAG is disconnected or contains extra task branches")
    actual_roles = [str(row["role"]) for row in ordered]
    if actual_roles != list(expected_roles):
        errors.append(
            "role order differs from registry: "
            + canonical_json({"expected": list(expected_roles), "actual": actual_roles})
        )
    return ordered, errors


def _simulation_marker(task: Mapping[str, Any]) -> bool:
    keys = set(task.keys())
    kind = str(task["kind"] if "kind" in keys and task["kind"] is not None else "")
    if kind == "shadow_soak" or kind.startswith("simulation."):
        return True
    for field in ("payload_json", "result_json"):
        value = task[field] if field in keys else None
        if not isinstance(value, str) or not value:
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping) and (
            parsed.get("simulation_run") is not None
            or parsed.get("simulated") is True
            or parsed.get("shadow") is True
        ):
            return True
    return False


def _verify_preview_binding(
    *,
    job_id: str,
    lane: str,
    tasks_by_role: Mapping[str, Mapping[str, Any]],
    files: _FileVerifier,
) -> dict[str, str]:
    _compiler_result, project = _artifact_result(
        tasks_by_role["compiler"], "project_manifest"
    )
    _preview_result, approval = _artifact_result(
        tasks_by_role["preview_review"], "preview_approval"
    )
    preview_payload = _json_object(
        tasks_by_role["preview_review"]["payload_json"],
        "preview_review.payload_json",
    )
    if (
        preview_payload.get("human_gate") is not True
        or preview_payload.get("checksum_bound") is not True
    ):
        raise ValidationError("preview approval task is not a checksum-bound human gate")
    project_sha = _artifact_sha256(project)
    approval_sha = _artifact_sha256(approval)
    if (
        project["job_id"] != job_id
        or project["lane_id"] != lane
        or approval["job_id"] != job_id
        or approval["project_id"] != project["project_id"]
        or approval["project_tree_sha256"] != project["project_tree_sha256"]
        or approval["project_manifest_sha256"] != project_sha
    ):
        raise ValidationError("preview approval is stale or cross-job")
    receipt_path = files.resolve(
        approval["check_receipt_path"], "preview_approval.check_receipt_path"
    )
    if files.sha256(receipt_path) != approval["check_receipt_sha256"]:
        raise ValidationError("preview check receipt checksum does not match bytes")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("preview check receipt is unreadable") from exc
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("ok") is not True
        or receipt.get("project_tree_sha256") != project["project_tree_sha256"]
    ):
        raise ValidationError("preview check receipt is not bound to the approved project")
    return {
        "project_manifest_sha256": project_sha,
        "preview_approval_sha256": approval_sha,
    }


def _verify_qc_evidence(
    *,
    job_id: str,
    lane: str,
    tasks_by_role: Mapping[str, Mapping[str, Any]],
    files: _FileVerifier,
) -> dict[str, Any]:
    preview_binding = _verify_preview_binding(
        job_id=job_id,
        lane=lane,
        tasks_by_role=tasks_by_role,
        files=files,
    )
    render_result, render = _artifact_result(tasks_by_role["render"], "render_manifest")
    if render["job_id"] != job_id:
        raise ValidationError("render_manifest job_id differs from queue job")
    output = files.resolve(render_result.get("output_path"), "render.output_path")
    output_sha = files.sha256(output)
    if output_sha != render["output_sha256"]:
        raise ValidationError("render master checksum differs from RenderManifest")
    if Path(render["output"]).name != output.name:
        raise ValidationError("render output path differs from RenderManifest.output")
    render_inputs: dict[str, list[str]] = defaultdict(list)
    for row in render["input_hashes"]:
        render_inputs[Path(row["path"]).name].append(row["sha256"])
    for filename, expected in (
        ("project_manifest.json", preview_binding["project_manifest_sha256"]),
        ("preview_approval.json", preview_binding["preview_approval_sha256"]),
    ):
        if render_inputs.get(filename) != [expected]:
            raise ValidationError(
                f"RenderManifest is not bound to the exact {filename} artifact"
            )

    caption_result, caption = _artifact_result(
        tasks_by_role["caption_transcript"], "caption_transcript_manifest"
    )
    if (
        caption["job_id"] != job_id
        or caption["lane_id"] != lane
        or caption["render_id"] != render["render_id"]
        or caption["render_sha256"] != output_sha
    ):
        raise ValidationError("caption transcript is stale or cross-job")
    files.descriptor(caption["evidence"], "caption_transcript.evidence")
    if caption_result.get("evidence") not in (None, caption["evidence"]):
        raise ValidationError("caption result evidence differs from its manifest")

    auto_result, auto = _artifact_result(
        tasks_by_role["qc_auto_evidence"], "qc_auto_evidence_manifest"
    )
    if (
        auto["job_id"] != job_id
        or auto["lane_id"] != lane
        or auto["render_id"] != render["render_id"]
        or auto["render_sha256"] != output_sha
    ):
        raise ValidationError("automatic QC evidence is stale or cross-job")
    auto_reports: dict[str, dict[str, Any]] = {}
    for report in auto["reports"]:
        validate_artifact("qc_analyzer_report", report)
        category = report["category"]
        if category in auto_reports:
            raise ValidationError("automatic QC contains duplicate categories")
        auto_reports[category] = report
    if set(auto_reports) != AUTO_QC_CATEGORIES:
        raise ValidationError("automatic QC does not contain technical/audio/rights")

    report_artifacts: dict[str, dict[str, Any]] = dict(auto_reports)
    evidence: dict[str, Mapping[str, Any]] = {}
    for category in AUTO_QC_CATEGORIES:
        descriptor = auto["evidence"][category]
        files.descriptor(descriptor, f"qc_auto_evidence.{category}")
        evidence[category] = descriptor
    if auto_result.get("evidence") not in (None, auto.get("evidence")):
        raise ValidationError("automatic QC result evidence differs from manifest")

    visual_contact: Mapping[str, Any] | None = None
    for category, role in ANALYZER_ROLES.items():
        analyzer_result, analyzer = _artifact_result(
            tasks_by_role[role], "qc_analyzer_report"
        )
        if (
            analyzer["category"] != category
            or analyzer["job_id"] != job_id
            or analyzer["lane_id"] != lane
            or analyzer["render_id"] != render["render_id"]
            or analyzer["render_sha256"] != output_sha
            or analyzer["status"] != "pass"
            or analyzer["needs_human_review"] is not False
        ):
            raise ValidationError(f"{category} analyzer is not an exact passing binding")
        descriptor = analyzer_result.get("evidence")
        files.descriptor(descriptor, f"{role}.evidence")
        evidence[category] = descriptor
        report_artifacts[category] = analyzer
        if category == "visual":
            visual_contact = analyzer_result.get("contact_sheet")
            files.descriptor(visual_contact, "visual_analyzer.contact_sheet")

    bundle_result, bundle = _artifact_result(
        tasks_by_role["qc_evidence_gate"], "qc_evidence_bundle"
    )
    if (
        bundle["job_id"] != job_id
        or bundle["lane_id"] != lane
        or bundle["render_id"] != render["render_id"]
        or bundle["render_sha256"] != output_sha
    ):
        raise ValidationError("QC evidence bundle is stale or cross-job")
    decision = bundle["decision"]
    if (
        decision["passed"] is not True
        or decision["needs_human_review"] is not False
        or decision["blocking_categories"]
    ):
        raise ValidationError("QC evidence bundle has not passed")
    bundle_reports = {row["category"]: row for row in bundle["reports"]}
    if set(bundle_reports) != set(QC_CATEGORIES):
        raise ValidationError("QC evidence bundle does not contain eight exact categories")
    for category in QC_CATEGORIES:
        row = bundle_reports[category]
        if row["artifact_sha256"] != _artifact_sha256(report_artifacts[category]):
            raise ValidationError(f"QC bundle {category} artifact checksum is stale")
        if row["evidence"] != evidence[category]:
            raise ValidationError(f"QC bundle {category} evidence descriptor is stale")
        files.descriptor(row["evidence"], f"qc_bundle.{category}")
    files.descriptor(bundle["contact_sheet"], "qc_bundle.contact_sheet")
    if visual_contact != bundle["contact_sheet"]:
        raise ValidationError("QC bundle contact sheet differs from visual analyzer")
    if bundle_result.keys() - {"artifact"}:
        # Extra deterministic diagnostics are allowed by handlers, but the
        # artifact remains the only authority.  Keep this branch explicit for
        # audit readability without rejecting harmless metadata.
        pass

    qc_result, qc = _artifact_result(tasks_by_role["qc"], "qc_report")
    if qc["job_id"] != job_id or qc["render_id"] != render["render_id"]:
        raise ValidationError("QC report is stale or cross-job")
    qc_decision = qc["decision"]
    if (
        qc_decision["passed"] is not True
        or qc_decision["needs_human_review"] is not False
        or qc_decision["blocking_check_ids"]
    ):
        raise ValidationError("QC report has not passed")
    checks = {row["category"]: row for row in qc["checks"]}
    if set(checks) != set(QC_CATEGORIES):
        raise ValidationError("QC report does not contain eight exact categories")
    expected_evidence_hashes: dict[str, str] = {}
    for category in QC_CATEGORIES:
        descriptor = bundle_reports[category]["evidence"]
        descriptor_path = files.resolve(descriptor["path"], f"qc_bundle.{category}.path")
        check = checks[category]
        if check["status"] != "pass":
            raise ValidationError(f"QC report category {category} is not pass")
        if files.resolve(check.get("artifact"), f"qc.{category}.artifact") != descriptor_path:
            raise ValidationError(f"QC report category {category} points to different evidence")
        if f"#sha256={descriptor['sha256']}" not in check["evidence"]:
            raise ValidationError(f"QC report category {category} lacks checksum binding")
        expected_evidence_hashes[category] = descriptor["sha256"]
    if qc_result.get("evidence_sha256") != expected_evidence_hashes:
        raise ValidationError("QC result evidence hash map differs from evidence bundle")
    if qc_result.get("visual_contact_sheet_sha256") != bundle["contact_sheet"]["sha256"]:
        raise ValidationError("QC result contact sheet checksum differs from evidence bundle")
    qc_output = files.resolve(qc_result.get("render_output_path"), "qc.render_output_path")
    if qc_output != output or files.sha256(qc_output) != output_sha:
        raise ValidationError("QC result points to different render bytes")

    return {
        "render_id": render["render_id"],
        "master_path": str(output),
        "master_sha256": output_sha,
        "duration_seconds": float(render["technical"]["duration_seconds"]),
        **preview_binding,
        "qc_report_sha256": _artifact_sha256(qc),
        "qc_evidence_bundle_sha256": _artifact_sha256(bundle),
        "evidence_categories": list(QC_CATEGORIES),
    }


def evaluate_throughput_acceptance(
    *,
    db_path: str | Path,
    target: int,
    deadline_hours: float,
    batch_id: str | None = None,
    registry_path: str | Path | None = None,
    safety_margin: float = 0.20,
    gpu_heavy_slots: int = 1,
    allowed_evidence_roots: Sequence[str | Path] | None = None,
    as_of: str | datetime | None = None,
) -> dict[str, Any]:
    """Evaluate one completed batch without mutating queue or external state."""

    if not _is_int(target) or not 10 <= target <= 15:
        raise ValidationError("target must be an integer from 10 to 15")
    deadline = _number(deadline_hours, "deadline_hours", minimum=0.001, maximum=168)
    margin = _number(safety_margin, "safety_margin", minimum=0, maximum=0.90)
    if not _is_int(gpu_heavy_slots) or not 1 <= gpu_heavy_slots <= 16:
        raise ValidationError("gpu_heavy_slots must be an integer from 1 to 16")
    evaluated_at = _timestamp(as_of, "as_of")
    registry = load_lane_registry(registry_path)
    expected_distribution = expected_lane_distribution(registry, target)
    enabled_lanes = tuple(expected_distribution)
    database = Path(db_path).expanduser().resolve()
    files = _FileVerifier(database.parent, allowed_evidence_roots)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    jobs_report: list[dict[str, Any]] = []
    handler_observations: list[tuple[str, str, float]] = []
    dwell_observations: list[tuple[str, str, float]] = []
    heavy_durations: list[float] = []
    batch_start_values: list[datetime] = []
    batch_end_values: list[datetime] = []
    resolved_retry_attempts = 0
    expired_lease_count = 0
    open_dead_count = 0
    final_review_statuses: Counter[str] = Counter()
    publisher_statuses: Counter[str] = Counter()

    with closing(_open_read_only(database)) as connection:
        selected_batch = _select_batch(connection, batch_id)
        if selected_batch is None:
            _issue(
                errors,
                "batch_not_found",
                "requested batch does not exist" if batch_id else "database contains no jobs",
                batch_id=batch_id,
            )
            return {
                "schema_version": REPORT_VERSION,
                "ok": False,
                "accepted": False,
                "command": "throughput-acceptance",
                "read_only": True,
                "production_ready": False,
                "throughput_accepted": False,
                "acceptance_scope": (
                    "one checksum-bound batch throughput result; not global production "
                    "readiness, final review, or publication approval"
                ),
                "database": str(database),
                "allowed_evidence_roots": [
                    str(root) for root in files.allowed_roots
                ],
                "batch_id": batch_id,
                "target": target,
                "deadline_hours": deadline,
                "safety_margin": margin,
                "errors": errors,
                "warnings": warnings,
                "evaluated_at": _iso(evaluated_at),
            }

        job_rows = connection.execute(
            """
            SELECT jobs.*, ideas.payload_json AS idea_payload_json
            FROM jobs JOIN ideas ON ideas.id = jobs.idea_id
            WHERE jobs.batch_id = ? ORDER BY jobs.id
            """,
            (selected_batch,),
        ).fetchall()
        if len(job_rows) != target:
            _issue(
                errors,
                "batch_target_mismatch",
                f"batch contains {len(job_rows)} jobs; exactly {target} are required",
            )
        job_ids = [str(row["id"]) for row in job_rows]
        if not job_ids:
            _issue(errors, "empty_batch", "selected batch has no jobs")
            task_rows: list[sqlite3.Row] = []
            attempt_rows: list[sqlite3.Row] = []
            dead_rows: list[sqlite3.Row] = []
        else:
            placeholders = ",".join("?" for _ in job_ids)
            task_rows = connection.execute(
                f"SELECT * FROM tasks WHERE job_id IN ({placeholders}) ORDER BY created_at, id",
                job_ids,
            ).fetchall()
            task_ids = [str(row["id"]) for row in task_rows]
            if task_ids:
                task_placeholders = ",".join("?" for _ in task_ids)
                attempt_rows = connection.execute(
                    f"SELECT * FROM task_attempts WHERE task_id IN ({task_placeholders}) "
                    "ORDER BY task_id, attempt_no",
                    task_ids,
                ).fetchall()
                dead_rows = connection.execute(
                    f"SELECT * FROM dead_letters WHERE task_id IN ({task_placeholders}) "
                    "ORDER BY task_id, cycle_no",
                    task_ids,
                ).fetchall()
            else:
                attempt_rows = []
                dead_rows = []

        tasks_by_job: dict[str, list[sqlite3.Row]] = defaultdict(list)
        tasks_by_id: dict[str, sqlite3.Row] = {}
        for row in task_rows:
            tasks_by_job[str(row["job_id"])].append(row)
            tasks_by_id[str(row["id"])] = row
        attempts_by_task: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in attempt_rows:
            attempts_by_task[str(row["task_id"])].append(row)
        open_dead_by_task: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in dead_rows:
            if row["status"] == "open":
                open_dead_by_task[str(row["task_id"])].append(row)
                open_dead_count += 1
        if open_dead_count:
            _issue(
                errors,
                "open_dead_letters",
                f"batch contains {open_dead_count} unresolved dead-letter records",
            )

        actual_distribution: Counter[str] = Counter()
        qc_passed = 0
        for job in job_rows:
            job_id = str(job["id"])
            rows = tasks_by_job.get(job_id, [])
            lane_values = {str(row["pod"]) for row in rows}
            lane = next(iter(lane_values)) if len(lane_values) == 1 else "unknown"
            job_errors_before = len(errors)
            if len(lane_values) != 1 or lane not in enabled_lanes:
                _issue(
                    errors,
                    "invalid_job_lane",
                    "job tasks must use exactly one enabled registry lane",
                    job_id=job_id,
                    lanes=sorted(lane_values),
                )
                expected_roles: tuple[str, ...] = ()
            else:
                actual_distribution[lane] += 1
                expected_roles = roles_for_lane(lane, registry=registry)
            ordered, chain_errors = _task_chain(rows, expected_roles)
            for message in chain_errors:
                _issue(errors, "registry_dag_mismatch", message, job_id=job_id, lane=lane)
            if any(_simulation_marker(row) for row in rows):
                _issue(
                    errors,
                    "simulation_evidence_rejected",
                    "simulation/shadow tasks cannot satisfy production throughput acceptance",
                    job_id=job_id,
                    lane=lane,
                )
            role_map = {str(row["role"]): row for row in ordered}
            qc_index = expected_roles.index("qc") if "qc" in expected_roles else -1
            for index, task in enumerate(ordered):
                task_id = str(task["id"])
                role = str(task["role"])
                status = str(task["status"])
                attempts = attempts_by_task.get(task_id, [])
                if task_id in open_dead_by_task:
                    _issue(
                        errors,
                        "task_open_dead_letter",
                        "task has an unresolved dead letter",
                        job_id=job_id,
                        role=role,
                        task_id=task_id,
                    )
                if status == "dead":
                    _issue(
                        errors,
                        "dead_task",
                        "batch contains a terminal dead task",
                        job_id=job_id,
                        role=role,
                        task_id=task_id,
                    )
                if status == "leased":
                    expiry = _timestamp(task["lease_expires_at"], "lease_expires_at")
                    if expiry <= evaluated_at:
                        expired_lease_count += 1
                    _issue(
                        errors,
                        "active_or_expired_lease",
                        "throughput batch still contains a leased task",
                        job_id=job_id,
                        role=role,
                        expired=expiry <= evaluated_at,
                    )
                required_succeeded = 0 <= index <= qc_index
                if required_succeeded and status != "succeeded":
                    _issue(
                        errors,
                        "incomplete_production_stage",
                        "every registry stage through QC must be succeeded",
                        job_id=job_id,
                        role=role,
                        status=status,
                    )
                if role == "publisher":
                    publisher_statuses[status] += 1
                    if status != "queued" or attempts or task["result_json"] is not None:
                        _issue(
                            errors,
                            "publisher_invoked",
                            "publisher must remain queued, unattempted, and result-free",
                            job_id=job_id,
                        )
                    continue
                if role == "final_review":
                    final_review_statuses[status] += 1
                    if status != "queued":
                        _issue(
                            errors,
                            "final_review_already_completed",
                            "throughput gate requires final_review to remain human-pending",
                            job_id=job_id,
                            status=status,
                        )
                    if status == "queued" and (
                        attempts or task["result_json"] is not None
                    ):
                        _issue(
                            errors,
                            "final_review_not_pristine",
                            "queued final_review must have no attempts or result",
                            job_id=job_id,
                        )
                    if status == "queued":
                        continue
                if status != "succeeded":
                    continue
                if int(task["attempt_count"]) != len(attempts):
                    _issue(
                        errors,
                        "attempt_count_mismatch",
                        "task attempt_count differs from durable attempts",
                        job_id=job_id,
                        role=role,
                    )
                succeeded_attempts = [row for row in attempts if row["status"] == "succeeded"]
                if len(succeeded_attempts) != 1:
                    _issue(
                        errors,
                        "successful_attempt_missing",
                        "succeeded task must have exactly one succeeded attempt",
                        job_id=job_id,
                        role=role,
                        succeeded_attempts=len(succeeded_attempts),
                    )
                    continue
                unresolved_attempts = [row for row in attempts if row["status"] == "leased"]
                if unresolved_attempts:
                    _issue(
                        errors,
                        "unresolved_attempt",
                        "succeeded task still has a leased attempt",
                        job_id=job_id,
                        role=role,
                    )
                resolved_retry_attempts += sum(
                    row["status"] in {"failed", "expired"} for row in attempts
                )
                attempt = succeeded_attempts[0]
                claimed = _timestamp(attempt["claimed_at"], "attempt.claimed_at")
                finished = _timestamp(attempt["finished_at"], "attempt.finished_at")
                if finished < claimed or finished > evaluated_at:
                    _issue(
                        errors,
                        "invalid_attempt_timing",
                        "attempt timing is negative or after evaluation time",
                        job_id=job_id,
                        role=role,
                    )
                    continue
                duration = (finished - claimed).total_seconds()
                dependency = tasks_by_id.get(str(task["dependency_task_id"]))
                eligible = max(
                    _timestamp(task["created_at"], "task.created_at"),
                    _timestamp(task["available_at"], "task.available_at"),
                )
                if dependency is not None and dependency["completed_at"] is not None:
                    eligible = max(
                        eligible,
                        _timestamp(dependency["completed_at"], "dependency.completed_at"),
                    )
                dwell = (claimed - eligible).total_seconds()
                if dwell < -0.001:
                    _issue(
                        errors,
                        "negative_queue_dwell",
                        "task was claimed before it became dependency-eligible",
                        job_id=job_id,
                        role=role,
                    )
                else:
                    dwell_observations.append((role, lane, max(0.0, dwell)))
                handler_observations.append((role, lane, duration))
                if default_resource_lock_path(role) is not None:
                    heavy_durations.append(duration)

            if rows:
                batch_start_values.extend(
                    _timestamp(row["created_at"], "task.created_at") for row in rows
                )
            qc_task = role_map.get("qc")
            qc_completed = None
            if qc_task is not None and qc_task["completed_at"] is not None:
                qc_completed = _timestamp(qc_task["completed_at"], "qc.completed_at")
                batch_end_values.append(qc_completed)
            artifact_summary: dict[str, Any] | None = None
            if all(
                role in role_map
                for role in (
                    "compiler",
                    "preview_review",
                    "render",
                    "qc_auto_evidence",
                    "caption_transcript",
                    *ANALYZER_ROLES.values(),
                    "qc_evidence_gate",
                    "qc",
                )
            ):
                try:
                    artifact_summary = _verify_qc_evidence(
                        job_id=job_id,
                        lane=lane,
                        tasks_by_role=role_map,
                        files=files,
                    )
                except (ValidationError, OSError) as exc:
                    _issue(
                        errors,
                        "production_evidence_invalid",
                        str(exc),
                        job_id=job_id,
                        lane=lane,
                    )
            else:
                _issue(
                    errors,
                    "qc_evidence_stage_missing",
                    "job lacks one or more render/QC evidence stages",
                    job_id=job_id,
                    lane=lane,
                )
            job_accepted = len(errors) == job_errors_before and artifact_summary is not None
            if job_accepted:
                qc_passed += 1
            jobs_report.append(
                {
                    "job_id": job_id,
                    "lane": lane,
                    "registry_roles": len(expected_roles),
                    "task_count": len(rows),
                    "qc_completed_at": _iso(qc_completed) if qc_completed else None,
                    "qc_evidence_valid": artifact_summary is not None,
                    "accepted_master": job_accepted,
                    "artifact": artifact_summary,
                }
            )

        actual_distribution_dict = {
            lane: actual_distribution.get(lane, 0) for lane in enabled_lanes
        }
        if actual_distribution_dict != expected_distribution:
            _issue(
                errors,
                "lane_distribution_mismatch",
                "batch lane allocation differs from registry target distribution",
                expected=expected_distribution,
                actual=actual_distribution_dict,
            )
        if qc_passed < target:
            _issue(
                errors,
                "qc_master_shortfall",
                f"only {qc_passed} checksum-verified QC masters passed; {target} required",
            )

    batch_start = min(batch_start_values) if batch_start_values else None
    batch_end = max(batch_end_values) if batch_end_values else None
    wall_clock_seconds = (
        (batch_end - batch_start).total_seconds()
        if batch_start is not None and batch_end is not None
        else None
    )
    deadline_seconds = deadline * 3600
    usable_seconds = deadline_seconds * (1 - margin)
    if wall_clock_seconds is None or wall_clock_seconds < 0:
        _issue(errors, "batch_timing_missing", "batch start/QC completion timing is missing")
    elif wall_clock_seconds > usable_seconds:
        _issue(
            errors,
            "deadline_or_margin_exceeded",
            "batch completion exceeds deadline after reserved safety margin",
            observed_seconds=round(wall_clock_seconds, 3),
            usable_seconds=round(usable_seconds, 3),
        )
    heavy_busy_seconds = sum(heavy_durations)
    if qc_passed and heavy_busy_seconds <= 0:
        _issue(
            errors,
            "gpu_heavy_timing_missing",
            "QC-passed batch has no measured shared gpu-heavy handler time",
        )
    heavy_capacity_seconds = usable_seconds * gpu_heavy_slots
    if heavy_busy_seconds > heavy_capacity_seconds:
        _issue(
            errors,
            "gpu_heavy_capacity_shortfall",
            "shared gpu-heavy work exceeds deadline capacity with safety margin",
            busy_seconds=round(heavy_busy_seconds, 3),
            capacity_seconds=round(heavy_capacity_seconds, 3),
        )
    observed_heavy_utilization = (
        heavy_busy_seconds / (wall_clock_seconds * gpu_heavy_slots)
        if wall_clock_seconds and wall_clock_seconds > 0
        else None
    )
    if observed_heavy_utilization is not None and observed_heavy_utilization > 1.001:
        _issue(
            errors,
            "gpu_heavy_overlap_inconsistent",
            "observed shared-role durations exceed configured slot wall time",
            utilization=round(observed_heavy_utilization, 6),
        )

    accepted = not errors and qc_passed >= target
    return {
        "schema_version": REPORT_VERSION,
        "ok": accepted,
        "accepted": accepted,
        "command": "throughput-acceptance",
        "read_only": True,
        "production_ready": False,
        "throughput_accepted": accepted,
        "acceptance_scope": (
            "one checksum-bound batch throughput result; not global production "
            "readiness, final review, or publication approval"
        ),
        "evidence_level": "real_queue_artifacts_and_bytes",
        "database": str(database),
        "allowed_evidence_roots": [str(root) for root in files.allowed_roots],
        "database_schema_version": SCHEMA_VERSION,
        "batch_id": selected_batch,
        "registry_version": registry["registry_version"],
        "target": target,
        "deadline_hours": deadline,
        "safety_margin": margin,
        "gpu_heavy_slots": gpu_heavy_slots,
        "expected_lane_distribution": expected_distribution,
        "actual_lane_distribution": actual_distribution_dict,
        "counts": {
            "jobs": len(jobs_report),
            "qc_passed_masters": qc_passed,
            "open_dead_letters": open_dead_count,
            "expired_leases": expired_lease_count,
            "resolved_retry_attempts": resolved_retry_attempts,
        },
        "timing": {
            "batch_started_at": _iso(batch_start) if batch_start else None,
            "batch_qc_completed_at": _iso(batch_end) if batch_end else None,
            "batch_wall_clock_seconds": (
                round(wall_clock_seconds, 3) if wall_clock_seconds is not None else None
            ),
            "deadline_seconds": round(deadline_seconds, 3),
            "usable_seconds_after_margin": round(usable_seconds, 3),
            "queue_dwell": _grouped_metrics(dwell_observations),
            "handler_duration": _grouped_metrics(handler_observations),
        },
        "gpu_heavy": {
            "roles": sorted(
                role
                for role in {role for lane in registry["lanes"] for role in lane["roles"]}
                if default_resource_lock_path(role) is not None
            ),
            "successful_attempts": len(heavy_durations),
            "busy_seconds": round(heavy_busy_seconds, 3),
            "usable_capacity_seconds": round(heavy_capacity_seconds, 3),
            "observed_utilization_proxy": (
                round(observed_heavy_utilization, 6)
                if observed_heavy_utilization is not None
                else None
            ),
            "deadline_utilization_proxy": round(
                heavy_busy_seconds / (deadline_seconds * gpu_heavy_slots), 6
            ),
        },
        "human_gates": {
            "preview_review_required_before_render": True,
            "gate_performed_actions": False,
            "observed_final_review_status_counts": dict(sorted(final_review_statuses.items())),
            "observed_publisher_status_counts": dict(sorted(publisher_statuses.items())),
        },
        "jobs": jobs_report,
        "errors": errors,
        "warnings": warnings,
        "evaluated_at": _iso(evaluated_at),
    }


__all__ = [
    "REPORT_VERSION",
    "evaluate_throughput_acceptance",
    "expected_lane_distribution",
]
