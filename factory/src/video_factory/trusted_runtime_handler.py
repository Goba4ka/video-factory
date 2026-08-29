"""Fail-closed JSON-stdio dispatcher for deterministic production handlers.

The queue worker still claims exactly one role.  This dispatcher exists only
to make a single hardened systemd template practical; it rejects human and
network/editorial roles and forwards the unchanged task to the pinned local
handler for that exact role.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Mapping, TextIO

from .errors import FactoryError, ValidationError
from .bgm_handler import handle_task as freeze_bgm
from .caption_analyzer import handle_task as analyze_captions
from .caption_transcript_handler import handle_task as transcribe_captions
from .dedup_analyzer_handler import handle_task as analyze_dedup
from .facts_analyzer import handle_task as analyze_facts
from .hyperframes_compiler import handle_task as compile_project
from .media_handler import handle_task as freeze_media
from .policy_analyzer import handle_task as analyze_policy
from .program_audio_handler import handle_task as mix_program_audio
from .qc_auto_evidence_handler import handle_task as produce_auto_qc_evidence
from .qc_evidence_gate import handle_task as gate_qc_evidence
from .render_handler import handle_task as render_project
from .semantic_qc_handler import handle_task as run_semantic_qc
from .source_audio_handler import handle_task as extract_source_audio
from .visual_analyzer_handler import handle_task as analyze_visual
from .validators import canonical_json


Handler = Callable[[Mapping[str, Any]], dict[str, Any]]

TRUSTED_RUNTIME_HANDLERS: dict[str, Handler] = {
    "media": freeze_media,
    "source_audio": extract_source_audio,
    "bgm": freeze_bgm,
    "audio_mix": mix_program_audio,
    "compiler": compile_project,
    "render": render_project,
    "qc_auto_evidence": produce_auto_qc_evidence,
    "caption_transcript": transcribe_captions,
    "captions_analyzer": analyze_captions,
    "facts_analyzer": analyze_facts,
    "policy_analyzer": analyze_policy,
    "dedup_analyzer": analyze_dedup,
    "visual_analyzer": analyze_visual,
    "qc_evidence_gate": gate_qc_evidence,
    "qc": run_semantic_qc,
}


def handle_task(
    task: Mapping[str, Any], *, handlers: Mapping[str, Handler] | None = None
) -> dict[str, Any]:
    role = task.get("role")
    if not isinstance(role, str) or not role:
        raise ValidationError("task.role must be a non-empty string")
    if role not in TRUSTED_RUNTIME_HANDLERS:
        allowed = ", ".join(sorted(TRUSTED_RUNTIME_HANDLERS))
        raise ValidationError(
            f"role {role!r} is not a trusted runtime role; allowed: {allowed}"
        )
    registry = TRUSTED_RUNTIME_HANDLERS if handlers is None else handlers
    selected = registry.get(role)
    if selected is None:
        raise ValidationError(f"trusted handler for role={role!r} is not configured")
    result = selected(task)
    if not isinstance(result, dict):
        raise ValidationError(f"trusted handler for role={role!r} returned no object")
    return result


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
        sys.stderr.write(
            f"trusted_runtime_handler_error:{type(exc).__name__}:{exc}\n"
        )
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
