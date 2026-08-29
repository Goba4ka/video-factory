"""JSON-stdio trusted runtime handler for frame-level visual analysis."""

from __future__ import annotations

from typing import Any, Callable, Mapping, TextIO

from ._qc_analyzer_common import (
    common_task_context,
    emit_main,
    safe_id,
    upstream_artifact,
    validate_master,
)
from ._qc_analyzer_handler_common import (
    evidence_paths,
    reject_untrusted_overrides,
    require_configured_face_observer,
    verify_analyzer_result,
)
from .errors import ValidationError
from .visual_analyzer import analyze_visual


VisualRunner = Callable[..., dict[str, Any]]
_SPEAKER_REQUIRED_LANES = frozenset({"celebrity_news", "motivation"})


def handle_task(
    task: Mapping[str, Any], *, analyzer: VisualRunner = analyze_visual
) -> dict[str, Any]:
    payload, job_id, lane_id = common_task_context(task, "visual_analyzer")
    reject_untrusted_overrides(payload)
    require_configured_face_observer()
    render_upstream = upstream_artifact(
        task, roles=("render",), contract="render_manifest"
    )
    shotlist_upstream = upstream_artifact(
        task, roles=("editor",), contract="shotlist"
    )
    assert render_upstream is not None
    assert shotlist_upstream is not None
    render_result, render = render_upstream
    _, shotlist = shotlist_upstream
    output, render_sha256, render_id = validate_master(
        render_result, render, job_id
    )
    idea_id = safe_id(payload.get("idea_id"), "payload.idea_id")
    if shotlist.get("idea_id") != idea_id:
        raise ValidationError("shotlist is not bound to payload.idea_id")
    report_path, contact_sheet_path = evidence_paths(
        job_id=job_id, render_id=render_id, category="visual"
    )
    assert contact_sheet_path is not None
    result = analyzer(
        output,
        render,
        shotlist,
        lane_id=lane_id,
        speaker_required=lane_id in _SPEAKER_REQUIRED_LANES,
        report_path=report_path,
        contact_sheet_path=contact_sheet_path,
    )
    return verify_analyzer_result(
        result,
        category="visual",
        job_id=job_id,
        lane_id=lane_id,
        render_id=render_id,
        render_sha256=render_sha256,
        report_path=report_path,
        contact_sheet_path=contact_sheet_path,
    )


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    return emit_main(handle_task, stdin=stdin, stdout=stdout)


if __name__ == "__main__":
    raise SystemExit(main())
