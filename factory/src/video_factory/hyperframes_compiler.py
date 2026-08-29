"""Deterministic ShotList -> HyperFrames preview-project compiler.

This stage deliberately stops before preview approval and render.  It accepts
only schema-valid, job-bound editorial artifacts and an already verified local
FrozenMediaManifest.  Every referenced media byte is copied into one immutable,
job-scoped project tree and bound into a ProjectManifest by SHA-256.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, TextIO

from .contracts import validate_artifact
from .errors import FactoryError, ValidationError
from .media_freeze import MediaFreezeError, verify_frozen_media_manifest
from .source_audio import (
    is_multisource_manifest,
    source_audio_duration,
    source_audio_is_publishable,
    source_audio_segments,
    verify_multisource_program,
)
from .validators import canonical_json, digest_text, require_nonempty_string


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_REMOTE_MEDIA_ATTRIBUTE = re.compile(
    r"\b(?:src|href)\s*=\s*['\"]\s*(?:https?:|//)", re.IGNORECASE
)
_CHUNK_BYTES = 1024 * 1024
_CONTENT_TYPE_SUFFIXES = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
}


def _safe_id(value: Any, field: str) -> str:
    normalized = require_nonempty_string(value, field)
    if not _SAFE_ID.fullmatch(normalized) or ".." in normalized:
        raise ValidationError(f"{field} contains unsafe path characters")
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValidationError(f"cannot hash project file {path}: {exc}") from exc
    return digest.hexdigest()


def _artifact_sha256(document: Mapping[str, Any]) -> str:
    return digest_text(canonical_json(dict(document)))


def _format_seconds(value: Any) -> str:
    normalized = float(value)
    text = f"{normalized:.6f}".rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def _binding(contract: str, artifact: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "contract": contract,
        "schema_version": artifact["schema_version"],
        "idea_id": artifact["idea_id"],
        "sha256": _artifact_sha256(artifact),
    }
    if contract in {"script_package", "frozen_media_manifest"}:
        result["job_id"] = artifact["job_id"]
    return result


def _authoritative_audio_binding(audio: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract": audio["contract"],
        "schema_version": audio["artifact"]["schema_version"],
        "job_id": audio["artifact"]["job_id"],
        "sha256": _artifact_sha256(audio["artifact"]),
        "audio_sha256": audio["sha256"],
    }


def _program_audio_binding(audio: Mapping[str, Any]) -> dict[str, Any]:
    artifact = audio["artifact"]
    return {
        "contract": "program_audio_manifest",
        "schema_version": artifact["schema_version"],
        "job_id": artifact["job_id"],
        "idea_id": artifact["idea_id"],
        "lane_id": artifact["lane_id"],
        "sha256": _artifact_sha256(artifact),
        "audio_sha256": audio["sha256"],
        "project_path": audio["project_path"],
        "size_bytes": audio["size_bytes"],
    }


def _local_regular_audio(value: Any, field: str) -> Path:
    text = require_nonempty_string(value, field)
    if (_URI.match(text) and not re.match(r"^[A-Za-z]:[\\/]", text)) or text.startswith(
        "//"
    ):
        raise ValidationError(f"{field} must be a local file")
    raw = Path(text).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise ValidationError(f"{field} must be an absolute regular local file")
    path = raw.resolve()
    if not path.is_file() or path.suffix.lower() != ".wav":
        raise ValidationError(f"{field} must be a regular local WAV file")
    return path


def _spoken_text(script_package: Mapping[str, Any]) -> str:
    return " ".join(
        require_nonempty_string(segment["spoken_text"], "segment.spoken_text")
        for segment in script_package["segments"]
    ).strip()


def _resolve_authoritative_audio(
    *,
    job_id: str,
    script_package: Mapping[str, Any],
    frozen_media_manifest: Mapping[str, Any],
    audio_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    lane = script_package["lane_id"]
    duration = float(script_package["target_duration_seconds"])
    if lane == "motivation":
        contract = "source_audio_manifest"
        validate_artifact(contract, dict(audio_manifest))
        if audio_manifest["job_id"] != job_id or audio_manifest["lane"] != "motivation":
            raise ValidationError("SourceAudioManifest is not bound to the compiler job/lane")
        if not source_audio_is_publishable(audio_manifest):
            raise ValidationError("motivation source audio is not cleared for production")
        frozen_root = Path(frozen_media_manifest["frozen_root"]).expanduser().resolve()
        for index, segment in enumerate(source_audio_segments(audio_manifest)):
            selected = [
                item
                for item in frozen_media_manifest["assets"]
                if item["asset_id"] == segment["asset_id"]
            ]
            if len(selected) != 1:
                raise ValidationError(
                    f"SourceAudioManifest segment {index} is not bound to one frozen asset"
                )
            frozen_source = (frozen_root / Path(selected[0]["frozen_path"])).resolve()
            declared_source = Path(
                require_nonempty_string(
                    segment["source_video_uri_or_path"],
                    f"source_audio_manifest.segments[{index}].source_video_uri_or_path",
                )
            ).expanduser().resolve()
            if declared_source != frozen_source:
                raise ValidationError(
                    f"SourceAudioManifest segment {index} source path does not match frozen media"
                )
            if (
                segment["checksums"]["source_video_sha256"] != selected[0]["sha256"]
                or _sha256_file(frozen_source) != selected[0]["sha256"]
            ):
                raise ValidationError(
                    f"SourceAudioManifest segment {index} source hash does not match frozen media"
                )
        expected_text = " ".join(_spoken_text(script_package).split())
        if " ".join(audio_manifest["transcript"].split()) != expected_text:
            raise ValidationError("motivation script is not the authoritative source transcript")
        selected_duration = source_audio_duration(audio_manifest)
        if abs(selected_duration - duration) > 0.25:
            raise ValidationError("source-audio duration does not match the composition")
        source = (
            verify_multisource_program(audio_manifest)
            if is_multisource_manifest(audio_manifest)
            else _local_regular_audio(
                audio_manifest["extracted_audio_path"],
                "source_audio_manifest.extracted_audio_path",
            )
        )
        expected_sha = audio_manifest["checksums"]["extracted_audio_sha256"]
    else:
        contract = "voice_manifest"
        validate_artifact(contract, dict(audio_manifest))
        if audio_manifest["job_id"] != job_id or audio_manifest["video_id"] != job_id:
            raise ValidationError("VoiceManifest is not bound to the compiler job")
        if audio_manifest["voice_rights_status"] not in {
            "approved_owned_voice",
            "approved_licensed_voice",
        }:
            raise ValidationError("Fish voice is not rights-approved for production")
        spoken = _spoken_text(script_package)
        if hashlib.sha256(spoken.encode("utf-8")).hexdigest() != audio_manifest["text_sha256"]:
            raise ValidationError("VoiceManifest text hash does not match ScriptPackage")
        audio_duration = float(audio_manifest["audio"]["duration_seconds"])
        tolerance = max(0.5, duration * 0.03)
        if abs(audio_duration - duration) > tolerance:
            raise ValidationError("Fish voice duration does not match the composition")
        source = _local_regular_audio(
            audio_manifest["immutable_output_path"],
            "voice_manifest.immutable_output_path",
        )
        expected_sha = audio_manifest["output_sha256"]
        if source.stat().st_size != audio_manifest["output_bytes"]:
            raise ValidationError("VoiceManifest output_bytes does not match actual audio")

    actual_sha = _sha256_file(source)
    if actual_sha != expected_sha:
        raise ValidationError(f"{contract} audio checksum does not match actual bytes")
    return {
        "contract": contract,
        "artifact": dict(audio_manifest),
        "source": source,
        "sha256": actual_sha,
        "size_bytes": source.stat().st_size,
    }


def _resolve_program_audio(
    *,
    job_id: str,
    script_package: Mapping[str, Any],
    authoritative_audio: Mapping[str, Any],
    bgm_manifest: Mapping[str, Any],
    program_audio_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    validate_artifact("bgm_manifest", dict(bgm_manifest))
    validate_artifact("program_audio_manifest", dict(program_audio_manifest))
    lane = script_package["lane_id"]
    idea_id = script_package["idea_id"]
    if (
        bgm_manifest["job_id"] != job_id
        or bgm_manifest["lane_id"] != lane
        or bgm_manifest["idea_id"] != idea_id
    ):
        raise ValidationError("BgmManifest is not bound to compiler job/idea/lane")
    if (
        program_audio_manifest["job_id"] != job_id
        or program_audio_manifest["lane_id"] != lane
        or program_audio_manifest["idea_id"] != idea_id
    ):
        raise ValidationError("ProgramAudioManifest is not bound to compiler job/idea/lane")
    expected_authority = {
        "contract": authoritative_audio["contract"],
        "manifest_sha256": _artifact_sha256(authoritative_audio["artifact"]),
        "audio_sha256": authoritative_audio["sha256"],
        "authority": "spoken_content_and_timing",
        "tts": authoritative_audio["contract"] == "voice_manifest",
    }
    if program_audio_manifest["source_authority"] != expected_authority:
        raise ValidationError(
            "ProgramAudioManifest does not preserve the exact authoritative speech"
        )
    expected_bgm = {
        "asset_id": bgm_manifest["bgm_asset_id"],
        "manifest_sha256": _artifact_sha256(bgm_manifest),
        "audio_sha256": bgm_manifest["checksums"]["immutable_wav_sha256"],
        "license_evidence_sha256": bgm_manifest["checksums"][
            "license_evidence_sha256"
        ],
        "human_approval_sha256": bgm_manifest["checksums"][
            "human_approval_sha256"
        ],
    }
    if program_audio_manifest["bgm"] != expected_bgm:
        raise ValidationError("ProgramAudioManifest does not match exact licensed BGM")
    mix = program_audio_manifest["mix"]
    if (
        mix["sidechain_ducking"] is not True
        or mix["broll_audio_muted"] is not True
        or mix["deterministic"] is not True
    ):
        raise ValidationError("ProgramAudioManifest has no accepted deterministic mix")
    source = _local_regular_audio(
        program_audio_manifest["immutable_output_path"],
        "program_audio_manifest.immutable_output_path",
    )
    if source.stat().st_size != program_audio_manifest["output_bytes"]:
        raise ValidationError("ProgramAudioManifest output_bytes does not match WAV")
    actual_sha = _sha256_file(source)
    if actual_sha != program_audio_manifest["output_sha256"]:
        raise ValidationError("ProgramAudioManifest checksum does not match WAV bytes")
    expected_duration = float(script_package["target_duration_seconds"])
    if abs(float(program_audio_manifest["audio"]["duration_seconds"]) - expected_duration) > 0.1:
        raise ValidationError("program mix duration does not match composition")
    return {
        "artifact": dict(program_audio_manifest),
        "source": source,
        "project_path": "assets/audio/program_mix.wav",
        "sha256": actual_sha,
        "size_bytes": source.stat().st_size,
    }


def _validate_artifact_bindings(
    *,
    job_id: str,
    shotlist: Mapping[str, Any],
    script_package: Mapping[str, Any],
    frozen_media_manifest: Mapping[str, Any],
) -> None:
    validate_artifact("shotlist", dict(shotlist))
    validate_artifact("script_package", dict(script_package))
    validate_artifact("frozen_media_manifest", dict(frozen_media_manifest))
    if script_package["job_id"] != job_id:
        raise ValidationError("script_package is not bound to compiler job_id")
    if frozen_media_manifest["job_id"] != job_id:
        raise ValidationError("frozen_media_manifest is not bound to compiler job_id")
    idea_ids = {
        shotlist["idea_id"],
        script_package["idea_id"],
        frozen_media_manifest["idea_id"],
    }
    if len(idea_ids) != 1:
        raise ValidationError("ShotList, ScriptPackage and FrozenMediaManifest idea_id mismatch")
    if script_package["decision"]["passed"] is not True:
        raise ValidationError("script_package has not passed its editorial gate")
    if script_package["decision"]["needs_human_review"] is not False:
        raise ValidationError("script_package still needs human review")
    if frozen_media_manifest["decision"]["passed"] is not True:
        raise ValidationError("frozen_media_manifest has not passed")
    if frozen_media_manifest["decision"]["all_hashes_verified"] is not True:
        raise ValidationError("frozen_media_manifest hashes are not verified")
    if frozen_media_manifest["decision"]["all_rights_approved"] is not True:
        raise ValidationError("frozen_media_manifest rights are not approved")
    try:
        verify_frozen_media_manifest(
            frozen_media_manifest,
            expected_job_id=job_id,
        )
    except MediaFreezeError as exc:
        raise ValidationError(f"frozen media verification failed: {exc}") from exc


def _validate_timing_binding(
    shotlist: Mapping[str, Any], script_package: Mapping[str, Any]
) -> None:
    duration = float(shotlist["duration_seconds"])
    script_duration = float(script_package["target_duration_seconds"])
    if abs(duration - script_duration) > 0.01:
        raise ValidationError(
            "ShotList duration_seconds must equal ScriptPackage target_duration_seconds"
        )

    shots = list(shotlist["shots"])
    segments = list(script_package["segments"])
    for segment in segments:
        start = float(segment["start_seconds"])
        end = float(segment["end_seconds"])
        if end > duration + 0.001:
            raise ValidationError("script segment extends beyond ShotList duration")
        if not any(float(shot["start"]) < end and float(shot["end"]) > start for shot in shots):
            raise ValidationError(
                f"script segment {segment['segment_id']!r} has no overlapping shot"
            )

    for shot in shots:
        start = float(shot["start"])
        end = float(shot["end"])
        overlapping = [
            segment
            for segment in segments
            if float(segment["start_seconds"]) < end
            and float(segment["end_seconds"]) > start
        ]
        if not overlapping:
            raise ValidationError(f"shot {shot['shot_id']!r} has no overlapping script segment")
        timed_claims = {
            claim_id
            for segment in overlapping
            for claim_id in segment["claim_ids"]
        }
        unknown_claims = sorted(set(shot["claim_ids"]) - timed_claims)
        if unknown_claims:
            raise ValidationError(
                f"shot {shot['shot_id']!r} claims are not bound to overlapping script timing: "
                + ", ".join(unknown_claims)
            )


def _relative_frozen_path(value: Any, asset_id: str) -> PurePosixPath:
    text = require_nonempty_string(value, f"frozen asset {asset_id}.frozen_path")
    if (
        "\\" in text
        or text.startswith("/")
        or _URI.match(text)
        or text.startswith("//")
    ):
        raise ValidationError(f"frozen asset {asset_id} must use a local relative path")
    relative = PurePosixPath(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(f"frozen asset {asset_id} path escapes frozen_root")
    return relative


def _media_suffix(item: Mapping[str, Any], source: Path) -> str:
    suffix = source.suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return suffix
    return _CONTENT_TYPE_SUFFIXES.get(str(item["content_type"]).lower(), ".mp4")


def _resolve_referenced_assets(
    shotlist: Mapping[str, Any], frozen_media_manifest: Mapping[str, Any]
) -> list[dict[str, Any]]:
    root_text = require_nonempty_string(
        frozen_media_manifest["frozen_root"], "frozen_media_manifest.frozen_root"
    )
    if _URI.match(root_text) and not re.match(r"^[A-Za-z]:[\\/]", root_text):
        raise ValidationError("frozen_root must be a local absolute directory")
    root = Path(root_text).expanduser().resolve()
    if not root.is_dir():
        raise ValidationError("frozen_root must be an existing local directory")
    frozen_by_id = {
        item["asset_id"]: dict(item) for item in frozen_media_manifest["assets"]
    }
    referenced_ids = sorted({shot["asset_id"] for shot in shotlist["shots"]})
    unknown = sorted(set(referenced_ids) - set(frozen_by_id))
    if unknown:
        raise ValidationError("ShotList references unfrozen assets: " + ", ".join(unknown))

    resolved: list[dict[str, Any]] = []
    for index, asset_id in enumerate(referenced_ids, start=1):
        item = frozen_by_id[asset_id]
        if not str(item["content_type"]).startswith("video/"):
            raise ValidationError(
                f"compiler v1 accepts only frozen video assets; {asset_id} is {item['content_type']}"
            )
        relative = _relative_frozen_path(item["frozen_path"], asset_id)
        source = (root / Path(*relative.parts)).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValidationError(f"frozen asset {asset_id} escapes frozen_root") from exc
        if source.is_symlink() or not source.is_file():
            raise ValidationError(f"frozen asset {asset_id} is not a regular local file")
        if source.stat().st_size != item["size_bytes"]:
            raise ValidationError(f"frozen asset {asset_id} size changed before compilation")
        if _sha256_file(source) != item["sha256"]:
            raise ValidationError(f"frozen asset {asset_id} hash changed before compilation")
        suffix = _media_suffix(item, source)
        project_path = f"assets/media/media-{index:03d}-{item['sha256'][:12]}{suffix}"
        resolved.append(
            {
                "asset_id": asset_id,
                "source": source,
                "frozen_path": relative.as_posix(),
                "project_path": project_path,
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "content_type": item["content_type"],
                "shot_ids": [
                    shot["shot_id"]
                    for shot in shotlist["shots"]
                    if shot["asset_id"] == asset_id
                ],
            }
        )
    return resolved


def _validate_gsap_source(value: str | Path) -> Path:
    text = str(value)
    if (_URI.match(text) and not re.match(r"^[A-Za-z]:[\\/]", text)) or text.startswith(
        "//"
    ):
        raise ValidationError("GSAP dependency must be a local file")
    path = Path(value).expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        raise ValidationError("GSAP dependency must be a regular local file")
    if path.stat().st_size < 1:
        raise ValidationError("GSAP dependency is empty")
    return path


def _render_index_html(
    *,
    shotlist: Mapping[str, Any],
    script_package: Mapping[str, Any],
    assets: list[dict[str, Any]],
    program_audio: Mapping[str, Any],
) -> str:
    project_path_by_asset = {item["asset_id"]: item["project_path"] for item in assets}
    duration = _format_seconds(shotlist["duration_seconds"])
    media_elements: list[str] = []
    for index, shot in enumerate(shotlist["shots"], start=1):
        start = _format_seconds(shot["start"])
        clip_duration = _format_seconds(float(shot["end"]) - float(shot["start"]))
        media_start = _format_seconds(shot.get("source_in", 0))
        source = html.escape(project_path_by_asset[shot["asset_id"]], quote=True)
        media_elements.append(
            "\n".join(
                [
                    f'    <video id="vf-video-{index:03d}" class="clip media-clip" src="{source}"',
                    f'      data-start="{start}" data-duration="{clip_duration}" data-media-start="{media_start}"',
                    '      data-track-index="0" muted playsinline></video>',
                ]
            )
        )

    captions: list[str] = []
    for index, segment in enumerate(script_package["segments"], start=1):
        start = _format_seconds(segment["start_seconds"])
        caption_duration = _format_seconds(
            float(segment["end_seconds"]) - float(segment["start_seconds"])
        )
        text = html.escape(segment["caption_text"], quote=False)
        captions.append(
            f'    <section id="vf-caption-{index:03d}" class="clip caption-clip" '
            f'data-start="{start}" data-duration="{caption_duration}" '
            f'data-track-index="2"><p>{text}</p></section>'
        )

    title = html.escape(script_package["hook"]["first_frame_text"], quote=False)
    narration_source = html.escape(program_audio["project_path"], quote=True)
    narration = (
        f'    <audio id="vf-program-mix" src="{narration_source}" '
        f'data-start="0" data-duration="{duration}" data-media-start="0" '
        'data-track-index="10" data-volume="1"></audio>'
    )
    body = "\n".join(media_elements + [narration] + captions)
    document = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=1080, height=1920" />
  <title>{title}</title>
  <script src="assets/vendor/gsap.min.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ width: 1080px; height: 1920px; margin: 0; overflow: hidden; background: #080808; }}
    body {{ font-family: Arial, sans-serif; color: #fff; }}
    #root {{ position: relative; width: 1080px; height: 1920px; overflow: hidden; background: #080808; }}
    .clip {{ position: absolute; inset: 0; }}
    .media-clip {{ width: 1080px; height: 1920px; object-fit: cover; object-position: center; }}
    .caption-clip {{ z-index: 20; display: flex; align-items: flex-end; justify-content: center; padding: 0 70px 270px; pointer-events: none; }}
    .caption-clip p {{ width: 940px; margin: 0; color: #fff; font-size: 76px; font-weight: 800; line-height: 1.04; text-align: center; text-wrap: balance; text-shadow: 0 4px 18px #000, 0 0 3px #000; }}
  </style>
</head>
<body>
  <div id="root" data-root="true" data-composition-id="main" data-start="0"
    data-width="1080" data-height="1920" data-duration="{duration}" data-fps="30">
{body}
  </div>
  <script>
    const tl = gsap.timeline({{ paused: true }});
    window.__timelines["main"] = tl;
  </script>
</body>
</html>
"""
    if _REMOTE_MEDIA_ATTRIBUTE.search(document):
        raise ValidationError("compiled HyperFrames HTML contains a remote media reference")
    if "fetch(" in document or "XMLHttpRequest" in document:
        raise ValidationError("compiled HyperFrames HTML contains a network operation")
    if document.count("gsap.timeline({ paused: true })") != 1:
        raise ValidationError("compiled HyperFrames HTML must register one paused timeline")
    return document


