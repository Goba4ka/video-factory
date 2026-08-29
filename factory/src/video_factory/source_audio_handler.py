"""Trusted JSON-stdio handler for rights-bound motivation source audio.

The handler never discovers media and never synthesizes speech.  It accepts an
explicit source interval, verifies the passed rights and frozen-media artifacts
against the actual local bytes, and extracts only the original audio track to a
job-scoped PCM WAV file.
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
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, TextIO

from .contracts import validate_artifact
from .errors import FactoryError, ValidationError
from .media_freeze import MediaFreezeError, verify_frozen_media_manifest
from .media_tools import media_summary, probe_media, resolve_media_binary
from .source_audio import verify_multisource_program
from .validators import canonical_json, digest_text, require_nonempty_string


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SELECTION_FIELDS = frozenset(
    {
        "asset_id",
        "source_in_seconds",
        "source_out_seconds",
        "speaker_name",
        "transcript",
        "rights_status",
    }
)
_MULTI_SELECTION_FIELDS = _SELECTION_FIELDS | frozenset(
    {"source_language", "original_transcript", "bilingual_review"}
)
_BILINGUAL_REVIEW_FIELDS = frozenset(
    {
        "approved",
        "approved_by",
        "approved_at",
        "asset_id",
        "source_in_seconds",
        "source_out_seconds",
        "original_transcript_sha256",
        "russian_transcript_sha256",
        "review_notes",
    }
)
_PUBLISHABLE_RIGHTS = frozenset(
    {"consent_confirmed", "commercial_license_confirmed"}
)
_RIGHTS_STATUSES = _PUBLISHABLE_RIGHTS | {"internal_prototype"}
_CHUNK_BYTES = 1024 * 1024


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValidationError(f"cannot hash media file {path}: {exc}") from exc
    return digest.hexdigest()


def _upstream_artifact(task: Mapping[str, Any], role: str, contract: str) -> dict[str, Any]:
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
            f"source_audio task requires exactly one upstream {contract} from role={role!r}"
        )
    validate_artifact(contract, matches[0])
    return matches[0]


def _selection(
    raw: Mapping[str, Any], *, field: str, multi: bool
) -> dict[str, Any]:
    allowed = _MULTI_SELECTION_FIELDS if multi else _SELECTION_FIELDS
    unknown = set(raw) - allowed
    missing = allowed - set(raw)
    if unknown:
        raise ValidationError(
            f"payload.{field} contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    if missing:
        raise ValidationError(
            f"payload.{field} is missing fields: "
            + ", ".join(sorted(missing))
        )

    asset_id = require_nonempty_string(raw.get("asset_id"), f"{field}.asset_id")
    if not _SAFE_ID.fullmatch(asset_id):
        raise ValidationError(f"{field}.asset_id contains unsafe characters")

    def timestamp(field: str) -> float:
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field_name}.{field} must be a number")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValidationError(
                f"{field_name}.{field} must be finite and non-negative"
            )
        return normalized

    field_name = field
    source_in = timestamp("source_in_seconds")
    source_out = timestamp("source_out_seconds")
    if source_out <= source_in:
        raise ValidationError(
            f"{field}.source_out_seconds must be greater than source_in_seconds"
        )

    speaker = raw.get("speaker_name")
    if speaker is not None:
        speaker = require_nonempty_string(speaker, f"{field}.speaker_name")
    if multi and speaker is None:
        raise ValidationError(f"{field}.speaker_name must identify the segment speaker")
    transcript = raw.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValidationError(f"{field}.transcript must be a non-empty string")
    rights_status = require_nonempty_string(
        raw.get("rights_status"), f"{field}.rights_status"
    )
    if rights_status not in _RIGHTS_STATUSES:
        raise ValidationError(
            f"{field}.rights_status must be consent_confirmed, "
            "commercial_license_confirmed, or internal_prototype"
        )
    result = {
        "asset_id": asset_id,
        "source_in_seconds": source_in,
        "source_out_seconds": source_out,
        "speaker_name": speaker,
        "transcript": transcript,
        "rights_status": rights_status,
    }
    if multi:
        language = raw.get("source_language")
        if language not in {"ru", "en"}:
            raise ValidationError(f"{field}.source_language must be 'ru' or 'en'")
        original = raw.get("original_transcript")
        if not isinstance(original, str) or not original.strip():
            raise ValidationError(f"{field}.original_transcript must be non-empty")
        review = raw.get("bilingual_review")
        if language == "ru":
            if original != transcript or review is not None:
                raise ValidationError(
                    f"{field} Russian original/display transcripts must match and review must be null"
                )
        elif not isinstance(review, Mapping):
            raise ValidationError(f"{field} English segment requires bilingual_review")
        else:
            if set(review) != _BILINGUAL_REVIEW_FIELDS:
                raise ValidationError(
                    f"{field}.bilingual_review must contain the exact human-review fields"
                )
            if review.get("approved") is not True:
                raise ValidationError(f"{field}.bilingual_review must be approved")
            require_nonempty_string(
                review.get("approved_by"), f"{field}.bilingual_review.approved_by"
            )
            require_nonempty_string(
                review.get("review_notes"), f"{field}.bilingual_review.review_notes"
            )
            try:
                approved_at = datetime.fromisoformat(
                    str(review.get("approved_at")).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ValidationError(
                    f"{field}.bilingual_review.approved_at must be ISO-8601"
                ) from exc
            if approved_at.tzinfo is None:
                raise ValidationError(
                    f"{field}.bilingual_review.approved_at must include a timezone"
                )
            expected_review = {
                "asset_id": asset_id,
                "source_in_seconds": source_in,
                "source_out_seconds": source_out,
                "original_transcript_sha256": digest_text(original),
                "russian_transcript_sha256": digest_text(transcript),
            }
            for review_field, expected in expected_review.items():
                if review.get(review_field) != expected:
                    raise ValidationError(
                        f"{field}.bilingual_review is not bound to {review_field}"
                    )
        result.update(
            {
                "source_language": language,
                "original_transcript": original,
                "bilingual_review": dict(review) if isinstance(review, Mapping) else None,
            }
        )
    return result


def _selections(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    single_present = payload.get("source_audio_selection") is not None
    multi_present = payload.get("source_audio_selections") is not None
    if single_present == multi_present:
        raise ValidationError(
            "payload must contain exactly one of source_audio_selection or source_audio_selections"
        )
    if single_present:
        raw = payload["source_audio_selection"]
        if not isinstance(raw, Mapping):
            raise ValidationError("payload.source_audio_selection must be an object")
        return [
            _selection(raw, field="source_audio_selection", multi=False)
        ], False
    raw_items = payload["source_audio_selections"]
    if not isinstance(raw_items, list) or not 2 <= len(raw_items) <= 6:
        raise ValidationError("payload.source_audio_selections must contain 2 to 6 objects")
    selections: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            raise ValidationError(
                f"payload.source_audio_selections[{index}] must be an object"
            )
        selections.append(
            _selection(
                raw,
                field=f"source_audio_selections[{index}]",
                multi=True,
            )
        )
    return selections, True


def _selected_asset(
    rights_manifest: Mapping[str, Any],
    frozen_manifest: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    asset_id = selection["asset_id"]
    rights_matches = [
        dict(item) for item in rights_manifest["assets"] if item.get("asset_id") == asset_id
    ]
    frozen_matches = [
        dict(item) for item in frozen_manifest["assets"] if item.get("asset_id") == asset_id
    ]
    if len(rights_matches) != 1 or len(frozen_matches) != 1:
        raise ValidationError("selected source-audio asset is not uniquely rights-bound and frozen")
    rights = rights_matches[0]
    frozen = frozen_matches[0]
    if (
        rights.get("rights_status") != "approved"
        or rights.get("commercial_use") is not True
        or rights.get("modification_allowed") is not True
    ):
        raise ValidationError(
            "selected source-audio asset is not approved for commercial modified use"
        )
    if not str(frozen.get("content_type", "")).startswith("video/"):
        raise ValidationError("selected source-audio asset must be frozen video media")

    root = Path(frozen_manifest["frozen_root"]).expanduser().resolve()
    relative = Path(frozen["frozen_path"])
    source_path = (root / relative).resolve()
    if source_path.is_symlink() or not source_path.is_file():
        raise ValidationError("selected frozen source must be a regular local file")
    return rights, frozen, source_path


def _rights_evidence(rights: Mapping[str, Any], rights_status: str) -> str | None:
    if rights_status == "internal_prototype":
        return None
    receipt = rights.get("license_receipt")
    if isinstance(receipt, str) and receipt.strip():
        return receipt.strip()
    if rights_status == "commercial_license_confirmed":
        license_url = rights.get("license_url")
        if isinstance(license_url, str) and license_url.strip():
            return license_url.strip()
    raise ValidationError(
        f"selected source-audio asset lacks evidence for {rights_status}"
    )


def _validate_source_duration(source_path: Path, source_out: float) -> None:
    summary = media_summary(probe_media(source_path))
    if summary["video"] is None:
        raise ValidationError("selected source-audio input has no video stream")
    if summary["audio"] is None:
        raise ValidationError("selected source-audio input has no audio stream")
    duration = summary["duration_seconds"]
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise ValidationError("selected source-audio input has no reliable duration")
    if source_out > duration + 0.05:
        raise ValidationError(
            "source_audio_selection interval exceeds the frozen source duration"
        )


def _extract_pcm_wav(source: Path, destination: Path, start: float, end: float) -> None:
    ffmpeg = resolve_media_binary("ffmpeg")
    timeout_raw = os.environ.get("VIDEO_FACTORY_SOURCE_AUDIO_TIMEOUT_SECONDS", "600")
    try:
        timeout = float(timeout_raw)
    except ValueError as exc:
        raise ValidationError(
            "VIDEO_FACTORY_SOURCE_AUDIO_TIMEOUT_SECONDS must be numeric"
        ) from exc
    if not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValidationError(
            "VIDEO_FACTORY_SOURCE_AUDIO_TIMEOUT_SECONDS must be from 0 to 3600"
        )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-ss",
        f"{start:.6f}",
        "-t",
        f"{end - start:.6f}",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-f",
        "wav",
        str(destination),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError(f"source-audio extraction failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().replace("\r", " ").replace("\n", " ")[-1200:]
        raise ValidationError(
            f"source-audio extraction exited {completed.returncode}: {detail}"
        )


def _validate_pcm_wav(path: Path, expected_duration: float) -> tuple[int, float]:
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frames = audio.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise ValidationError("extracted source audio is not a readable PCM WAV") from exc
    if channels != 1 or sample_width != 2 or sample_rate != 48_000 or frames < 1:
        raise ValidationError("extracted source audio is not mono 48 kHz 16-bit PCM")
    actual_duration = frames / sample_rate
    tolerance = max(0.1, expected_duration * 0.02)
    if abs(actual_duration - expected_duration) > tolerance:
        raise ValidationError(
            "extracted source-audio duration does not match the selected interval"
        )
    return frames, actual_duration


def _concat_pcm_wavs(sources: list[Path], destination: Path) -> tuple[int, float]:
    total_frames = 0
    try:
        with wave.open(str(destination), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(48_000)
            target.setcomptype("NONE", "not compressed")
            for source in sources:
                with wave.open(str(source), "rb") as audio:
                    if (
                        audio.getnchannels() != 1
                        or audio.getsampwidth() != 2
                        or audio.getframerate() != 48_000
                        or audio.getcomptype() != "NONE"
                    ):
                        raise ValidationError(
                            "multi-source segment is not mono 48 kHz 16-bit PCM"
                        )
                    frames = audio.getnframes()
                    target.writeframesraw(audio.readframes(frames))
                    total_frames += frames
    except (OSError, EOFError, wave.Error) as exc:
        raise ValidationError("cannot concatenate source-audio PCM segments") from exc
    if total_frames < 1:
        raise ValidationError("multi-source program has no PCM frames")
    return total_frames, total_frames / 48_000


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def handle_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if task.get("role") != "source_audio":
        raise ValidationError("source_audio_handler accepts only role='source_audio'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "source_audio_manifest":
        raise ValidationError(
            "source_audio task must declare required_result_contract='source_audio_manifest'"
        )
    job_id = _job_id(task)
    if payload.get("job_id") != job_id:
        raise ValidationError("payload.job_id is not bound to task.job_id")
    if payload.get("lane_id") != "motivation" or task.get("pod") != "motivation":
        raise ValidationError("source_audio_handler accepts only the motivation lane")

    selections, multi = _selections(payload)
    rights_manifest = _upstream_artifact(task, "rights", "rights_manifest")
    if rights_manifest["decision"]["passed"] is not True:
        raise ValidationError("upstream rights_manifest has not passed")
    if rights_manifest["decision"]["needs_human_review"] is not False:
        raise ValidationError("upstream rights_manifest still needs human review")
    if rights_manifest["decision"]["missing_asset_ids"]:
        raise ValidationError("upstream rights_manifest still has missing assets")

    frozen_manifest = _upstream_artifact(task, "media", "frozen_media_manifest")
    if frozen_manifest["job_id"] != job_id:
        raise ValidationError("frozen_media_manifest is not bound to task.job_id")
    if frozen_manifest["idea_id"] != rights_manifest["idea_id"]:
        raise ValidationError("frozen media and rights manifests have different idea_id values")
    try:
        verify_frozen_media_manifest(
            frozen_manifest,
            rights_manifest=rights_manifest,
            expected_job_id=job_id,
        )
    except MediaFreezeError as exc:
        raise ValidationError(f"frozen media verification failed: {exc}") from exc

    selected: list[dict[str, Any]] = []
    for index, selection in enumerate(selections):
        rights, frozen, source_path = _selected_asset(
            rights_manifest, frozen_manifest, selection
        )
        expected_source_hash = frozen["sha256"]
        if _sha256_file(source_path) != expected_source_hash:
            raise ValidationError(
                f"selected frozen source hash does not match its manifest at segment {index}"
            )
        _validate_source_duration(source_path, selection["source_out_seconds"])
        selected.append(
            {
                "selection": selection,
                "rights": rights,
                "frozen": frozen,
                "source_path": source_path,
                "source_sha256": expected_source_hash,
                "rights_evidence": _rights_evidence(
                    rights, selection["rights_status"]
                ),
            }
        )

    runtime_root = _configured_root(
        "VIDEO_FACTORY_RUNTIME_ROOT", Path.home() / ".video-factory"
    )
    output_root = _configured_root(
        "VIDEO_FACTORY_SOURCE_AUDIO_OUTPUT_ROOT", runtime_root / "source_audio"
    )
    job_root = output_root / job_id
    job_root.mkdir(parents=True, exist_ok=True)
    if not multi:
        record = selected[0]
        selection = record["selection"]
        source_path = record["source_path"]
        expected_source_hash = record["source_sha256"]
        with tempfile.NamedTemporaryFile(
            prefix=".source-audio.", suffix=".wav", dir=job_root, delete=False
        ) as handle:
            temporary_wav = Path(handle.name)
        temporary_wav.unlink(missing_ok=True)
        try:
            _extract_pcm_wav(
                source_path,
                temporary_wav,
                selection["source_in_seconds"],
                selection["source_out_seconds"],
            )
            _validate_pcm_wav(
                temporary_wav,
                selection["source_out_seconds"] - selection["source_in_seconds"],
            )
            if _sha256_file(source_path) != expected_source_hash:
                raise ValidationError("selected frozen source changed during extraction")
            audio_hash = _sha256_file(temporary_wav)
            final_wav = job_root / f"{selection['asset_id']}-{audio_hash}.wav"
            if final_wav.exists():
                if not final_wav.is_file() or _sha256_file(final_wav) != audio_hash:
                    raise ValidationError("existing immutable source-audio output is invalid")
                temporary_wav.unlink(missing_ok=True)
                reused = True
            else:
                os.replace(temporary_wav, final_wav)
                reused = False

            manifest = {
                "schema_version": "1.0.0",
                "job_id": job_id,
                "lane": "motivation",
                "audio_asset_id": selection["asset_id"],
                "source_video_uri_or_path": str(source_path),
                "source_in_seconds": selection["source_in_seconds"],
                "source_out_seconds": selection["source_out_seconds"],
                "speaker_name": selection["speaker_name"],
                "transcript": selection["transcript"],
                "rights_status": selection["rights_status"],
                "rights_evidence": record["rights_evidence"],
                "original_audio_only": True,
                "tts": False,
                "extracted_audio_path": str(final_wav.resolve()),
                "checksums": {
                    "source_video_sha256": expected_source_hash,
                    "extracted_audio_sha256": audio_hash,
                    "transcript_sha256": digest_text(selection["transcript"]),
                },
                "created_at": _utc_now(),
            }
            validate_artifact("source_audio_manifest", manifest)
            manifest_path = (
                job_root / f"{selection['asset_id']}-{audio_hash}.source_audio.json"
            )
            _atomic_json(manifest_path, manifest)
            return {
                "artifact": manifest,
                "output_path": str(final_wav.resolve()),
                "manifest_path": str(manifest_path.resolve()),
                "source_audio_execution": {
                    "provider": "original_source_audio",
                    "reused": reused,
                    "source_asset_id": selection["asset_id"],
                    "source_sha256": expected_source_hash,
                    "segment_count": 1,
                },
            }
        finally:
            temporary_wav.unlink(missing_ok=True)

    temporary_paths: list[Path] = []
    segment_paths: list[Path] = []
    segment_rows: list[dict[str, Any]] = []
    segment_reused: list[bool] = []
    cumulative_frames = 0
    try:
        for index, record in enumerate(selected):
            selection = record["selection"]
            source_path = record["source_path"]
            with tempfile.NamedTemporaryFile(
                prefix=f".source-audio-{index:02d}.",
                suffix=".wav",
                dir=job_root,
                delete=False,
            ) as handle:
                temporary_segment = Path(handle.name)
            temporary_segment.unlink(missing_ok=True)
            temporary_paths.append(temporary_segment)
            _extract_pcm_wav(
                source_path,
                temporary_segment,
                selection["source_in_seconds"],
                selection["source_out_seconds"],
            )
            frames, _ = _validate_pcm_wav(
                temporary_segment,
                selection["source_out_seconds"] - selection["source_in_seconds"],
            )
            if _sha256_file(source_path) != record["source_sha256"]:
                raise ValidationError(
                    f"selected frozen source changed during extraction at segment {index}"
                )
            segment_sha = _sha256_file(temporary_segment)
            final_segment = (
                job_root
                / f"segment-{index:02d}-{selection['asset_id']}-{segment_sha}.wav"
            )
            if final_segment.exists():
                if final_segment.is_symlink() or _sha256_file(final_segment) != segment_sha:
                    raise ValidationError(
                        "existing immutable source-audio segment is invalid"
                    )
                temporary_segment.unlink(missing_ok=True)
                was_reused = True
            else:
                os.replace(temporary_segment, final_segment)
                was_reused = False
            program_in = cumulative_frames / 48_000
            cumulative_frames += frames
            program_out = cumulative_frames / 48_000
            review = selection["bilingual_review"]
            segment_rows.append(
                {
                    "index": index,
                    "asset_id": selection["asset_id"],
                    "source_video_uri_or_path": str(source_path),
                    "source_in_seconds": selection["source_in_seconds"],
                    "source_out_seconds": selection["source_out_seconds"],
                    "program_in_seconds": program_in,
                    "program_out_seconds": program_out,
                    "speaker_name": selection["speaker_name"],
                    "source_language": selection["source_language"],
                    "original_transcript": selection["original_transcript"],
                    "transcript": selection["transcript"],
                    "bilingual_review": review,
                    "rights_status": selection["rights_status"],
                    "rights_evidence": record["rights_evidence"],
                    "extracted_audio_path": str(final_segment.resolve()),
                    "checksums": {
                        "source_video_sha256": record["source_sha256"],
                        "extracted_audio_sha256": segment_sha,
                        "original_transcript_sha256": digest_text(
                            selection["original_transcript"]
                        ),
                        "transcript_sha256": digest_text(selection["transcript"]),
                        "bilingual_review_sha256": (
                            digest_text(canonical_json(review))
                            if review is not None
                            else None
                        ),
                    },
                }
            )
            segment_paths.append(final_segment)
            segment_reused.append(was_reused)

        bindings_sha = digest_text(canonical_json(segment_rows))
        with tempfile.NamedTemporaryFile(
            prefix=".source-audio-program.", suffix=".wav", dir=job_root, delete=False
        ) as handle:
            temporary_program = Path(handle.name)
        temporary_program.unlink(missing_ok=True)
        temporary_paths.append(temporary_program)
        program_frames, _ = _concat_pcm_wavs(segment_paths, temporary_program)
        if program_frames != cumulative_frames:
            raise ValidationError("multi-source program frame count changed during concat")
        for record in selected:
            if _sha256_file(record["source_path"]) != record["source_sha256"]:
                raise ValidationError("selected frozen source changed during concatenation")
        program_sha = _sha256_file(temporary_program)
        final_program = (
            job_root / f"source-audio-program-{bindings_sha[:24]}-{program_sha}.wav"
        )
        if final_program.exists():
            if final_program.is_symlink() or _sha256_file(final_program) != program_sha:
                raise ValidationError("existing immutable source-audio program is invalid")
            temporary_program.unlink(missing_ok=True)
            program_reused = True
        else:
            os.replace(temporary_program, final_program)
            program_reused = False
        transcript = "\n".join(row["transcript"] for row in segment_rows)
        rights_statuses = [row["rights_status"] for row in segment_rows]
        aggregate_rights_status = (
            "internal_prototype"
            if "internal_prototype" in rights_statuses
            else "consent_confirmed"
            if "consent_confirmed" in rights_statuses
            else "commercial_license_confirmed"
        )
        manifest = {
            "schema_version": "1.1.0",
            "job_id": job_id,
            "lane": "motivation",
            "audio_asset_id": f"source-audio-program-{bindings_sha[:24]}",
            "segment_count": len(segment_rows),
            "segments": segment_rows,
            "transcript": transcript,
            "rights_status": aggregate_rights_status,
            "original_audio_only": True,
            "tts": False,
            "extracted_audio_path": str(final_program.resolve()),
            "checksums": {
                "extracted_audio_sha256": program_sha,
                "transcript_sha256": digest_text(transcript),
                "segment_bindings_sha256": bindings_sha,
            },
            "created_at": _utc_now(),
        }
        validate_artifact("source_audio_manifest", manifest)
        verify_multisource_program(manifest)
        manifest_path = job_root / f"{manifest['audio_asset_id']}.source_audio.json"
        _atomic_json(manifest_path, manifest)
        return {
            "artifact": manifest,
            "output_path": str(final_program.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "source_audio_execution": {
                "provider": "original_source_audio",
                "reused": program_reused and all(segment_reused),
                "segment_count": len(segment_rows),
                "source_asset_ids": [row["asset_id"] for row in segment_rows],
                "source_sha256": [
                    row["checksums"]["source_video_sha256"] for row in segment_rows
                ],
            },
        }
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)


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
        json.JSONDecodeError,
    ) as exc:
        sys.stderr.write(f"source_audio_handler_error:{type(exc).__name__}:{exc}\n")
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
