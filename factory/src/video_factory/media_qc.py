"""Two-stage, hash-cached media quality control.

FAST is cheap enough for every draft and never authorizes publication. FULL is
reserved for final master/Telegram files. Rights and human editorial approval
remain independent gates even when technical checks pass.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from .derived_cache import DerivedCache
from .errors import ValidationError
from .media_tools import (
    ffmpeg_version,
    media_summary,
    probe_media,
    resolve_media_binary,
)


QC_RUNNER_VERSION = "1.1.0"
QC_LEVELS = ("fast", "full")

QC_PROFILES: dict[str, dict[str, Any]] = {
    "portrait_draft": {
        "version": "1.0.0",
        "exact_resolution": None,
        "aspect": 9 / 16,
        "fps": 30.0,
        "require_h264_aac": False,
        "require_yuv420p": False,
        "sample_rate_hz": 48000,
        "loudness_min": -17.0,
        "loudness_max": -13.0,
        "true_peak_max": -1.0,
        "lra_min": 1.0,
        "lra_max": 10.0,
    },
    "motivation_v3_master": {
        "version": "1.0.0",
        "exact_resolution": [1080, 1920],
        "aspect": 9 / 16,
        "fps": 30.0,
        "require_h264_aac": True,
        "require_yuv420p": True,
        "sample_rate_hz": 48000,
        "loudness_min": -15.0,
        "loudness_max": -14.0,
        "true_peak_max": -1.2,
        "lra_min": 2.0,
        "lra_max": 7.0,
    },
    "motivation_v3_telegram": {
        "version": "1.0.0",
        "exact_resolution": [720, 1280],
        "aspect": 9 / 16,
        "fps": 30.0,
        "require_h264_aac": True,
        "require_yuv420p": True,
        "sample_rate_hz": 48000,
        "loudness_min": -15.5,
        "loudness_max": -13.5,
        "true_peak_max": -1.0,
        "lra_min": 2.0,
        "lra_max": 7.0,
        "size_target_mb_per_second": 0.08,
        "size_hard_mb_per_second": 0.10,
        "size_base_mb": 0.25,
    },
}


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_values(pattern: str, text: str) -> list[float]:
    values: list[float] = []
    for raw in re.findall(pattern, text, flags=re.IGNORECASE):
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values


def _loudnorm_metrics(text: str) -> dict[str, float] | None:
    matches = re.findall(r"\{\s*\"input_i\".*?\}", text, flags=re.DOTALL)
    for raw in reversed(matches):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        metrics: dict[str, float] = {}
        mapping = {
            "integrated_lufs": "input_i",
            "true_peak_dbtp": "input_tp",
            "lra_lu": "input_lra",
            "threshold_lufs": "input_thresh",
        }
        try:
            for destination, source in mapping.items():
                metrics[destination] = float(value[source])
        except (KeyError, TypeError, ValueError):
            continue
        return metrics
    return None


def _scan_media(
    source: Path,
    *,
    level: str,
    has_video: bool,
    has_audio: bool,
    threads: int,
) -> dict[str, Any]:
    ffmpeg = resolve_media_binary("ffmpeg")
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-threads",
        str(threads),
        "-i",
        str(source),
    ]
    if has_video:
        video_filter = "blackdetect=d=0.10:pix_th=0.10,freezedetect=n=-50dB:d=1.50"
        if level == "fast":
            video_filter = "scale=-2:270," + video_filter
        command.extend(["-vf", video_filter])
    if has_audio:
        audio_filter = "silencedetect=noise=-50dB:d=0.20"
        if level == "full":
            audio_filter += ",loudnorm=I=-14.5:TP=-1.2:LRA=7:print_format=json"
        command.extend(["-af", audio_filter])
    command.extend(["-f", "null", "-"])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError(f"FFmpeg QC scan failed: {exc}") from exc
    log = completed.stderr
    if completed.returncode != 0:
        detail = log.strip().replace("\r", " ").replace("\n", " ")[-1200:]
        raise ValidationError(f"FFmpeg QC decode failed: {detail}")
    black = _event_values(r"black_duration:([0-9.]+)", log)
    freeze = _event_values(r"freeze_duration:\s*([0-9.]+)", log)
    silence = _event_values(r"silence_duration:\s*([0-9.]+)", log)
    return {
        "decode_passed": True,
        "black_durations_seconds": black,
        "freeze_durations_seconds": freeze,
        "silence_durations_seconds": silence,
        "max_black_seconds": max(black, default=0.0),
        "max_freeze_seconds": max(freeze, default=0.0),
        "max_silence_seconds": max(silence, default=0.0),
        "loudness": _loudnorm_metrics(log) if level == "full" and has_audio else None,
    }


def _technical_checks(
    summary: Mapping[str, Any],
    scan: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    level: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    def fail(code: str, message: str) -> None:
        failures.append({"code": code, "message": message})

    def warn(code: str, message: str) -> None:
        warnings.append({"code": code, "message": message})

    video = summary.get("video")
    audio = summary.get("audio")
    if not isinstance(video, Mapping):
        fail("missing_video", "no video stream")
    if not isinstance(audio, Mapping):
        fail("missing_audio", "no audio stream")
    if isinstance(video, Mapping):
        width = video.get("width")
        height = video.get("height")
        exact = profile.get("exact_resolution")
        if exact and [width, height] != list(exact):
            fail("resolution", f"expected {exact[0]}x{exact[1]}, got {width}x{height}")
        elif width and height:
            aspect_error = abs((float(width) / float(height)) - float(profile["aspect"]))
            if aspect_error > 0.01:
                fail("aspect_ratio", "video is not a 9:16 portrait frame")
        fps = _float(video.get("fps"))
        if fps is None or abs(fps - float(profile["fps"])) > 0.01:
            fail("fps", f"expected CFR {profile['fps']:.0f} fps, got {fps}")
        if profile.get("require_h264_aac") and video.get("codec") != "h264":
            fail("video_codec", f"expected h264, got {video.get('codec')}")
        if profile.get("require_yuv420p") and video.get("pixel_format") != "yuv420p":
            fail("pixel_format", f"expected yuv420p, got {video.get('pixel_format')}")
    if isinstance(audio, Mapping):
        if profile.get("require_h264_aac") and audio.get("codec") != "aac":
            fail("audio_codec", f"expected aac, got {audio.get('codec')}")
        if audio.get("sample_rate_hz") != profile["sample_rate_hz"]:
            fail(
                "sample_rate",
                f"expected {profile['sample_rate_hz']} Hz, got {audio.get('sample_rate_hz')}",
            )
        if audio.get("channels") != 2:
            warn("audio_channels", f"expected stereo, got {audio.get('channels')} channels")
    if isinstance(video, Mapping) and isinstance(audio, Mapping):
        video_duration = _float(video.get("duration_seconds"))
        audio_duration = _float(audio.get("duration_seconds"))
        if video_duration is not None and audio_duration is not None:
            drift = abs(video_duration - audio_duration)
            if drift > (1 / 30):
                fail("av_drift", f"audio/video duration drift is {drift:.3f}s")
    size_rate = profile.get("size_target_mb_per_second")
    if size_rate is not None:
        duration = _float(summary.get("duration_seconds"))
        size_bytes = summary.get("size_bytes")
        if duration is not None and isinstance(size_bytes, int):
            target_bytes = (
                float(size_rate) * duration + float(profile["size_base_mb"])
            ) * 1_000_000
            hard_bytes = (
                float(profile["size_hard_mb_per_second"]) * duration
                + float(profile["size_base_mb"])
            ) * 1_000_000
            if size_bytes > hard_bytes:
                fail(
                    "delivery_size",
                    f"{size_bytes} bytes exceeds hard Telegram cap {int(hard_bytes)}",
                )
            elif size_bytes > target_bytes:
                warn(
                    "delivery_size",
                    f"{size_bytes} bytes exceeds Telegram target {int(target_bytes)}",
                )
    if float(scan.get("max_black_seconds", 0)) >= 0.10:
        fail("black_frame_run", "black run is at least 0.10s")
    if float(scan.get("max_freeze_seconds", 0)) >= 1.50:
        fail("freeze_run", "unmarked freeze is at least 1.50s")
    max_silence = float(scan.get("max_silence_seconds", 0))
    if max_silence > 0.75:
        if level == "full" and profile.get("exact_resolution"):
            fail("silence_run", f"unexplained silence is {max_silence:.3f}s")
        else:
            warn("silence_run", f"silence longer than 0.75s: {max_silence:.3f}s")
    loudness = scan.get("loudness")
    if level == "full":
        if not isinstance(loudness, Mapping):
            fail("loudness_missing", "full QC did not produce loudness metrics")
        else:
            integrated = float(loudness["integrated_lufs"])
            peak = float(loudness["true_peak_dbtp"])
            lra = float(loudness["lra_lu"])
            if not profile["loudness_min"] <= integrated <= profile["loudness_max"]:
                fail(
                    "integrated_loudness",
                    f"{integrated:.2f} LUFS is outside {profile['loudness_min']}..{profile['loudness_max']}",
                )
            if peak > profile["true_peak_max"]:
                fail("true_peak", f"{peak:.2f} dBTP exceeds {profile['true_peak_max']} dBTP")
            if not profile["lra_min"] <= lra <= profile["lra_max"]:
                warn(
                    "loudness_range",
                    f"{lra:.2f} LU is outside {profile['lra_min']}..{profile['lra_max']}",
                )
    return failures, warnings


def run_media_qc(
    source: str | Path,
    *,
    level: str = "fast",
    profile_name: str = "portrait_draft",
    cache_root: str | Path | None = None,
    ffmpeg_threads: int | None = None,
) -> dict[str, Any]:
    if level not in QC_LEVELS:
        raise ValidationError(f"level must be one of: {', '.join(QC_LEVELS)}")
    if profile_name not in QC_PROFILES:
        raise ValidationError(f"unknown QC profile: {profile_name}")
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise ValidationError(f"media file does not exist: {source_path}")
    threads = ffmpeg_threads or int(os.environ.get("VIDEO_FACTORY_FFMPEG_THREADS", "4"))
    if not 1 <= threads <= 64:
        raise ValidationError("ffmpeg_threads must be from 1 to 64")
    profile = QC_PROFILES[profile_name]
    ffmpeg = resolve_media_binary("ffmpeg")
    options = {
        "runner_version": QC_RUNNER_VERSION,
        "level": level,
        "profile_name": profile_name,
        "profile_version": profile["version"],
        "ffmpeg_version": ffmpeg_version(ffmpeg),
    }
    cache = DerivedCache(cache_root)

    def build(destination: Path) -> None:
        probe = probe_media(source_path)
        summary = media_summary(probe)
        scan = _scan_media(
            source_path,
            level=level,
            has_video=summary["video"] is not None,
            has_audio=summary["audio"] is not None,
            threads=threads,
        )
        failures, warnings = _technical_checks(summary, scan, profile, level=level)
        technical_pass = not failures
        report = {
            "schema_version": "1.0.0",
            "kind": "media_qc_report",
            "level": level,
            "profile": {"name": profile_name, "version": profile["version"]},
            "source": str(source_path),
            "technical_pass": technical_pass,
            "status": (
                "draft_pass" if technical_pass and not warnings and level == "fast"
                else "draft_warn" if technical_pass and level == "fast"
                else "draft_fail" if level == "fast"
                else "final_technical_pass" if technical_pass
                else "final_technical_fail"
            ),
            "publish_eligible": False,
            "publish_blockers": ["rights_gate", "human_final_review"],
            "media": summary,
            "scan": scan,
            "failures": failures,
            "warnings": warnings,
        }
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    started = time.monotonic()
    entry = cache.get_or_build(
        namespace="media-qc",
        version=QC_RUNNER_VERSION,
        sources=[source_path],
        options=options,
        suffix=".json",
        builder=build,
    )
    try:
        report = json.loads(Path(entry["path"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cached QC report is unreadable: {exc}") from exc
    report["ok"] = bool(report["technical_pass"])
    report["command"] = "media-qc"
    report["cache"] = {
        "hit": entry["cache_hit"],
        "key": entry["cache_key"],
        "report_path": entry["path"],
    }
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


__all__ = ["QC_LEVELS", "QC_PROFILES", "run_media_qc"]
