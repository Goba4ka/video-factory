"""Trusted JSON-stdio handler for job- and profile-bound Fish narration.

The handler deliberately refuses to synthesize speech until a job-specific
``voice_rights_approval`` artifact and a human-approved golden voice profile
both exist.  Credentials are loaded inside the Fish Audio client and never
enter the queue task or handler result.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, TextIO

from .contracts import validate_artifact
from .errors import FactoryError, ValidationError
from .fish_audio import DEFAULT_MODEL, FishTTSRequest, generate_tts
from .validators import canonical_json, require_nonempty_string
from .voice_profile_gate import load_approved_voice_profile


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _configured_root(name: str, default: Path) -> Path:
    root = Path(os.environ.get(name, str(default))).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValidationError(f"{name} must point to a directory")
    return root


def _job_id(task: Mapping[str, Any]) -> str:
    value = require_nonempty_string(task.get("job_id"), "task.job_id")
    if not _SAFE_ID.fullmatch(value):
        raise ValidationError("task.job_id contains unsafe path characters")
    return value


def _script_package(task: Mapping[str, Any]) -> dict[str, Any]:
    upstream = task.get("upstream_results")
    if not isinstance(upstream, list):
        raise ValidationError("task.upstream_results must be an array")
    for entry in reversed(upstream):
        if not isinstance(entry, Mapping) or entry.get("role") != "script":
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(artifact, dict):
            validate_artifact("script_package", artifact)
            return artifact
    raise ValidationError("voice task requires an upstream script_package")


def _approval(task: Mapping[str, Any], job_id: str, approval_root: Path) -> dict[str, Any]:
    payload = task.get("payload")
    embedded = payload.get("voice_rights_approval") if isinstance(payload, Mapping) else None
    if embedded is not None:
        if not isinstance(embedded, dict):
            raise ValidationError("payload.voice_rights_approval must be an object")
        approval = embedded
    else:
        path = approval_root / f"{job_id}.json"
        try:
            approval = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValidationError(
                f"voice rights approval is missing for job {job_id!r}"
            ) from exc
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("voice rights approval is unreadable") from exc
        if not isinstance(approval, dict):
            raise ValidationError("voice rights approval must contain one JSON object")
    validate_artifact("voice_rights_approval", approval)
    if approval["job_id"] != job_id:
        raise ValidationError("voice rights approval is not bound to task.job_id")
    return approval


def _spoken_text(script: Mapping[str, Any]) -> str:
    # Script segments cover the authoritative, contiguous 0..duration timeline;
    # hook.spoken_text is intentionally not prepended because it normally mirrors
    # the first segment and would otherwise be spoken twice.
    parts = [
        require_nonempty_string(segment.get("spoken_text"), "segment.spoken_text")
        for segment in script["segments"]
    ]
    text = " ".join(parts).strip()
    if not text:
        raise ValidationError("script_package contains no narration text")
    return text


def handle_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if task.get("role") != "voice":
        raise ValidationError("voice_handler accepts only role='voice'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "voice_manifest":
        raise ValidationError(
            "voice task must declare required_result_contract='voice_manifest'"
        )
    job_id = _job_id(task)
    lane = require_nonempty_string(payload.get("lane_id"), "payload.lane_id")
    if lane == "motivation":
        raise ValidationError("motivation must use the source_audio handler, not Fish Audio")

    runtime_root = _configured_root(
        "VIDEO_FACTORY_RUNTIME_ROOT", Path.home() / ".video-factory"
    )
    approval_root = _configured_root(
        "VIDEO_FACTORY_VOICE_APPROVAL_ROOT", runtime_root / "voice_approvals"
    )
    output_root = _configured_root(
        "VIDEO_FACTORY_VOICE_OUTPUT_ROOT", runtime_root / "voices"
    )
    script = _script_package(task)
    if script["job_id"] != job_id or script["lane_id"] != lane:
        raise ValidationError("script_package is not bound to this voice task")
    approval = _approval(task, job_id, approval_root)
    expected_profile_id = payload.get("voice_profile_id")
    if expected_profile_id is not None and not isinstance(expected_profile_id, str):
        raise ValidationError("payload.voice_profile_id must be a string")
    profile = load_approved_voice_profile(
        reference_id=approval["reference_id"],
        lane_id=lane,
        language=script["language"],
        expected_profile_id=expected_profile_id,
    )
    if approval["reference_id"] != profile.approval["reference_id"]:
        raise ValidationError("job voice approval reference_id does not match voice profile")
    if approval["voice_rights_status"] != profile.approval["rights_status"]:
        raise ValidationError(
            "job voice approval rights status does not match voice profile"
        )
    expected_basis = (
        "voice_owner_confirmation"
        if profile.approval["rights_status"] == "approved_owned_voice"
        else "commercial_license"
    )
    if approval["basis"] != expected_basis:
        raise ValidationError("job voice approval basis does not match voice profile")

    retry_reason = payload.get("retry_reason")
    defect_reference = payload.get("defect_reference")
    if retry_reason is not None and not isinstance(retry_reason, str):
        raise ValidationError("payload.retry_reason must be a string")
    if defect_reference is not None and not isinstance(defect_reference, str):
        raise ValidationError("payload.defect_reference must be a string")

    active_output = output_root / job_id / "voice.wav"
    result = generate_tts(
        FishTTSRequest(
            video_id=job_id,
            text=_spoken_text(script),
            output_path=active_output,
            reference_id=approval["reference_id"],
            model=os.environ.get("FISH_MODEL", DEFAULT_MODEL),
            speed=float(os.environ.get("FISH_SPEED", "1.0")),
            temperature=float(os.environ.get("FISH_TEMPERATURE", "0.7")),
            top_p=float(os.environ.get("FISH_TOP_P", "0.7")),
            timeout_seconds=float(os.environ.get("FISH_TIMEOUT_SECONDS", "180")),
            voice_rights_status=approval["voice_rights_status"],
            retry_reason=retry_reason,
            defect_reference=defect_reference,
        ),
        usage_db=os.environ.get(
            "FISH_USAGE_DB", str(runtime_root / "fish_audio_usage.sqlite3")
        ),
    )
    manifest_path = Path(result["voice_manifest_path"]).expanduser().resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError("Fish Audio voice manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise ValidationError("Fish Audio voice manifest must be an object")
    validate_artifact("voice_manifest", manifest)
    if manifest["job_id"] != job_id:
        raise ValidationError("Fish Audio voice manifest is not job-bound")
    if manifest["reference_id"] != profile.approval["reference_id"]:
        raise ValidationError("Fish Audio voice manifest reference_id changed")
    if manifest["voice_rights_status"] != profile.approval["rights_status"]:
        raise ValidationError("Fish Audio voice manifest rights status changed")
    return {
        "artifact": manifest,
        "voice_rights_approval": approval,
        "voice_profile_approval": profile.approval,
        "voice_profile_binding": profile.binding,
        "output_path": str(active_output.resolve()),
        "voice_execution": {
            "provider": "fish_audio",
            "generation_no": result["generation_no"],
            "reused": result["reused"],
            "remaining_generations": result["remaining_generations"],
            "estimated_cost_usd": result["estimated_cost_usd"],
            "manifest_path": str(manifest_path),
        },
    }


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    try:
        task = json.load(source)
        if not isinstance(task, dict):
            raise ValidationError("handler stdin must contain one JSON object")
        result = handle_task(task)
    except (FactoryError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"voice_handler_error:{type(exc).__name__}:{exc}\n")
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
