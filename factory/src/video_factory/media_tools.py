"""Cached FFmpeg transforms for edit proxies and Telegram delivery files."""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

from .derived_cache import DerivedCache
from .errors import ValidationError


MEDIA_TRANSFORM_VERSION = "1.0.0"
MEDIA_MODES = ("proxy", "draft", "telegram")


def resolve_media_binary(name: str) -> str:
    if name not in {"ffmpeg", "ffprobe"}:
        raise ValidationError("media binary must be ffmpeg or ffprobe")
    environment = f"VIDEO_FACTORY_{name.upper()}"
    configured = os.environ.get(environment)
    executable = f"{name}.exe" if os.name == "nt" else name
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(found) if (found := shutil.which(name)) else None,
        Path.home() / "bin" / executable,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate.resolve())
    raise ValidationError(f"{name} is required; configure {environment}")


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
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
        raise ValidationError(f"media command failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().replace("\r", " ").replace("\n", " ")[-1200:]
        raise ValidationError(f"media command exited {completed.returncode}: {detail}")
    return completed


@functools.lru_cache(maxsize=4)
def ffmpeg_version(ffmpeg_path: str | None = None) -> str:
    binary = ffmpeg_path or resolve_media_binary("ffmpeg")
    completed = _run([binary, "-version"], timeout=10)
    first = completed.stdout.splitlines()[0] if completed.stdout else "unknown"
    return first.strip()


@functools.lru_cache(maxsize=4)
def h264_nvenc_usable(ffmpeg_path: str | None = None) -> bool:
    """Test the encoder, not merely whether FFmpeg lists it."""

    binary = ffmpeg_path or resolve_media_binary("ffmpeg")
    try:
        completed = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                # Older NVENC generations reject tiny frames even when the
                # encoder is healthy. 256x256 stays above their minimum.
                "color=size=256x256:rate=1",
                "-frames:v",
                "1",
                "-c:v",
                "h264_nvenc",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def probe_media(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValidationError(f"media file does not exist: {source}")
    ffprobe = resolve_media_binary("ffprobe")
    completed = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,size,bit_rate,start_time,format_name:"
                "stream=index,codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,"
                "r_frame_rate,sample_rate,channels,duration,start_time"
            ),
            "-of",
            "json",
            str(source),
        ],
        timeout=60,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError("ffprobe returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise ValidationError("ffprobe returned no streams")
    payload["path"] = str(source)
    return payload


def _frame_rate(stream: dict[str, Any]) -> float | None:
    for field in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(field)
        if not value:
            continue
        try:
            rate = float(Fraction(str(value)))
        except (ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            return rate
    return None


def media_summary(probe: dict[str, Any]) -> dict[str, Any]:
    streams = probe.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    format_data = probe.get("format") if isinstance(probe.get("format"), dict) else {}

    def number(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return {
        "path": probe.get("path"),
        "duration_seconds": number(format_data.get("duration")),
        "size_bytes": int(format_data["size"]) if str(format_data.get("size", "")).isdigit() else None,
        "video": (
            {
                "codec": video.get("codec_name"),
                "width": video.get("width"),
                "height": video.get("height"),
                "pixel_format": video.get("pix_fmt"),
                "fps": _frame_rate(video),
                "duration_seconds": number(video.get("duration")),
                "start_time_seconds": number(video.get("start_time")),
            }
            if isinstance(video, dict)
            else None
        ),
        "audio": (
            {
                "codec": audio.get("codec_name"),
                "sample_rate_hz": (
                    int(audio["sample_rate"])
                    if str(audio.get("sample_rate", "")).isdigit()
                    else None
                ),
                "channels": audio.get("channels"),
                "duration_seconds": number(audio.get("duration")),
                "start_time_seconds": number(audio.get("start_time")),
            }
            if isinstance(audio, dict)
            else None
        ),
    }


def _video_filter(mode: str, proxy_max_height: int) -> str:
    if mode == "proxy":
        return (
            "scale=trunc(iw*min(1\\," + str(proxy_max_height)
            + "/ih)/2)*2:trunc(ih*min(1\\," + str(proxy_max_height)
            + "/ih)/2)*2:flags=lanczos,setsar=1,fps=30"
        )
    if mode in {"draft", "telegram"}:
        return (
            "scale=720:1280:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2:flags=lanczos,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30"
        )
    raise ValidationError(f"unknown media mode: {mode}")


def transcode_cached(
    source: str | Path,
    *,
    mode: str,
    cache_root: str | Path | None = None,
    proxy_max_height: int = 960,
    prefer_gpu: bool = True,
    ffmpeg_threads: int | None = None,
) -> dict[str, Any]:
    if mode not in MEDIA_MODES:
        raise ValidationError(f"mode must be one of: {', '.join(MEDIA_MODES)}")
    if isinstance(proxy_max_height, bool) or not 240 <= proxy_max_height <= 2160:
        raise ValidationError("proxy_max_height must be from 240 to 2160")
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise ValidationError(f"media file does not exist: {source_path}")
    ffmpeg = resolve_media_binary("ffmpeg")
    threads = ffmpeg_threads or int(os.environ.get("VIDEO_FACTORY_FFMPEG_THREADS", "4"))
    if not 1 <= threads <= 64:
        raise ValidationError("ffmpeg_threads must be from 1 to 64")
    # Delivery files favor libx264's materially smaller output. NVENC is kept
    # for disposable proxies/drafts where freeing CPU time matters more.
    gpu = bool(prefer_gpu and mode in {"proxy", "draft"} and h264_nvenc_usable(ffmpeg))
    encoder = "h264_nvenc" if gpu else "libx264"
    quality = 27 if mode == "draft" else 25 if mode == "proxy" else 23
    video_filter = _video_filter(mode, proxy_max_height)
    version = ffmpeg_version(ffmpeg)
    options = {
        "mode": mode,
        "video_filter": video_filter,
        "encoder": encoder,
        "quality": quality,
        "audio": "aac/96k/48000/stereo",
        "ffmpeg": version,
        "transform_version": MEDIA_TRANSFORM_VERSION,
    }
    cache = DerivedCache(cache_root)

    def build(destination: Path) -> None:
        video_options = (
            ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", str(quality), "-b:v", "0"]
            if gpu
            else [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                str(quality),
                "-threads",
                str(threads),
            ]
        )
        command = [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            video_filter,
            *video_options,
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-sn",
            "-dn",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(destination),
        ]
        _run(command, timeout=3600)

    started = time.monotonic()
    entry = cache.get_or_build(
        namespace="media-transform",
        version=MEDIA_TRANSFORM_VERSION,
        sources=[source_path],
        options=options,
        suffix=".mp4",
        builder=build,
    )
    result_probe = probe_media(entry["path"])
    return {
        "ok": True,
        "command": "cache-media",
        "mode": mode,
        "source": str(source_path),
        "output": entry["path"],
        "cache_key": entry["cache_key"],
        "cache_hit": entry["cache_hit"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "size_bytes": entry["size_bytes"],
        "sha256": entry["sha256"],
        "encoder": encoder,
        "gpu_encoded": gpu,
        "probe": media_summary(result_probe),
    }


__all__ = [
    "MEDIA_MODES",
    "h264_nvenc_usable",
    "media_summary",
    "probe_media",
    "resolve_media_binary",
    "transcode_cached",
]
