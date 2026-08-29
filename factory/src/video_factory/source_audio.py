"""Shared invariants for immutable motivation source-audio programs."""

from __future__ import annotations

import hashlib
import math
import wave
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError


_CHUNK_BYTES = 1024 * 1024
_SAMPLE_RATE = 48_000


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValidationError(f"cannot hash source-audio file {path}: {exc}") from exc
    return digest.hexdigest()


def is_multisource_manifest(manifest: Mapping[str, Any]) -> bool:
    return manifest.get("schema_version") == "1.1.0" and isinstance(
        manifest.get("segments"), list
    )


def source_audio_segments(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return ordered bindings for both v1.1 and legacy v1 manifests."""

    if is_multisource_manifest(manifest):
        return tuple(dict(segment) for segment in manifest["segments"])
    source_in = float(manifest["source_in_seconds"])
    source_out = float(manifest["source_out_seconds"])
    return (
        {
            "index": 0,
            "asset_id": manifest["audio_asset_id"],
            "source_video_uri_or_path": manifest["source_video_uri_or_path"],
            "source_in_seconds": source_in,
            "source_out_seconds": source_out,
            "program_in_seconds": 0.0,
            "program_out_seconds": source_out - source_in,
            "speaker_name": manifest["speaker_name"],
            "source_language": "legacy_unspecified",
            "original_transcript": manifest["transcript"],
            "transcript": manifest["transcript"],
            "bilingual_review": None,
            "rights_status": manifest["rights_status"],
            "rights_evidence": manifest["rights_evidence"],
            "extracted_audio_path": manifest["extracted_audio_path"],
            "checksums": {
                "source_video_sha256": manifest["checksums"]["source_video_sha256"],
                "extracted_audio_sha256": manifest["checksums"][
                    "extracted_audio_sha256"
                ],
                "original_transcript_sha256": manifest["checksums"][
                    "transcript_sha256"
                ],
                "transcript_sha256": manifest["checksums"]["transcript_sha256"],
                "bilingual_review_sha256": None,
            },
        },
    )


def source_audio_duration(manifest: Mapping[str, Any]) -> float:
    if is_multisource_manifest(manifest):
        segments = manifest["segments"]
        if not segments:
            raise ValidationError("multi-source audio manifest has no segments")
        return float(segments[-1]["program_out_seconds"])
    return float(manifest["source_out_seconds"]) - float(
        manifest["source_in_seconds"]
    )


def source_audio_is_publishable(manifest: Mapping[str, Any]) -> bool:
    allowed = {"consent_confirmed", "commercial_license_confirmed"}
    if is_multisource_manifest(manifest):
        return all(segment.get("rights_status") in allowed for segment in manifest["segments"])
    return manifest.get("rights_status") in allowed


def _read_pcm(path: Path, field: str) -> tuple[bytes, int]:
    if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".wav":
        raise ValidationError(f"{field} must be an existing regular WAV")
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getframerate() != _SAMPLE_RATE
                or audio.getcomptype() != "NONE"
            ):
                raise ValidationError(f"{field} must be mono 48 kHz 16-bit PCM")
            frames = audio.getnframes()
            if frames < 1:
                raise ValidationError(f"{field} must contain audio frames")
            return audio.readframes(frames), frames
    except (OSError, EOFError, wave.Error) as exc:
        raise ValidationError(f"{field} is not a readable PCM WAV") from exc


def verify_multisource_program(manifest: Mapping[str, Any]) -> Path:
    """Rebuild the segment relationship from bytes; a hidden premix cannot pass."""

    if not is_multisource_manifest(manifest):
        raise ValidationError("multi-source SourceAudioManifest is required")
    expected_pcm: list[bytes] = []
    cumulative_frames = 0
    for index, segment in enumerate(manifest["segments"]):
        raw_path = Path(str(segment["extracted_audio_path"])).expanduser()
        if not raw_path.is_absolute():
            raise ValidationError(
                f"source_audio_manifest.segments[{index}].extracted_audio_path must be absolute"
            )
        path = raw_path.resolve()
        expected_sha = segment["checksums"]["extracted_audio_sha256"]
        if sha256_file(path) != expected_sha:
            raise ValidationError(
                f"source_audio_manifest.segments[{index}] extracted hash differs from bytes"
            )
        pcm, frames = _read_pcm(
            path, f"source_audio_manifest.segments[{index}].extracted_audio_path"
        )
        program_in = cumulative_frames / _SAMPLE_RATE
        cumulative_frames += frames
        program_out = cumulative_frames / _SAMPLE_RATE
        if not math.isclose(
            float(segment["program_in_seconds"]), program_in, abs_tol=1 / _SAMPLE_RATE
        ) or not math.isclose(
            float(segment["program_out_seconds"]), program_out, abs_tol=1 / _SAMPLE_RATE
        ):
            raise ValidationError(
                f"source_audio_manifest.segments[{index}] program range differs from PCM frames"
            )
        expected_pcm.append(pcm)

    raw_program = Path(str(manifest["extracted_audio_path"])).expanduser()
    if not raw_program.is_absolute():
        raise ValidationError("source_audio_manifest.extracted_audio_path must be absolute")
    program = raw_program.resolve()
    if sha256_file(program) != manifest["checksums"]["extracted_audio_sha256"]:
        raise ValidationError("source_audio_manifest program hash differs from bytes")
    program_pcm, program_frames = _read_pcm(
        program, "source_audio_manifest.extracted_audio_path"
    )
    if program_frames != cumulative_frames or program_pcm != b"".join(expected_pcm):
        raise ValidationError(
            "source_audio_manifest program WAV is not the ordered PCM concatenation of segments"
        )
    return program
