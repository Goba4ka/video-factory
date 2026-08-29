from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from video_factory.dedup_analyzer import analyze_dedup, dhash64
from video_factory.errors import ValidationError
from video_factory.media_tools import resolve_media_binary
from video_factory.qc_analyzer_common import (
    GrayFrame,
    extract_gray_frames,
    sha256_file,
    validate_qc_analyzer_report,
)


MASK64 = (1 << 64) - 1


def frames_for_hashes(hashes: list[str]) -> list[GrayFrame]:
    frames = []
    for index, raw_hash in enumerate(hashes):
        bits = f"{int(raw_hash, 16):064b}"
        pixels = bytearray()
        for row in range(8):
            current = 128
            pixels.append(current)
            for bit in bits[row * 8 : (row + 1) * 8]:
                current += -4 if bit == "1" else 4
                pixels.append(current)
        frame = GrayFrame(index, float(index), 9, 8, bytes(pixels))
        if dhash64(frame) != raw_hash:
            raise AssertionError("hash fixture construction failed")
        frames.append(frame)
    return frames


class DedupAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.render_path = self.root / "master.mp4"
        self.render_path.write_bytes(b"checksum-bound-render" * 128)
        self.render_sha256 = sha256_file(self.render_path)
        self.render_manifest = {
            "schema_version": "1.0.0",
            "render_id": "render_dedup_001",
            "job_id": "job_dedup_001",
            "composition": "main",
            "output": "master.mp4",
            "output_sha256": self.render_sha256,
            "technical": {
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 15,
                "video_codec": "h264",
                "audio_codec": "aac",
                "audio_sample_rate_hz": 48000,
            },
            "input_hashes": [{"path": "index.html", "sha256": "a" * 64}],
            "created_at": "2026-08-29T12:00:00Z",
        }
        self.current_hashes = [
            f"{((index + 1) * 0x9E3779B97F4A7C15) & MASK64:016x}"
            for index in range(15)
        ]
        self.frames = frames_for_hashes(self.current_hashes)
        self.report_path = self.root / "dedup.json"
        self.corpus_counter = 0

    def extractor(self, path: Path, **kwargs: object) -> list[GrayFrame]:
        self.assertEqual(path, self.render_path)
        self.assertEqual(kwargs["width"], 9)
        self.assertEqual(kwargs["height"], 8)
        return self.frames

    def corpus(self, hashes: list[str], *, render_sha256: str = "b" * 64) -> dict:
        self.corpus_counter += 1
        value = {
            "schema_version": "1.0.0",
            "snapshot_id": "corpus_20260829",
            "generated_at": "2026-08-29T12:00:00Z",
            "algorithm": "dhash-64-v1",
            "sample_interval_seconds": 1.0,
            "entries": [
                {
                    "comparison_id": "comparison_001",
                    "job_id": "job_prior_001",
                    "render_id": "render_prior_001",
                    "render_sha256": render_sha256,
                    "frame_hashes": hashes,
                }
            ],
        }
        path = self.root / f"corpus-{self.corpus_counter}.json"
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        return {"path": str(path), "sha256": sha256_file(path)}

    def analyze(self, corpus: dict) -> dict:
        return analyze_dedup(
            self.render_path,
            self.render_manifest,
            corpus,
            lane_id="motivation",
            report_path=self.report_path,
            frame_extractor=self.extractor,
            completed_at=datetime(2026, 8, 29, 12, 1, tzinfo=UTC),
        )

    def test_pass_has_real_fingerprints_corpus_comparisons_and_evidence(self) -> None:
        different = [f"{int(value, 16) ^ MASK64:016x}" for value in self.current_hashes]
        result = self.analyze(self.corpus(different))
        report = result["artifact"]
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["lane_id"], "motivation")
        self.assertEqual(report["metrics"]["frame_hashes"], self.current_hashes)
        self.assertEqual(len(report["metrics"]["comparisons"]), 1)
        self.assertEqual(
            report["bindings"]["output_sha256"], self.render_sha256
        )
        self.assertEqual(result["evidence"]["path"], str(self.report_path))
        self.assertEqual(result["evidence"]["sha256"], sha256_file(self.report_path))
        self.assertIs(validate_qc_analyzer_report(report), report)

    def test_exact_near_duplicate_and_reused_sequence_each_fail(self) -> None:
        different = [f"{int(value, 16) ^ MASK64:016x}" for value in self.current_hashes]
        reused = different.copy()
        reused[2:8] = self.current_hashes[4:10]
        cases = (
            (
                "exact",
                self.corpus(different, render_sha256=self.render_sha256),
                "exact_render_duplicate",
            ),
            ("near", self.corpus(self.current_hashes), "perceptual_near_duplicate"),
            ("reuse", self.corpus(reused), "reused_visual_sequence"),
        )
        for label, corpus, expected in cases:
            with self.subTest(label=label):
                result = self.analyze(corpus)
                report = result["artifact"]
                self.assertEqual(report["status"], "fail")
                self.assertIn(expected, {item["code"] for item in report["findings"]})

    def test_missing_empty_or_tampered_corpus_fails_before_pass(self) -> None:
        different = [f"{int(value, 16) ^ MASK64:016x}" for value in self.current_hashes]
        descriptor = self.corpus(different)
        corpus_path = Path(descriptor["path"])
        corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
        corpus["entries"] = []
        corpus_path.write_text(json.dumps(corpus) + "\n", encoding="utf-8")
        descriptor["sha256"] = sha256_file(corpus_path)
        with self.assertRaisesRegex(ValidationError, "non-empty"):
            self.analyze(descriptor)

        descriptor = self.corpus(different)
        Path(descriptor["path"]).write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "checksum"):
            self.analyze(descriptor)

    def test_render_hash_binding_is_checked_before_frame_extraction(self) -> None:
        different = [f"{int(value, 16) ^ MASK64:016x}" for value in self.current_hashes]
        self.render_path.write_bytes(b"tampered")
        called = False

        def extractor(*args: object, **kwargs: object) -> list[GrayFrame]:
            nonlocal called
            called = True
            return self.frames

        with self.assertRaisesRegex(ValidationError, "do not match"):
            analyze_dedup(
                self.render_path,
                self.render_manifest,
                self.corpus(different),
                lane_id="motivation",
                report_path=self.report_path,
                frame_extractor=extractor,
            )
        self.assertFalse(called)

    def test_bare_warn_not_run_or_forged_pass_is_rejected(self) -> None:
        different = [f"{int(value, 16) ^ MASK64:016x}" for value in self.current_hashes]
        report = self.analyze(self.corpus(different))["artifact"]
        for status in ("warn", "not_run"):
            with self.subTest(status=status):
                changed = copy.deepcopy(report)
                changed["status"] = status
                with self.assertRaisesRegex(ValidationError, "fail-closed"):
                    validate_qc_analyzer_report(changed)

        forged = copy.deepcopy(report)
        forged["metrics"]["summary"]["near_duplicate_count"] = 1
        with self.assertRaisesRegex(ValidationError, "does not match comparisons"):
            validate_qc_analyzer_report(forged)

    def test_default_extractor_decodes_actual_ffmpeg_frame_bytes(self) -> None:
        try:
            ffmpeg = resolve_media_binary("ffmpeg")
        except ValidationError as exc:
            self.skipTest(str(exc))
        generated = self.root / "actual-video.mp4"
        completed = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=64x64:rate=2:duration=8",
                "-an",
                "-c:v",
                "mpeg4",
                "-y",
                str(generated),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest(completed.stderr.decode("utf-8", errors="replace")[-400:])
        frames = extract_gray_frames(
            generated,
            interval_seconds=1.0,
            width=9,
            height=8,
            maximum_frames=10,
        )
        self.assertGreaterEqual(len(frames), 8)
        self.assertGreater(len({frame.sha256 for frame in frames}), 1)
        self.assertTrue(all(len(frame.pixels) == 72 for frame in frames))


if __name__ == "__main__":
    unittest.main()
