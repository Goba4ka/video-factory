from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_factory.errors import ValidationError
from video_factory.render_handler import handle_task
from video_factory.validators import canonical_json, digest_text


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RenderHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project_root = self.root / "project"
        (self.project_root / "assets" / "media").mkdir(parents=True)
        (self.project_root / "assets" / "audio").mkdir(parents=True)
        (self.project_root / "assets" / "vendor").mkdir(parents=True)
        (self.project_root / "index.html").write_text("<html>fixture</html>", encoding="utf-8")
        (self.project_root / "assets" / "media" / "media.mp4").write_bytes(
            b"project-media" * 32
        )
        (self.project_root / "assets" / "audio" / "narration.wav").write_bytes(
            b"RIFF" + b"licensed-voice" * 8
        )
        program_mix_path = self.project_root / "assets" / "audio" / "program_mix.wav"
        program_mix_path.write_bytes(b"RIFF" + b"mixed-program-audio" * 8)
        (self.project_root / "assets" / "vendor" / "gsap.min.js").write_text(
            "window.gsap={};", encoding="utf-8"
        )
        self.program_audio_manifest = {
            "schema_version": "1.0.0",
            "job_id": "job_render_001",
            "idea_id": "idea_render_001",
            "lane_id": "health",
            "source_authority": {
                "contract": "voice_manifest",
                "manifest_sha256": "4" * 64,
                "audio_sha256": file_sha(
                    self.project_root / "assets" / "audio" / "narration.wav"
                ),
                "authority": "spoken_content_and_timing",
                "tts": True,
            },
            "bgm": {
                "asset_id": "music-render-001",
                "manifest_sha256": "5" * 64,
                "audio_sha256": "6" * 64,
                "license_evidence_sha256": "7" * 64,
                "human_approval_sha256": "8" * 64,
            },
            "mix": {
                "engine": "ffmpeg",
                "ffmpeg_version": "ffmpeg fixture",
                "recipe_version": "program-mix-1.0.0",
                "filtergraph_sha256": "9" * 64,
                "loudness_target_lufs": -15,
                "true_peak_max_dbtp": -1,
                "lra_target_lu": 7,
                "mix_profile_id": "speech-forward-audible-bgm-v1",
                "bgm_preduck_gain_db": -9,
                "sidechain_threshold_dbfs": -34,
                "sidechain_ratio": 10,
                "sidechain_attack_ms": 15,
                "sidechain_release_ms": 350,
                "sidechain_ducking": True,
                "broll_audio_muted": True,
                "deterministic": True,
            },
            "immutable_output_path": str(program_mix_path.resolve()),
            "output_sha256": file_sha(program_mix_path),
            "output_bytes": program_mix_path.stat().st_size,
            "audio": {
                "sample_rate_hz": 48000,
                "channels": 2,
                "sample_width_bits": 16,
                "frames": 720000,
                "duration_seconds": 15,
                "integrated_loudness_lufs": -15,
                "loudness_range_lu": 3,
                "true_peak_dbtp": -1,
            },
            "created_at": "2026-08-29T12:00:00Z",
        }
        files = []
        for path in sorted(self.project_root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_file():
                files.append(
                    {
                        "path": path.relative_to(self.project_root).as_posix(),
                        "sha256": file_sha(path),
                        "size_bytes": path.stat().st_size,
                    }
                )
        media_record = next(item for item in files if item["path"].endswith("media.mp4"))
        program_audio_record = next(
            item for item in files if item["path"].endswith("program_mix.wav")
        )
        self.project = {
            "schema_version": "1.0.0",
            "project_id": "project-job_render_001",
            "job_id": "job_render_001",
            "idea_id": "idea_render_001",
            "lane_id": "health",
            "project_root": str(self.project_root.resolve()),
            "entrypoint": "index.html",
            "composition": {
                "composition_id": "main",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 15,
            },
            "bindings": {
                "shotlist": {
                    "contract": "shotlist",
                    "schema_version": "1.0.0",
                    "idea_id": "idea_render_001",
                    "sha256": "1" * 64,
                },
                "script_package": {
                    "contract": "script_package",
                    "schema_version": "1.0.0",
                    "idea_id": "idea_render_001",
                    "job_id": "job_render_001",
                    "sha256": "2" * 64,
                },
                "frozen_media_manifest": {
                    "contract": "frozen_media_manifest",
                    "schema_version": "1.0.0",
                    "idea_id": "idea_render_001",
                    "job_id": "job_render_001",
                    "sha256": "3" * 64,
                },
                "authoritative_audio": {
                    "contract": "voice_manifest",
                    "schema_version": "1.0.0",
                    "job_id": "job_render_001",
                    "sha256": "4" * 64,
                    "audio_sha256": file_sha(
                        self.project_root / "assets" / "audio" / "narration.wav"
                    ),
                },
                "program_audio": {
                    "contract": "program_audio_manifest",
                    "schema_version": self.program_audio_manifest["schema_version"],
                    "job_id": self.program_audio_manifest["job_id"],
                    "idea_id": self.program_audio_manifest["idea_id"],
                    "lane_id": self.program_audio_manifest["lane_id"],
                    "sha256": digest_text(
                        canonical_json(self.program_audio_manifest)
                    ),
                    "audio_sha256": program_audio_record["sha256"],
                    "project_path": program_audio_record["path"],
                    "size_bytes": program_audio_record["size_bytes"],
                },
            },
            "assets": [
                {
                    "asset_id": "asset-render-001",
                    "frozen_path": "asset-render-001.mp4",
                    "project_path": media_record["path"],
                    "sha256": media_record["sha256"],
                    "size_bytes": media_record["size_bytes"],
                    "content_type": "video/mp4",
                    "shot_ids": ["shot-render-001"],
                }
            ],
            "files": files,
            "project_tree_sha256": digest_text(canonical_json(files)),
            "preview": {
                "status": "ready_for_human_review",
                "render_authorized": False,
                "human_approval_required": True,
            },
        }
        self.receipt = self.root / "check.json"
        self.receipt.write_text(
            json.dumps(
                {
                    "ok": True,
                    "project_tree_sha256": self.project["project_tree_sha256"],
                    "check_mode": "strict-all",
                }
            ),
            encoding="utf-8",
        )
        self.approval = {
            "schema_version": "1.0.0",
            "job_id": "job_render_001",
            "project_id": self.project["project_id"],
            "approved": True,
            "approved_by": "owner",
            "approved_at": "2026-08-29T12:00:00Z",
            "project_tree_sha256": self.project["project_tree_sha256"],
            "project_manifest_sha256": digest_text(canonical_json(self.project)),
            "check_receipt_path": str(self.receipt.resolve()),
            "check_receipt_sha256": file_sha(self.receipt),
            "studio_url": "http://127.0.0.1:3002/#project/job-render-001",
            "review_notes": ["Approved in final Studio preview."],
        }
        self.binary = self.root / "hyperframes"
        self.binary.write_bytes(b"pinned-hyperframes-fixture")
        self.output_root = self.root / "renders"

    def task(self) -> dict:
        return {
            "id": "task-render-001",
            "job_id": "job_render_001",
            "role": "render",
            "pod": "health",
            "attempt_count": 1,
            "payload": {
                "job_id": "job_render_001",
                "lane_id": "health",
                "required_result_contract": "render_manifest",
            },
            "upstream_results": [
                {"role": "compiler", "result": {"artifact": self.project}},
                {"role": "preview_review", "result": {"artifact": self.approval}},
            ],
        }

    @staticmethod
    def technical() -> dict:
        return {
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "duration_seconds": 15.0,
            "video_codec": "h264",
            "audio_codec": "aac",
            "audio_sample_rate_hz": 48000,
            "integrated_lufs": None,
            "true_peak_dbtp": None,
        }

    def test_requires_approval_and_runs_pinned_strict_atomic_render(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            output = Path(command[command.index("--output") + 1])
            output.write_bytes(b"rendered-mp4" * 128)
            self.assertEqual(kwargs["cwd"], self.project_root.resolve())
            self.assertNotIn("shell", kwargs)
            return subprocess.CompletedProcess(command, 0, "ok", "")

        environment = {
            "HYPERFRAMES_BIN": str(self.binary),
            "VIDEO_FACTORY_RENDER_OUTPUT_ROOT": str(self.output_root),
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch(
            "video_factory.render_handler.subprocess.run", side_effect=fake_run
        ), mock.patch(
            "video_factory.render_handler._probe_render", return_value=self.technical()
        ):
            first = handle_task(self.task())
            second = handle_task(self.task())
        self.assertEqual(len(calls), 1)
        command = calls[0]
        self.assertEqual(command[0], str(self.binary.resolve()))
        self.assertIn("--strict-all", command)
        self.assertIn("--quality", command)
        self.assertEqual(command[command.index("--quality") + 1], "high")
        self.assertFalse(first["render_execution"]["reused"])
        self.assertTrue(second["render_execution"]["reused"])
        self.assertEqual(
            first["artifact"]["input_hashes"],
            sorted(first["artifact"]["input_hashes"], key=lambda item: item["path"]),
        )
        self.assertTrue(Path(first["output_path"]).is_file())

    def test_refuses_missing_or_mismatched_approval_before_subprocess(self) -> None:
        task = self.task()
        task["upstream_results"] = task["upstream_results"][:1]
        with mock.patch("video_factory.render_handler.subprocess.run") as run:
            with self.assertRaisesRegex(ValidationError, "preview_approval"):
                handle_task(task)
        run.assert_not_called()

        task = self.task()
        task["upstream_results"][1]["result"]["artifact"] = {
            **self.approval,
            "project_tree_sha256": "f" * 64,
        }
        with mock.patch("video_factory.render_handler.subprocess.run") as run:
            with self.assertRaisesRegex(ValidationError, "not bound"):
                handle_task(task)
        run.assert_not_called()

    def test_refuses_project_tamper_before_subprocess(self) -> None:
        (self.project_root / "index.html").write_text("tampered", encoding="utf-8")
        with mock.patch("video_factory.render_handler.subprocess.run") as run:
            with self.assertRaisesRegex(ValidationError, "changed after preview"):
                handle_task(self.task())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
