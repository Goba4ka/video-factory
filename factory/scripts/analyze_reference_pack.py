from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
from pathlib import Path


FFMPEG = Path.home() / "bin" / "ffmpeg.exe"
FFPROBE = Path.home() / "bin" / "ffprobe.exe"


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def probe(path: Path) -> dict:
    result = run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    return json.loads(result.stdout)


def scene_cuts(path: Path, threshold: float) -> int:
    result = run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-vf",
            f"select='gt(scene,{threshold})',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    return len(re.findall(r"Parsed_showinfo.*?\bn:\s*\d+", result.stderr))


def loudness(path: Path) -> dict:
    result = run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
            "-vn",
            "-f",
            "null",
            "-",
        ]
    )
    blocks = re.findall(r"\{\s*\"input_i\".*?\}", result.stderr, flags=re.S)
    return json.loads(blocks[-1]) if blocks else {}


def silence(path: Path) -> dict:
    result = run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "silencedetect=noise=-50dB:d=0.2",
            "-vn",
            "-f",
            "null",
            "-",
        ]
    )
    durations = [
        float(value)
        for value in re.findall(r"silence_duration:\s*([0-9.]+)", result.stderr)
    ]
    return {
        "events": len(durations),
        "total_seconds": round(sum(durations), 3),
        "max_seconds": round(max(durations, default=0.0), 3),
    }


def signal_stats(path: Path) -> dict:
    result = run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-vf",
            "fps=1,signalstats,metadata=print",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    yavg = [float(v) for v in re.findall(r"lavfi\.signalstats\.YAVG=([0-9.]+)", result.stderr)]
    satavg = [float(v) for v in re.findall(r"lavfi\.signalstats\.SATAVG=([0-9.]+)", result.stderr)]
    return {
        "sample_count": len(yavg),
        "yavg_mean": round(statistics.fmean(yavg), 2) if yavg else None,
        "yavg_p10": round(sorted(yavg)[max(0, int(len(yavg) * 0.1) - 1)], 2) if yavg else None,
        "satavg_mean": round(statistics.fmean(satavg), 2) if satavg else None,
    }


def analyze(path: Path) -> dict:
    info = probe(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    duration = float(info["format"]["duration"])
    cuts_015 = scene_cuts(path, 0.15)
    cuts_025 = scene_cuts(path, 0.25)
    return {
        "source": str(path),
        "duration_seconds": round(duration, 3),
        "width": video["width"],
        "height": video["height"],
        "fps": video["r_frame_rate"],
        "video_codec": video["codec_name"],
        "pixel_format": video.get("pix_fmt"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "audio_sample_rate_hz": int(audio["sample_rate"]) if audio else None,
        "scene_cuts": {"threshold_0_15": cuts_015, "threshold_0_25": cuts_025},
        "estimated_seconds_per_shot": {
            "threshold_0_15": round(duration / (cuts_015 + 1), 2),
            "threshold_0_25": round(duration / (cuts_025 + 1), 2),
        },
        "loudness": loudness(path),
        "silence": silence(path),
        "signal": signal_stats(path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    payload = {"version": 1, "items": [analyze(path) for path in args.inputs]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
