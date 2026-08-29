"""Offline, checksum-pinned Faster-Whisper caption observer.

The normal executable contract is deliberately tiny: read exactly one JSON
object from stdin and write exactly one word-level transcript JSON object to
stdout. Diagnostics are stderr-only and inference failures never produce a
partial transcript.

The model is an already-converted local Faster-Whisper directory. Every job
checks its deterministic tree fingerprint against a root-owned environment
value before loading it with ``local_files_only=True``. Model downloads and
payload/script fallbacks are intentionally unsupported.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO


_ENGINE_DISTRIBUTION = "faster-whisper"
_ENGINE_VERSION = "1.2.1"
_MAX_STDIN_BYTES = 64 * 1024
_MODEL_FILE_LIMIT = 256
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_WORD_TOKEN = re.compile(r"[^\W_]+(?:[-'’][^\W_]+)*", re.UNICODE)
_LANES = frozenset(
    {"war_history", "celebrity_news", "motivation", "chinese_medicine", "health"}
)
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "lane_id",
        "render_id",
        "render_path",
        "render_sha256",
        "duration_seconds",
        "language",
        "require_word_timestamps",
    }
)
_REQUIRED_MODEL_FILES = frozenset({"config.json", "model.bin", "tokenizer.json"})
_OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "DO_NOT_TRACK": "1",
}
_CREDENTIAL_ENVIRONMENT = (
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
)


class CaptionObserverError(ValueError):
    """Expected, safe-to-report observer failure."""


class _Discard:
    """Keep third-party progress output away from the JSON stdout contract."""

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptionObserverError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise CaptionObserverError(f"{field} must be finite")
    return result


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CaptionObserverError(f"{name} must be configured")
    return value.strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise CaptionObserverError("cannot read a pinned local file") from exc
    return digest.hexdigest()


def _absolute_regular_file(raw: Any, field: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise CaptionObserverError(f"{field} must be a non-empty absolute path")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise CaptionObserverError(f"{field} must be absolute")
    if candidate.is_symlink():
        raise CaptionObserverError(f"{field} must not be a symlink")
    path = candidate.resolve()
    if not path.is_file():
        raise CaptionObserverError(f"{field} must be a regular file")
    return path


def _absolute_model_directory(raw: str | Path) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise CaptionObserverError("caption model path must be absolute")
    if candidate.is_symlink():
        raise CaptionObserverError("caption model path must not be a symlink")
    path = candidate.resolve()
    if not path.is_dir():
        raise CaptionObserverError("caption model path must be a directory")
    return path


def _model_files(root: Path) -> list[Path]:
    files: list[Path] = []
    try:
        entries = sorted(root.rglob("*"), key=lambda value: value.as_posix())
    except OSError as exc:
        raise CaptionObserverError("cannot enumerate caption model directory") from exc
    for entry in entries:
        if entry.is_symlink():
            raise CaptionObserverError("caption model directory must not contain symlinks")
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise CaptionObserverError("caption model directory contains a special file")
        files.append(entry)
    if not files or len(files) > _MODEL_FILE_LIMIT:
        raise CaptionObserverError("caption model directory has an invalid file count")
    relative_names = {path.relative_to(root).as_posix() for path in files}
    missing = sorted(_REQUIRED_MODEL_FILES - relative_names)
    if missing:
        raise CaptionObserverError(
            "caption model directory is incomplete: " + ", ".join(missing)
        )
    if not any(
        name.startswith("vocabulary.") and "/" not in name
        for name in relative_names
    ):
        raise CaptionObserverError(
            "caption model directory is incomplete: vocabulary.*"
        )
    return files


def model_tree_fingerprint(raw: str | Path) -> str:
    """Hash names, sizes and bytes of a staged local model deterministically."""

    root = _absolute_model_directory(raw)
    digest = hashlib.sha256(b"video-factory-caption-model-v1\0")
    for path in _model_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise CaptionObserverError("cannot stat a pinned model file") from exc
        digest.update(relative)
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise CaptionObserverError("cannot read a pinned model file") from exc
        digest.update(b"\0")
    return digest.hexdigest()


def _model_snapshot(root: Path) -> tuple[tuple[str, int, int, int], ...]:
    values: list[tuple[str, int, int, int]] = []
    for path in _model_files(root):
        try:
            stat = path.stat()
        except OSError as exc:
            raise CaptionObserverError("cannot stat a pinned model file") from exc
        values.append(
            (
                path.relative_to(root).as_posix(),
                stat.st_size,
                stat.st_mtime_ns,
                getattr(stat, "st_ino", 0),
            )
        )
    return tuple(values)


def _validate_request(value: Any) -> tuple[dict[str, Any], Path, float]:
    if not isinstance(value, dict) or set(value) != _REQUEST_FIELDS:
        raise CaptionObserverError("stdin must contain the exact caption observer request fields")
    if value.get("schema_version") != "1.0.0":
        raise CaptionObserverError("schema_version must be 1.0.0")
    for field in ("job_id", "render_id"):
        raw = value.get(field)
        if not isinstance(raw, str) or not _SAFE_ID.fullmatch(raw) or ".." in raw:
            raise CaptionObserverError(f"{field} contains unsafe characters")
    if value.get("lane_id") not in _LANES:
        raise CaptionObserverError("lane_id is unsupported")
    if value.get("language") != "ru":
        raise CaptionObserverError("language must be ru")
    if value.get("require_word_timestamps") is not True:
        raise CaptionObserverError("require_word_timestamps must be true")
    expected_sha256 = value.get("render_sha256")
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        raise CaptionObserverError("render_sha256 must be lowercase SHA-256")
    duration = _finite_number(value.get("duration_seconds"), "duration_seconds")
    if duration <= 0 or duration > 180:
        raise CaptionObserverError("duration_seconds must be within 0..180")
    render_path = _absolute_regular_file(value.get("render_path"), "render_path")
    if _sha256_file(render_path) != expected_sha256:
        raise CaptionObserverError("render bytes do not match render_sha256")
    return dict(value), render_path, duration


def _language_probability_threshold() -> float:
    raw = os.environ.get("VIDEO_FACTORY_CAPTION_LANGUAGE_PROBABILITY_MIN", "0.65")
    try:
        threshold = float(raw)
    except (TypeError, ValueError) as exc:
        raise CaptionObserverError(
            "VIDEO_FACTORY_CAPTION_LANGUAGE_PROBABILITY_MIN must be a number"
        ) from exc
    if not math.isfinite(threshold) or threshold < 0.5 or threshold > 1:
        raise CaptionObserverError(
            "VIDEO_FACTORY_CAPTION_LANGUAGE_PROBABILITY_MIN must be within 0.5..1"
        )
    return threshold


def _engine_settings() -> tuple[str, int, str, int, int]:
    device = os.environ.get("VIDEO_FACTORY_CAPTION_DEVICE", "cuda").strip()
    if device not in {"cuda", "cpu"}:
        raise CaptionObserverError("VIDEO_FACTORY_CAPTION_DEVICE must be cuda or cpu")
    default_compute = "float16" if device == "cuda" else "int8"
    compute_type = os.environ.get(
        "VIDEO_FACTORY_CAPTION_COMPUTE_TYPE", default_compute
    ).strip()
    if compute_type not in {
        "default",
        "float16",
        "float32",
        "bfloat16",
        "int8",
        "int8_float16",
        "int8_float32",
        "int16",
    }:
        raise CaptionObserverError("VIDEO_FACTORY_CAPTION_COMPUTE_TYPE is unsupported")
    try:
        device_index = int(os.environ.get("VIDEO_FACTORY_CAPTION_DEVICE_INDEX", "0"))
        cpu_threads = int(os.environ.get("VIDEO_FACTORY_CAPTION_CPU_THREADS", "0"))
        beam_size = int(os.environ.get("VIDEO_FACTORY_CAPTION_BEAM_SIZE", "5"))
    except ValueError as exc:
        raise CaptionObserverError("caption engine integer setting is invalid") from exc
    if device_index < 0 or device_index > 15:
        raise CaptionObserverError("VIDEO_FACTORY_CAPTION_DEVICE_INDEX must be within 0..15")
    if cpu_threads < 0 or cpu_threads > 128:
        raise CaptionObserverError("VIDEO_FACTORY_CAPTION_CPU_THREADS must be within 0..128")
    if beam_size < 1 or beam_size > 10:
        raise CaptionObserverError("VIDEO_FACTORY_CAPTION_BEAM_SIZE must be within 1..10")
    return device, device_index, compute_type, cpu_threads, beam_size


def _engine_class() -> type[Any]:
    try:
        from faster_whisper import WhisperModel
    except (ImportError, OSError) as exc:
        raise CaptionObserverError(
            "pinned faster-whisper caption observer dependency is unavailable"
        ) from exc
    return WhisperModel


def _installed_engine_version(
    version_getter: Callable[[str], str] = importlib.metadata.version,
) -> str:
    try:
        version = version_getter(_ENGINE_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError as exc:
        raise CaptionObserverError(
            "pinned faster-whisper caption observer dependency is unavailable"
        ) from exc
    if version != _ENGINE_VERSION:
        raise CaptionObserverError(
            f"faster-whisper version drift: expected {_ENGINE_VERSION}"
        )
    return version


def _word_measurements(segments: Iterable[Any], duration: float) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    previous_start = -1.0
    for segment in segments:
        segment_words = getattr(segment, "words", None)
        if segment_words is None:
            raise CaptionObserverError("engine did not return word-level timestamps")
        for raw_word in segment_words:
            raw_text = getattr(raw_word, "word", None)
            if not isinstance(raw_text, str):
                raise CaptionObserverError("engine returned a word without text")
            text = raw_text.strip()
            tokens = _WORD_TOKEN.findall(text)
            if not text or len(text) > 80:
                raise CaptionObserverError("engine returned a non-atomic word token")
            # Faster-Whisper can emit a standalone dash or other punctuation as
            # its own timestamped item.  It is not a spoken word and therefore
            # does not belong in the word-evidence contract.  Real multi-word
            # items still fail closed instead of being split with invented
            # timings.
            if not tokens:
                continue
            if len(tokens) != 1:
                raise CaptionObserverError("engine returned a non-atomic word token")
            start = _finite_number(
                getattr(raw_word, "start", None), "engine word start"
            )
            end = _finite_number(getattr(raw_word, "end", None), "engine word end")
            if start < 0 or end <= start or start < previous_start:
                raise CaptionObserverError("engine returned invalid or unordered word timing")
            if end > duration + 0.25:
                raise CaptionObserverError("engine word timing exceeds media duration")
            raw_probability = getattr(raw_word, "probability", None)
            confidence: float | None
            if raw_probability is None:
                confidence = None
            else:
                confidence = _finite_number(raw_probability, "engine word confidence")
                if confidence < 0 or confidence > 1:
                    raise CaptionObserverError("engine word confidence is outside 0..1")
                confidence = round(confidence, 6)
            start = round(start, 4)
            end = round(end, 4)
            if end <= start:
                raise CaptionObserverError("engine word timing loses precision when serialized")
            words.append(
                {
                    "text": text,
                    "start_seconds": start,
                    "end_seconds": end,
                    "confidence": confidence,
                }
            )
            previous_start = start
    if not words:
        raise CaptionObserverError("engine returned no spoken words")
    return words


def _prepare_offline_environment() -> None:
    for name, value in _OFFLINE_ENVIRONMENT.items():
        os.environ[name] = value
    for name in _CREDENTIAL_ENVIRONMENT:
        os.environ.pop(name, None)


def observe(
    request: Any,
    *,
    model_class: type[Any] | None = None,
    version_getter: Callable[[str], str] = importlib.metadata.version,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Run one real local inference and return the handler's exact measurement."""

    normalized, render_path, requested_duration = _validate_request(request)
    model_root = _absolute_model_directory(
        _required_environment("VIDEO_FACTORY_CAPTION_MODEL_PATH")
    )
    expected_model_sha256 = _required_environment(
        "VIDEO_FACTORY_CAPTION_MODEL_SHA256"
    )
    if not _SHA256.fullmatch(expected_model_sha256):
        raise CaptionObserverError(
            "VIDEO_FACTORY_CAPTION_MODEL_SHA256 must be lowercase SHA-256"
        )
    model_sha256 = model_tree_fingerprint(model_root)
    if model_sha256 != expected_model_sha256:
        raise CaptionObserverError("caption model bytes do not match the pinned fingerprint")
    before_model = _model_snapshot(model_root)
    engine_version = _installed_engine_version(version_getter)
    device, device_index, compute_type, cpu_threads, beam_size = _engine_settings()
    language_threshold = _language_probability_threshold()
    _prepare_offline_environment()
    whisper_model = model_class or _engine_class()

    discard = _Discard()
    try:
        with contextlib.redirect_stdout(discard), contextlib.redirect_stderr(discard):
            model = whisper_model(
                str(model_root),
                device=device,
                device_index=device_index,
                compute_type=compute_type,
                cpu_threads=cpu_threads,
                num_workers=1,
                local_files_only=True,
                use_auth_token=False,
            )
            segments, info = model.transcribe(
                str(render_path),
                task="transcribe",
                language=None,
                log_progress=False,
                beam_size=beam_size,
                temperature=0.0,
                condition_on_previous_text=False,
                word_timestamps=True,
                vad_filter=True,
            )
            detected_language = getattr(info, "language", None)
            language_probability = _finite_number(
                getattr(info, "language_probability", None),
                "engine language probability",
            )
            measured_duration = _finite_number(
                getattr(info, "duration", None), "engine media duration"
            )
            if detected_language != "ru" or language_probability < language_threshold:
                raise CaptionObserverError("engine did not confidently detect Russian speech")
            if measured_duration <= 0 or abs(measured_duration - requested_duration) > 0.25:
                raise CaptionObserverError("engine media duration does not match the request")
            words = _word_measurements(segments, measured_duration)
    except CaptionObserverError:
        raise
    except Exception as exc:
        raise CaptionObserverError("local caption inference failed") from exc

    if _sha256_file(render_path) != normalized["render_sha256"]:
        raise CaptionObserverError("render bytes changed during caption inference")
    if (
        _model_snapshot(model_root) != before_model
        or model_tree_fingerprint(model_root) != model_sha256
    ):
        raise CaptionObserverError("caption model files changed during inference")

    completed = (now or (lambda: datetime.now(timezone.utc)))()
    if completed.tzinfo is None:
        raise CaptionObserverError("observer clock must be timezone-aware")
    completed_at = completed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    run_material = {
        "job_id": normalized["job_id"],
        "render_id": normalized["render_id"],
        "render_sha256": normalized["render_sha256"],
        "model_sha256": model_sha256,
        "engine_version": engine_version,
        "completed_at": completed_at,
        "words": words,
    }
    run_id = hashlib.sha256(_canonical_json(run_material).encode("utf-8")).hexdigest()[:24]
    return {
        "status": "completed",
        "warnings": [],
        "language": "ru",
        "duration_seconds": round(measured_duration, 4),
        "engine": {
            "name": f"faster-whisper-local-{model_sha256[:12]}",
            "version": engine_version,
            "run_id": run_id,
        },
        "completed_at": completed_at,
        "words": words,
    }


