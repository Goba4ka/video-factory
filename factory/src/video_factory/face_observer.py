"""Offline OpenCV face observer for rendered masters.

The executable accepts the visual analyzer's JSON request either on stdin or
as the sole request-file argument. It never downloads a model. A selected
OpenCV detector and its expected SHA-256 must be configured before the process
starts; an unavailable engine, missing model, checksum drift, malformed frame,
or incomplete observation aborts the whole request.

``speaker`` means an unambiguous on-screen speaker candidate: exactly one face
was measured in the sampled frame. With zero or multiple faces this adapter
marks every face as non-speaker instead of guessing who is active. Audio-aware
active-speaker attribution belongs in a separately versioned stronger adapter.
Occlusion is a geometric proxy derived from detector-box clipping and, when
YuNet supplies them, facial landmarks outside the visible box.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, TextIO

from .errors import ValidationError


FACE_OBSERVER_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0.0"
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RENDER_BYTES = 4 * 1024 * 1024 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_TOTAL_PIXEL_BYTES = 64 * 1024 * 1024
MAX_FRAMES = 400
MAX_FACES_PER_FRAME = 16
DETECTOR_SCORE_THRESHOLD = 0.55

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class GrayImage:
    width: int
    height: int
    pixels: bytes


@dataclass(frozen=True)
class FaceDetection:
    """One detector result in source-frame pixel coordinates."""

    bbox: tuple[float, float, float, float]
    confidence: float
    landmarks: tuple[tuple[float, float], ...] = ()


class FaceBackend(Protocol):
    name: str
    version: str
    model_sha256: str

    def detect(self, frame: GrayImage) -> Sequence[FaceDetection]: ...


BackendFactory = Callable[[], FaceBackend]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ValidationError(f"cannot hash observer input {path}: {exc}") from exc
    return digest.hexdigest()


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValidationError(f"{field} must be lowercase SHA-256")
    return value


def _regular_absolute_file(value: Any, field: str, *, maximum_bytes: int) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValidationError(f"{field} must be absolute")
    if candidate.is_symlink():
        raise ValidationError(f"{field} must not be a symlink")
    path = candidate.resolve()
    if not path.is_file():
        raise ValidationError(f"{field} must be an existing file")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValidationError(f"cannot inspect {field}: {exc}") from exc
    if size <= 0 or size > maximum_bytes:
        raise ValidationError(f"{field} must contain 1..{maximum_bytes} bytes")
    return path


def _positive_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValidationError(f"{field} must be from {minimum} to {maximum}")
    return value


def _read_pgm(path: Path, *, expected_width: int, expected_height: int) -> GrayImage:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValidationError(f"cannot read observer frame {path}: {exc}") from exc
    if len(payload) > MAX_FRAME_BYTES:
        raise ValidationError("observer frame exceeds the byte limit")

    position = 0
    tokens: list[bytes] = []
    while len(tokens) < 4:
        while position < len(payload) and payload[position] in b" \t\r\n":
            position += 1
        if position < len(payload) and payload[position] == ord("#"):
            newline = payload.find(b"\n", position)
            if newline < 0:
                raise ValidationError("PGM comment is not terminated")
            position = newline + 1
            continue
        start = position
        while position < len(payload) and payload[position] not in b" \t\r\n":
            position += 1
        if start == position:
            raise ValidationError("PGM header is truncated")
        tokens.append(payload[start:position])
        if position > 1024:
            raise ValidationError("PGM header exceeds 1024 bytes")
    if tokens[0] != b"P5":
        raise ValidationError("observer frames must be binary PGM (P5)")
    try:
        width, height, maximum = (int(item.decode("ascii")) for item in tokens[1:])
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValidationError("PGM geometry is invalid") from exc
    if width != expected_width or height != expected_height or maximum != 255:
        raise ValidationError("PGM geometry/maxval differs from observer request")
    if position >= len(payload) or payload[position] not in b" \t\r\n":
        raise ValidationError("PGM raster separator is missing")
    position += 2 if payload[position : position + 2] == b"\r\n" else 1
    pixels = payload[position:]
    if len(pixels) != width * height:
        raise ValidationError("PGM raster is truncated or oversized")
    return GrayImage(width=width, height=height, pixels=pixels)


def _import_opencv() -> Any:
    try:
        import cv2  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:
        raise ValidationError(
            "OpenCV engine is unavailable; install the factory visual-qc extra"
        ) from exc
    try:
        cv2.setNumThreads(1)
        cv2.ocl.setUseOpenCL(False)
        cv2.setRNGSeed(0)
    except (AttributeError, TypeError) as exc:
        raise ValidationError("OpenCV build lacks deterministic runtime controls") from exc
    return cv2


def _configured_model(cv2: Any, engine: str) -> tuple[Path, str]:
    raw_model = os.environ.get("VIDEO_FACTORY_FACE_MODEL_PATH")
    if engine == "haar" and not raw_model:
        data = getattr(cv2, "data", None)
        root = getattr(data, "haarcascades", None)
        if not isinstance(root, str) or not root:
            raise ValidationError("OpenCV Haar model directory is unavailable")
        raw_model = str(Path(root) / "haarcascade_frontalface_default.xml")
    model = _regular_absolute_file(
        raw_model,
        "VIDEO_FACTORY_FACE_MODEL_PATH",
        maximum_bytes=256 * 1024 * 1024,
    )
    expected = _sha256(
        os.environ.get("VIDEO_FACTORY_FACE_MODEL_SHA256"),
        "VIDEO_FACTORY_FACE_MODEL_SHA256",
    )
    actual = _sha256_file(model)
    if actual != expected:
        raise ValidationError("configured face model SHA-256 does not match model bytes")
    return model, actual


def _numpy() -> Any:
    try:
        import numpy as np  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:
        raise ValidationError("NumPy required by the OpenCV backend is unavailable") from exc
    return np


def _opencv_model_path(model_path: Path) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    """Mirror a model to an ASCII temp path for OpenCV Windows file APIs."""

    try:
        str(model_path).encode("ascii")
    except UnicodeEncodeError:
        temporary = tempfile.TemporaryDirectory(prefix="video-factory-face-model-")
        mirror = Path(temporary.name) / f"model{model_path.suffix.lower()}"
        try:
            shutil.copyfile(model_path, mirror)
        except OSError as exc:
            temporary.cleanup()
            raise ValidationError(f"cannot stage face model for OpenCV: {exc}") from exc
        return mirror, temporary
    return model_path, None


class _OpenCVYuNetBackend:
    name = "video_factory_face_observer_yunet"
    version = FACE_OBSERVER_VERSION

    def __init__(self, cv2: Any, model_path: Path, model_sha256: str) -> None:
        creator = getattr(cv2, "FaceDetectorYN_create", None)
        if not callable(creator):
            raise ValidationError("OpenCV build does not provide FaceDetectorYN")
        compatible_path, self._model_temporary = _opencv_model_path(model_path)
        try:
            self._detector = creator(
                str(compatible_path),
                "",
                (180, 320),
                DETECTOR_SCORE_THRESHOLD,
                0.3,
                5000,
            )
        except Exception as exc:  # OpenCV exposes backend-specific exceptions.
            raise ValidationError(f"cannot load configured YuNet model: {exc}") from exc
        self._cv2 = cv2
        self.model_sha256 = model_sha256

    def detect(self, frame: GrayImage) -> Sequence[FaceDetection]:
        np = _numpy()
        gray = np.frombuffer(frame.pixels, dtype=np.uint8).reshape(
            (frame.height, frame.width)
        )
        bgr = self._cv2.cvtColor(gray, self._cv2.COLOR_GRAY2BGR)
        try:
            self._detector.setInputSize((frame.width, frame.height))
            _, raw_faces = self._detector.detect(bgr)
        except Exception as exc:
            raise ValidationError(f"YuNet face inference failed: {exc}") from exc
        if raw_faces is None:
            return ()
        results: list[FaceDetection] = []
        for row in raw_faces[:MAX_FACES_PER_FRAME]:
            values = [float(item) for item in row]
            if len(values) < 15:
                raise ValidationError("YuNet returned an incomplete face row")
            results.append(
                FaceDetection(
                    bbox=(values[0], values[1], values[2], values[3]),
                    confidence=values[14],
                    landmarks=tuple(
                        (values[index], values[index + 1])
                        for index in range(4, 14, 2)
                    ),
                )
            )
        return results


class _OpenCVHaarBackend:
    name = "video_factory_face_observer_haar"
    version = FACE_OBSERVER_VERSION

    def __init__(self, cv2: Any, model_path: Path, model_sha256: str) -> None:
        compatible_path, self._model_temporary = _opencv_model_path(model_path)
        try:
            self._cascade = cv2.CascadeClassifier(str(compatible_path))
        except Exception as exc:
            raise ValidationError(f"cannot load configured Haar model: {exc}") from exc
        if self._cascade.empty():
            raise ValidationError("configured Haar model is empty or unsupported")
        self._cv2 = cv2
        self.model_sha256 = model_sha256

    def detect(self, frame: GrayImage) -> Sequence[FaceDetection]:
        np = _numpy()
        gray = np.frombuffer(frame.pixels, dtype=np.uint8).reshape(
            (frame.height, frame.width)
        )
        minimum = max(18, round(min(frame.width, frame.height) * 0.08))
        try:
            boxes, _, weights = self._cascade.detectMultiScale3(
                gray,
                scaleFactor=1.1,
                minNeighbors=5,
                flags=0,
                minSize=(minimum, minimum),
                outputRejectLevels=True,
            )
        except Exception as exc:
            raise ValidationError(f"Haar face inference failed: {exc}") from exc
        results: list[FaceDetection] = []
        for box, weight in zip(boxes, weights, strict=True):
            x, y, width, height = (float(item) for item in box)
            raw_weight = float(weight)
            confidence = 1.0 / (
                1.0 + math.exp(-max(-40.0, min(40.0, raw_weight)))
            )
            results.append(
                FaceDetection(
                    bbox=(x, y, width, height),
                    confidence=confidence,
                )
            )
        results.sort(key=lambda item: (-item.confidence, item.bbox))
        return results[:MAX_FACES_PER_FRAME]


def build_backend() -> FaceBackend:
    engine = os.environ.get("VIDEO_FACTORY_FACE_ENGINE")
    if engine not in {"yunet", "haar"}:
        raise ValidationError(
            "VIDEO_FACTORY_FACE_ENGINE must explicitly be yunet or haar"
        )
    cv2 = _import_opencv()
    model_path, model_sha256 = _configured_model(cv2, engine)
    if engine == "yunet":
        return _OpenCVYuNetBackend(cv2, model_path, model_sha256)
    return _OpenCVHaarBackend(cv2, model_path, model_sha256)


def _normalize_detection(
    detection: FaceDetection, frame: GrayImage
) -> dict[str, Any] | None:
    x, y, width, height = detection.bbox
    confidence = float(detection.confidence)
    if any(not math.isfinite(item) for item in (x, y, width, height, confidence)):
        raise ValidationError("face detector returned a non-finite value")
    if width <= 0 or height <= 0 or not 0.0 <= confidence <= 1.0:
        raise ValidationError("face detector returned invalid geometry/confidence")
    clipped_x = max(0.0, min(float(frame.width), x))
    clipped_y = max(0.0, min(float(frame.height), y))
    clipped_right = max(0.0, min(float(frame.width), x + width))
    clipped_bottom = max(0.0, min(float(frame.height), y + height))
    clipped_width = clipped_right - clipped_x
    clipped_height = clipped_bottom - clipped_y
    if clipped_width <= 0 or clipped_height <= 0:
        return None
    clip_fraction = 1.0 - (clipped_width * clipped_height) / (width * height)
    landmark_fraction = 0.0
    if detection.landmarks:
        missing = sum(
            not (
                clipped_x <= point_x <= clipped_right
                and clipped_y <= point_y <= clipped_bottom
            )
            for point_x, point_y in detection.landmarks
        )
        landmark_fraction = missing / len(detection.landmarks)
    return {
        "bbox": [
            round(clipped_x / frame.width, 8),
            round(clipped_y / frame.height, 8),
            round(clipped_width / frame.width, 8),
            round(clipped_height / frame.height, 8),
        ],
        "confidence": round(confidence, 8),
        "speaker": False,
        "occlusion_fraction": round(
            max(0.0, min(1.0, max(clip_fraction, landmark_fraction))), 8
        ),
    }


def _validate_request(request: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(request, Mapping) or set(request) != {
        "schema_version",
        "render_path",
        "render_sha256",
        "frame_width",
        "frame_height",
        "frames",
    }:
        raise ValidationError("face observer request has invalid fields")
    if request.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError("face observer request schema_version is unsupported")
    render_sha256 = _sha256(request.get("render_sha256"), "render_sha256")
    render_path = _regular_absolute_file(
        request.get("render_path"), "render_path", maximum_bytes=MAX_RENDER_BYTES
    )
    if _sha256_file(render_path) != render_sha256:
        raise ValidationError("render_sha256 does not match render bytes")
    width = _positive_int(
        request.get("frame_width"), "frame_width", minimum=8, maximum=1080
    )
    height = _positive_int(
        request.get("frame_height"), "frame_height", minimum=8, maximum=1920
    )
    raw_frames = request.get("frames")
    if not isinstance(raw_frames, list) or not 1 <= len(raw_frames) <= MAX_FRAMES:
        raise ValidationError(f"frames must contain 1..{MAX_FRAMES} items")
    if len(raw_frames) * width * height > MAX_TOTAL_PIXEL_BYTES:
        raise ValidationError("requested frame pixels exceed the aggregate limit")
    frames: list[dict[str, Any]] = []
    previous_timestamp = -1.0
    for position, row in enumerate(raw_frames):
        if not isinstance(row, Mapping) or set(row) != {
            "frame_index",
            "timestamp_seconds",
            "frame_sha256",
            "path",
        }:
            raise ValidationError(f"frames[{position}] has invalid fields")
        index = row.get("frame_index")
        if index != position or isinstance(index, bool):
            raise ValidationError("frame indexes must be contiguous and ordered")
        timestamp = row.get("timestamp_seconds")
        if (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(float(timestamp))
            or float(timestamp) < previous_timestamp
        ):
            raise ValidationError("frame timestamps must be finite and ordered")
        previous_timestamp = float(timestamp)
        expected_sha256 = _sha256(
            row.get("frame_sha256"), f"frames[{position}].frame_sha256"
        )
        path = _regular_absolute_file(
            row.get("path"),
            f"frames[{position}].path",
            maximum_bytes=MAX_FRAME_BYTES,
        )
        image = _read_pgm(path, expected_width=width, expected_height=height)
        if hashlib.sha256(image.pixels).hexdigest() != expected_sha256:
            raise ValidationError(f"frames[{position}] SHA-256 does not match pixels")
        frames.append(
            {
                "frame_index": position,
                "frame_sha256": expected_sha256,
                "image": image,
            }
        )
    return render_sha256, frames


def observe_request(
    request: Mapping[str, Any], *, backend: FaceBackend | None = None
) -> dict[str, Any]:
    render_sha256, frames = _validate_request(request)
    detector = backend or build_backend()
    if not isinstance(detector.name, str) or not detector.name:
        raise ValidationError("face backend name is missing")
    if not isinstance(detector.version, str) or not detector.version:
        raise ValidationError("face backend version is missing")
    model_sha256 = _sha256(detector.model_sha256, "face backend model_sha256")

    observations: list[dict[str, Any]] = []
    for frame_row in frames:
        image = frame_row["image"]
        raw = detector.detect(image)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise ValidationError("face backend returned no detection sequence")
        if len(raw) > MAX_FACES_PER_FRAME:
            raise ValidationError("face backend exceeded the per-frame face limit")
        faces: list[dict[str, Any]] = []
        for detection in raw:
            if not isinstance(detection, FaceDetection):
                raise ValidationError("face backend returned an invalid detection")
            value = _normalize_detection(detection, image)
            if value is not None:
                faces.append(value)
        faces.sort(
            key=lambda item: (
                -float(item["confidence"]),
                -float(item["bbox"][2]) * float(item["bbox"][3]),
                item["bbox"],
            )
        )
        if len(faces) == 1:
            faces[0]["speaker"] = True
        observations.append(
            {
                "frame_index": frame_row["frame_index"],
                "frame_sha256": frame_row["frame_sha256"],
                "faces": faces,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "render_sha256": render_sha256,
        "checker": {
            "name": detector.name,
            "version": detector.version,
            "model_sha256": model_sha256,
        },
        "observations": observations,
    }


def _read_request(argv: Sequence[str], stdin: TextIO) -> Mapping[str, Any]:
    if len(argv) > 1:
        raise ValidationError("face observer accepts at most one request-file argument")
    if argv:
        path = _regular_absolute_file(
            argv[0], "request_path", maximum_bytes=MAX_REQUEST_BYTES
        )
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValidationError(f"cannot read observer request: {exc}") from exc
    else:
        raw = stdin.read(MAX_REQUEST_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValidationError("face observer request exceeds 8 MiB")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("face observer request is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValidationError("face observer request must be a JSON object")
    return value


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    backend_factory: BackendFactory = build_backend,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    try:
        request = _read_request(arguments, input_stream)
        result = observe_request(request, backend=backend_factory())
        output_stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
        output_stream.flush()
        return 0
    except (ValidationError, OSError, ValueError) as exc:
        error_stream.write(f"face observer failed closed: {exc}\n")
        error_stream.flush()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FACE_OBSERVER_VERSION",
    "FaceBackend",
    "FaceDetection",
    "GrayImage",
    "build_backend",
    "main",
    "observe_request",
]
