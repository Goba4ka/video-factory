import copy
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from unittest import mock

from video_factory.cli import main as cli_main
from video_factory.contracts import validate_artifact
from video_factory.dedup_analyzer import dhash64
from video_factory.dedup_corpus import (
    CORPUS_APPROVAL_CONFIRMATION,
    create_corpus_approval,
    update_dedup_corpus,
)
from video_factory.errors import ValidationError
from video_factory.media_tools import probe_media, resolve_media_binary
from video_factory.qc_analyzer_common import GrayFrame


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DedupCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.master = self.root / "master.mp4"
        self.master.write_bytes(b"approved-master-bytes" * 128)
        self.manifest_path = self.root / "render_manifest.json"
        self.manifest = self._manifest(
            job_id="job_corpus_001",
            render_id="render_corpus_001",
            master=self.master,
        )
        self._write_manifest(self.manifest)
        self.approval_path = self.root / "approval.json"
        self.snapshot_path = self.root / "corpus.json"
        self.approved_at = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    @staticmethod
    def _manifest(*, job_id: str, render_id: str, master: Path) -> dict:
        return {
            "schema_version": "1.0.0",
            "render_id": render_id,
            "job_id": job_id,
            "composition": "main",
            "output": master.name,
            "output_sha256": file_sha256(master),
            "technical": {
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 15.0,
                "video_codec": "h264",
                "audio_codec": "aac",
                "audio_sample_rate_hz": 48000,
                "integrated_lufs": None,
                "true_peak_dbtp": None,
            },
            "input_hashes": [{"path": "input.json", "sha256": "1" * 64}],
            "created_at": "2026-08-29T11:59:00Z",
        }

    def _write_manifest(self, manifest: dict) -> None:
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    @staticmethod
    def fake_probe(path: str | Path) -> dict:
        source = Path(path)
        return {
            "path": str(source),
            "format": {
                "duration": "15.0",
                "size": str(source.stat().st_size),
            },
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1080,
                    "height": 1920,
                    "avg_frame_rate": "30/1",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                },
            ],
        }

    @staticmethod
    def fake_frames(path: str | Path, **kwargs: object) -> list[GrayFrame]:
        seed = Path(path).read_bytes()[0]
        frames: list[GrayFrame] = []
        for index in range(16):
            pixels = bytes(
                ((seed + index * 11 + y * 17 + x * 23) % 256)
                for y in range(8)
                for x in range(9)
            )
            frames.append(
                GrayFrame(
                    index=index,
                    timestamp_seconds=float(index),
                    width=9,
                    height=8,
                    pixels=pixels,
                )
            )
        return frames

    def approve(self, *, output: Path | None = None) -> dict:
        return create_corpus_approval(
            self.manifest_path,
            self.master,
            output or self.approval_path,
            approved_by="owner@example.test",
            approval_note="Master reviewed and accepted for originality comparison.",
            human_confirm=CORPUS_APPROVAL_CONFIRMATION,
            approved_at=self.approved_at,
            media_prober=self.fake_probe,
        )

    def update(self, *approval_paths: Path) -> dict:
        return update_dedup_corpus(
            self.snapshot_path,
            approval_paths or (self.approval_path,),
            frame_extractor=self.fake_frames,
            media_prober=self.fake_probe,
            generated_at=datetime(2026, 8, 29, 12, 1, tzinfo=UTC),
        )

    def test_approval_and_first_update_bind_exact_bytes_and_real_dhash(self) -> None:
        approval_result = self.approve()
        approval = approval_result["approval"]
        self.assertEqual(approval_result["authority"], "dedup_corpus_only")
        self.assertEqual(approval["master"]["sha256"], file_sha256(self.master))
        self.assertEqual(
            approval["render_manifest"]["file_sha256"],
            file_sha256(self.manifest_path),
        )
        self.assertIs(validate_artifact("dedup_corpus_approval", approval), approval)

        result = self.update()
        snapshot = result["snapshot"]
        self.assertTrue(result["changed"])
        self.assertEqual(result["counts"], {
            "entries": 1,
            "added": 1,
            "replaced": 0,
            "unchanged_inputs": 0,
        })
        self.assertEqual(snapshot["algorithm"], "dhash-64-v1")
        expected = [dhash64(frame) for frame in self.fake_frames(self.master)]
        self.assertEqual(snapshot["entries"][0]["frame_hashes"], expected)
        self.assertEqual(snapshot["entries"][0]["render_sha256"], file_sha256(self.master))
        self.assertIs(validate_artifact("dedup_corpus_snapshot", snapshot), snapshot)
        self.assertFalse(
            self.snapshot_path.with_name(".corpus.json.write-lock").exists()
        )

    def test_replay_is_byte_stable_and_preserves_generated_at(self) -> None:
        self.approve()
        first = self.update()
        first_bytes = self.snapshot_path.read_bytes()
        second = update_dedup_corpus(
            self.snapshot_path,
            [self.approval_path],
            frame_extractor=self.fake_frames,
            media_prober=self.fake_probe,
            generated_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        self.assertFalse(second["changed"])
        self.assertEqual(second["counts"]["unchanged_inputs"], 1)
        self.assertEqual(self.snapshot_path.read_bytes(), first_bytes)
        self.assertEqual(second["snapshot"]["snapshot_id"], first["snapshot"]["snapshot_id"])
        self.assertEqual(second["snapshot"]["generated_at"], first["snapshot"]["generated_at"])

    def test_same_render_identity_replaces_entry_but_preserves_comparison_id(self) -> None:
        self.approve()
        first = self.update()
        comparison_id = first["snapshot"]["entries"][0]["comparison_id"]

        self.master.write_bytes(b"replacement-approved-master" * 128)
        self.manifest = self._manifest(
            job_id="job_corpus_001",
            render_id="render_corpus_001",
            master=self.master,
        )
        self._write_manifest(self.manifest)
        replacement_approval = self.root / "replacement-approval.json"
        create_corpus_approval(
            self.manifest_path,
            self.master,
            replacement_approval,
            approved_by="owner@example.test",
            approval_note="Replacement master reviewed and accepted for corpus comparison.",
            human_confirm=CORPUS_APPROVAL_CONFIRMATION,
            approved_at=datetime(2026, 8, 29, 13, 0, tzinfo=UTC),
            media_prober=self.fake_probe,
        )
        result = self.update(replacement_approval)
        entry = result["snapshot"]["entries"][0]
        self.assertEqual(result["counts"]["replaced"], 1)
        self.assertEqual(entry["comparison_id"], comparison_id)
        self.assertEqual(entry["render_sha256"], file_sha256(self.master))

    def test_batch_add_is_deterministic_independent_of_approval_order(self) -> None:
        self.approve()
        second_master = self.root / "second.mp4"
        second_master.write_bytes(b"second-approved-master" * 128)
        second_manifest_path = self.root / "second-render-manifest.json"
        second_manifest = self._manifest(
            job_id="job_corpus_002",
            render_id="render_corpus_002",
            master=second_master,
        )
        second_manifest_path.write_text(
            json.dumps(second_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        second_approval = self.root / "second-approval.json"
        create_corpus_approval(
            second_manifest_path,
            second_master,
            second_approval,
            approved_by="owner@example.test",
            approval_note="Second master reviewed for corpus inclusion.",
            human_confirm=CORPUS_APPROVAL_CONFIRMATION,
            approved_at=self.approved_at,
            media_prober=self.fake_probe,
        )

        first_path = self.root / "first-order.json"
        second_path = self.root / "second-order.json"
        first = update_dedup_corpus(
            first_path,
            [self.approval_path, second_approval],
            frame_extractor=self.fake_frames,
            media_prober=self.fake_probe,
            generated_at=self.approved_at,
        )
        second = update_dedup_corpus(
            second_path,
            [second_approval, self.approval_path],
            frame_extractor=self.fake_frames,
            media_prober=self.fake_probe,
            generated_at=self.approved_at,
        )
        self.assertEqual(first["snapshot"], second["snapshot"])
        self.assertEqual(first_path.read_bytes(), second_path.read_bytes())

    def test_rejects_unapproved_missing_tampered_or_unprobeable_inputs(self) -> None:
        self.approve()
        original = json.loads(self.approval_path.read_text(encoding="utf-8"))
        cases: list[tuple[str, Callable[[], None], str]] = []

        def unapproved() -> None:
            changed = copy.deepcopy(original)
            changed["decision"] = "rejected"
            self.approval_path.write_text(json.dumps(changed), encoding="utf-8")
            self.update()

        cases.append(("unapproved", unapproved, "decision"))

        def missing() -> None:
            self.approval_path.write_text(json.dumps(original), encoding="utf-8")
            self.master.unlink()
            self.update()

        cases.append(("missing", missing, "existing regular file"))

        for label, operation, message in cases:
            with self.subTest(label=label):
                # Isolate destructive fixture mutations per subtest.
                self.master.write_bytes(b"approved-master-bytes" * 128)
                self._write_manifest(self.manifest)
                self.approval_path.unlink(missing_ok=True)
                self.approve()
                with self.assertRaisesRegex(ValidationError, message):
                    operation()
                self.assertFalse(self.snapshot_path.exists())

        self.master.write_bytes(b"approved-master-bytes" * 128)
        self._write_manifest(self.manifest)
        self.approval_path.unlink(missing_ok=True)
        self.approve()
        self.master.write_bytes(b"tampered-after-approval")
        with self.assertRaisesRegex(ValidationError, "size|checksum"):
            self.update()
        self.assertFalse(self.snapshot_path.exists())

        self.master.write_bytes(b"approved-master-bytes" * 128)
        self._write_manifest(self.manifest)
        self.approval_path.unlink(missing_ok=True)
        self.approve()
        with self.assertRaisesRegex(ValidationError, "width"):
            update_dedup_corpus(
                self.snapshot_path,
                [self.approval_path],
                frame_extractor=self.fake_frames,
                media_prober=lambda path: {
                    **self.fake_probe(path),
                    "streams": [
                        {**self.fake_probe(path)["streams"][0], "width": 720},
                        self.fake_probe(path)["streams"][1],
                    ],
                },
            )
        self.assertFalse(self.snapshot_path.exists())

    def test_refuses_no_approval_relative_paths_empty_or_corrupt_existing_corpus(self) -> None:
        with self.assertRaisesRegex(ValidationError, "at least one explicit"):
            update_dedup_corpus(self.snapshot_path, [])
        with self.assertRaisesRegex(ValidationError, "absolute"):
            create_corpus_approval(
                "relative-manifest.json",
                self.master,
                self.approval_path,
                approved_by="owner",
                approval_note="Explicit review note.",
                human_confirm=CORPUS_APPROVAL_CONFIRMATION,
                media_prober=self.fake_probe,
            )
        with self.assertRaisesRegex(ValidationError, "human_confirm"):
            create_corpus_approval(
                self.manifest_path,
                self.master,
                self.approval_path,
                approved_by="owner",
                approval_note="Explicit review note.",
                human_confirm="yes",
                media_prober=self.fake_probe,
            )

        self.approve()
        self.snapshot_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "snapshot_id": "dedup_corrupt",
                    "generated_at": "2026-08-29T12:00:00Z",
                    "algorithm": "dhash-64-v1",
                    "sample_interval_seconds": 1.0,
                    "entries": [],
                }
            ),
            encoding="utf-8",
        )
        before = self.snapshot_path.read_bytes()
        with self.assertRaisesRegex(ValidationError, "minItems"):
            self.update()
        self.assertEqual(self.snapshot_path.read_bytes(), before)

    def test_atomic_replace_failure_preserves_existing_snapshot(self) -> None:
        self.approve()
        self.update()
        before = self.snapshot_path.read_bytes()

        self.master.write_bytes(b"replacement-approved-master" * 128)
        self.manifest = self._manifest(
            job_id="job_corpus_001",
            render_id="render_corpus_001",
            master=self.master,
        )
        self._write_manifest(self.manifest)
        replacement = self.root / "replace.json"
        create_corpus_approval(
            self.manifest_path,
            self.master,
            replacement,
            approved_by="owner",
            approval_note="Approved exact replacement master.",
            human_confirm=CORPUS_APPROVAL_CONFIRMATION,
            approved_at=datetime(2026, 8, 29, 14, 0, tzinfo=UTC),
            media_prober=self.fake_probe,
        )
        with mock.patch(
            "video_factory.dedup_corpus.os.replace", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.update(replacement)
        self.assertEqual(self.snapshot_path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".corpus.json.*.tmp")), [])

    def test_cli_exposes_separate_non_publish_approval_and_update_commands(self) -> None:
        approval_out = io.StringIO()
        with mock.patch(
            "video_factory.cli.create_corpus_approval",
            return_value={"ok": True, "authority": "dedup_corpus_only"},
        ) as approve:
            code = cli_main(
                [
                    "dedup-corpus-approve",
                    "--render-manifest",
                    str(self.manifest_path),
                    "--master",
                    str(self.master),
                    "--output",
                    str(self.approval_path),
                    "--approved-by",
                    "owner",
                    "--approval-note",
                    "Explicit corpus approval.",
                    "--human-confirm",
                    CORPUS_APPROVAL_CONFIRMATION,
                ],
                out=approval_out,
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(approval_out.getvalue())["authority"], "dedup_corpus_only")
        approve.assert_called_once()

        update_out = io.StringIO()
        with mock.patch(
            "video_factory.cli.update_dedup_corpus",
            return_value={"ok": True, "command": "dedup-corpus-update"},
        ) as update:
            code = cli_main(
                [
                    "dedup-corpus-update",
                    "--snapshot",
                    str(self.snapshot_path),
                    "--approval",
                    str(self.approval_path),
                ],
                out=update_out,
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(update_out.getvalue())["command"], "dedup-corpus-update")
        update.assert_called_once()

    def test_real_ffmpeg_master_is_probed_and_fingerprinted_when_available(self) -> None:
        try:
            ffmpeg = resolve_media_binary("ffmpeg")
            resolve_media_binary("ffprobe")
        except ValidationError as exc:
            self.skipTest(str(exc))
        real_master = self.root / "real.mp4"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1080x1920:r=24:d=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=15",
            "-c:v",
            "mpeg4",
            "-q:v",
            "20",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-shortest",
            "-y",
            str(real_master),
        ]
        completed = subprocess.run(command, capture_output=True, timeout=120, check=False)
        if completed.returncode != 0:
            self.skipTest(completed.stderr.decode("utf-8", errors="replace")[-400:])
        probe = probe_media(real_master)
        video = next(item for item in probe["streams"] if item["codec_type"] == "video")
        audio = next(item for item in probe["streams"] if item["codec_type"] == "audio")
        real_manifest = self._manifest(
            job_id="job_corpus_real",
            render_id="render_corpus_real",
            master=real_master,
        )
        real_manifest["technical"].update(
            {
                "fps": 24,
                "video_codec": video["codec_name"],
                "audio_codec": audio["codec_name"],
            }
        )
        # Use the exact container duration observed by the same ffprobe path.
        real_manifest["technical"]["duration_seconds"] = float(
            probe["format"]["duration"]
        )
        real_manifest_path = self.root / "real-render-manifest.json"
        real_manifest_path.write_text(
            json.dumps(real_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        real_approval = self.root / "real-approval.json"
        create_corpus_approval(
            real_manifest_path,
            real_master,
            real_approval,
            approved_by="integration-test",
            approval_note="Real FFmpeg fixture accepted for builder integration test.",
            human_confirm=CORPUS_APPROVAL_CONFIRMATION,
            approved_at=self.approved_at,
        )
        real_snapshot = self.root / "real-corpus.json"
        result = update_dedup_corpus(
            real_snapshot,
            [real_approval],
            generated_at=self.approved_at,
        )
        self.assertGreaterEqual(
            len(result["snapshot"]["entries"][0]["frame_hashes"]), 8
        )
        self.assertEqual(
            result["snapshot"]["entries"][0]["render_sha256"],
            file_sha256(real_master),
        )


if __name__ == "__main__":
    unittest.main()
