"""Shared primitives for checksum-bound, frame-observing QC analyzers.

The helpers in this module deliberately do not accept a caller supplied pass
decision.  They bind a render to its manifest checksum and obtain actual pixel
bytes from FFmpeg.  Category analyzers derive their own findings from those
bytes and may only emit ``pass`` when every mandatory observation exists.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import validate_artifact
from .errors import ValidationError
from .media_tools import resolve_media_binary
from .validators import canonical_json, require_nonempty_string


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_HEX64 = re.compile(r"^[a-f0-9]{16}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "category",
        "job_id",
        "lane_id",
        "render_id",
        "render_sha256",
        "status",
        "needs_human_review",
        "warnings",
        "findings",
        "checker",
        "completed_at",
        "bindings",
        "metrics",
    }
)


@dataclass(frozen=True)
class GrayFrame:
    """One sampled frame with deterministic timeline and immutable pixel bytes."""

    index: int
    timestamp_seconds: float
    width: int
    height: int
    pixels: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.pixels).hexdigest()


FrameExtractor = Callable[..., list[GrayFrame]]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ValidationError(f"cannot hash QC input {path}: {exc}") from exc
    return digest.hexdigest()


def safe_id(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    if not _SAFE_ID.fullmatch(text) or ".." in text:
        raise ValidationError(f"{field} contains unsafe characters")
    return text


def sha256_value(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    if not _SHA256.fullmatch(text):
        raise ValidationError(f"{field} must be lowercase SHA-256")
    return text


def hex64_value(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    if not _HEX64.fullmatch(text):
        raise ValidationError(f"{field} must be a 64-bit lowercase hex hash")
    return text


def artifact_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


def bind_render(
    render_path: str | Path, render_manifest: Mapping[str, Any]
) -> tuple[Path, str, str, str]:
    """Validate the render contract and bind it to immutable local bytes."""

    if not isinstance(render_manifest, Mapping):
        raise ValidationError("render_manifest must be an object")
    manifest = dict(render_manifest)
    validate_artifact("render_manifest", manifest)
    raw_path = Path(render_path).expanduser()
    if raw_path.is_symlink():
        raise ValidationError("render_path must not be a symlink")
    path = raw_path.resolve()
    if not path.is_file():
        raise ValidationError(f"render_path does not exist: {path}")
    actual = sha256_file(path)
    expected = sha256_value(manifest.get("output_sha256"), "render_manifest.output_sha256")
    if actual != expected:
        raise ValidationError("render bytes do not match render_manifest.output_sha256")
    return (
        path,
        actual,
        safe_id(manifest.get("job_id"), "render_manifest.job_id"),
        safe_id(manifest.get("render_id"), "render_manifest.render_id"),
    )


def evidence_file(
    descriptor: Mapping[str, Any],
    *,
    field: str,
    max_bytes: int,
    suffix: str | None = None,
) -> tuple[Path, str]:
    """Resolve a checksum descriptor without following a final symlink."""

    if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256"}:
        raise ValidationError(f"{field} must contain exactly path and sha256")
    raw = Path(require_nonempty_string(descriptor.get("path"), f"{field}.path")).expanduser()
    if not raw.is_absolute():
        raise ValidationError(f"{field}.path must be absolute")
    if raw.is_symlink():
        raise ValidationError(f"{field}.path must not be a symlink")
    path = raw.resolve()
    if not path.is_file():
        raise ValidationError(f"{field}.path does not exist")
    if suffix is not None and path.suffix.lower() != suffix.lower():
        raise ValidationError(f"{field}.path must end with {suffix}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValidationError(f"cannot inspect {field}.path: {exc}") from exc
    if size <= 0 or size > max_bytes:
        raise ValidationError(f"{field}.path must contain 1..{max_bytes} bytes")
    expected = sha256_value(descriptor.get("sha256"), f"{field}.sha256")
    if sha256_file(path) != expected:
        raise ValidationError(f"{field} checksum does not match actual bytes")
    return path, expected


def load_json_object(path: Path, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{field} is not readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must contain one JSON object")
    return value


def extract_gray_frames(
    render_path: Path,
    *,
    interval_seconds: float,
    width: int,
    height: int,
    maximum_frames: int,
) -> list[GrayFrame]:
    """Decode deterministic grayscale samples from the actual render via FFmpeg."""

    if not 0.1 <= interval_seconds <= 10.0:
        raise ValidationError("frame interval_seconds must be from 0.1 to 10.0")
    if not 8 <= width <= 1080 or not 8 <= height <= 1920:
        raise ValidationError("frame extraction dimensions are outside safe limits")
    if not 1 <= maximum_frames <= 1000:
        raise ValidationError("maximum_frames must be from 1 to 1000")
    ffmpeg = resolve_media_binary("ffmpeg")
    fps = 1.0 / interval_seconds
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(render_path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        f"fps={fps:.12g},scale={width}:{height}:flags=area,format=gray",
        "-frames:v",
        str(maximum_frames),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError(f"FFmpeg frame observation failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")
        detail = detail.strip().replace("\r", " ").replace("\n", " ")[-1200:]
        raise ValidationError(
            f"FFmpeg frame observation exited {completed.returncode}: {detail}"
        )
    frame_bytes = width * height
    if not completed.stdout or len(completed.stdout) % frame_bytes:
        raise ValidationError("FFmpeg returned missing or truncated frame bytes")
    count = len(completed.stdout) // frame_bytes
    if count > maximum_frames:
        raise ValidationError("FFmpeg exceeded maximum frame observation count")
    return [
        GrayFrame(
            index=index,
            timestamp_seconds=round(index * interval_seconds, 6),
            width=width,
            height=height,
            pixels=completed.stdout[index * frame_bytes : (index + 1) * frame_bytes],
        )
        for index in range(count)
    ]


def utc_now(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValidationError("completed_at source must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def checker_run_id(
    category: str, render_sha256: str, binding_sha256: str, settings: Mapping[str, Any]
) -> str:
    source = canonical_json(
        {
            "category": category,
            "render_sha256": render_sha256,
            "binding_sha256": binding_sha256,
            "settings": dict(settings),
        }
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def write_report(report: Mapping[str, Any], output_path: str | Path) -> Path:
    """Atomically persist one report after strict in-memory validation."""

    validate_qc_analyzer_report(report)
    raw_path = Path(output_path).expanduser()
    if not raw_path.is_absolute():
        raise ValidationError("analyzer report output_path must be absolute")
    if raw_path.is_symlink():
        raise ValidationError("analyzer report output_path must not be a symlink")
    path = raw_path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValidationError(f"cannot write analyzer report {path}: {exc}") from exc
    return path


def _number(value: Any, field: str, *, minimum: float | None = None) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValidationError(f"{field} must be a finite number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ValidationError(f"{field} must be at least {minimum}")
    return number


def validate_qc_analyzer_report(report: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the generic report and category-specific evidence invariants.

    This is intentionally stricter than a JSON shape check: ``warn`` and
    ``not_run`` are never acceptable analyzer outcomes, and a bare ``pass``
    without sufficient measured observations is rejected.
    """

    if not isinstance(report, Mapping) or set(report) != REPORT_FIELDS:
        raise ValidationError("qc_analyzer_report has invalid top-level fields")
    if report.get("schema_version") != "1.0.0":
        raise ValidationError("qc_analyzer_report.schema_version must be 1.0.0")
    category = report.get("category")
    if category not in {"dedup", "visual"}:
        raise ValidationError("qc_analyzer_report.category must be dedup or visual")
    safe_id(report.get("job_id"), "qc_analyzer_report.job_id")
    safe_id(report.get("lane_id"), "qc_analyzer_report.lane_id")
    safe_id(report.get("render_id"), "qc_analyzer_report.render_id")
    render_sha256 = sha256_value(
        report.get("render_sha256"), "qc_analyzer_report.render_sha256"
    )
    status = report.get("status")
    if status not in {"pass", "fail"}:
        raise ValidationError("qc_analyzer_report status warn/not_run is fail-closed")
    if report.get("needs_human_review") is not False:
        raise ValidationError("automated analyzer report cannot defer a gate to human review")
    if report.get("warnings") != []:
        raise ValidationError("qc_analyzer_report warnings are fail-closed")
    findings = report.get("findings")
    if not isinstance(findings, list):
        raise ValidationError("qc_analyzer_report.findings must be an array")
    if (status == "pass") != (findings == []):
        raise ValidationError("qc_analyzer_report status and findings are inconsistent")
    for index, finding in enumerate(findings):
        if not isinstance(finding, Mapping) or set(finding) != {
            "code",
            "message",
            "observation_refs",
        }:
            raise ValidationError(f"findings[{index}] has invalid fields")
        safe_id(finding.get("code"), f"findings[{index}].code")
        require_nonempty_string(finding.get("message"), f"findings[{index}].message")
        refs = finding.get("observation_refs")
        if not isinstance(refs, list) or not refs or not all(
            isinstance(item, int) and item >= 0 for item in refs
        ):
            raise ValidationError(
                f"findings[{index}].observation_refs must contain frame/comparison indexes"
            )
    checker = report.get("checker")
    if not isinstance(checker, Mapping) or set(checker) != {"name", "version", "run_id"}:
        raise ValidationError("qc_analyzer_report.checker is invalid")
    for field in ("name", "version", "run_id"):
        require_nonempty_string(checker.get(field), f"checker.{field}")
    completed = require_nonempty_string(report.get("completed_at"), "completed_at")
    try:
        parsed = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("completed_at must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValidationError("completed_at must include a timezone")
    bindings = report.get("bindings")
    expected_binding_fields = (
        {
            "output_sha256",
            "render_manifest_sha256",
            "corpus_snapshot_sha256",
        }
        if category == "dedup"
        else {
            "output_sha256",
            "render_manifest_sha256",
            "shotlist_sha256",
            "contact_sheet_sha256",
        }
    )
    if not isinstance(bindings, Mapping) or set(bindings) != expected_binding_fields:
        raise ValidationError(f"{category}.bindings has invalid fields")
    for field, value in bindings.items():
        sha256_value(value, f"{category}.bindings.{field}")
    if bindings["output_sha256"] != render_sha256:
        raise ValidationError(f"{category} report is not bound to render bytes")
    metrics = report.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValidationError(f"{category}.metrics must be an object")
    if category == "dedup":
        _validate_dedup_metrics(metrics, status=status)
    else:
        _validate_visual_metrics(metrics, status=status)
    return report


def _validate_dedup_metrics(metrics: Mapping[str, Any], *, status: str) -> None:
    required = {
        "algorithm",
        "sample_interval_seconds",
        "sampled_frame_count",
        "frame_hashes",
        "thresholds",
        "corpus_entry_count",
        "comparisons",
        "summary",
    }
    if set(metrics) != required or metrics.get("algorithm") != "dhash-64-v1":
        raise ValidationError("dedup.metrics has invalid fields or algorithm")
    _number(metrics.get("sample_interval_seconds"), "dedup.sample_interval_seconds", minimum=0.1)
    count = metrics.get("sampled_frame_count")
    hashes = metrics.get("frame_hashes")
    if not isinstance(count, int) or count < 8:
        raise ValidationError("dedup requires at least eight sampled frames")
    if not isinstance(hashes, list) or len(hashes) != count:
        raise ValidationError("dedup frame_hashes do not match sampled_frame_count")
    for index, value in enumerate(hashes):
        hex64_value(value, f"dedup.frame_hashes[{index}]")
    corpus_count = metrics.get("corpus_entry_count")
    comparisons = metrics.get("comparisons")
    if not isinstance(corpus_count, int) or corpus_count < 1:
        raise ValidationError("dedup requires a non-empty comparison corpus")
    if not isinstance(comparisons, list) or len(comparisons) != corpus_count:
        raise ValidationError("dedup must record one comparison per corpus entry")
    thresholds = metrics.get("thresholds")
    if not isinstance(thresholds, Mapping) or set(thresholds) != {
        "frame_hamming_distance",
        "near_duplicate_ratio",
        "reuse_sequence_seconds",
        "reuse_ratio",
    }:
        raise ValidationError("dedup.thresholds is incomplete")
    distance_limit = thresholds.get("frame_hamming_distance")
    if not isinstance(distance_limit, int) or isinstance(distance_limit, bool) or not 0 <= distance_limit <= 16:
        raise ValidationError("dedup frame_hamming_distance is invalid")
    for field in ("near_duplicate_ratio", "reuse_ratio"):
        value = _number(thresholds.get(field), f"dedup.thresholds.{field}", minimum=0)
        if value > 1:
            raise ValidationError(f"dedup.thresholds.{field} must not exceed 1")
    _number(
        thresholds.get("reuse_sequence_seconds"),
        "dedup.thresholds.reuse_sequence_seconds",
        minimum=0,
    )
    expected_comparison_fields = {
        "comparison_id",
        "job_id",
        "render_id",
        "render_sha256",
        "exact_duplicate",
        "near_duplicate",
        "reused_sequence",
        "best_alignment_offset_frames",
        "aligned_frame_count",
        "matching_frame_count",
        "perceptual_match_ratio",
        "mean_aligned_hamming_distance",
        "longest_reused_sequence_frames",
        "longest_reused_sequence_seconds",
        "reuse_ratio",
    }
    recomputed = {
        "exact_duplicate_count": 0,
        "near_duplicate_count": 0,
        "reused_sequence_count": 0,
    }
    for index, comparison in enumerate(comparisons):
        if not isinstance(comparison, Mapping) or set(comparison) != expected_comparison_fields:
            raise ValidationError(f"dedup.comparisons[{index}] has invalid fields")
        safe_id(comparison.get("comparison_id"), f"dedup.comparisons[{index}].comparison_id")
        safe_id(comparison.get("job_id"), f"dedup.comparisons[{index}].job_id")
        safe_id(comparison.get("render_id"), f"dedup.comparisons[{index}].render_id")
        sha256_value(
            comparison.get("render_sha256"),
            f"dedup.comparisons[{index}].render_sha256",
        )
        flags = []
        for field, summary_field in (
            ("exact_duplicate", "exact_duplicate_count"),
            ("near_duplicate", "near_duplicate_count"),
            ("reused_sequence", "reused_sequence_count"),
        ):
            value = comparison.get(field)
            if not isinstance(value, bool):
                raise ValidationError(f"dedup.comparisons[{index}].{field} must be boolean")
            flags.append(value)
            recomputed[summary_field] += int(value)
        if sum(flags) > 1:
            raise ValidationError("dedup comparison outcomes must be mutually exclusive")
        for field in (
            "best_alignment_offset_frames",
            "aligned_frame_count",
            "matching_frame_count",
            "longest_reused_sequence_frames",
        ):
            value = comparison.get(field)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValidationError(f"dedup.comparisons[{index}].{field} must be integer")
        if comparison["aligned_frame_count"] < 0 or comparison["matching_frame_count"] < 0:
            raise ValidationError("dedup aligned/matching frame counts must be nonnegative")
        if comparison["matching_frame_count"] > comparison["aligned_frame_count"]:
            raise ValidationError("dedup matching frames exceed aligned frames")
        for field in (
            "perceptual_match_ratio",
            "longest_reused_sequence_seconds",
            "reuse_ratio",
        ):
            value = _number(
                comparison.get(field), f"dedup.comparisons[{index}].{field}", minimum=0
            )
            if field.endswith("ratio") and value > 1:
                raise ValidationError(f"dedup.comparisons[{index}].{field} exceeds 1")
        mean_distance = comparison.get("mean_aligned_hamming_distance")
        if mean_distance is not None:
            value = _number(
                mean_distance,
                f"dedup.comparisons[{index}].mean_aligned_hamming_distance",
                minimum=0,
            )
            if value > 64:
                raise ValidationError("dedup mean hamming distance exceeds 64")
    summary = metrics.get("summary")
    if not isinstance(summary, Mapping) or set(summary) != {
        "exact_duplicate_count",
        "near_duplicate_count",
        "reused_sequence_count",
    }:
        raise ValidationError("dedup.summary is incomplete")
    for field in summary:
        value = summary[field]
        if not isinstance(value, int) or value < 0:
            raise ValidationError(f"dedup.summary.{field} must be a nonnegative integer")
        if value != recomputed[field]:
            raise ValidationError(f"dedup.summary.{field} does not match comparisons")
    total = sum(recomputed.values())
    if (status == "pass") != (total == 0):
        raise ValidationError("dedup pass is inconsistent with measured duplicate counters")


def _validate_visual_metrics(metrics: Mapping[str, Any], *, status: str) -> None:
    required = {
        "geometry",
        "sample_interval_seconds",
        "sampled_frame_count",
        "speaker_required",
        "safe_zone",
        "thresholds",
        "face_detector",
        "observations",
        "summary",
    }
    if set(metrics) != required:
        raise ValidationError("visual.metrics has invalid fields")
    geometry = metrics.get("geometry")
    if not isinstance(geometry, Mapping) or set(geometry) != {
        "width",
        "height",
        "aspect_ratio",
        "duration_seconds",
        "expected_width",
        "expected_height",
        "expected_duration_seconds",
        "shotlist_duration_seconds",
    }:
        raise ValidationError("visual.geometry is incomplete")
    for field in ("width", "height", "expected_width", "expected_height"):
        value = geometry.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"visual.geometry.{field} must be a positive integer")
    for field in (
        "aspect_ratio",
        "duration_seconds",
        "expected_duration_seconds",
        "shotlist_duration_seconds",
    ):
        _number(geometry.get(field), f"visual.geometry.{field}", minimum=0)
    count = metrics.get("sampled_frame_count")
    observations = metrics.get("observations")
    if not isinstance(count, int) or count < 8:
        raise ValidationError("visual requires at least eight sampled frames")
    if not isinstance(observations, list) or len(observations) != count:
        raise ValidationError("visual observations do not match sampled_frame_count")
    if not isinstance(metrics.get("speaker_required"), bool):
        raise ValidationError("visual.speaker_required must be boolean")
    safe_zone = metrics.get("safe_zone")
    if not isinstance(safe_zone, Mapping) or set(safe_zone) != {"x", "y", "width", "height"}:
        raise ValidationError("visual.safe_zone is incomplete")
    for field in safe_zone:
        value = _number(safe_zone[field], f"visual.safe_zone.{field}", minimum=0)
        if value > 1:
            raise ValidationError(f"visual.safe_zone.{field} exceeds 1")
    if safe_zone["width"] <= 0 or safe_zone["height"] <= 0:
        raise ValidationError("visual.safe_zone dimensions must be positive")
    if safe_zone["x"] + safe_zone["width"] > 1 or safe_zone["y"] + safe_zone["height"] > 1:
        raise ValidationError("visual.safe_zone escapes the frame")
    thresholds = metrics.get("thresholds")
    threshold_fields = {
        "black_luma_max",
        "edge_dark_ratio",
        "core_dark_ratio",
        "minimum_blur_score",
        "maximum_black_bar_frame_ratio",
        "maximum_blurred_frame_ratio",
        "minimum_face_confidence",
        "minimum_speaker_frame_ratio",
        "minimum_speaker_face_area_ratio",
        "minimum_median_speaker_face_area_ratio",
        "maximum_small_speaker_face_frame_ratio",
        "edge_crop_margin",
        "maximum_cropped_speaker_frame_ratio",
        "maximum_cropped_detected_face_frame_ratio",
        "maximum_safe_zone_face_overlap",
        "maximum_safe_zone_overlap_frame_ratio",
        "maximum_face_occlusion_fraction",
        "maximum_occluded_speaker_frame_ratio",
    }
    if not isinstance(thresholds, Mapping) or set(thresholds) != threshold_fields:
        raise ValidationError("visual.thresholds is incomplete")
    if not isinstance(thresholds["black_luma_max"], int) or isinstance(
        thresholds["black_luma_max"], bool
    ):
        raise ValidationError("visual.thresholds.black_luma_max must be integer")
    for field in threshold_fields - {"black_luma_max"}:
        _number(thresholds[field], f"visual.thresholds.{field}", minimum=0)
    face_detector = metrics.get("face_detector")
    if not isinstance(face_detector, Mapping) or set(face_detector) != {
        "name",
        "version",
        "model_sha256",
    }:
        raise ValidationError("visual.face_detector is incomplete")
    safe_id(face_detector.get("name"), "visual.face_detector.name")
    safe_id(face_detector.get("version"), "visual.face_detector.version")
    sha256_value(face_detector.get("model_sha256"), "visual.face_detector.model_sha256")

    expected_observation_fields = {
        "frame_index",
        "timestamp_seconds",
        "frame_sha256",
        "blur_score",
        "black_edges",
        "faces",
    }
    expected_black_fields = {
        "top_dark_ratio",
        "bottom_dark_ratio",
        "left_dark_ratio",
        "right_dark_ratio",
        "core_dark_ratio",
        "full_dark_ratio",
        "horizontal_bar",
        "vertical_bar",
        "black_frame",
    }
    expected_face_fields = {
        "bbox",
        "confidence",
        "speaker",
        "occlusion_fraction",
        "accepted",
        "area_ratio",
        "crop_risk",
        "safe_zone_overlap_ratio",
        "occluded",
    }
    black_count = 0
    blur_count = 0
    face_count = 0
    speaker_count = 0
    small_speaker_count = 0
    cropped_speaker_count = 0
    cropped_detected_face_count = 0
    speaker_face_areas: list[float] = []
    safe_overlap_count = 0
    occluded_count = 0
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping) or set(observation) != expected_observation_fields:
            raise ValidationError(f"visual.observations[{index}] has invalid fields")
        if observation.get("frame_index") != index:
            raise ValidationError("visual observations must be contiguous and ordered")
        _number(
            observation.get("timestamp_seconds"),
            f"visual.observations[{index}].timestamp_seconds",
            minimum=0,
        )
        sha256_value(
            observation.get("frame_sha256"),
            f"visual.observations[{index}].frame_sha256",
        )
        blur_score = _number(
            observation.get("blur_score"),
            f"visual.observations[{index}].blur_score",
            minimum=0,
        )
        blur_count += int(blur_score < float(thresholds["minimum_blur_score"]))
        black = observation.get("black_edges")
        if not isinstance(black, Mapping) or set(black) != expected_black_fields:
            raise ValidationError(f"visual.observations[{index}].black_edges is invalid")
        for field in expected_black_fields - {"horizontal_bar", "vertical_bar", "black_frame"}:
            ratio = _number(black.get(field), f"visual.black_edges.{field}", minimum=0)
            if ratio > 1:
                raise ValidationError(f"visual.black_edges.{field} exceeds 1")
        for field in ("horizontal_bar", "vertical_bar", "black_frame"):
            if not isinstance(black.get(field), bool):
                raise ValidationError(f"visual.black_edges.{field} must be boolean")
        expected_horizontal = (
            black["top_dark_ratio"] >= thresholds["edge_dark_ratio"]
            and black["bottom_dark_ratio"] >= thresholds["edge_dark_ratio"]
            and black["core_dark_ratio"] < thresholds["core_dark_ratio"]
        )
        expected_vertical = (
            black["left_dark_ratio"] >= thresholds["edge_dark_ratio"]
            and black["right_dark_ratio"] >= thresholds["edge_dark_ratio"]
            and black["core_dark_ratio"] < thresholds["core_dark_ratio"]
        )
        expected_black_frame = black["full_dark_ratio"] >= thresholds["edge_dark_ratio"]
        if (
            black["horizontal_bar"] != expected_horizontal
            or black["vertical_bar"] != expected_vertical
            or black["black_frame"] != expected_black_frame
        ):
            raise ValidationError("visual black classification does not match measured ratios")
        black_count += int(expected_horizontal or expected_vertical or expected_black_frame)
        faces = observation.get("faces")
        if not isinstance(faces, list):
            raise ValidationError(f"visual.observations[{index}].faces must be an array")
        accepted_in_frame = False
        speaker_in_frame = False
        speaker_crop_in_frame = False
        detected_face_crop_in_frame = False
        safe_in_frame = False
        occluded_in_frame = False
        speaker_areas_in_frame: list[float] = []
        for face_index, face in enumerate(faces):
            if not isinstance(face, Mapping) or set(face) != expected_face_fields:
                raise ValidationError(
                    f"visual.observations[{index}].faces[{face_index}] has invalid fields"
                )
            bbox = face.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                raise ValidationError("visual face bbox must contain four numbers")
            x, y, width, height = [
                _number(item, "visual face bbox", minimum=0) for item in bbox
            ]
            if width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
                raise ValidationError("visual face bbox escapes the frame")
            confidence = _number(face.get("confidence"), "visual face confidence", minimum=0)
            reported_area_ratio = _number(
                face.get("area_ratio"), "visual face area_ratio", minimum=0
            )
            area_ratio = width * height
            if abs(reported_area_ratio - area_ratio) > 0.000002:
                raise ValidationError("visual face area_ratio is not derived from bbox")
            occlusion = _number(
                face.get("occlusion_fraction"), "visual face occlusion", minimum=0
            )
            if confidence > 1 or occlusion > 1:
                raise ValidationError("visual face confidence/occlusion exceeds 1")
            if not all(
                isinstance(face.get(field), bool)
                for field in ("speaker", "accepted", "crop_risk", "occluded")
            ):
                raise ValidationError("visual face decision fields must be boolean")
            accepted = confidence >= thresholds["minimum_face_confidence"]
            margin = thresholds["edge_crop_margin"]
            crop_risk = accepted and (
                x <= margin
                or y <= margin
                or x + width >= 1 - margin
                or y + height >= 1 - margin
            )
            right = min(x + width, safe_zone["x"] + safe_zone["width"])
            bottom = min(y + height, safe_zone["y"] + safe_zone["height"])
            overlap = (
                max(0.0, right - max(x, safe_zone["x"]))
                * max(0.0, bottom - max(y, safe_zone["y"]))
                / (width * height)
            )
            reported_overlap = _number(
                face.get("safe_zone_overlap_ratio"),
                "visual face safe_zone_overlap_ratio",
                minimum=0,
            )
            if abs(reported_overlap - overlap) > 0.000002:
                raise ValidationError("visual face safe-zone overlap is not derived from bbox")
            speaker = bool(face["speaker"]) and accepted
            occluded = speaker and occlusion > thresholds["maximum_face_occlusion_fraction"]
            if face["accepted"] != accepted or face["crop_risk"] != crop_risk or face["occluded"] != occluded:
                raise ValidationError("visual face classifications do not match thresholds")
            accepted_in_frame = accepted_in_frame or accepted
            speaker_in_frame = speaker_in_frame or speaker
            speaker_crop_in_frame = speaker_crop_in_frame or (speaker and crop_risk)
            detected_face_crop_in_frame = detected_face_crop_in_frame or crop_risk
            safe_in_frame = safe_in_frame or (
                speaker and overlap > thresholds["maximum_safe_zone_face_overlap"]
            )
            occluded_in_frame = occluded_in_frame or occluded
            if speaker:
                speaker_areas_in_frame.append(area_ratio)
        face_count += int(accepted_in_frame)
        speaker_count += int(speaker_in_frame)
        cropped_speaker_count += int(speaker_crop_in_frame)
        cropped_detected_face_count += int(detected_face_crop_in_frame)
        safe_overlap_count += int(safe_in_frame)
        occluded_count += int(occluded_in_frame)
        if speaker_areas_in_frame:
            dominant_area = max(speaker_areas_in_frame)
            speaker_face_areas.append(dominant_area)
            small_speaker_count += int(
                dominant_area < thresholds["minimum_speaker_face_area_ratio"]
            )
    summary = metrics.get("summary")
    required_summary = {
        "geometry_failed",
        "black_bar_frame_count",
        "blurred_frame_count",
        "face_frame_count",
        "speaker_frame_count",
        "small_speaker_face_frame_count",
        "cropped_speaker_frame_count",
        "cropped_detected_face_frame_count",
        "safe_zone_overlap_frame_count",
        "occluded_speaker_frame_count",
        "speaker_coverage_ratio",
        "median_speaker_face_area_ratio",
        "small_speaker_face_frame_ratio",
    }
    if not isinstance(summary, Mapping) or set(summary) != required_summary:
        raise ValidationError("visual.summary is incomplete")
    expected_summary = {
        "black_bar_frame_count": black_count,
        "blurred_frame_count": blur_count,
        "face_frame_count": face_count,
        "speaker_frame_count": speaker_count,
        "small_speaker_face_frame_count": small_speaker_count,
        "cropped_speaker_frame_count": cropped_speaker_count,
        "cropped_detected_face_frame_count": cropped_detected_face_count,
        "safe_zone_overlap_frame_count": safe_overlap_count,
        "occluded_speaker_frame_count": occluded_count,
    }
    for field, expected in expected_summary.items():
        value = summary.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value != expected:
            raise ValidationError(f"visual.summary.{field} does not match observations")
    expected_geometry_failed = (
        geometry["width"] != geometry["expected_width"]
        or geometry["height"] != geometry["expected_height"]
        or abs(float(geometry["aspect_ratio"]) - (9 / 16)) > 0.001
        or abs(float(geometry["duration_seconds"]) - float(geometry["expected_duration_seconds"])) > 0.25
        or abs(float(geometry["duration_seconds"]) - float(geometry["shotlist_duration_seconds"])) > 0.25
    )
    if summary.get("geometry_failed") is not expected_geometry_failed:
        raise ValidationError("visual.summary.geometry_failed does not match geometry")
    coverage = _number(
        summary.get("speaker_coverage_ratio"),
        "visual.summary.speaker_coverage_ratio",
        minimum=0,
    )
    if abs(coverage - (speaker_count / count)) > 0.000002:
        raise ValidationError("visual.summary.speaker_coverage_ratio is inconsistent")
    median_speaker_area = _number(
        summary.get("median_speaker_face_area_ratio"),
        "visual.summary.median_speaker_face_area_ratio",
        minimum=0,
    )
    expected_median_speaker_area = (
        float(statistics.median(speaker_face_areas)) if speaker_face_areas else 0.0
    )
    if abs(median_speaker_area - expected_median_speaker_area) > 0.000002:
        raise ValidationError(
            "visual.summary.median_speaker_face_area_ratio is inconsistent"
        )
    small_speaker_ratio = _number(
        summary.get("small_speaker_face_frame_ratio"),
        "visual.summary.small_speaker_face_frame_ratio",
        minimum=0,
    )
    expected_small_speaker_ratio = (
        small_speaker_count / speaker_count if speaker_count else 0.0
    )
    if abs(small_speaker_ratio - expected_small_speaker_ratio) > 0.000002:
        raise ValidationError(
            "visual.summary.small_speaker_face_frame_ratio is inconsistent"
        )
    blockers = (
        expected_geometry_failed
        or black_count / count > thresholds["maximum_black_bar_frame_ratio"]
        or blur_count / count > thresholds["maximum_blurred_frame_ratio"]
        or cropped_speaker_count / count
        > thresholds["maximum_cropped_speaker_frame_ratio"]
        or cropped_detected_face_count / count
        > thresholds["maximum_cropped_detected_face_frame_ratio"]
        or safe_overlap_count / count > thresholds["maximum_safe_zone_overlap_frame_ratio"]
        or occluded_count / count > thresholds["maximum_occluded_speaker_frame_ratio"]
        or (
            metrics["speaker_required"]
            and (
                coverage < thresholds["minimum_speaker_frame_ratio"]
                or (
                    speaker_count > 0
                    and (
                        median_speaker_area
                        < thresholds["minimum_median_speaker_face_area_ratio"]
                        or small_speaker_ratio
                        > thresholds["maximum_small_speaker_face_frame_ratio"]
                    )
                )
            )
        )
    )
    if (status == "pass") == blockers:
        raise ValidationError("visual pass is inconsistent with frame-level blocker metrics")


__all__ = [
    "FrameExtractor",
    "GrayFrame",
    "artifact_sha256",
    "bind_render",
    "checker_run_id",
    "evidence_file",
    "extract_gray_frames",
    "hex64_value",
    "load_json_object",
    "sha256_file",
    "sha256_value",
    "utc_now",
    "validate_qc_analyzer_report",
    "write_report",
]
