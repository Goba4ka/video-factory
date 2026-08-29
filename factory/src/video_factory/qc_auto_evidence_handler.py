"""Produce the deterministic technical, audio and rights QC evidence set.

The role runs one FULL FFmpeg scan, binds the result to the actual render bytes,
and independently re-verifies the rights/frozen-media chain.  It never turns a
warning into a pass.  Semantic analyzers run later and a separate evidence gate
requires all eight categories before final QC.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from ._qc_analyzer_common import (
    artifact_sha256,
    emit_main,
    file_sha256,
    persist_report,
    safe_id,
)
from .contracts import validate_artifact
from .errors import ValidationError
from .media_freeze import MediaFreezeError, verify_frozen_media_manifest
from .media_qc import QC_PROFILES, run_media_qc
from .source_audio import (
    is_multisource_manifest,
    source_audio_segments,
    verify_multisource_program,
)


CHECKER_VERSION = "1.0.0"
_AUDIO_CODES = frozenset(
    {
        "missing_audio",
        "audio_codec",
        "sample_rate",
        "audio_channels",
        "av_drift",
        "silence_run",
        "loudness_missing",
        "integrated_loudness",
        "true_peak",
        "loudness_range",
    }
)


def _upstream(
    task: Mapping[str, Any], role: str, contract: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = task.get("upstream_results")
    if not isinstance(raw, list):
        raise ValidationError("task.upstream_results must be an array")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or entry.get("role") != role:
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(result, dict) and isinstance(artifact, dict):
            matches.append((result, artifact))
    if len(matches) != 1:
        raise ValidationError(
            f"qc_auto_evidence requires exactly one upstream {contract} "
            f"from role={role!r}"
        )
    validate_artifact(contract, matches[0][1])
    return matches[0]


def _optional_upstream(
    task: Mapping[str, Any], role: str, contract: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    raw = task.get("upstream_results")
    if not isinstance(raw, list):
        raise ValidationError("task.upstream_results must be an array")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or entry.get("role") != role:
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(result, dict) and isinstance(artifact, dict):
            matches.append((result, artifact))
    if len(matches) > 1:
        raise ValidationError(
            f"qc_auto_evidence received duplicate {contract} from role={role!r}"
        )
    if not matches:
        return None
    validate_artifact(contract, matches[0][1])
    return matches[0]


def _messages(rows: Any, *, category: str) -> list[str]:
    if not isinstance(rows, list):
        raise ValidationError("FULL media QC findings must be arrays")
    selected: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValidationError("FULL media QC finding must be an object")
        code = row.get("code")
        message = row.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            raise ValidationError("FULL media QC finding requires code and message")
        is_audio = code in _AUDIO_CODES
        if (category == "audio" and is_audio) or (
            category == "technical" and not is_audio
        ):
            selected.append(f"{code}: {message}")
    return selected


def _report(
    *,
    category: str,
    job_id: str,
    lane_id: str,
    render_id: str,
    render_sha256: str,
    bindings: Mapping[str, str],
    metrics: Mapping[str, Any],
    findings: list[str],
    warnings: list[str],
    completed_at: str,
    run_id: str,
) -> dict[str, Any]:
    passed = not findings and not warnings
    report = {
        "schema_version": "1.0.0",
        "category": category,
        "job_id": job_id,
        "lane_id": lane_id,
        "render_id": render_id,
        "render_sha256": render_sha256,
        "status": "pass" if passed else "fail",
        "needs_human_review": not passed,
        "warnings": warnings,
        "findings": findings,
        "checker": {
            "name": "video_factory.qc_auto_evidence_handler",
            "version": CHECKER_VERSION,
            "run_id": run_id,
        },
        "completed_at": completed_at,
        "bindings": dict(bindings),
        "metrics": dict(metrics),
    }
    validate_artifact("qc_analyzer_report", report)
    return report


def handle_task(
    task: Mapping[str, Any],
    *,
    media_qc_runner: Callable[..., dict[str, Any]] = run_media_qc,
) -> dict[str, Any]:
    if task.get("role") != "qc_auto_evidence":
        raise ValidationError("handler accepts only role='qc_auto_evidence'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "qc_auto_evidence_manifest":
        raise ValidationError(
            "qc_auto_evidence task must require qc_auto_evidence_manifest"
        )
    job_id = safe_id(task.get("job_id") or payload.get("job_id"), "task.job_id")
    lane_id = safe_id(payload.get("lane_id"), "payload.lane_id")
    if payload.get("job_id") != job_id or task.get("pod") != lane_id:
        raise ValidationError("qc_auto_evidence task identity is not bound")

    render_result, render = _upstream(task, "render", "render_manifest")
    _, rights = _upstream(task, "rights", "rights_manifest")
    _, frozen = _upstream(task, "media", "frozen_media_manifest")
    _, shotlist = _upstream(task, "editor", "shotlist")
    project: dict[str, Any] | None = None
    program_audio: dict[str, Any] | None = None
    source_audio: dict[str, Any] | None = None
    if lane_id == "motivation":
        source_entry = _optional_upstream(
            task, "source_audio", "source_audio_manifest"
        )
        if source_entry is not None:
            _, source_audio = source_entry
            _, project = _upstream(task, "compiler", "project_manifest")
            _, program_audio = _upstream(
                task, "audio_mix", "program_audio_manifest"
            )
    if render.get("job_id") != job_id or frozen.get("job_id") != job_id:
        raise ValidationError("QC inputs are not bound to task.job_id")
    if len({rights.get("idea_id"), frozen.get("idea_id"), shotlist.get("idea_id")}) != 1:
        raise ValidationError("QC inputs cross the idea boundary")

    output_value = render_result.get("output_path")
    if not isinstance(output_value, str) or not output_value.strip():
        raise ValidationError("render result requires output_path")
    output = Path(output_value).expanduser().resolve()
    if not output.is_file():
        raise ValidationError(f"render output does not exist: {output}")
    render_sha256 = file_sha256(output)
    if render_sha256 != render.get("output_sha256"):
        raise ValidationError("render output checksum does not match RenderManifest")
    render_id = safe_id(render.get("render_id"), "render_manifest.render_id")
    render_manifest_sha256 = artifact_sha256(render)

    profile_name = payload.get("technical_profile")
    if profile_name != "vertical_master":
        raise ValidationError("qc_auto_evidence requires technical_profile=vertical_master")
    profile = QC_PROFILES.get(profile_name)
    if not isinstance(profile, Mapping) or profile.get("exact_resolution") != [1080, 1920]:
        raise ValidationError("vertical_master profile must enforce 1080x1920")
    media_qc = media_qc_runner(
        output,
        level="full",
        profile_name=profile_name,
        cache_root=os.environ.get("VIDEO_FACTORY_QC_CACHE_ROOT"),
    )
    if not isinstance(media_qc, Mapping) or media_qc.get("level") != "full":
        raise ValidationError("FULL media QC returned no usable report")
    if Path(str(media_qc.get("source", ""))).expanduser().resolve() != output:
        raise ValidationError("FULL media QC is not bound to render output")
    media = media_qc.get("media")
    scan = media_qc.get("scan")
    if not isinstance(media, Mapping) or not isinstance(scan, Mapping):
        raise ValidationError("FULL media QC lacks media/scan evidence")
    video = media.get("video")
    audio = media.get("audio")
    if not isinstance(video, Mapping) or not isinstance(audio, Mapping):
        raise ValidationError("FULL media QC requires video and audio streams")
    technical = render["technical"]
    if (
        video.get("width") != technical["width"]
        or video.get("height") != technical["height"]
        or abs(float(video.get("fps", -1)) - float(technical["fps"])) > 0.01
        or audio.get("sample_rate_hz") != technical["audio_sample_rate_hz"]
    ):
        raise ValidationError("FULL media QC metadata differs from RenderManifest")

    failures = media_qc.get("failures")
    media_warnings = media_qc.get("warnings")
    technical_findings = _messages(failures, category="technical")
    technical_warnings = _messages(media_warnings, category="technical")
    audio_findings = _messages(failures, category="audio")
    audio_warnings = _messages(media_warnings, category="audio")

    rights_findings: list[str] = []
    decision = rights.get("decision")
    if not isinstance(decision, Mapping):
        raise ValidationError("RightsManifest has no decision")
    if decision.get("passed") is not True:
        rights_findings.append("rights hard gate did not pass")
    if decision.get("needs_human_review") is not False:
        rights_findings.append("rights manifest still needs human review")
    missing = decision.get("missing_asset_ids")
    if not isinstance(missing, list):
        raise ValidationError("RightsManifest missing_asset_ids must be an array")
    if missing:
        rights_findings.append("rights manifest has missing asset ids")
    try:
        verify_frozen_media_manifest(
            frozen, rights_manifest=rights, expected_job_id=job_id
        )
    except MediaFreezeError as exc:
        rights_findings.append(f"frozen media verification failed: {exc}")
    frozen_ids = {item["asset_id"] for item in frozen.get("assets", [])}
    used_ids = {item["asset_id"] for item in shotlist.get("shots", [])}
    unknown = sorted(used_ids - frozen_ids)
    if unknown:
        rights_findings.append("shotlist uses unfrozen assets: " + ", ".join(unknown))

    source_audio_sha256: str | None = None
    source_segment_bindings_sha256: str | None = None
    if source_audio is not None:
        if project is None or program_audio is None:
            raise ValidationError("source-audio QC lacks program/project consumers")
        source_audio_sha256 = artifact_sha256(source_audio)
        source_segment_bindings_sha256 = (
            source_audio["checksums"]["segment_bindings_sha256"]
            if is_multisource_manifest(source_audio)
            else source_audio_sha256
        )
        expected_authority = {
            "contract": "source_audio_manifest",
            "schema_version": source_audio["schema_version"],
            "job_id": source_audio["job_id"],
            "sha256": source_audio_sha256,
            "audio_sha256": source_audio["checksums"]["extracted_audio_sha256"],
        }
        if project["bindings"]["authoritative_audio"] != expected_authority:
            rights_findings.append(
                "ProjectManifest is not bound to the exact SourceAudioManifest"
            )
        expected_program_authority = {
            "contract": "source_audio_manifest",
            "manifest_sha256": source_audio_sha256,
            "audio_sha256": source_audio["checksums"]["extracted_audio_sha256"],
            "authority": "spoken_content_and_timing",
            "tts": False,
        }
        if program_audio["source_authority"] != expected_program_authority:
            rights_findings.append(
                "ProgramAudioManifest is not bound to the exact SourceAudioManifest"
            )
        frozen_by_id = {item["asset_id"]: item for item in frozen.get("assets", [])}
        rights_by_id = {item["asset_id"]: item for item in rights.get("assets", [])}
        for index, segment in enumerate(source_audio_segments(source_audio)):
            frozen_item = frozen_by_id.get(segment["asset_id"])
            rights_item = rights_by_id.get(segment["asset_id"])
            if frozen_item is None or rights_item is None:
                rights_findings.append(
                    f"source-audio segment {index} is absent from rights/frozen manifests"
                )
                continue
            if (
                rights_item.get("rights_status") != "approved"
                or rights_item.get("commercial_use") is not True
                or rights_item.get("modification_allowed") is not True
            ):
                rights_findings.append(
                    f"source-audio segment {index} lacks commercial modified-use rights"
                )
            expected_evidence = None
            if segment["rights_status"] != "internal_prototype":
                receipt = rights_item.get("license_receipt")
                license_url = rights_item.get("license_url")
                if isinstance(receipt, str) and receipt.strip():
                    expected_evidence = receipt.strip()
                elif (
                    segment["rights_status"] == "commercial_license_confirmed"
                    and isinstance(license_url, str)
                    and license_url.strip()
                ):
                    expected_evidence = license_url.strip()
                else:
                    rights_findings.append(
                        f"source-audio segment {index} lacks rights evidence"
                    )
            if segment["rights_evidence"] != expected_evidence:
                rights_findings.append(
                    f"source-audio segment {index} rights evidence differs from RightsManifest"
                )
            frozen_source = (
                Path(frozen["frozen_root"]) / Path(frozen_item["frozen_path"])
            ).expanduser().resolve()
            if (
                Path(segment["source_video_uri_or_path"]).expanduser().resolve()
                != frozen_source
                or segment["checksums"]["source_video_sha256"]
                != frozen_item["sha256"]
                or not frozen_source.is_file()
                or file_sha256(frozen_source) != frozen_item["sha256"]
            ):
                rights_findings.append(
                    f"source-audio segment {index} differs from frozen source bytes"
                )
        if is_multisource_manifest(source_audio):
            try:
                verify_multisource_program(source_audio)
            except ValidationError as exc:
                rights_findings.append(f"multi-source program verification failed: {exc}")
        program_sha256 = artifact_sha256(program_audio)
        expected_program_binding = {
            "contract": "program_audio_manifest",
            "schema_version": program_audio["schema_version"],
            "job_id": program_audio["job_id"],
            "idea_id": program_audio["idea_id"],
            "lane_id": program_audio["lane_id"],
            "sha256": program_sha256,
            "audio_sha256": program_audio["output_sha256"],
            "project_path": "assets/audio/program_mix.wav",
            "size_bytes": program_audio["output_bytes"],
        }
        if project["bindings"]["program_audio"] != expected_program_binding:
            rights_findings.append(
                "ProjectManifest is not bound to the exact ProgramAudioManifest"
            )
        program_path = Path(program_audio["immutable_output_path"]).expanduser().resolve()
        if (
            program_path.is_symlink()
            or not program_path.is_file()
            or program_path.stat().st_size != program_audio["output_bytes"]
            or file_sha256(program_path) != program_audio["output_sha256"]
        ):
            rights_findings.append("ProgramAudioManifest output bytes changed before QC")

    completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    common_bindings = {
        "output_sha256": render_sha256,
        "render_manifest_sha256": render_manifest_sha256,
    }
    run_seed = artifact_sha256(
        {
            "checker": CHECKER_VERSION,
            "job_id": job_id,
            "render_sha256": render_sha256,
            "media_qc": dict(media_qc),
            "rights_manifest_sha256": artifact_sha256(rights),
            "frozen_media_manifest_sha256": artifact_sha256(frozen),
        }
    )
    technical_report = _report(
        category="technical",
        job_id=job_id,
        lane_id=lane_id,
        render_id=render_id,
        render_sha256=render_sha256,
        bindings=common_bindings,
        metrics={
            "profile": profile_name,
            "video": dict(video),
            "black_durations_seconds": list(scan.get("black_durations_seconds", [])),
            "freeze_durations_seconds": list(scan.get("freeze_durations_seconds", [])),
        },
        findings=technical_findings,
        warnings=technical_warnings,
        completed_at=completed_at,
        run_id=f"technical-{run_seed[:24]}",
    )
    audio_report = _report(
        category="audio",
        job_id=job_id,
        lane_id=lane_id,
        render_id=render_id,
        render_sha256=render_sha256,
        bindings=common_bindings,
        metrics={
            "profile": profile_name,
            "audio": dict(audio),
            "silence_durations_seconds": list(scan.get("silence_durations_seconds", [])),
            "loudness": scan.get("loudness"),
        },
        findings=audio_findings,
        warnings=audio_warnings,
        completed_at=completed_at,
        run_id=f"audio-{run_seed[:24]}",
    )
    rights_report = _report(
        category="rights",
        job_id=job_id,
        lane_id=lane_id,
        render_id=render_id,
        render_sha256=render_sha256,
        bindings={
            **common_bindings,
            "rights_manifest_sha256": artifact_sha256(rights),
            "frozen_media_manifest_sha256": artifact_sha256(frozen),
            "shotlist_sha256": artifact_sha256(shotlist),
            **(
                {
                    "project_manifest_sha256": artifact_sha256(project),
                    "program_audio_manifest_sha256": artifact_sha256(program_audio),
                }
                if project is not None and program_audio is not None
                else {}
            ),
            **(
                {
                    "source_audio_manifest_sha256": source_audio_sha256,
                    "source_audio_segment_bindings_sha256": source_segment_bindings_sha256,
                }
                if source_audio_sha256 is not None
                and source_segment_bindings_sha256 is not None
                else {}
            ),
        },
        metrics={
            "rights_assets_total": len(rights.get("assets", [])),
            "frozen_assets_total": len(frozen.get("assets", [])),
            "shot_assets_used": len(used_ids),
            "missing_asset_ids": list(missing),
            "source_audio_segments": (
                len(source_audio_segments(source_audio)) if source_audio is not None else 0
            ),
        },
        findings=rights_findings,
        warnings=[],
        completed_at=completed_at,
        run_id=f"rights-{run_seed[:24]}",
    )
    reports = [technical_report, audio_report, rights_report]
    evidence = {
        report["category"]: persist_report(report) for report in reports
    }
    artifact = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "lane_id": lane_id,
        "render_id": render_id,
        "render_sha256": render_sha256,
        "reports": reports,
        "evidence": evidence,
        "created_at": completed_at,
    }
    validate_artifact("qc_auto_evidence_manifest", artifact)
    return {
        "artifact": artifact,
        "render_output_path": str(output),
        "media_qc_report_path": (
            media_qc.get("cache", {}).get("report_path")
            if isinstance(media_qc.get("cache"), Mapping)
            else None
        ),
    }


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    return emit_main(handle_task, stdin=stdin, stdout=stdout)


if __name__ == "__main__":
    raise SystemExit(main())