def _read_request(source: TextIO) -> Any:
    try:
        if hasattr(source, "buffer"):
            raw = source.buffer.read(_MAX_STDIN_BYTES + 1)
            if len(raw) > _MAX_STDIN_BYTES:
                raise CaptionObserverError("caption observer stdin exceeds 64 KiB")
            text = raw.decode("utf-8", errors="strict")
        else:
            text = source.read(_MAX_STDIN_BYTES + 1)
            if len(text.encode("utf-8")) > _MAX_STDIN_BYTES:
                raise CaptionObserverError("caption observer stdin exceeds 64 KiB")
        return json.loads(text)
    except UnicodeError as exc:
        raise CaptionObserverError("caption observer stdin must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise CaptionObserverError("caption observer stdin must be one JSON object") from exc


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    target = stdout or sys.stdout
    try:
        if arguments:
            if len(arguments) != 2 or arguments[0] != "--fingerprint-model":
                raise CaptionObserverError(
                    "usage: caption-observer [--fingerprint-model ABSOLUTE_MODEL_PATH]"
                )
            target.write(model_tree_fingerprint(arguments[1]) + "\n")
            target.flush()
            return 0
        request = _read_request(stdin or sys.stdin)
        result = observe(request)
        target.write(_canonical_json(result) + "\n")
        target.flush()
        return 0
    except CaptionObserverError as exc:
        sys.stderr.write(f"caption_observer_error:{exc}\n")
        return 2
    except Exception:
        # No raw third-party exception is emitted: it could include process or
        # driver details and stdout must remain an all-or-nothing JSON channel.
        sys.stderr.write("caption_observer_error:unexpected local failure\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
