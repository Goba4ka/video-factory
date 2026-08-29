from __future__ import annotations

import hashlib
import struct
import wave
from pathlib import Path
from typing import Mapping, Sequence

from video_factory.validators import canonical_json, digest_text


SAMPLE_RATE = 48_000


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pcm(path: Path, frames: int, sample: int) -> bytes:
    pcm = struct.pack("<h", sample) * frames
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(pcm)
    return pcm


def _write_program(path: Path, chunks: Sequence[bytes]) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        for chunk in chunks:
            audio.writeframesraw(chunk)


def build_multisource_manifest(
    root: Path,
    *,
    job_id: str,
    frozen_root: Path,
    frozen_assets: Sequence[Mapping[str, object]],
    transcript_parts: Sequence[str],
    durations: Sequence[float],
    idea_id: str | None = None,
) -> dict:
    """Build a byte-valid v1.1 fixture; callers still provide real frozen bytes."""

    if not (
        len(frozen_assets) == len(transcript_parts) == len(durations)
        and 2 <= len(durations) <= 6
    ):
        raise ValueError("fixture requires 2-6 aligned source-audio segments")
    source_root = root / "multi-source-audio"
    source_root.mkdir(parents=True, exist_ok=True)
    segments: list[dict] = []
    chunks: list[bytes] = []
    cumulative_frames = 0
    for index, (asset, transcript, duration) in enumerate(
        zip(frozen_assets, transcript_parts, durations, strict=True)
    ):
        frames = int(round(float(duration) * SAMPLE_RATE))
        if frames < 1:
            raise ValueError("segment duration must contain PCM frames")
        segment_path = (source_root / f"segment-{index:02d}.wav").resolve()
        pcm = _write_pcm(segment_path, frames, 500 + index * 250)
        chunks.append(pcm)
        program_in = cumulative_frames / SAMPLE_RATE
        cumulative_frames += frames
        program_out = cumulative_frames / SAMPLE_RATE
        frozen_path = (frozen_root / str(asset["frozen_path"])).resolve()
        segments.append(
            {
                "index": index,
                "asset_id": asset["asset_id"],
                "source_video_uri_or_path": str(frozen_path),
                "source_in_seconds": 0,
                "source_out_seconds": frames / SAMPLE_RATE,
                "program_in_seconds": program_in,
                "program_out_seconds": program_out,
                "speaker_name": f"Licensed speaker {index + 1}",
                "source_language": "ru",
                "original_transcript": transcript,
                "transcript": transcript,
                "bilingual_review": None,
                "rights_status": "commercial_license_confirmed",
                "rights_evidence": "receipt-001",
                "extracted_audio_path": str(segment_path),
                "checksums": {
                    "source_video_sha256": asset["sha256"],
                    "extracted_audio_sha256": file_sha256(segment_path),
                    "original_transcript_sha256": digest_text(transcript),
                    "transcript_sha256": digest_text(transcript),
                    "bilingual_review_sha256": None,
                },
            }
        )
    program_path = (source_root / "program.wav").resolve()
    _write_program(program_path, chunks)
    transcript = "\n".join(transcript_parts)
    segment_bindings_sha256 = digest_text(canonical_json(segments))
    return {
        "schema_version": "1.1.0",
        "job_id": job_id,
        "lane": "motivation",
        "audio_asset_id": f"source-audio-program-{segment_bindings_sha256[:24]}",
        "segment_count": len(segments),
        "segments": segments,
        "transcript": transcript,
        "rights_status": "commercial_license_confirmed",
        "original_audio_only": True,
        "tts": False,
        "extracted_audio_path": str(program_path),
        "checksums": {
            "extracted_audio_sha256": file_sha256(program_path),
            "transcript_sha256": digest_text(transcript),
            "segment_bindings_sha256": segment_bindings_sha256,
        },
        "created_at": "2026-08-30T10:00:00Z",
    }
