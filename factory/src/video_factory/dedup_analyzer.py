"""Perceptual duplicate and reused-sequence analyzer for final renders.

The analyzer fingerprints pixels decoded from the checksum-bound render.  It
then compares every sampled frame against every entry in a checksum-bound,
non-empty corpus snapshot.  Exact duplicates, high-ratio perceptual copies and
long reused sequences are independently blocking findings.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ValidationError
from .qc_analyzer_common import (
    FrameExtractor,
    GrayFrame,
    artifact_sha256,
    bind_render,
    checker_run_id,
    evidence_file,
    extract_gray_frames,
    hex64_value,
    load_json_object,
    safe_id,
    sha256_file,
    sha256_value,
    utc_now,
    validate_qc_analyzer_report,
    write_report,
)


DEDUP_ANALYZER_VERSION = "1.0.0"
DEDUP_ALGORITHM = "dhash-64-v1"
DEFAULT_THRESHOLDS = {
    "frame_hamming_distance": 6,
    "near_duplicate_ratio": 0.80,
    "reuse_sequence_seconds": 5.0,
    "reuse_ratio": 0.25,
}
DEFAULT_SAMPLE_INTERVAL_SECONDS = 1.0
MINIMUM_SAMPLE_FRAMES = 8


def dhash64(frame: GrayFrame) -> str:
    """Return a 64-bit difference hash for one 9x8 grayscale frame."""

    if frame.width != 9 or frame.height != 8 or len(frame.pixels) != 72:
        raise ValidationError("dhash64 requires one complete 9x8 grayscale frame")
    value = 0
    for y in range(8):
        offset = y * 9
        for x in range(8):
            value = (value << 1) | int(
                frame.pixels[offset + x] > frame.pixels[offset + x + 1]
            )
    return f"{value:016x}"


def hamming_distance(first: str, second: str) -> int:
    return (int(hex64_value(first, "first_hash"), 16) ^ int(hex64_value(second, "second_hash"), 16)).bit_count()


def fingerprint_frames(frames: Sequence[GrayFrame]) -> list[str]:
    if len(frames) < MINIMUM_SAMPLE_FRAMES:
        raise ValidationError(
            f"dedup analysis requires at least {MINIMUM_SAMPLE_FRAMES} decoded samples"
        )
    if [frame.index for frame in frames] != list(range(len(frames))):
        raise ValidationError("dedup frames must be contiguous and ordered")
    return [dhash64(frame) for frame in frames]


def _number(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValidationError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValidationError(f"{field} must be from {minimum} to {maximum}")
    return number


def _validate_thresholds(value: Mapping[str, Any] | None) -> dict[str, float | int]:
    thresholds = dict(DEFAULT_THRESHOLDS if value is None else value)
    if set(thresholds) != set(DEFAULT_THRESHOLDS):
        raise ValidationError("dedup thresholds must contain exactly the supported fields")
    distance = thresholds["frame_hamming_distance"]
    if not isinstance(distance, int) or isinstance(distance, bool) or not 0 <= distance <= 16:
        raise ValidationError("frame_hamming_distance must be an integer from 0 to 16")
    return {
        "frame_hamming_distance": distance,
        "near_duplicate_ratio": _number(
            thresholds["near_duplicate_ratio"],
            "near_duplicate_ratio",
            minimum=0.50,
            maximum=1.0,
        ),
        "reuse_sequence_seconds": _number(
            thresholds["reuse_sequence_seconds"],
            "reuse_sequence_seconds",
            minimum=2.0,
            maximum=30.0,
        ),
        "reuse_ratio": _number(
            thresholds["reuse_ratio"],
            "reuse_ratio",
            minimum=0.10,
            maximum=1.0,
        ),
    }


def _load_corpus(
    descriptor: Mapping[str, Any], *, sample_interval_seconds: float
) -> tuple[dict[str, Any], str]:
    path, snapshot_sha256 = evidence_file(
        descriptor,
        field="corpus_snapshot",
        max_bytes=16 * 1024 * 1024,
        suffix=".json",
    )
    value = load_json_object(path, field="corpus_snapshot")
    if set(value) != {
        "schema_version",
        "snapshot_id",
        "generated_at",
        "algorithm",
        "sample_interval_seconds",
        "entries",
    }:
        raise ValidationError("corpus_snapshot has invalid top-level fields")
    if value.get("schema_version") != "1.0.0" or value.get("algorithm") != DEDUP_ALGORITHM:
        raise ValidationError("corpus_snapshot version or algorithm is unsupported")
    safe_id(value.get("snapshot_id"), "corpus_snapshot.snapshot_id")
    generated = value.get("generated_at")
    if not isinstance(generated, str):
        raise ValidationError("corpus_snapshot.generated_at must be ISO 8601")
    try:
        parsed = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("corpus_snapshot.generated_at must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValidationError("corpus_snapshot.generated_at must include a timezone")
    interval = _number(
        value.get("sample_interval_seconds"),
        "corpus_snapshot.sample_interval_seconds",
        minimum=0.1,
        maximum=10.0,
    )
    if abs(interval - sample_interval_seconds) > 1e-9:
        raise ValidationError("corpus_snapshot sample interval does not match analyzer")
    entries = value.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValidationError("corpus_snapshot.entries must be non-empty")
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or set(entry) != {
            "comparison_id",
            "job_id",
            "render_id",
            "render_sha256",
            "frame_hashes",
        }:
            raise ValidationError(f"corpus_snapshot.entries[{index}] has invalid fields")
        comparison_id = safe_id(
            entry.get("comparison_id"), f"corpus_snapshot.entries[{index}].comparison_id"
        )
        if comparison_id in seen_ids:
            raise ValidationError("corpus_snapshot comparison_id values must be unique")
        seen_ids.add(comparison_id)
        safe_id(entry.get("job_id"), f"corpus_snapshot.entries[{index}].job_id")
        safe_id(entry.get("render_id"), f"corpus_snapshot.entries[{index}].render_id")
        sha256_value(
            entry.get("render_sha256"),
            f"corpus_snapshot.entries[{index}].render_sha256",
        )
        hashes = entry.get("frame_hashes")
        if not isinstance(hashes, list) or len(hashes) < MINIMUM_SAMPLE_FRAMES:
            raise ValidationError(
                f"corpus_snapshot.entries[{index}] requires at least eight frame hashes"
            )
        for hash_index, frame_hash in enumerate(hashes):
            hex64_value(
                frame_hash,
                f"corpus_snapshot.entries[{index}].frame_hashes[{hash_index}]",
            )
    return value, snapshot_sha256


def _comparison(
    current_hashes: Sequence[str],
    entry: Mapping[str, Any],
    *,
    render_sha256: str,
    interval_seconds: float,
    thresholds: Mapping[str, float | int],
) -> dict[str, Any]:
    reference = entry["frame_hashes"]
    limit = int(thresholds["frame_hamming_distance"])
    minimum_length = min(len(current_hashes), len(reference))

    best_match_count = 0
    best_overlap_count = 0
    best_offset = 0
    best_distance_sum = 0
    for offset in range(-(len(reference) - 1), len(current_hashes)):
        pairs: list[tuple[int, int]] = []
        for current_index in range(len(current_hashes)):
            reference_index = current_index - offset
            if 0 <= reference_index < len(reference):
                pairs.append((current_index, reference_index))
        if len(pairs) < MINIMUM_SAMPLE_FRAMES:
            continue
        distances = [
            hamming_distance(current_hashes[current], reference[other])
            for current, other in pairs
        ]
        matches = sum(distance <= limit for distance in distances)
        if (matches, len(pairs), -abs(offset)) > (
            best_match_count,
            best_overlap_count,
            -abs(best_offset),
        ):
            best_match_count = matches
            best_overlap_count = len(pairs)
            best_offset = offset
            best_distance_sum = sum(distances)

    previous = [0] * (len(reference) + 1)
    longest = 0
    for current_hash in current_hashes:
        row = [0] * (len(reference) + 1)
        for index, reference_hash in enumerate(reference, start=1):
            if hamming_distance(current_hash, reference_hash) <= limit:
                row[index] = previous[index - 1] + 1
                longest = max(longest, row[index])
        previous = row

    match_ratio = best_match_count / minimum_length
    reuse_ratio = longest / minimum_length
    exact = entry["render_sha256"] == render_sha256
    near = not exact and match_ratio >= float(thresholds["near_duplicate_ratio"])
    reused = (
        not exact
        and not near
        and longest * interval_seconds >= float(thresholds["reuse_sequence_seconds"])
        and reuse_ratio >= float(thresholds["reuse_ratio"])
    )
    return {
        "comparison_id": entry["comparison_id"],
        "job_id": entry["job_id"],
        "render_id": entry["render_id"],
        "render_sha256": entry["render_sha256"],
        "exact_duplicate": exact,
        "near_duplicate": near,
        "reused_sequence": reused,
        "best_alignment_offset_frames": best_offset,
        "aligned_frame_count": best_overlap_count,
        "matching_frame_count": best_match_count,
        "perceptual_match_ratio": round(match_ratio, 6),
        "mean_aligned_hamming_distance": (
            round(best_distance_sum / best_overlap_count, 6)
            if best_overlap_count
            else None
        ),
        "longest_reused_sequence_frames": longest,
        "longest_reused_sequence_seconds": round(longest * interval_seconds, 6),
        "reuse_ratio": round(reuse_ratio, 6),
    }


def analyze_dedup(
    render_path: str | Path,
    render_manifest: Mapping[str, Any],
    corpus_snapshot: Mapping[str, Any],
    *,
    lane_id: str,
    report_path: str | Path,
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    thresholds: Mapping[str, Any] | None = None,
    frame_extractor: FrameExtractor = extract_gray_frames,
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    """Analyze one render against a frozen perceptual comparison corpus."""

    interval = _number(
        sample_interval_seconds,
        "sample_interval_seconds",
        minimum=0.25,
        maximum=5.0,
    )
    policy = _validate_thresholds(thresholds)
    path, render_sha256, job_id, render_id = bind_render(render_path, render_manifest)
    lane = safe_id(lane_id, "lane_id")
    render_manifest_sha256 = artifact_sha256(render_manifest)
    corpus, corpus_sha256 = _load_corpus(
        corpus_snapshot, sample_interval_seconds=interval
    )
    duration = float(render_manifest["technical"]["duration_seconds"])
    maximum_frames = min(400, max(MINIMUM_SAMPLE_FRAMES, math.ceil(duration / interval) + 2))
    frames = frame_extractor(
        path,
        interval_seconds=interval,
        width=9,
        height=8,
        maximum_frames=maximum_frames,
    )
    frame_hashes = fingerprint_frames(frames)
    comparisons = [
        _comparison(
            frame_hashes,
            entry,
            render_sha256=render_sha256,
            interval_seconds=interval,
            thresholds=policy,
        )
        for entry in corpus["entries"]
    ]
    exact_count = sum(bool(item["exact_duplicate"]) for item in comparisons)
    near_count = sum(bool(item["near_duplicate"]) for item in comparisons)
    reuse_count = sum(bool(item["reused_sequence"]) for item in comparisons)
    findings: list[dict[str, Any]] = []
    for index, comparison in enumerate(comparisons):
        if comparison["exact_duplicate"]:
            findings.append(
                {
                    "code": "exact_render_duplicate",
                    "message": f"render bytes duplicate corpus item {comparison['comparison_id']}",
                    "observation_refs": [index],
                }
            )
        elif comparison["near_duplicate"]:
            findings.append(
                {
                    "code": "perceptual_near_duplicate",
                    "message": (
                        f"perceptual match ratio {comparison['perceptual_match_ratio']:.3f} "
                        f"exceeds threshold for {comparison['comparison_id']}"
                    ),
                    "observation_refs": [index],
                }
            )
        elif comparison["reused_sequence"]:
            findings.append(
                {
                    "code": "reused_visual_sequence",
                    "message": (
                        f"reused sequence {comparison['longest_reused_sequence_seconds']:.3f}s "
                        f"detected against {comparison['comparison_id']}"
                    ),
                    "observation_refs": [index],
                }
            )
    settings = {"sample_interval_seconds": interval, "thresholds": policy}
    report = {
        "schema_version": "1.0.0",
        "category": "dedup",
        "job_id": job_id,
        "lane_id": lane,
        "render_id": render_id,
        "render_sha256": render_sha256,
        "status": "fail" if findings else "pass",
        "needs_human_review": False,
        "warnings": [],
        "findings": findings,
        "checker": {
            "name": "video_factory.dedup_analyzer",
            "version": DEDUP_ANALYZER_VERSION,
            "run_id": checker_run_id("dedup", render_sha256, corpus_sha256, settings),
        },
        "completed_at": utc_now(completed_at),
        "bindings": {
            "output_sha256": render_sha256,
            "render_manifest_sha256": render_manifest_sha256,
            "corpus_snapshot_sha256": corpus_sha256,
        },
        "metrics": {
            "algorithm": DEDUP_ALGORITHM,
            "sample_interval_seconds": interval,
            "sampled_frame_count": len(frames),
            "frame_hashes": frame_hashes,
            "thresholds": policy,
            "corpus_entry_count": len(corpus["entries"]),
            "comparisons": comparisons,
            "summary": {
                "exact_duplicate_count": exact_count,
                "near_duplicate_count": near_count,
                "reused_sequence_count": reuse_count,
            },
        },
    }
    validate_qc_analyzer_report(report)
    stored_report = write_report(report, report_path)
    return {
        "artifact": report,
        "evidence": {
            "path": str(stored_report),
            "sha256": sha256_file(stored_report),
        },
    }


__all__ = [
    "DEDUP_ALGORITHM",
    "DEFAULT_SAMPLE_INTERVAL_SECONDS",
    "DEFAULT_THRESHOLDS",
    "analyze_dedup",
    "dhash64",
    "fingerprint_frames",
    "hamming_distance",
]
