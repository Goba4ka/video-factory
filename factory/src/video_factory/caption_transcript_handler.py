"""Trusted runtime adapter for word-level caption transcript evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from .caption_analyzer import (
    _artifact_sha256,
    _configured_evidence_root,
    _master_path,
    _parse_datetime,
    _safe_id,
    _sha256_file,
    _upstream,
    _validate_transcript,
)
from .contracts import validate_artifact
from .errors import FactoryError, ValidationError
from .validators import canonical_json, digest_text, require_nonempty_string


_LANES = frozenset(
    {"war_history", "celebrity_news", "motivation", "chinese_medicine", "health"}
)
_OBSERVER_FIELDS = frozenset(
    {
        "status",
        "warnings",
        "language",
        "duration_seconds",
        "engine",
        "completed_at",
        "words",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "lane_id",
        "render_id",
        "render_sha256",
        "status",
        "warnings",
        "observer",
        "evidence",
        "word_count",
        "created_at",
    }
)


def _observer_executable() -> Path:
    raw = os.environ.get("VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE")
    if not raw:
        raise ValidationError("VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE must be configured")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValidationError("VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE must be absolute")
    if candidate.is_symlink():
        raise ValidationError("VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE must not be a symlink")
    executable = candidate.resolve()
    if not executable.is_file():
        raise ValidationError("VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE must be a regular file")
    return executable


def _observer_timeout() -> int:
    raw = os.environ.get("VIDEO_FACTORY_CAPTION_OBSERVER_TIMEOUT_SECONDS", "600")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationError("VIDEO_FACTORY_CAPTION_OBSERVER_TIMEOUT_SECONDS must be an integer") from exc
    if value < 1 or value > 1800:
        raise ValidationError("VIDEO_FACTORY_CAPTION_OBSERVER_TIMEOUT_SECONDS must be 1..1800")
    return value


def _run_observer(executable: Path, request: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(executable)],
            input=canonical_json(dict(request)) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ValidationError(f"caption observer execution failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:500]
        raise ValidationError(
            f"caption observer exited with {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"caption observer stdout is not JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise ValidationError("caption observer stdout must contain one JSON object")
    return result


def _validate_observer_measurement(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _OBSERVER_FIELDS:
        raise ValidationError("caption observer must return word-level measurement fields")
    return value


def _write_evidence(root: Path, job_id: str, run_id: str, evidence: Mapping[str, Any]) -> Path:
    directory = root / job_id
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValidationError("caption transcript directory must not be a symlink")
    path = directory / f"caption-transcript-{run_id}.json"
    if path.is_symlink():
        raise ValidationError("caption transcript evidence path must not be a symlink")
    encoded = canonical_json(dict(evidence)) + "\n"
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError(f"cannot read existing caption transcript: {exc}") from exc
        if existing != encoded:
            raise ValidationError("immutable transcript evidence path contains different bytes")
        return path.resolve()
    temporary = directory / f".{path.name}.tmp-{os.getpid()}"
    try:
        temporary.write_text(encoded, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except OSError as exc:
        raise ValidationError(f"cannot write caption transcript evidence: {exc}") from exc
    return path.resolve()


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    validate_artifact("caption_transcript_manifest", dict(manifest))
    if set(manifest) != _MANIFEST_FIELDS:
        raise ValidationError("caption_transcript_manifest fields are invalid")
    if manifest.get("schema_version") != "1.0.0":
        raise ValidationError("caption_transcript_manifest schema_version must be 1.0.0")
    if manifest.get("lane_id") not in _LANES:
        raise ValidationError("caption_transcript_manifest lane_id is invalid")
    if manifest.get("status") != "completed" or manifest.get("warnings") != []:
        raise ValidationError("caption_transcript_manifest is not a clean completion")
    if not isinstance(manifest.get("word_count"), int) or manifest["word_count"] < 1:
        raise ValidationError("caption_transcript_manifest word_count is invalid")
    observer = manifest.get("observer")
    expected_observer = {"executable_sha256", "engine_name", "engine_version", "run_id"}
    if not isinstance(observer, Mapping) or set(observer) != expected_observer:
        raise ValidationError("caption_transcript_manifest observer is invalid")
    for field in ("engine_name", "engine_version", "run_id"):
        require_nonempty_string(observer.get(field), f"caption_transcript_manifest.observer.{field}")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != {"path", "sha256"}:
        raise ValidationError("caption_transcript_manifest evidence is invalid")
    require_nonempty_string(evidence.get("path"), "caption_transcript_manifest.evidence.path")
    _parse_datetime(manifest.get("created_at"), "caption_transcript_manifest.created_at")


def handle_task(
    task: Mapping[str, Any],
    *,
    observer_runner: Callable[[Path, Mapping[str, Any], int], dict[str, Any]] = _run_observer,
) -> dict[str, Any]:
    if task.get("role") != "caption_transcript":
        raise ValidationError("caption_transcript_handler accepts only role='caption_transcript'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "caption_transcript_manifest":
        raise ValidationError("caption transcript task must require caption_transcript_manifest")
    job_id = _safe_id(task.get("job_id") or payload.get("job_id"), "task.job_id")
    if payload.get("job_id") != job_id:
        raise ValidationError("payload.job_id is not bound to task.job_id")
    lane = require_nonempty_string(payload.get("lane_id"), "payload.lane_id")
    if lane not in _LANES or task.get("pod") != lane:
        raise ValidationError("payload.lane_id is not bound to a supported task.pod")

    render_result, render = _upstream(task, "render", "render_manifest")
    if render["job_id"] != job_id:
        raise ValidationError("render artifact is not bound to task.job_id")
    master = _master_path(render_result, render)
    executable = _observer_executable()
    request = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "lane_id": lane,
        "render_id": render["render_id"],
        "render_path": str(master),
        "render_sha256": render["output_sha256"],
        "duration_seconds": render["technical"]["duration_seconds"],
        "language": "ru",
        "require_word_timestamps": True,
    }
    measurement = _validate_observer_measurement(
        observer_runner(executable, request, _observer_timeout())
    )
    evidence = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "render_id": render["render_id"],
        "render_sha256": render["output_sha256"],
        **measurement,
    }
    words = _validate_transcript(evidence, job_id=job_id, render=render)
    engine = evidence["engine"]
    run_material = {
        "job_id": job_id,
        "lane_id": lane,
        "render_manifest_sha256": _artifact_sha256(render),
        "output_sha256": render["output_sha256"],
        "observer_executable_sha256": _sha256_file(executable),
        "observer_run_id": engine["run_id"],
    }
    run_id = digest_text(canonical_json(run_material))[:24]
    root = _configured_evidence_root()
    evidence_path = _write_evidence(root, job_id, run_id, evidence)
    descriptor = {"path": str(evidence_path), "sha256": _sha256_file(evidence_path)}
    created_at = _parse_datetime(
        evidence["completed_at"], "transcript.completed_at"
    ).isoformat().replace("+00:00", "Z")
    manifest = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "lane_id": lane,
        "render_id": render["render_id"],
        "render_sha256": render["output_sha256"],
        "status": "completed",
        "warnings": [],
        "observer": {
            "executable_sha256": _sha256_file(executable),
            "engine_name": engine["name"],
            "engine_version": engine["version"],
            "run_id": engine["run_id"],
        },
        "evidence": descriptor,
        "word_count": len(words),
        "created_at": created_at,
    }
    _validate_manifest(manifest)
    return {"artifact": manifest, "evidence": descriptor, "master_path": str(master)}


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
        sys.stderr.write(f"caption_transcript_handler_error:{type(exc).__name__}:{exc}\n")
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
