"""Shared fail-closed primitives for deterministic semantic QC analyzers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from .contracts import validate_artifact
from .errors import FactoryError, ValidationError
from .validators import canonical_json, digest_text, require_nonempty_string


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def artifact_sha256(value: Mapping[str, Any]) -> str:
    return digest_text(canonical_json(dict(value)))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ValidationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def safe_id(value: Any, field: str) -> str:
    normalized = require_nonempty_string(value, field)
    if not _SAFE_ID.fullmatch(normalized) or ".." in normalized:
        raise ValidationError(f"{field} contains unsafe characters")
    return normalized


def require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValidationError(f"{field} must be lowercase SHA-256")
    return value


def parse_completed_at(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def upstream_artifact(
    task: Mapping[str, Any],
    *,
    roles: Sequence[str],
    contract: str,
    required: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    upstream = task.get("upstream_results")
    if not isinstance(upstream, list):
        raise ValidationError("task.upstream_results must be an array")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in upstream:
        if not isinstance(entry, Mapping) or entry.get("role") not in roles:
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(result, dict) and isinstance(artifact, dict):
            matches.append((result, artifact))
    if len(matches) != (1 if required else 0) and not (not required and len(matches) == 1):
        qualifier = "exactly one" if required else "at most one"
        raise ValidationError(
            f"task requires {qualifier} upstream {contract} from role(s) "
            + ", ".join(roles)
        )
    if not matches:
        return None
    validate_artifact(contract, matches[0][1])
    return matches[0]


def common_task_context(
    task: Mapping[str, Any], expected_role: str
) -> tuple[dict[str, Any], str, str]:
    if task.get("role") != expected_role:
        raise ValidationError(f"handler accepts only role={expected_role!r}")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "qc_analyzer_report":
        raise ValidationError(
            "task payload must require result contract 'qc_analyzer_report'"
        )
    job_id = safe_id(task.get("job_id") or payload.get("job_id"), "task.job_id")
    if payload.get("job_id") != job_id:
        raise ValidationError("payload.job_id is not bound to task.job_id")
    lane_id = require_nonempty_string(payload.get("lane_id"), "payload.lane_id")
    if task.get("pod") != lane_id:
        raise ValidationError("payload.lane_id is not bound to task.pod")
    return dict(payload), job_id, lane_id


def validate_master(
    render_result: Mapping[str, Any], render_manifest: Mapping[str, Any], job_id: str
) -> tuple[Path, str, str]:
    if render_manifest.get("job_id") != job_id:
        raise ValidationError("render_manifest.job_id does not match task.job_id")
    output_value = render_result.get("output_path")
    if not isinstance(output_value, str) or not output_value.strip():
        raise ValidationError("render result requires output_path")
    output = Path(output_value).expanduser().resolve()
    if not output.is_file():
        raise ValidationError(f"render master does not exist: {output}")
    actual = file_sha256(output)
    expected = require_sha256(
        render_manifest.get("output_sha256"), "render_manifest.output_sha256"
    )
    if actual != expected:
        raise ValidationError("render master checksum does not match render_manifest")
    render_id = safe_id(render_manifest.get("render_id"), "render_manifest.render_id")
    return output, actual, render_id


def emit_main(
    handler: Any, stdin: TextIO | None = None, stdout: TextIO | None = None
) -> int:
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    try:
        task = json.load(source)
        if not isinstance(task, dict):
            raise ValidationError("handler stdin must contain one JSON object")
        result = handler(task)
    except (FactoryError, json.JSONDecodeError, OSError) as exc:
        print(
            json.dumps(
                {"error": exc.__class__.__name__, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    json.dump(result, target, ensure_ascii=False, sort_keys=True)
    target.write("\n")
    return 0


def persist_report(report: Mapping[str, Any]) -> dict[str, str]:
    """Persist canonical analyzer evidence under the configured immutable root."""

    raw_root = os.environ.get("VIDEO_FACTORY_QC_EVIDENCE_ROOT")
    if not raw_root:
        raise ValidationError("VIDEO_FACTORY_QC_EVIDENCE_ROOT must be configured")
    root = Path(raw_root).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValidationError(f"cannot create QC evidence root {root}: {exc}") from exc
    job_id = safe_id(report.get("job_id"), "report.job_id")
    render_id = safe_id(report.get("render_id"), "report.render_id")
    category = safe_id(report.get("category"), "report.category")
    directory = root / job_id / render_id
    path = directory / f"{category}.json"
    temporary = directory / f".{category}.json.tmp"
    body = canonical_json(dict(report)) + "\n"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise ValidationError(f"cannot persist analyzer evidence {path}: {exc}") from exc
    return {"path": str(path), "sha256": file_sha256(path)}
