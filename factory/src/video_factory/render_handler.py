"""Trusted JSON-stdio HyperFrames render worker.

Rendering is deliberately separated from compilation.  This handler runs only
after a checksum-bound human preview approval, re-verifies the immutable project
tree, invokes one pinned HyperFrames binary without a shell, and emits a
byte-bound RenderManifest.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, TextIO

from .contracts import validate_artifact
from .errors import FactoryError, ValidationError
from .media_tools import media_summary, probe_media
from .validators import canonical_json, digest_text, require_nonempty_string


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CHUNK_BYTES = 1024 * 1024


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_id(value: Any, field: str) -> str:
    result = require_nonempty_string(value, field)
    if not _SAFE_ID.fullmatch(result) or ".." in result:
        raise ValidationError(f"{field} contains unsafe path characters")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(block)
    except OSError as exc:
        raise ValidationError(f"cannot hash file {path}: {exc}") from exc
    return digest.hexdigest()


def _artifact_sha256(value: Mapping[str, Any]) -> str:
    return digest_text(canonical_json(dict(value)))


def _upstream_artifact(
    task: Mapping[str, Any], role: str, contract: str
) -> dict[str, Any]:
    upstream = task.get("upstream_results")
    if not isinstance(upstream, list):
        raise ValidationError("task.upstream_results must be an array")
    matches: list[dict[str, Any]] = []
    for entry in upstream:
        if not isinstance(entry, Mapping) or entry.get("role") != role:
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(artifact, dict):
            matches.append(artifact)
    if len(matches) != 1:
        raise ValidationError(
            f"render requires exactly one upstream {contract} from role={role!r}"
        )
    validate_artifact(contract, matches[0])
    return matches[0]


def _project_files(project: Mapping[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
    raw_root = Path(project["project_root"]).expanduser()
    if raw_root.is_symlink():
        raise ValidationError("project_root must not be a symlink")
    root = raw_root.resolve()
    if not root.is_dir():
        raise ValidationError("project_root must be an existing directory")
    actual: list[dict[str, Any]] = []
    try:
        entries = sorted(root.rglob("*"), key=lambda item: item.as_posix())
    except OSError as exc:
        raise ValidationError(f"cannot enumerate project tree: {exc}") from exc
    for path in entries:
        if path.is_symlink():
            raise ValidationError(f"project tree contains a symlink: {path}")
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
        except OSError as exc:
            raise ValidationError(f"cannot inspect project file {path}: {exc}") from exc
        actual.append(
            {"path": relative, "sha256": _sha256_file(path), "size_bytes": size}
        )
    if actual != project["files"]:
        raise ValidationError("project tree changed after preview approval")
    if digest_text(canonical_json(actual)) != project["project_tree_sha256"]:
        raise ValidationError("project tree hash does not match ProjectManifest")
    entrypoint = (root / project["entrypoint"]).resolve()
    try:
        entrypoint.relative_to(root)
    except ValueError as exc:
        raise ValidationError("project entrypoint escapes project_root") from exc
    if not entrypoint.is_file():
        raise ValidationError("project entrypoint is missing")
    return root, actual


def _verify_approval(
    approval: Mapping[str, Any], project: Mapping[str, Any]
) -> None:
    if approval["approved"] is not True:
        raise ValidationError("preview approval has not passed")
    expected = {
        "job_id": project["job_id"],
        "project_id": project["project_id"],
        "project_tree_sha256": project["project_tree_sha256"],
        "project_manifest_sha256": _artifact_sha256(project),
    }
    mismatched = [key for key, value in expected.items() if approval.get(key) != value]
    if mismatched:
        raise ValidationError(
            "preview approval is not bound to ProjectManifest: "
            + ", ".join(mismatched)
        )
    raw_receipt = Path(approval["check_receipt_path"]).expanduser()
    if raw_receipt.is_symlink():
        raise ValidationError("preview check receipt must not be a symlink")
    receipt_path = raw_receipt.resolve()
    if not receipt_path.is_file():
        raise ValidationError("preview check receipt is missing")
    if _sha256_file(receipt_path) != approval["check_receipt_sha256"]:
        raise ValidationError("preview check receipt hash does not match")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("preview check receipt is unreadable") from exc
    if not isinstance(receipt, dict) or receipt.get("ok") is not True:
        raise ValidationError("preview check receipt must record ok=true")
    if receipt.get("project_tree_sha256") != project["project_tree_sha256"]:
        raise ValidationError("preview check receipt is not bound to the project tree")


def _input_hashes(
    project: Mapping[str, Any], approval: Mapping[str, Any]
) -> list[dict[str, str]]:
    result = [
        {"path": "project_manifest.json", "sha256": _artifact_sha256(project)},
        {"path": "preview_approval.json", "sha256": _artifact_sha256(approval)},
    ]
    result.extend(
        {"path": f"project/{item['path']}", "sha256": item["sha256"]}
        for item in project["files"]
    )
    return sorted(result, key=lambda item: item["path"])


def _configured_binary() -> Path:
    raw = os.environ.get("HYPERFRAMES_BIN")
    if not raw:
        raise ValidationError("HYPERFRAMES_BIN must point to a pinned local binary")
    path = Path(raw).expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        raise ValidationError("HYPERFRAMES_BIN must be a regular local file")
    return path


def _positive_timeout() -> float:
    raw = os.environ.get("VIDEO_FACTORY_RENDER_TIMEOUT_SECONDS", "7200")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValidationError("VIDEO_FACTORY_RENDER_TIMEOUT_SECONDS must be numeric") from exc
    if not math.isfinite(value) or not 1 <= value <= 86400:
        raise ValidationError(
            "VIDEO_FACTORY_RENDER_TIMEOUT_SECONDS must be from 1 to 86400"
        )
    return value


def _probe_render(path: Path, project: Mapping[str, Any]) -> dict[str, Any]:
    summary = media_summary(probe_media(path))
    video = summary.get("video")
    audio = summary.get("audio")
    if not isinstance(video, Mapping) or not isinstance(audio, Mapping):
        raise ValidationError("render must contain video and audio streams")
    duration = summary.get("duration_seconds")
    if not isinstance(duration, (int, float)) or not math.isfinite(float(duration)):
        raise ValidationError("render duration is unavailable")
    expected = project["composition"]
    if video.get("width") != 1080 or video.get("height") != 1920:
        raise ValidationError("render geometry must be 1080x1920")
    fps = video.get("fps")
    if not isinstance(fps, (int, float)) or abs(float(fps) - 30.0) > 0.02:
        raise ValidationError("render frame rate must be 30 fps")
    if abs(float(duration) - float(expected["duration_seconds"])) > 0.25:
        raise ValidationError("render duration does not match ProjectManifest")
    if audio.get("sample_rate_hz") != 48000:
        raise ValidationError("render audio sample rate must be 48000 Hz")
    return {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "duration_seconds": float(duration),
        "video_codec": require_nonempty_string(video.get("codec"), "video codec"),
        "audio_codec": require_nonempty_string(audio.get("codec"), "audio codec"),
        "audio_sample_rate_hz": 48000,
        "integrated_lufs": None,
        "true_peak_dbtp": None,
    }


def _load_existing(
    manifest_path: Path,
    *,
    job_id: str,
    expected_inputs: list[dict[str, str]],
) -> dict[str, Any]:
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("existing RenderManifest is unreadable") from exc
    if not isinstance(document, dict):
        raise ValidationError("existing RenderManifest must be an object")
    validate_artifact("render_manifest", document)
    if document["job_id"] != job_id or document["input_hashes"] != expected_inputs:
        raise ValidationError("immutable render output conflicts with current inputs")
    output = manifest_path.parent / document["output"]
    if not output.is_file() or _sha256_file(output) != document["output_sha256"]:
        raise ValidationError("existing render bytes do not match RenderManifest")
    return document


def handle_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if task.get("role") != "render":
        raise ValidationError("render_handler accepts only role='render'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "render_manifest":
        raise ValidationError(
            "render task must declare required_result_contract='render_manifest'"
        )
    job_id = _safe_id(task.get("job_id"), "task.job_id")
    if payload.get("job_id") != job_id:
        raise ValidationError("payload.job_id is not bound to task.job_id")
    project = _upstream_artifact(task, "compiler", "project_manifest")
    approval = _upstream_artifact(task, "preview_review", "preview_approval")
    if project["job_id"] != job_id or project["lane_id"] != task.get("pod"):
        raise ValidationError("ProjectManifest is not bound to render task")
    root, _ = _project_files(project)
    _verify_approval(approval, project)
    expected_inputs = _input_hashes(project, approval)

    runtime_root = Path(
        os.environ.get("VIDEO_FACTORY_RUNTIME_ROOT", str(Path.home() / ".video-factory"))
    ).expanduser().resolve()
    output_root = Path(
        os.environ.get(
            "VIDEO_FACTORY_RENDER_OUTPUT_ROOT", str(runtime_root / "renders")
        )
    ).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    job_root = (output_root / job_id).resolve()
    if job_root.parent != output_root:
        raise ValidationError("render output escaped configured output root")
    job_root.mkdir(parents=True, exist_ok=True)
    manifest_path = job_root / "render_manifest.json"
    if manifest_path.exists():
        existing = _load_existing(
            manifest_path, job_id=job_id, expected_inputs=expected_inputs
        )
        return {
            "artifact": existing,
            "output_path": str((job_root / existing["output"]).resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "render_execution": {"reused": True, "approval_verified": True},
        }

    binary = _configured_binary()
    fd, temporary_name = tempfile.mkstemp(
        prefix=".rendering.", suffix=".mp4", dir=job_root
    )
    os.close(fd)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    try:
        command = [
            str(binary),
            "render",
            str(root),
            "--output",
            str(temporary),
            "--quality",
            "high",
            "--fps",
            "30",
            "--crf",
            os.environ.get("VIDEO_FACTORY_RENDER_CRF", "16"),
            "--workers",
            os.environ.get("HYPERFRAMES_WORKERS", "1"),
            "--strict-all",
            "--no-best-effort",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_positive_timeout(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError(f"HyperFrames render failed: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip().replace("\r", " ").replace("\n", " ")[-2000:]
            raise ValidationError(
                f"HyperFrames render exited {completed.returncode}: {detail}"
            )
        if not temporary.is_file() or temporary.stat().st_size < 1:
            raise ValidationError("HyperFrames did not produce a non-empty MP4")
        technical = _probe_render(temporary, project)
        output_sha = _sha256_file(temporary)
        final_output = job_root / "final.mp4"
        if final_output.exists():
            if not final_output.is_file() or _sha256_file(final_output) != output_sha:
                raise ValidationError("existing immutable final.mp4 conflicts with render")
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, final_output)
        approval_sha = _artifact_sha256(approval)
        render_id = f"render-{digest_text(job_id + project['project_tree_sha256'] + approval_sha)[:24]}"
        manifest = {
            "schema_version": "1.0.0",
            "render_id": render_id,
            "job_id": job_id,
            "composition": project["composition"]["composition_id"],
            "output": final_output.name,
            "output_sha256": output_sha,
            "technical": technical,
            "input_hashes": expected_inputs,
            "created_at": _utc_now(),
        }
        validate_artifact("render_manifest", manifest)
        manifest_data = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        fd, temporary_manifest_name = tempfile.mkstemp(
            prefix=".render-manifest.", suffix=".tmp", dir=job_root
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(manifest_data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_manifest_name, manifest_path)
        except BaseException:
            Path(temporary_manifest_name).unlink(missing_ok=True)
            raise
        return {
            "artifact": manifest,
            "output_path": str(final_output.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "render_execution": {
                "reused": False,
                "approval_verified": True,
                "binary": str(binary),
                "quality": "high",
                "fps": 30,
            },
        }
    finally:
        temporary.unlink(missing_ok=True)


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    try:
        task = json.load(source)
        if not isinstance(task, dict):
            raise ValidationError("handler stdin must contain one JSON object")
        result = handle_task(task)
    except (FactoryError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"render_handler_error:{type(exc).__name__}:{exc}\n")
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
