from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from video_factory.errors import ValidationError
from video_factory.qc_analyzer_common import (
    GrayFrame,
    sha256_file,
    validate_qc_analyzer_report,
)
from video_factory.visual_analyzer import analyze_visual


def checkerboard_frames(count: int = 15) -> list[GrayFrame]:
    width, height = 180, 320
    pixels = bytes(
        255 if (x + y) % 2 else 0
        for y in range(height)
        for x in range(width)
    )
    return [GrayFrame(index, float(index), width, height, pixels) for index in range(count)]


class VisualAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.render_path = self.root / "master.mp4"
        self.render_path.write_bytes(b"visual-render-bytes" * 128)
        render_sha = sha256_file(self.render_path)
        self.render_manifest = {
            "schema_version": "1.0.0",
            "render_id": "render_visual_001",
            "job_id": "job_visual_001",
            "composition": "main",
            "output": "master.mp4",
            "output_sha256": render_sha,
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
        self.shotlist = {
            "schema_version": "1.0.0",
            "idea_id": "idea_visual_001",
            "duration_seconds": 15,
            "aspect": "9:16",
            "shots": [
                {
                    "shot_id": "shot_001",
                    "start": 0,
                    "end": 15,
                    "narration": "speaker",
                    "caption": "caption",
                    "visual_intent": "speaker portrait",
                    "asset_id": "asset_001",
                    "claim_ids": [],
                    "transition": "hard_cut",
                }
            ],
        }
        self.frames = checkerboard_frames()
        self.face = {
            "bbox": [0.35, 0.12, 0.30, 0.30],
            "confidence": 0.99,
            "speaker": True,
            "occlusion_fraction": 0.0,
        }
        self.report_path = self.root / "visual.json"
        self.contact_path = self.root / "contact-sheet.pgm"

    def extractor(self, path: Path, **kwargs: object) -> list[GrayFrame]:
        self.assertEqual(path, self.render_path)
        self.assertEqual(kwargs["width"], 180)
        self.assertEqual(kwargs["height"], 320)
        return self.frames

    def probe(self, path: str | Path) -> dict:
        return {
            "path": str(path),
            "format": {"duration": "15.0", "size": "1000"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1080,
                    "height": 1920,
                    "pix_fmt": "yuv420p",
                    "avg_frame_rate": "30/1",
                    "duration": "15.0",
                }
            ],
        }

    def observer(
        self, face: dict | list[dict] | None = None, *, stale: bool = False
    ):
        chosen = copy.deepcopy(self.face if face is None else face)
        chosen_faces = chosen if isinstance(chosen, list) else [chosen]

        def observe(
            path: Path,
            render_sha256: str,
            frames: list[GrayFrame],
            **kwargs: object,
        ) -> dict:
            return {
                "schema_version": "1.0.0",
                "render_sha256": render_sha256,
                "checker": {
                    "name": "fixture_face_model",
                    "version": "1.0.0",
                    "model_sha256": "d" * 64,
                },
                "observations": [
                    {
                        "frame_index": frame.index,
                        "frame_sha256": (
                            "e" * 64 if stale and frame.index == 0 else frame.sha256
                        ),
                        "faces": copy.deepcopy(chosen_faces),
                    }
                    for frame in frames
                ],
            }

        return observe

    def analyze(
        self,
        *,
        face: dict | list[dict] | None = None,
        speaker_required: bool = True,
        probe=None,
        observer=None,
    ) -> dict:
        return analyze_visual(
            self.render_path,
            self.render_manifest,
            self.shotlist,
            lane_id="motivation",
            speaker_required=speaker_required,
            report_path=self.report_path,
            contact_sheet_path=self.contact_path,
            frame_extractor=self.extractor,
            face_observer=observer or self.observer(face),
            probe_runner=probe or self.probe,
            completed_at=datetime(2026, 8, 29, 12, 2, tzinfo=UTC),
        )

    def test_pass_contains_actual_frame_observations_and_bound_sidecars(self) -> None:
        result = self.analyze()
        report = result["artifact"]
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["lane_id"], "motivation")
        self.assertEqual(report["metrics"]["sampled_frame_count"], 15)
        self.assertEqual(len(report["metrics"]["observations"]), 15)
        self.assertTrue(
            all(row["blur_score"] > 35 for row in report["metrics"]["observations"])
        )
        self.assertEqual(result["evidence"]["sha256"], sha256_file(self.report_path))
        self.assertEqual(
            result["contact_sheet"]["sha256"], sha256_file(self.contact_path)
        )
        self.assertEqual(
            report["bindings"]["contact_sheet_sha256"],
            result["contact_sheet"]["sha256"],
        )
        self.assertTrue(self.contact_path.read_bytes().startswith(b"P5\n"))
        self.assertIs(validate_qc_analyzer_report(report), report)

    def test_black_bar_blur_crop_safe_zone_occlusion_and_missing_speaker_fail(self) -> None:
        sharp = checkerboard_frames()
        blackbar_frames = []
        for frame in sharp:
            pixels = bytearray(frame.pixels)
            band = 13
            pixels[: band * frame.width] = b"\x00" * (band * frame.width)
            pixels[-band * frame.width :] = b"\x00" * (band * frame.width)
            blackbar_frames.append(
                GrayFrame(frame.index, frame.timestamp_seconds, 180, 320, bytes(pixels))
            )
        cases = (
            ("black", blackbar_frames, self.face, "black_bars_or_frames"),
            (
                "blur",
                [GrayFrame(index, float(index), 180, 320, b"\x80" * (180 * 320)) for index in range(15)],
                self.face,
                "blurred_frames",
            ),
            (
                "crop",
                sharp,
                {**self.face, "bbox": [0.0, 0.12, 0.30, 0.30]},
                "cropped_speaker_face",
            ),
            (
                "safe-zone",
                sharp,
                {**self.face, "bbox": [0.30, 0.65, 0.40, 0.25]},
                "speaker_caption_safe_zone_overlap",
            ),
            (
                "occlusion",
                sharp,
                {**self.face, "occlusion_fraction": 0.80},
                "occluded_speaker_face",
            ),
            (
                "speaker",
                sharp,
                {**self.face, "speaker": False},
                "speaker_visibility",
            ),
        )
        for label, frames, face, expected in cases:
            with self.subTest(label=label):
                self.frames = frames
                result = self.analyze(face=face)
                report = result["artifact"]
                self.assertEqual(report["status"], "fail")
                self.assertIn(expected, {item["code"] for item in report["findings"]})

    def test_required_speaker_face_size_uses_median_and_low_frame_ratio(self) -> None:
        result = self.analyze(
            face={**self.face, "bbox": [0.43, 0.18, 0.14, 0.16]}
        )
        report = result["artifact"]
        self.assertEqual(report["status"], "fail")
        self.assertIn(
            "speaker_face_too_small",
            {item["code"] for item in report["findings"]},
        )
        summary = report["metrics"]["summary"]
        thresholds = report["metrics"]["thresholds"]
        self.assertEqual(summary["median_speaker_face_area_ratio"], 0.0224)
        self.assertEqual(summary["small_speaker_face_frame_ratio"], 1.0)
        self.assertEqual(thresholds["minimum_speaker_face_area_ratio"], 0.025)
        self.assertEqual(
            thresholds["minimum_median_speaker_face_area_ratio"], 0.045
        )
        self.assertEqual(
            thresholds["maximum_small_speaker_face_frame_ratio"], 0.20
        )

    def test_confident_non_speaker_face_crop_is_a_hard_failure(self) -> None:
        other_face = {
            "bbox": [0.0, 0.18, 0.15, 0.18],
            "confidence": 0.98,
            "speaker": False,
            "occlusion_fraction": 0.0,
        }
        result = self.analyze(face=[self.face, other_face])
        report = result["artifact"]
        self.assertEqual(report["status"], "fail")
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("cropped_detected_face", codes)
        self.assertNotIn("cropped_speaker_face", codes)
        self.assertEqual(
            report["metrics"]["summary"]["cropped_detected_face_frame_count"],
            15,
        )
        self.assertEqual(
            report["metrics"]["summary"]["cropped_speaker_frame_count"], 0
        )

    def test_non_vertical_actual_geometry_is_a_measured_failure(self) -> None:
        def wrong_geometry(path: str | Path) -> dict:
            value = self.probe(path)
            value["streams"][0]["width"] = 720
            return value

        result = self.analyze(probe=wrong_geometry)
        self.assertEqual(result["artifact"]["status"], "fail")
        self.assertIn(
            "invalid_vertical_geometry",
            {item["code"] for item in result["artifact"]["findings"]},
        )

    def test_missing_or_stale_face_observer_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValidationError, "stale for decoded frame"):
            self.analyze(observer=self.observer(stale=True))

        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValidationError, "FACE_OBSERVER is required"):
                analyze_visual(
                    self.render_path,
                    self.render_manifest,
                    self.shotlist,
                    lane_id="motivation",
                    speaker_required=True,
                    report_path=self.report_path,
                    contact_sheet_path=self.contact_path,
                    frame_extractor=self.extractor,
                    probe_runner=self.probe,
                )

    def test_warn_not_run_and_forged_summary_cannot_be_promoted(self) -> None:
        report = self.analyze()["artifact"]
        for status in ("warn", "not_run"):
            with self.subTest(status=status):
                changed = copy.deepcopy(report)
                changed["status"] = status
                with self.assertRaisesRegex(ValidationError, "fail-closed"):
                    validate_qc_analyzer_report(changed)

        forged = copy.deepcopy(report)
        forged["metrics"]["summary"]["speaker_frame_count"] = 0
        with self.assertRaisesRegex(ValidationError, "does not match observations"):
            validate_qc_analyzer_report(forged)

        forged_area = copy.deepcopy(report)
        forged_area["metrics"]["observations"][0]["faces"][0]["area_ratio"] = 0.5
        with self.assertRaisesRegex(ValidationError, "not derived from bbox"):
            validate_qc_analyzer_report(forged_area)


if __name__ == "__main__":
    unittest.main()
