from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_ROOT / "src"))

from video_factory.errors import ValidationError  # noqa: E402
from video_factory.queue import Dispatcher  # noqa: E402
from video_factory.validators import canonical_json, digest_text  # noqa: E402


T0 = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PreviewGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.queue = Dispatcher(self.root / "factory.sqlite3")
        self.project_root = self.root / "project"
        (self.project_root / "assets" / "media").mkdir(parents=True)
        (self.project_root / "assets" / "audio").mkdir(parents=True)
        (self.project_root / "assets" / "vendor").mkdir(parents=True)
        (self.project_root / "index.html").write_text(
            "<!doctype html><html></html>", encoding="utf-8"
        )
        (self.project_root / "assets" / "media" / "clip.mp4").write_bytes(
            b"licensed-video-bytes"
        )
        (self.project_root / "assets" / "audio" / "narration.wav").write_bytes(
            b"RIFF" + b"licensed-voice-bytes" * 4
        )
        (self.project_root / "assets" / "vendor" / "gsap.min.js").write_bytes(
            b"window.gsap={};"
        )
        files = []
        for path in sorted(self.project_root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_file():
                files.append(
                    {
                        "path": path.relative_to(self.project_root).as_posix(),
                        "sha256": _file_sha(path),
                        "size_bytes": path.stat().st_size,
                    }
                )
        self.project = {
            "schema_version": "1.0.0",
            "project_id": "project-job_preview_001",
            "job_id": "job_preview_001",
            "idea_id": "idea_preview_001",
            "lane_id": "health",
            "project_root": str(self.project_root.resolve()),
            "entrypoint": "index.html",
            "composition": {
                "composition_id": "main",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 30,
            },
            "bindings": {
                "shotlist": {
                    "contract": "shotlist",
                    "schema_version": "1.0.0",
                    "idea_id": "idea_preview_001",
                    "sha256": "1" * 64,
                },
                "script_package": {
                    "contract": "script_package",
                    "schema_version": "1.0.0",
                    "idea_id": "idea_preview_001",
                    "job_id": "job_preview_001",
                    "sha256": "2" * 64,
                },
                "frozen_media_manifest": {
                    "contract": "frozen_media_manifest",
                    "schema_version": "1.0.0",
                    "idea_id": "idea_preview_001",
                    "job_id": "job_preview_001",
                    "sha256": "3" * 64,
                },
                "authoritative_audio": {
                    "contract": "voice_manifest",
                    "schema_version": "1.0.0",
                    "job_id": "job_preview_001",
                    "sha256": "4" * 64,
                    "audio_sha256": _file_sha(
                        self.project_root / "assets" / "audio" / "narration.wav"
                    ),
                    "project_path": "assets/audio/narration.wav",
                    "size_bytes": (
                        self.project_root / "assets" / "audio" / "narration.wav"
                    ).stat().st_size,
                },
            },
            "assets": [
                {
                    "asset_id": "asset_preview_001",
                    "frozen_path": "asset_preview_001/source.mp4",
                    "project_path": "assets/media/clip.mp4",
                    "sha256": _file_sha(
                        self.project_root / "assets" / "media" / "clip.mp4"
                    ),
                    "size_bytes": (
                        self.project_root / "assets" / "media" / "clip.mp4"
                    ).stat().st_size,
                    "content_type": "video/mp4",
                    "shot_ids": ["shot_preview_001"],
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
        self.receipt = self.root / "check-receipt.json"
        self.receipt.write_text(
            json.dumps(
                {
                    "ok": True,
                    "project_tree_sha256": self.project["project_tree_sha256"],
                    "checks": [],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed_project(self) -> dict:
        task = self.queue.enqueue(
            role="compiler",
            pod="health",
            kind="compiler_job",
            payload={
                "job_id": "job_preview_001",
                "lane_id": "health",
                "required_result_contract": "project_manifest",
            },
            idempotency_key="seed-project",
            now=T0,
        )["task"]
        self.queue.db.initialize()
        with closing(self.queue.db.connect()) as connection:
            connection.execute(
                "UPDATE tasks SET status='succeeded', result_json=? WHERE id=?",
                (canonical_json({"artifact": self.project}), task["id"]),
            )
            connection.commit()
        return task

    def _approval(self) -> dict:
        return {
            "schema_version": "1.0.0",
            "job_id": "job_preview_001",
            "project_id": "project-job_preview_001",
            "approved": True,
            "approved_by": "human-reviewer@example.test",
            "approved_at": "2026-08-29T12:05:00Z",
            "project_tree_sha256": self.project["project_tree_sha256"],
            "project_manifest_sha256": digest_text(canonical_json(self.project)),
            "check_receipt_path": str(self.receipt.resolve()),
            "check_receipt_sha256": _file_sha(self.receipt),
            "studio_url": "http://127.0.0.1:3002/#project/job_preview_001",
            "review_notes": ["Reviewed the complete final timeline in Studio."],
        }

    def _preview_task(self, project_task: dict, *, human_gate: bool = True) -> dict:
        return self.queue.enqueue(
            role="preview_review",
            pod="health",
            kind="preview_review_job",
            dependency_task_id=project_task["id"],
            payload={
                "job_id": "job_preview_001",
                "lane_id": "health",
                "required_result_contract": "preview_approval",
                "human_gate": human_gate,
                "checksum_bound": True,
            },
            idempotency_key=f"preview-{human_gate}",
            now=T0,
        )["task"]

    def _complete_preview(self, task: dict, approval: dict) -> dict:
        claim = self.queue.claim(
            worker_id="human-preview-reviewer",
            role="preview_review",
            idempotency_key=f"claim-{task['id']}",
            now=T0,
        )
        return self.queue.complete(
            task["id"],
            lease_token=claim["task"]["lease_token"],
            result={"artifact": approval},
            idempotency_key=f"complete-{task['id']}",
            now=T0,
        )

    def test_preview_approval_passes_only_when_exact_project_and_receipt_match(self) -> None:
        project_task = self._seed_project()
        preview_task = self._preview_task(project_task)
        result = self._complete_preview(preview_task, self._approval())
        self.assertEqual(result["task"]["status"], "succeeded")

    def test_preview_approval_rejects_nonhuman_gate(self) -> None:
        project_task = self._seed_project()
        preview_task = self._preview_task(project_task, human_gate=False)
        with self.assertRaisesRegex(ValidationError, "human gate"):
            self._complete_preview(preview_task, self._approval())

    def test_preview_approval_rejects_unbound_check_receipt(self) -> None:
        self.receipt.write_text(
            json.dumps({"ok": True, "project_tree_sha256": "f" * 64}),
            encoding="utf-8",
        )
        project_task = self._seed_project()
        preview_task = self._preview_task(project_task)
        with self.assertRaisesRegex(ValidationError, "approved project tree"):
            self._complete_preview(preview_task, self._approval())

    def test_render_rejects_project_changed_after_preview_approval(self) -> None:
        project_task = self._seed_project()
        preview_task = self._preview_task(project_task)
        self._complete_preview(preview_task, self._approval())
        (self.project_root / "index.html").write_text("mutated", encoding="utf-8")
        render_task = self.queue.enqueue(
            role="render",
            pod="health",
            kind="render_job",
            dependency_task_id=preview_task["id"],
            payload={
                "job_id": "job_preview_001",
                "lane_id": "health",
                "required_result_contract": "render_manifest",
            },
            idempotency_key="render-after-preview",
            now=T0,
        )["task"]
        claim = self.queue.claim(
            worker_id="render-worker",
            role="render",
            idempotency_key="claim-render-after-preview",
            now=T0,
        )
        render = {
            "schema_version": "1.0.0",
            "render_id": "render_preview_001",
            "job_id": "job_preview_001",
            "composition": "main",
            "output": "out.mp4",
            "output_sha256": "4" * 64,
            "technical": {
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 30,
                "video_codec": "h264",
                "audio_codec": "aac",
                "audio_sample_rate_hz": 48000,
            },
            "input_hashes": [
                {"path": "project_manifest.json", "sha256": "5" * 64}
            ],
            "created_at": "2026-08-29T12:10:00Z",
        }
        with self.assertRaisesRegex(ValidationError, "tree changed"):
            self.queue.complete(
                render_task["id"],
                lease_token=claim["task"]["lease_token"],
                result={"artifact": render},
                idempotency_key="complete-render-after-preview",
                now=T0,
            )


if __name__ == "__main__":
    unittest.main()
