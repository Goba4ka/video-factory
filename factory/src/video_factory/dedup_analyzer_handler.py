"""JSON-stdio trusted runtime handler for perceptual dedup analysis."""

from __future__ import annotations

from typing import Any, Callable, Mapping, TextIO

from ._qc_analyzer_common import (
    common_task_context,
    emit_main,
    upstream_artifact,
    validate_master,
)
from ._qc_analyzer_handler_common import (
    configured_snapshot_descriptor,
    evidence_paths,
    reject_untrusted_overrides,
    verify_analyzer_result,
)
from .dedup_analyzer import analyze_dedup


DedupRunner = Callable[..., dict[str, Any]]


def handle_task(
    task: Mapping[str, Any], *, analyzer: DedupRunner = analyze_dedup
) -> dict[str, Any]:
    payload, job_id, lane_id = common_task_context(task, "dedup_analyzer")
    reject_untrusted_overrides(payload)
    render_upstream = upstream_artifact(
        task, roles=("render",), contract="render_manifest"
    )
    assert render_upstream is not None
    render_result, render = render_upstream
    output, render_sha256, render_id = validate_master(
        render_result, render, job_id
    )
    report_path, _ = evidence_paths(
        job_id=job_id, render_id=render_id, category="dedup"
    )
    corpus = configured_snapshot_descriptor()
    result = analyzer(
        output,
        render,
        corpus,
        lane_id=lane_id,
        report_path=report_path,
    )
    return verify_analyzer_result(
        result,
        category="dedup",
        job_id=job_id,
        lane_id=lane_id,
        render_id=render_id,
        render_sha256=render_sha256,
        report_path=report_path,
    )


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    return emit_main(handle_task, stdin=stdin, stdout=stdout)


if __name__ == "__main__":
    raise SystemExit(main())
