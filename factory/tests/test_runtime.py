from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_factory.db import SCHEMA_VERSION
from video_factory.queue import Dispatcher
from video_factory.runtime import (
    apply_runtime_plan,
    build_runtime_plan,
    choose_profile,
    database_status,
)


class RuntimePlanTests(unittest.TestCase):
    def test_auto_profile_is_conservative_on_a_16_gib_twelve_thread_host(self) -> None:
        host = {
            "logical_cpu_count": 12,
            "memory_total_bytes": 16 * 1024**3,
            "gpu": {"available": True},
        }
        self.assertEqual(choose_profile(host), "balanced")

    def test_plan_uses_five_chats_three_waves_and_one_render_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = {
                "logical_cpu_count": 12,
                "memory_total_bytes": 16 * 1024**3,
                "memory_available_bytes": 8 * 1024**3,
                "disk_free_bytes": 100 * 1024**3,
                "runtime_root": str(root),
                "runtime_outside_onedrive": True,
                "ffmpeg": "ffmpeg",
                "ffprobe": "ffprobe",
                "ffmpeg_version": "fixture",
                "h264_nvenc_listed": True,
                "gpu": {"available": True, "name": "fixture", "memory_total_mib": 4096},
            }
            with patch("video_factory.runtime.inspect_host", return_value=host):
                plan = build_runtime_plan(profile="auto", target=15, runtime_root=root)

        self.assertEqual(plan["selected_profile"], "balanced")
        self.assertEqual(plan["wip_limits"]["render"], 1)
        self.assertEqual(plan["wip_limits"]["voice"], 2)
        self.assertEqual(plan["wip_limits"]["source_audio"], 2)
        self.assertEqual(len(plan["lanes"]), 5)
        self.assertEqual([wave["job_count"] for wave in plan["waves"]], [5, 5, 5])
        self.assertEqual(len({lane["chat_id"] for lane in plan["lanes"]}), 5)

    def test_apply_creates_clean_current_schema_database_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = {
                "logical_cpu_count": 12,
                "memory_total_bytes": 16 * 1024**3,
                "memory_available_bytes": 8 * 1024**3,
                "disk_free_bytes": 100 * 1024**3,
                "runtime_root": str(root),
                "runtime_outside_onedrive": True,
                "ffmpeg": "ffmpeg",
                "ffprobe": "ffprobe",
                "ffmpeg_version": "fixture",
                "h264_nvenc_listed": True,
                "gpu": {"available": True, "name": "fixture", "memory_total_mib": 4096},
            }
            with patch("video_factory.runtime.inspect_host", return_value=host):
                plan = build_runtime_plan(profile="balanced", target=10, runtime_root=root)
            database = root / "factory-v3.sqlite3"
            result = apply_runtime_plan(plan, db_path=database)
            status = database_status(database)
            queue = Dispatcher(database).status()

        self.assertTrue(result["ok"])
        self.assertEqual(status["schema_version"], SCHEMA_VERSION)
        limits = {(item["role"], item["pod"]): item["max_leased"] for item in queue["limits"]}
        self.assertEqual(limits[("render", None)], 1)
        self.assertEqual(limits[("voice", None)], 2)
        self.assertEqual(limits[("source_audio", None)], 2)
        self.assertEqual(len(plan["waves"]), 2)


if __name__ == "__main__":
    unittest.main()
