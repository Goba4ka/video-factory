"""Resource-aware runtime planning for the five-lane video factory."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .derived_cache import default_runtime_root
from .errors import ValidationError
from .lanes import load_lane_registry
from .media_tools import h264_nvenc_usable
from .queue import Dispatcher
from .validators import canonical_json


RUNTIME_PLAN_VERSION = "1.0.0"
PROFILE_NAMES = ("auto", "economy", "balanced", "throughput")

_PROFILE_SETTINGS: dict[str, dict[str, Any]] = {
    "economy": {
        "wip_limits": {
            "scout": 2,
            "research": 2,
            "sensitivity_review": 1,
            "privacy_review": 1,
            "medical_review": 1,
            "rights": 1,
            "script": 2,
            "voice": 1,
            "source_audio": 1,
            "editor": 1,
            "bgm": 1,
            "audio_mix": 1,
            "render": 1,
            "qc": 1,
            "final_review": 1,
            "publisher": 1,
        },
        "process_limits": {"heavy_render": 1, "media_decode": 1, "local_model": 1},
        "ffmpeg_threads": 2,
        "proxy_max_height": 720,
        "draft_resolution": "540x960",
    },
    "balanced": {
        "wip_limits": {
            "scout": 4,
            "research": 4,
            "sensitivity_review": 2,
            "privacy_review": 2,
            "medical_review": 2,
            "rights": 2,
            "script": 3,
            "voice": 2,
            "source_audio": 2,
            "editor": 2,
            "bgm": 2,
            "audio_mix": 1,
            "render": 1,
            "qc": 1,
            "final_review": 1,
            "publisher": 1,
        },
        "process_limits": {"heavy_render": 1, "media_decode": 2, "local_model": 1},
        "ffmpeg_threads": 4,
        "proxy_max_height": 960,
        "draft_resolution": "720x1280",
    },
    "throughput": {
        "wip_limits": {
            "scout": 6,
            "research": 6,
            "sensitivity_review": 3,
            "privacy_review": 3,
            "medical_review": 3,
            "rights": 3,
            "script": 4,
            "voice": 3,
            "source_audio": 3,
            "editor": 3,
            "bgm": 3,
            "audio_mix": 2,
            "render": 2,
            "qc": 2,
            "final_review": 1,
            "publisher": 1,
        },
        "process_limits": {"heavy_render": 2, "media_decode": 3, "local_model": 1},
        "ffmpeg_threads": 4,
        "proxy_max_height": 1080,
        "draft_resolution": "720x1280",
    },
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _memory_bytes() -> tuple[int | None, int | None]:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_phys", ctypes.c_ulonglong),
                ("avail_phys", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("avail_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("avail_virtual", ctypes.c_ulonglong),
                ("avail_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_phys), int(status.avail_phys)
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        total = page_size * os.sysconf("SC_PHYS_PAGES")
        available = page_size * os.sysconf("SC_AVPHYS_PAGES")
        return int(total), int(available)
    except (AttributeError, OSError, ValueError):
        return None, None


def _find_executable(name: str, env_name: str) -> str | None:
    configured = os.environ.get(env_name)
    executable_name = f"{name}.exe" if os.name == "nt" else name
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(found) if (found := shutil.which(name)) else None,
        Path.home() / "bin" / executable_name,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate.resolve())
    return None


def _command_line(command: list[str], *, timeout: float = 10.0) -> str | None:
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
    except (OSError, subprocess.TimeoutExpired):
        return None
    combined = (completed.stdout + "\n" + completed.stderr).strip()
    return combined if completed.returncode == 0 else None


def _gpu_info() -> dict[str, Any]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        common = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "NVIDIA Corporation"
        candidate = common / "NVSMI" / "nvidia-smi.exe"
        nvidia_smi = str(candidate) if candidate.is_file() else None
    if not nvidia_smi:
        return {"available": False, "name": None, "memory_total_mib": None}
    output = _command_line(
        [
            nvidia_smi,
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    if not output:
        return {"available": False, "name": None, "memory_total_mib": None}
    first = output.splitlines()[0]
    name, _, memory = first.rpartition(",")
    try:
        memory_mib = int(memory.strip())
    except ValueError:
        memory_mib = None
    return {"available": True, "name": name.strip() or first.strip(), "memory_total_mib": memory_mib}


def inspect_host(runtime_root: str | Path | None = None) -> dict[str, Any]:
    root = (
        Path(runtime_root).expanduser().resolve()
        if runtime_root is not None
        else default_runtime_root()
    )
    root.mkdir(parents=True, exist_ok=True)
    total_memory, available_memory = _memory_bytes()
    disk = shutil.disk_usage(root)
    ffmpeg = _find_executable("ffmpeg", "VIDEO_FACTORY_FFMPEG")
    ffprobe = _find_executable("ffprobe", "VIDEO_FACTORY_FFPROBE")
    ffmpeg_banner = _command_line([ffmpeg, "-version"]) if ffmpeg else None
    encoders = _command_line([ffmpeg, "-hide_banner", "-encoders"]) if ffmpeg else None
    nvenc_listed = bool(encoders and "h264_nvenc" in encoders)
    nvenc_usable = bool(ffmpeg and nvenc_listed and h264_nvenc_usable(ffmpeg))
    return {
        "logical_cpu_count": os.cpu_count() or 1,
        "memory_total_bytes": total_memory,
        "memory_available_bytes": available_memory,
        "disk_free_bytes": disk.free,
        "runtime_root": str(root),
        "runtime_outside_onedrive": "onedrive" not in str(root).lower(),
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "ffmpeg_version": ffmpeg_banner.splitlines()[0] if ffmpeg_banner else None,
        "h264_nvenc_listed": nvenc_listed,
        "h264_nvenc_usable": nvenc_usable,
        "gpu": _gpu_info(),
    }


def choose_profile(host: Mapping[str, Any]) -> str:
    cpus = int(host.get("logical_cpu_count") or 1)
    memory = int(host.get("memory_total_bytes") or 0)
    gib = memory / (1024**3) if memory else 0
    if cpus <= 4 or (gib and gib < 12):
        return "economy"
    if cpus >= 16 and gib >= 32 and host.get("gpu", {}).get("available"):
        return "throughput"
    return "balanced"


def _wave_plan(target: int, registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    if isinstance(target, bool) or not isinstance(target, int) or not 10 <= target <= 15:
        raise ValidationError("target must be an integer from 10 to 15")
    enabled = [dict(lane) for lane in registry["lanes"] if lane["enabled"]]
    counts = {lane["id"]: lane["daily"]["min"] for lane in enabled}
    remaining = target - sum(counts.values())
    for lane in enabled:
        if remaining <= 0:
            break
        capacity = lane["daily"]["max"] - counts[lane["id"]]
        addition = min(capacity, remaining)
        counts[lane["id"]] += addition
        remaining -= addition
    waves: list[dict[str, Any]] = []
    for index in range(1, max(counts.values()) + 1):
        jobs = [
            {
                "lane_id": lane["id"],
                "chat_id": lane["chat_id"],
                "ordinal_for_lane": index,
            }
            for lane in enabled
            if counts[lane["id"]] >= index
        ]
        waves.append({"wave": index, "jobs": jobs, "job_count": len(jobs)})
    return waves


def build_runtime_plan(
    *,
    profile: str = "auto",
    target: int = 15,
    runtime_root: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> dict[str, Any]:
    if profile not in PROFILE_NAMES:
        raise ValidationError(f"profile must be one of: {', '.join(PROFILE_NAMES)}")
    host = inspect_host(runtime_root)
    selected = choose_profile(host) if profile == "auto" else profile
    settings = json.loads(json.dumps(_PROFILE_SETTINGS[selected]))
    registry = load_lane_registry(registry_path)
    root = Path(host["runtime_root"])
    lane_limits = {
        lane["id"]: min(3, lane["daily"]["max"])
        for lane in registry["lanes"]
        if lane["enabled"]
    }
    warnings: list[str] = []
    if not host["ffmpeg"] or not host["ffprobe"]:
        warnings.append("FFmpeg/ffprobe is missing; media cache and automated QC are unavailable")
    if not host["runtime_outside_onedrive"]:
        warnings.append("runtime root is synchronized; move SQLite, WAL, cache, and scratch outside OneDrive")
    if int(host["disk_free_bytes"]) < 10 * 1024**3:
        warnings.append("runtime disk has less than 10 GiB free")
    if host.get("h264_nvenc_listed") and not host.get("h264_nvenc_usable"):
        warnings.append("FFmpeg lists h264_nvenc but the live encoder probe failed; CPU fallback is active")
    return {
        "schema_version": RUNTIME_PLAN_VERSION,
        "ok": not (not host["ffmpeg"] or not host["ffprobe"]),
        "command": "optimize-runtime",
        "generated_at": _utc_now(),
        "requested_profile": profile,
        "selected_profile": selected,
        "target_outputs": target,
        "host": host,
        "paths": {
            "runtime_root": str(root),
            "database": str(root / "factory-v3.sqlite3"),
            "cache": str(root / "cache"),
            "scratch": str(root / "scratch"),
        },
        "wip_limits": settings["wip_limits"],
        "lane_limits": lane_limits,
        "process_limits": settings["process_limits"],
        "media": {
            "ffmpeg_threads": settings["ffmpeg_threads"],
            "proxy_max_height": settings["proxy_max_height"],
            "draft_resolution": settings["draft_resolution"],
            "final_resolution": "1080x1920",
            "telegram_resolution": "720x1280",
            "fast_qc_every_draft": True,
            "full_qc_final_only": True,
            "cache_key": "source_sha256+options+tool_version+profile_version",
        },
        "render": {
            "hyperframes_workers": settings["process_limits"]["heavy_render"],
            "explicit_workers_required": True,
            "reuse_intermediate_for_delivery_versions": True,
            "wrapper": "factory/tools/render_hyperframes.ps1",
            "required_flags": [
                "--workers 1",
                "--max-concurrent-renders 1",
                "--frames-cache-dir <runtime>/hyperframes-frames",
            ],
        },
        "resource_exclusions": [
            ["heavy_render", "local_model"],
            ["heavy_render", "full_qc"],
        ],
        "lanes": [
            {
                "lane_id": lane["id"],
                "chat_id": lane["chat_id"],
                "roles": lane["roles"],
                "daily": lane["daily"],
            }
            for lane in registry["lanes"]
            if lane["enabled"]
        ],
        "waves": _wave_plan(target, registry),
        "environment": {
            "VIDEO_FACTORY_RUNTIME_ROOT": str(root),
            "VIDEO_FACTORY_DB": str(root / "factory-v3.sqlite3"),
            "VIDEO_FACTORY_FFMPEG": host["ffmpeg"],
            "VIDEO_FACTORY_FFPROBE": host["ffprobe"],
            "VIDEO_FACTORY_FFMPEG_THREADS": str(settings["ffmpeg_threads"]),
            "HYPERFRAMES_WORKERS": str(settings["process_limits"]["heavy_render"]),
        },
        "warnings": warnings,
    }


def database_status(path: str | Path) -> dict[str, Any]:
    db_path = Path(path).expanduser().resolve()
    if not db_path.is_file():
        return {"exists": False, "path": str(db_path), "schema_version": None}
    try:
        with closing(sqlite3.connect(db_path)) as connection:
            schema = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            counts = {}
            for table in ("ideas", "jobs", "tasks", "task_attempts"):
                if table in tables:
                    counts[table] = connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
    except sqlite3.Error as exc:
        raise ValidationError(f"cannot inspect runtime database: {exc}") from exc
    return {
        "exists": True,
        "path": str(db_path),
        "schema_version": schema,
        "counts": counts,
    }


def apply_runtime_plan(plan: Mapping[str, Any], *, db_path: str | Path | None = None) -> dict[str, Any]:
    if plan.get("schema_version") != RUNTIME_PLAN_VERSION:
        raise ValidationError("unsupported runtime plan version")
    selected_db = Path(db_path or plan["paths"]["database"]).expanduser().resolve()
    dispatcher = Dispatcher(selected_db)
    applied: list[dict[str, Any]] = []
    for role, maximum in sorted(plan["wip_limits"].items()):
        applied.append(dispatcher.configure_limit(role=role, max_leased=int(maximum)))
    for lane, maximum in sorted(plan["lane_limits"].items()):
        applied.append(dispatcher.configure_limit(pod=lane, max_leased=int(maximum)))
    return {
        "ok": True,
        "command": "apply-runtime-plan",
        "database": str(selected_db),
        "database_status": database_status(selected_db),
        "limits_applied": len(applied),
        "render_limit": plan["wip_limits"]["render"],
        "qc_limit": plan["wip_limits"]["qc"],
        "legacy_database_untouched": True,
    }


def write_runtime_plan(plan: Mapping[str, Any], path: str | Path) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(dict(plan)))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return destination


__all__ = [
    "PROFILE_NAMES",
    "RUNTIME_PLAN_VERSION",
    "apply_runtime_plan",
    "build_runtime_plan",
    "choose_profile",
    "database_status",
    "inspect_host",
    "write_runtime_plan",
]
