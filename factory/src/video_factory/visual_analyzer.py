"""Frame-level visual QC for vertical social-video masters.

Geometry, black bars and blur are measured directly from decoded render
pixels.  Face, active-speaker and occlusion observations must come from a
model-backed adapter and are checksum-bound to every decoded frame.  Missing
adapter output is an analyzer error, never a synthetic pass.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import validate_artifact
from .errors import ValidationError
from .media_tools import media_summary, probe_media
from .qc_analyzer_common import (
    FrameExtractor,
    GrayFrame,
    artifact_sha256,
    bind_render,
    checker_run_id,
    extract_gray_frames,
    safe_id,
    sha256_file,
    sha256_value,
    utc_now,
    validate_qc_analyzer_report,
    write_report,
)


VISUAL_ANALYZER_VERSION = "1.0.0"
DEFAULT_SAMPLE_INTERVAL_SECONDS = 1.0
FRAME_WIDTH = 180
FRAME_HEIGHT = 320
MINIMUM_SAMPLE_FRAMES = 8
DEFAULT_SAFE_ZONE = {"x": 0.08, "y": 0.62, "width": 0.84, "height": 0.28}
DEFAULT_THRESHOLDS = {
    "black_luma_max": 16,
    "edge_dark_ratio": 0.98,
    "core_dark_ratio": 0.80,
    "minimum_blur_score": 35.0,
    "maximum_black_bar_frame_ratio": 0.0,
    "maximum_blurred_frame_ratio": 0.10,
    "minimum_face_confidence": 0.70,
    "minimum_speaker_frame_ratio": 0.60,
    "minimum_speaker_face_area_ratio": 0.025,
    "minimum_median_speaker_face_area_ratio": 0.045,
    "maximum_small_speaker_face_frame_ratio": 0.20,
    "edge_crop_margin": 0.015,
    "maximum_cropped_speaker_frame_ratio": 0.05,
    "maximum_cropped_detected_face_frame_ratio": 0.0,
    "maximum_safe_zone_face_overlap": 0.10,
    "maximum_safe_zone_overlap_frame_ratio": 0.05,
    "maximum_face_occlusion_fraction": 0.35,
    "maximum_occluded_speaker_frame_ratio": 0.05,
}


FaceObserver = Callable[..., Mapping[str, Any]]
ProbeRunner = Callable[[str | Path], dict[str, Any]]


def _number(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValidationError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValidationError(f"{field} must be from {minimum} to {maximum}")
    return number


def _validate_thresholds(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(DEFAULT_THRESHOLDS if value is None else value)
    if set(raw) != set(DEFAULT_THRESHOLDS):
        raise ValidationError("visual thresholds must contain exactly the supported fields")
    black_luma = raw["black_luma_max"]
    if not isinstance(black_luma, int) or isinstance(black_luma, bool) or not 0 <= black_luma <= 64:
        raise ValidationError("black_luma_max must be an integer from 0 to 64")
    result: dict[str, Any] = {"black_luma_max": black_luma}
    ranges = {
        "edge_dark_ratio": (0.80, 1.0),
        "core_dark_ratio": (0.20, 0.95),
        "minimum_blur_score": (1.0, 10000.0),
        "maximum_black_bar_frame_ratio": (0.0, 0.25),
        "maximum_blurred_frame_ratio": (0.0, 0.50),
        "minimum_face_confidence": (0.50, 1.0),
        "minimum_speaker_frame_ratio": (0.10, 1.0),
        "minimum_speaker_face_area_ratio": (0.005, 0.30),
        "minimum_median_speaker_face_area_ratio": (0.005, 0.30),
        "maximum_small_speaker_face_frame_ratio": (0.0, 1.0),
        "edge_crop_margin": (0.0, 0.10),
        "maximum_cropped_speaker_frame_ratio": (0.0, 0.25),
        "maximum_cropped_detected_face_frame_ratio": (0.0, 0.25),
        "maximum_safe_zone_face_overlap": (0.0, 0.50),
        "maximum_safe_zone_overlap_frame_ratio": (0.0, 0.25),
        "maximum_face_occlusion_fraction": (0.0, 0.75),
        "maximum_occluded_speaker_frame_ratio": (0.0, 0.25),
    }
    for field, (minimum, maximum) in ranges.items():
        result[field] = _number(raw[field], field, minimum=minimum, maximum=maximum)
    return result


def _validate_safe_zone(value: Mapping[str, Any] | None) -> dict[str, float]:
    raw = dict(DEFAULT_SAFE_ZONE if value is None else value)
    if set(raw) != {"x", "y", "width", "height"}:
        raise ValidationError("safe_zone must contain x, y, width and height")
    zone = {
        field: _number(raw[field], f"safe_zone.{field}", minimum=0.0, maximum=1.0)
        for field in ("x", "y", "width", "height")
    }
    if zone["width"] <= 0 or zone["height"] <= 0:
        raise ValidationError("safe_zone dimensions must be positive")
    if zone["x"] + zone["width"] > 1 or zone["y"] + zone["height"] > 1:
        raise ValidationError("safe_zone must stay inside the frame")
    return zone


def _laplacian_variance(frame: GrayFrame) -> float:
    width = frame.width
    height = frame.height
    if width < 3 or height < 3 or len(frame.pixels) != width * height:
        raise ValidationError("visual frame is truncated")
    count = (width - 2) * (height - 2)
    total = 0.0
    total_squared = 0.0
    pixels = frame.pixels
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1):
            index = row + x
            laplacian = (
                -4 * pixels[index]
                + pixels[index - 1]
                + pixels[index + 1]
                + pixels[index - width]
                + pixels[index + width]
            )
            total += laplacian
            total_squared += laplacian * laplacian
    mean = total / count
    return max(0.0, (total_squared / count) - mean * mean)


def _dark_ratio(
    frame: GrayFrame,
    *,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    threshold: int,
) -> float:
    if x0 >= x1 or y0 >= y1:
        raise ValidationError("black-bar observation region is empty")
    dark = 0
    total = (x1 - x0) * (y1 - y0)
    for y in range(y0, y1):
        offset = y * frame.width
        dark += sum(frame.pixels[offset + x] <= threshold for x in range(x0, x1))
    return dark / total


def _black_observation(frame: GrayFrame, policy: Mapping[str, Any]) -> dict[str, Any]:
    row_band = max(2, round(frame.height * 0.04))
    column_band = max(2, round(frame.width * 0.04))
    threshold = int(policy["black_luma_max"])
    top = _dark_ratio(
        frame, x0=0, y0=0, x1=frame.width, y1=row_band, threshold=threshold
    )
    bottom = _dark_ratio(
        frame,
        x0=0,
        y0=frame.height - row_band,
        x1=frame.width,
        y1=frame.height,
        threshold=threshold,
    )
    left = _dark_ratio(
        frame, x0=0, y0=0, x1=column_band, y1=frame.height, threshold=threshold
    )
    right = _dark_ratio(
        frame,
        x0=frame.width - column_band,
        y0=0,
        x1=frame.width,
        y1=frame.height,
        threshold=threshold,
    )
    core = _dark_ratio(
        frame,
        x0=column_band,
        y0=row_band,
        x1=frame.width - column_band,
        y1=frame.height - row_band,
        threshold=threshold,
    )
    full = _dark_ratio(
        frame,
        x0=0,
        y0=0,
        x1=frame.width,
        y1=frame.height,
        threshold=threshold,
    )
    edge_limit = float(policy["edge_dark_ratio"])
    core_limit = float(policy["core_dark_ratio"])
    return {
        "top_dark_ratio": round(top, 6),
        "bottom_dark_ratio": round(bottom, 6),
        "left_dark_ratio": round(left, 6),
        "right_dark_ratio": round(right, 6),
        "core_dark_ratio": round(core, 6),
        "full_dark_ratio": round(full, 6),
        "horizontal_bar": top >= edge_limit and bottom >= edge_limit and core < core_limit,
        "vertical_bar": left >= edge_limit and right >= edge_limit and core < core_limit,
        "black_frame": full >= edge_limit,
    }


def _pgm_bytes(frame: GrayFrame) -> bytes:
    return f"P5\n{frame.width} {frame.height}\n255\n".encode("ascii") + frame.pixels


def _face_observer_from_command(
    render_path: Path,
    render_sha256: str,
    frames: Sequence[GrayFrame],
    *,
    width: int,
    height: int,
) -> Mapping[str, Any]:
    configured = os.environ.get("VIDEO_FACTORY_FACE_OBSERVER")
    if not configured:
        raise ValidationError(
            "VIDEO_FACTORY_FACE_OBSERVER is required for face/speaker visual QC"
        )
    raw_executable = Path(configured).expanduser()
    if not raw_executable.is_absolute() or raw_executable.is_symlink():
        raise ValidationError("VIDEO_FACTORY_FACE_OBSERVER must be an absolute non-symlink file")
    executable = raw_executable.resolve()
    if not executable.is_file():
        raise ValidationError("VIDEO_FACTORY_FACE_OBSERVER does not exist")
    with tempfile.TemporaryDirectory(prefix="video-factory-face-observer-") as temporary:
        root = Path(temporary)
        request_frames = []
        for frame in frames:
            frame_path = root / f"frame-{frame.index:04d}.pgm"
            frame_path.write_bytes(_pgm_bytes(frame))
            request_frames.append(
                {
                    "frame_index": frame.index,
                    "timestamp_seconds": frame.timestamp_seconds,
                    "frame_sha256": frame.sha256,
                    "path": str(frame_path),
                }
            )
        request = {
            "schema_version": "1.0.0",
            "render_path": str(render_path),
            "render_sha256": render_sha256,
            "frame_width": width,
            "frame_height": height,
            "frames": request_frames,
        }
        request_path = root / "request.json"
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            completed = subprocess.run(
                [str(executable), str(request_path)],
                capture_output=True,
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError(f"face observer failed: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")
            detail = detail.strip().replace("\r", " ").replace("\n", " ")[-1200:]
            raise ValidationError(
                f"face observer exited {completed.returncode}: {detail}"
            )
        if not completed.stdout or len(completed.stdout) > 16 * 1024 * 1024:
            raise ValidationError("face observer returned missing or oversized JSON")
        try:
            response = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("face observer returned invalid JSON") from exc
        if not isinstance(response, Mapping):
            raise ValidationError("face observer response must be an object")
        return response


def _validate_face_response(
    response: Mapping[str, Any],
    *,
    render_sha256: str,
    frames: Sequence[GrayFrame],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if not isinstance(response, Mapping) or set(response) != {
        "schema_version",
        "render_sha256",
        "checker",
        "observations",
    }:
        raise ValidationError("face observer response has invalid fields")
    if response.get("schema_version") != "1.0.0" or response.get("render_sha256") != render_sha256:
        raise ValidationError("face observer response is stale for this render")
    checker = response.get("checker")
    if not isinstance(checker, Mapping) or set(checker) != {
        "name",
        "version",
        "model_sha256",
    }:
        raise ValidationError("face observer checker metadata is incomplete")
    normalized_checker = {
        "name": safe_id(checker.get("name"), "face_observer.checker.name"),
        "version": safe_id(checker.get("version"), "face_observer.checker.version"),
        "model_sha256": sha256_value(
            checker.get("model_sha256"), "face_observer.checker.model_sha256"
        ),
    }
    raw_observations = response.get("observations")
    if not isinstance(raw_observations, list) or len(raw_observations) != len(frames):
        raise ValidationError("face observer must return one observation for every frame")
    by_index: dict[int, dict[str, Any]] = {}
    for position, observation in enumerate(raw_observations):
        if not isinstance(observation, Mapping) or set(observation) != {
            "frame_index",
            "frame_sha256",
            "faces",
        }:
            raise ValidationError(f"face observations[{position}] has invalid fields")
        index = observation.get("frame_index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(frames):
            raise ValidationError(f"face observations[{position}].frame_index is invalid")
        if index in by_index:
            raise ValidationError("face observer frame indexes must be unique")
        frame_sha256 = sha256_value(
            observation.get("frame_sha256"),
            f"face observations[{position}].frame_sha256",
        )
        if frame_sha256 != frames[index].sha256:
            raise ValidationError("face observer observation is stale for decoded frame bytes")
        faces = observation.get("faces")
        if not isinstance(faces, list):
            raise ValidationError(f"face observations[{position}].faces must be an array")
        normalized_faces: list[dict[str, Any]] = []
        for face_index, face in enumerate(faces):
            if not isinstance(face, Mapping) or set(face) != {
                "bbox",
                "confidence",
                "speaker",
                "occlusion_fraction",
            }:
                raise ValidationError(
                    f"face observations[{position}].faces[{face_index}] has invalid fields"
                )
            bbox = face.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                raise ValidationError("face bbox must be [x, y, width, height]")
            x, y, width, height = [
                _number(item, "face bbox", minimum=0.0, maximum=1.0) for item in bbox
            ]
            if width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
                raise ValidationError("face bbox must have positive area inside the frame")
            confidence = _number(
                face.get("confidence"), "face confidence", minimum=0.0, maximum=1.0
            )
            speaker = face.get("speaker")
            if not isinstance(speaker, bool):
                raise ValidationError("face speaker must be boolean")
            occlusion = _number(
                face.get("occlusion_fraction"),
                "face occlusion_fraction",
                minimum=0.0,
                maximum=1.0,
            )
            normalized_faces.append(
                {
                    "bbox": [x, y, width, height],
                    "confidence": confidence,
                    "speaker": speaker,
                    "occlusion_fraction": occlusion,
                }
            )
        by_index[index] = {
            "frame_index": index,
            "frame_sha256": frame_sha256,
            "faces": normalized_faces,
        }
    return [by_index[index] for index in range(len(frames))], normalized_checker


def _intersection_fraction(
    bbox: Sequence[float], zone: Mapping[str, float]
) -> float:
    x, y, width, height = bbox
    right = min(x + width, zone["x"] + zone["width"])
    bottom = min(y + height, zone["y"] + zone["height"])
    overlap_width = max(0.0, right - max(x, zone["x"]))
    overlap_height = max(0.0, bottom - max(y, zone["y"]))
    return (overlap_width * overlap_height) / (width * height)


def _write_contact_sheet(frames: Sequence[GrayFrame], output_path: str | Path) -> Path:
    raw = Path(output_path).expanduser()
    if not raw.is_absolute():
        raise ValidationError("contact_sheet_path must be absolute")
    if raw.is_symlink():
        raise ValidationError("contact_sheet_path must not be a symlink")
    path = raw.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    selected_count = min(12, len(frames))
    selected_indexes = (
        [0]
        if selected_count == 1
        else [round(index * (len(frames) - 1) / (selected_count - 1)) for index in range(selected_count)]
    )
    tile_width = frames[0].width // 2
    tile_height = frames[0].height // 2
    columns = 4
    rows = math.ceil(selected_count / columns)
    sheet_width = columns * tile_width
    sheet_height = rows * tile_height
    pixels = bytearray([255]) * (sheet_width * sheet_height)
    for tile_index, frame_index in enumerate(selected_indexes):
        frame = frames[frame_index]
        tile_x = (tile_index % columns) * tile_width
        tile_y = (tile_index // columns) * tile_height
        for y in range(tile_height):
            source = (y * 2) * frame.width
            destination = (tile_y + y) * sheet_width + tile_x
            pixels[destination : destination + tile_width] = frame.pixels[
                source : source + tile_width * 2 : 2
            ]
    payload = f"P5\n{sheet_width} {sheet_height}\n255\n".encode("ascii") + bytes(pixels)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValidationError(f"cannot write visual contact sheet: {exc}") from exc
    return path


def _finding(code: str, message: str, refs: Sequence[int]) -> dict[str, Any]:
    references = sorted(set(int(item) for item in refs))
    if not references:
        references = [0]
    return {"code": code, "message": message, "observation_refs": references}


def analyze_visual(
    render_path: str | Path,
    render_manifest: Mapping[str, Any],
    shotlist: Mapping[str, Any],
    *,
    lane_id: str,
    speaker_required: bool,
    report_path: str | Path,
    contact_sheet_path: str | Path,
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    safe_zone: Mapping[str, Any] | None = None,
    thresholds: Mapping[str, Any] | None = None,
    frame_extractor: FrameExtractor = extract_gray_frames,
    face_observer: FaceObserver | None = None,
    probe_runner: ProbeRunner = probe_media,
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    """Produce immutable visual evidence from actual frame-level observations."""

    if not isinstance(speaker_required, bool):
        raise ValidationError("speaker_required must be explicitly boolean")
    interval = _number(
        sample_interval_seconds,
        "sample_interval_seconds",
        minimum=0.25,
        maximum=1.5,
    )
    policy = _validate_thresholds(thresholds)
    zone = _validate_safe_zone(safe_zone)
    path, render_sha256, job_id, render_id = bind_render(render_path, render_manifest)
    lane = safe_id(lane_id, "lane_id")
    manifest = dict(render_manifest)
    render_manifest_sha256 = artifact_sha256(manifest)
    if not isinstance(shotlist, Mapping):
        raise ValidationError("shotlist must be an object")
    shotlist_value = dict(shotlist)
    validate_artifact("shotlist", shotlist_value)
    shotlist_sha256 = artifact_sha256(shotlist_value)

    probe = probe_runner(path)
    if not isinstance(probe, dict):
        raise ValidationError("visual probe returned no evidence")
    summary = media_summary(probe)
    video = summary.get("video")
    if not isinstance(video, Mapping):
        raise ValidationError("visual probe found no video stream")
    actual_width = video.get("width")
    actual_height = video.get("height")
    duration = summary.get("duration_seconds")
    if not isinstance(actual_width, int) or not isinstance(actual_height, int):
        raise ValidationError("visual probe lacks frame geometry")
    if not isinstance(duration, (int, float)) or not math.isfinite(float(duration)):
        raise ValidationError("visual probe lacks duration")
    duration_value = float(duration)
    aspect = actual_width / actual_height
    manifest_technical = manifest["technical"]
    geometry_failed = (
        [actual_width, actual_height] != [1080, 1920]
        or abs(aspect - (9 / 16)) > 0.001
        or abs(duration_value - float(manifest_technical["duration_seconds"])) > 0.25
        or abs(duration_value - float(shotlist_value["duration_seconds"])) > 0.25
    )

    maximum_frames = min(400, max(MINIMUM_SAMPLE_FRAMES, math.ceil(duration_value / interval) + 2))
    frames = frame_extractor(
        path,
        interval_seconds=interval,
        width=FRAME_WIDTH,
        height=FRAME_HEIGHT,
        maximum_frames=maximum_frames,
    )
    if len(frames) < MINIMUM_SAMPLE_FRAMES:
        raise ValidationError(
            f"visual analysis requires at least {MINIMUM_SAMPLE_FRAMES} decoded samples"
        )
    if [frame.index for frame in frames] != list(range(len(frames))):
        raise ValidationError("visual frames must be contiguous and ordered")
    if any(
        frame.width != FRAME_WIDTH
        or frame.height != FRAME_HEIGHT
        or len(frame.pixels) != FRAME_WIDTH * FRAME_HEIGHT
        for frame in frames
    ):
        raise ValidationError("visual frame extractor returned invalid pixel geometry")

    observer = face_observer or _face_observer_from_command
    response = observer(
        path,
        render_sha256,
        frames,
        width=FRAME_WIDTH,
        height=FRAME_HEIGHT,
    )
    face_rows, detector_checker = _validate_face_response(
        response, render_sha256=render_sha256, frames=frames
    )

    observations: list[dict[str, Any]] = []
    black_refs: list[int] = []
    blur_refs: list[int] = []
    face_refs: list[int] = []
    speaker_refs: list[int] = []
    speaker_face_areas: list[float] = []
    small_speaker_refs: list[int] = []
    cropped_speaker_refs: list[int] = []
    cropped_face_refs: list[int] = []
    safe_zone_refs: list[int] = []
    occlusion_refs: list[int] = []
    for frame, face_row in zip(frames, face_rows, strict=True):
        black = _black_observation(frame, policy)
        blur_score = _laplacian_variance(frame)
        if black["horizontal_bar"] or black["vertical_bar"] or black["black_frame"]:
            black_refs.append(frame.index)
        if blur_score < float(policy["minimum_blur_score"]):
            blur_refs.append(frame.index)
        normalized_faces: list[dict[str, Any]] = []
        accepted_faces = 0
        frame_has_speaker = False
        frame_speaker_crop = False
        frame_detected_face_crop = False
        frame_safe_zone = False
        frame_occlusion = False
        frame_speaker_face_areas: list[float] = []
        for face in face_row["faces"]:
            bbox = face["bbox"]
            confidence = float(face["confidence"])
            accepted = confidence >= float(policy["minimum_face_confidence"])
            overlap = _intersection_fraction(bbox, zone)
            x, y, width, height = bbox
            area_ratio = width * height
            margin = float(policy["edge_crop_margin"])
            crop_risk = accepted and (
                x <= margin
                or y <= margin
                or x + width >= 1 - margin
                or y + height >= 1 - margin
            )
            speaker = bool(face["speaker"]) and accepted
            occluded = speaker and float(face["occlusion_fraction"]) > float(
                policy["maximum_face_occlusion_fraction"]
            )
            safe_overlap = speaker and overlap > float(
                policy["maximum_safe_zone_face_overlap"]
            )
            accepted_faces += int(accepted)
            frame_has_speaker = frame_has_speaker or speaker
            frame_speaker_crop = frame_speaker_crop or (speaker and crop_risk)
            frame_detected_face_crop = frame_detected_face_crop or crop_risk
            frame_safe_zone = frame_safe_zone or safe_overlap
            frame_occlusion = frame_occlusion or occluded
            if speaker:
                frame_speaker_face_areas.append(area_ratio)
            normalized_faces.append(
                {
                    **face,
                    "accepted": accepted,
                    "area_ratio": round(area_ratio, 6),
                    "crop_risk": crop_risk,
                    "safe_zone_overlap_ratio": round(overlap, 6),
                    "occluded": occluded,
                }
            )
        if accepted_faces:
            face_refs.append(frame.index)
        if frame_has_speaker:
            speaker_refs.append(frame.index)
            dominant_speaker_area = max(frame_speaker_face_areas)
            speaker_face_areas.append(dominant_speaker_area)
            if dominant_speaker_area < float(
                policy["minimum_speaker_face_area_ratio"]
            ):
                small_speaker_refs.append(frame.index)
        if frame_speaker_crop:
            cropped_speaker_refs.append(frame.index)
        if frame_detected_face_crop:
            cropped_face_refs.append(frame.index)
        if frame_safe_zone:
            safe_zone_refs.append(frame.index)
        if frame_occlusion:
            occlusion_refs.append(frame.index)
        observations.append(
            {
                "frame_index": frame.index,
                "timestamp_seconds": frame.timestamp_seconds,
                "frame_sha256": frame.sha256,
                "blur_score": round(blur_score, 6),
                "black_edges": black,
                "faces": normalized_faces,
            }
        )

    frame_count = len(frames)
    speaker_ratio = len(speaker_refs) / frame_count
    median_speaker_face_area = (
        float(statistics.median(speaker_face_areas)) if speaker_face_areas else 0.0
    )
    small_speaker_face_ratio = (
        len(small_speaker_refs) / len(speaker_refs) if speaker_refs else 0.0
    )
    ratios = {
        "black": len(black_refs) / frame_count,
        "blur": len(blur_refs) / frame_count,
        "speaker_crop": len(cropped_speaker_refs) / frame_count,
        "detected_face_crop": len(cropped_face_refs) / frame_count,
        "safe_zone": len(safe_zone_refs) / frame_count,
        "occlusion": len(occlusion_refs) / frame_count,
    }
    findings: list[dict[str, Any]] = []
    if geometry_failed:
        findings.append(
            _finding(
                "invalid_vertical_geometry",
                f"actual geometry/duration is {actual_width}x{actual_height}, {duration_value:.3f}s",
                [0],
            )
        )
    if ratios["black"] > float(policy["maximum_black_bar_frame_ratio"]):
        findings.append(
            _finding("black_bars_or_frames", "black bars/frames exceed threshold", black_refs)
        )
    if ratios["blur"] > float(policy["maximum_blurred_frame_ratio"]):
        findings.append(
            _finding("blurred_frames", "blurred-frame ratio exceeds threshold", blur_refs)
        )
    if speaker_required and speaker_ratio < float(policy["minimum_speaker_frame_ratio"]):
        missing_refs = [index for index in range(frame_count) if index not in set(speaker_refs)]
        findings.append(
            _finding(
                "speaker_visibility",
                f"speaker is visible in only {speaker_ratio:.3f} of sampled frames",
                missing_refs,
            )
        )
    if speaker_required and speaker_refs and (
        median_speaker_face_area
        < float(policy["minimum_median_speaker_face_area_ratio"])
        or small_speaker_face_ratio
        > float(policy["maximum_small_speaker_face_frame_ratio"])
    ):
        size_refs = small_speaker_refs or speaker_refs
        findings.append(
            _finding(
                "speaker_face_too_small",
                "speaker face is not dominant enough: "
                f"median area={median_speaker_face_area:.4f}, "
                f"small-frame ratio={small_speaker_face_ratio:.3f}",
                size_refs,
            )
        )
    if ratios["speaker_crop"] > float(policy["maximum_cropped_speaker_frame_ratio"]):
        findings.append(
            _finding(
                "cropped_speaker_face",
                "speaker face crop ratio exceeds threshold",
                cropped_speaker_refs,
            )
        )
    if ratios["detected_face_crop"] > float(
        policy["maximum_cropped_detected_face_frame_ratio"]
    ):
        findings.append(
            _finding(
                "cropped_detected_face",
                "a confidently detected face touches the crop margin",
                cropped_face_refs,
            )
        )
    if ratios["safe_zone"] > float(policy["maximum_safe_zone_overlap_frame_ratio"]):
        findings.append(
            _finding(
                "speaker_caption_safe_zone_overlap",
                "speaker face overlaps the caption safe zone",
                safe_zone_refs,
            )
        )
    if ratios["occlusion"] > float(policy["maximum_occluded_speaker_frame_ratio"]):
        findings.append(
            _finding(
                "occluded_speaker_face",
                "speaker face occlusion ratio exceeds threshold",
                occlusion_refs,
            )
        )

    contact_sheet = _write_contact_sheet(frames, contact_sheet_path)
    contact_sheet_sha256 = sha256_file(contact_sheet)
    settings = {
        "sample_interval_seconds": interval,
        "speaker_required": speaker_required,
        "safe_zone": zone,
        "thresholds": policy,
        "face_model_sha256": detector_checker["model_sha256"],
        "render_manifest_sha256": render_manifest_sha256,
        "shotlist_sha256": shotlist_sha256,
    }
    report = {
        "schema_version": "1.0.0",
        "category": "visual",
        "job_id": job_id,
        "lane_id": lane,
        "render_id": render_id,
        "render_sha256": render_sha256,
        "status": "fail" if findings else "pass",
        "needs_human_review": False,
        "warnings": [],
        "findings": findings,
        "checker": {
            "name": "video_factory.visual_analyzer",
            "version": VISUAL_ANALYZER_VERSION,
            "run_id": checker_run_id(
                "visual", render_sha256, contact_sheet_sha256, settings
            ),
        },
        "completed_at": utc_now(completed_at),
        "bindings": {
            "output_sha256": render_sha256,
            "render_manifest_sha256": render_manifest_sha256,
            "shotlist_sha256": shotlist_sha256,
            "contact_sheet_sha256": contact_sheet_sha256,
        },
        "metrics": {
            "geometry": {
                "width": actual_width,
                "height": actual_height,
                "aspect_ratio": round(aspect, 9),
                "duration_seconds": round(duration_value, 6),
                "expected_width": manifest_technical["width"],
                "expected_height": manifest_technical["height"],
                "expected_duration_seconds": manifest_technical["duration_seconds"],
                "shotlist_duration_seconds": shotlist_value["duration_seconds"],
            },
            "sample_interval_seconds": interval,
            "sampled_frame_count": frame_count,
            "speaker_required": speaker_required,
            "safe_zone": zone,
            "thresholds": policy,
            "face_detector": detector_checker,
            "observations": observations,
            "summary": {
                "geometry_failed": geometry_failed,
                "black_bar_frame_count": len(black_refs),
                "blurred_frame_count": len(blur_refs),
                "face_frame_count": len(face_refs),
                "speaker_frame_count": len(speaker_refs),
                "small_speaker_face_frame_count": len(small_speaker_refs),
                "cropped_speaker_frame_count": len(cropped_speaker_refs),
                "cropped_detected_face_frame_count": len(cropped_face_refs),
                "safe_zone_overlap_frame_count": len(safe_zone_refs),
                "occluded_speaker_frame_count": len(occlusion_refs),
                "speaker_coverage_ratio": round(speaker_ratio, 6),
                "median_speaker_face_area_ratio": round(
                    median_speaker_face_area, 6
                ),
                "small_speaker_face_frame_ratio": round(
                    small_speaker_face_ratio, 6
                ),
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
        "contact_sheet": {
            "path": str(contact_sheet),
            "sha256": contact_sheet_sha256,
        },
    }


__all__ = [
    "DEFAULT_SAFE_ZONE",
    "DEFAULT_THRESHOLDS",
    "FaceObserver",
    "analyze_visual",
]