def _file_records(project_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(project_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValidationError(f"project tree contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(project_root).as_posix()
        records.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return records


def _write_project_tree(
    *,
    project_root: Path,
    shotlist: Mapping[str, Any],
    script_package: Mapping[str, Any],
    assets: list[dict[str, Any]],
    authoritative_audio: Mapping[str, Any],
    program_audio: Mapping[str, Any],
    gsap_source: Path,
) -> None:
    (project_root / "assets" / "media").mkdir(parents=True, exist_ok=False)
    (project_root / "assets" / "audio").mkdir(parents=True, exist_ok=False)
    (project_root / "assets" / "vendor").mkdir(parents=True, exist_ok=False)
    shutil.copyfile(gsap_source, project_root / "assets" / "vendor" / "gsap.min.js")
    for asset in assets:
        destination = project_root / Path(*PurePosixPath(asset["project_path"]).parts)
        shutil.copyfile(asset["source"], destination)
        if _sha256_file(destination) != asset["sha256"]:
            raise ValidationError(f"copied project asset {asset['asset_id']} failed hash check")
    narration_destination = project_root / Path(
        *PurePosixPath(program_audio["project_path"]).parts
    )
    shutil.copyfile(program_audio["source"], narration_destination)
    if (
        narration_destination.stat().st_size != program_audio["size_bytes"]
        or _sha256_file(narration_destination) != program_audio["sha256"]
    ):
        raise ValidationError("copied program mix failed hash check")
    index_html = _render_index_html(
        shotlist=shotlist,
        script_package=script_package,
        assets=assets,
        program_audio=program_audio,
    )
    (project_root / "index.html").write_text(index_html, encoding="utf-8", newline="\n")


def _project_manifest(
    *,
    tree_root: Path,
    manifest_project_root: Path,
    job_id: str,
    shotlist: Mapping[str, Any],
    script_package: Mapping[str, Any],
    frozen_media_manifest: Mapping[str, Any],
    assets: list[dict[str, Any]],
    authoritative_audio: Mapping[str, Any],
    program_audio: Mapping[str, Any],
) -> dict[str, Any]:
    files = _file_records(tree_root)
    manifest_assets = [
        {
            key: asset[key]
            for key in (
                "asset_id",
                "frozen_path",
                "project_path",
                "sha256",
                "size_bytes",
                "content_type",
                "shot_ids",
            )
        }
        for asset in assets
    ]
    manifest = {
        "schema_version": "1.0.0",
        "project_id": f"project-{job_id}",
        "job_id": job_id,
        "idea_id": shotlist["idea_id"],
        "lane_id": script_package["lane_id"],
        "project_root": str(manifest_project_root.resolve()),
        "entrypoint": "index.html",
        "composition": {
            "composition_id": "main",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "duration_seconds": shotlist["duration_seconds"],
        },
        "bindings": {
            "shotlist": _binding("shotlist", shotlist),
            "script_package": _binding("script_package", script_package),
            "frozen_media_manifest": _binding(
                "frozen_media_manifest", frozen_media_manifest
            ),
            "authoritative_audio": _authoritative_audio_binding(authoritative_audio),
            "program_audio": _program_audio_binding(program_audio),
        },
        "assets": manifest_assets,
        "files": files,
        "project_tree_sha256": digest_text(canonical_json(files)),
        "preview": {
            "status": "ready_for_human_review",
            "render_authorized": False,
            "human_approval_required": True,
        },
    }
    validate_artifact("project_manifest", manifest)
    return manifest


def _load_existing(final_job_root: Path, desired: Mapping[str, Any]) -> dict[str, Any]:
    if final_job_root.is_symlink() or not final_job_root.is_dir():
        raise ValidationError("existing compiler output is not a regular job directory")
    manifest_path = final_job_root / "project_manifest.json"
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError("existing compiler ProjectManifest is unreadable") from exc
    if not isinstance(existing, dict):
        raise ValidationError("existing compiler ProjectManifest must be an object")
    validate_artifact("project_manifest", existing)
    project_root = final_job_root / "project"
    if Path(existing["project_root"]).resolve() != project_root.resolve():
        raise ValidationError("existing ProjectManifest project_root is not job-scoped")
    actual_files = _file_records(project_root)
    if actual_files != existing["files"]:
        raise ValidationError("existing HyperFrames project tree was modified")
    if digest_text(canonical_json(actual_files)) != existing["project_tree_sha256"]:
        raise ValidationError("existing HyperFrames project tree hash is invalid")
    if existing != desired:
        raise ValidationError("immutable compiler job output conflicts with requested inputs")
    return existing


def compile_hyperframes_project(
    *,
    job_id: str,
    shotlist: Mapping[str, Any],
    script_package: Mapping[str, Any],
    frozen_media_manifest: Mapping[str, Any],
    authoritative_audio_manifest: Mapping[str, Any],
    bgm_manifest: Mapping[str, Any],
    program_audio_manifest: Mapping[str, Any],
    output_root: str | Path,
    gsap_source_path: str | Path,
) -> dict[str, Any]:
    """Compile one immutable, preview-ready project; never preview or render it."""

    safe_job_id = _safe_id(job_id, "job_id")
    _validate_artifact_bindings(
        job_id=safe_job_id,
        shotlist=shotlist,
        script_package=script_package,
        frozen_media_manifest=frozen_media_manifest,
    )
    _validate_timing_binding(shotlist, script_package)
    assets = _resolve_referenced_assets(shotlist, frozen_media_manifest)
    authoritative_audio = _resolve_authoritative_audio(
        job_id=safe_job_id,
        script_package=script_package,
        frozen_media_manifest=frozen_media_manifest,
        audio_manifest=authoritative_audio_manifest,
    )
    program_audio = _resolve_program_audio(
        job_id=safe_job_id,
        script_package=script_package,
        authoritative_audio=authoritative_audio,
        bgm_manifest=bgm_manifest,
        program_audio_manifest=program_audio_manifest,
    )
    gsap_source = _validate_gsap_source(gsap_source_path)

    destination = Path(output_root).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        raise ValidationError("compiler output_root must be a directory")
    final_job_root = destination / safe_job_id
    final_project_root = final_job_root / "project"
    temporary_job_root = Path(
        tempfile.mkdtemp(prefix=f".{safe_job_id}.", suffix=".tmp", dir=destination)
    )
    try:
        temporary_project_root = temporary_job_root / "project"
        temporary_project_root.mkdir()
        _write_project_tree(
            project_root=temporary_project_root,
            shotlist=shotlist,
            script_package=script_package,
            assets=assets,
            authoritative_audio=authoritative_audio,
            program_audio=program_audio,
            gsap_source=gsap_source,
        )
        manifest = _project_manifest(
            tree_root=temporary_project_root,
            manifest_project_root=final_project_root,
            job_id=safe_job_id,
            shotlist=shotlist,
            script_package=script_package,
            frozen_media_manifest=frozen_media_manifest,
            assets=assets,
            authoritative_audio=authoritative_audio,
            program_audio=program_audio,
        )
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        (temporary_job_root / "project_manifest.json").write_bytes(manifest_bytes)

        if final_job_root.exists() or final_job_root.is_symlink():
            existing = _load_existing(final_job_root, manifest)
            return {
                "artifact": existing,
                "manifest_path": str((final_job_root / "project_manifest.json").resolve()),
                "project_root": str(final_project_root.resolve()),
                "compiler_execution": {"reused": True, "render_attempted": False},
            }
        try:
            os.replace(temporary_job_root, final_job_root)
        except OSError:
            if not final_job_root.exists():
                raise
            existing = _load_existing(final_job_root, manifest)
            return {
                "artifact": existing,
                "manifest_path": str((final_job_root / "project_manifest.json").resolve()),
                "project_root": str(final_project_root.resolve()),
                "compiler_execution": {"reused": True, "render_attempted": False},
            }
        return {
            "artifact": manifest,
            "manifest_path": str((final_job_root / "project_manifest.json").resolve()),
            "project_root": str(final_project_root.resolve()),
            "compiler_execution": {"reused": False, "render_attempted": False},
        }
    finally:
        if temporary_job_root.exists():
            shutil.rmtree(temporary_job_root)


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
            f"compiler requires exactly one upstream {contract} from role={role!r}"
        )
    validate_artifact(contract, matches[0])
    return matches[0]


def _upstream_authoritative_audio(
    task: Mapping[str, Any], lane: str
) -> dict[str, Any]:
    upstream = task.get("upstream_results")
    if not isinstance(upstream, list):
        raise ValidationError("task.upstream_results must be an array")
    found: dict[str, list[dict[str, Any]]] = {"voice": [], "source_audio": []}
    for entry in upstream:
        if not isinstance(entry, Mapping) or entry.get("role") not in found:
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(artifact, dict):
            found[str(entry["role"])].append(artifact)
    expected_role = "source_audio" if lane == "motivation" else "voice"
    forbidden_role = "voice" if expected_role == "source_audio" else "source_audio"
    if len(found[expected_role]) != 1 or found[forbidden_role]:
        expected_contract = (
            "source_audio_manifest" if expected_role == "source_audio" else "voice_manifest"
        )
        raise ValidationError(
            f"compiler requires exactly one upstream {expected_contract} and no "
            f"{forbidden_role} artifact for lane={lane!r}"
        )
    return found[expected_role][0]


def handle_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if task.get("role") != "compiler":
        raise ValidationError("hyperframes_compiler accepts only role='compiler'")
    job_id = _safe_id(task.get("job_id"), "task.job_id")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "project_manifest":
        raise ValidationError(
            "compiler task must declare required_result_contract='project_manifest'"
        )
    if payload.get("job_id") != job_id:
        raise ValidationError("payload.job_id is not bound to task.job_id")
    script = _upstream_artifact(task, "script", "script_package")
    shotlist = _upstream_artifact(task, "editor", "shotlist")
    frozen = _upstream_artifact(task, "media", "frozen_media_manifest")
    if task.get("pod") != script["lane_id"] or payload.get("lane_id") != script["lane_id"]:
        raise ValidationError("compiler task lane is not bound to ScriptPackage lane_id")
    output_root = Path(
        os.environ.get(
            "VIDEO_FACTORY_HYPERFRAMES_PROJECT_ROOT",
            str(Path.home() / ".video-factory" / "hyperframes_projects"),
        )
    )
    gsap_path = os.environ.get("VIDEO_FACTORY_GSAP_PATH")
    if not gsap_path:
        raise ValidationError("VIDEO_FACTORY_GSAP_PATH is required")
    authoritative_audio = _upstream_authoritative_audio(task, script["lane_id"])
    bgm = _upstream_artifact(task, "bgm", "bgm_manifest")
    program_audio = _upstream_artifact(
        task, "audio_mix", "program_audio_manifest"
    )
    return compile_hyperframes_project(
        job_id=job_id,
        shotlist=shotlist,
        script_package=script,
        frozen_media_manifest=frozen,
        authoritative_audio_manifest=authoritative_audio,
        bgm_manifest=bgm,
        program_audio_manifest=program_audio,
        output_root=output_root,
        gsap_source_path=gsap_path,
    )


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    try:
        task = json.load(source)
        if not isinstance(task, dict):
            raise ValidationError("compiler stdin must contain one JSON object")
        result = handle_task(task)
    except (
        FactoryError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        sys.stderr.write(f"hyperframes_compiler_error:{type(exc).__name__}:{exc}\n")
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
