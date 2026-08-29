from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_factory.caption_analyzer import handle_task as analyze_captions
from video_factory.caption_analyzer import main as analyzer_main
from video_factory.caption_transcript_handler import handle_task as observe_captions
from video_factory.errors import ValidationError
from video_factory.validators import canonical_json, digest_text


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_sha(value: dict) -> str:
    return digest_text(canonical_json(value))


class CaptionAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.evidence_root = self.root / "evidence"
        self.evidence_root.mkdir()
        self.observer_executable = self.root / "caption-observer.exe"
        self.observer_executable.write_bytes(b"trusted-caption-observer-v1")
        environment = mock.patch.dict(
            "os.environ",
            {
                "VIDEO_FACTORY_QC_EVIDENCE_ROOT": str(self.evidence_root),
                "VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE": str(
                    self.observer_executable
                ),
                "VIDEO_FACTORY_CAPTION_OBSERVER_TIMEOUT_SECONDS": "30",
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)

        self.job_id = "job_caption_001"
        self.render_id = "render_caption_001"
        self.master = self.root / "master.mp4"
        self.master.write_bytes(b"rendered-master-with-real-audio" * 128)
        self.render = {
            "schema_version": "1.0.0",
            "render_id": self.render_id,
            "job_id": self.job_id,
            "composition": "main",
            "output": "master.mp4",
            "output_sha256": file_sha(self.master),
            "technical": {
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 15,
                "video_codec": "h264",
                "audio_codec": "aac",
                "audio_sample_rate_hz": 48000,
                "integrated_lufs": -14.0,
                "true_peak_dbtp": -1.2,
            },
            "input_hashes": [{"path": "index.html", "sha256": "a" * 64}],
            "created_at": "2026-08-29T08:00:00Z",
        }
        self.script = {
            "schema_version": "1.0.0",
            "idea_id": "idea_caption_001",
            "job_id": self.job_id,
            "lane_id": "motivation",
            "language": "ru",
            "target_duration_seconds": 15,
            "hook": {
                "spoken_text": "Первый шаг начинается прямо сейчас.",
                "first_frame_text": "Начни прямо сейчас",
                "duration_seconds": 2,
            },
            "segments": [
                {
                    "segment_id": "s1",
                    "start_seconds": 0,
                    "end_seconds": 5,
                    "spoken_text": "Первый шаг требует настоящей смелости",
                    "caption_text": "Первый шаг требует смелости",
                    "visual_intent": "Крупный план русского спикера",
                    "claim_ids": [],
                },
                {
                    "segment_id": "s2",
                    "start_seconds": 5,
                    "end_seconds": 10,
                    "spoken_text": "Дисциплина побеждает любое настроение",
                    "caption_text": "Дисциплина побеждает любое настроение",
                    "visual_intent": "Динамичный средний план спикера",
                    "claim_ids": [],
                },
                {
                    "segment_id": "s3",
                    "start_seconds": 10,
                    "end_seconds": 15,
                    "spoken_text": "Сделай важное дело именно сегодня",
                    "caption_text": "Сделай важное дело сегодня",
                    "visual_intent": "Финальный крупный план спикера",
                    "claim_ids": [],
                },
            ],
            "caption_style": {
                "max_lines": 2,
                "max_words_per_card": 5,
                "safe_zone": "center_lower_third",
                "side_labels": False,
            },
            "edit_direction": {
                "visual_world": "Контрастный кинематографичный портрет",
                "music_mood": "Сдержанный напряженный ритм",
                "average_cut_seconds": 2,
                "speaker_scale": 0.9,
            },
            "disclaimer": None,
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "review_notes": [],
            },
        }

    def _render_upstream(self) -> dict:
        return {
            "role": "render",
            "result": {"artifact": self.render, "output_path": str(self.master)},
        }

    def _observer_words(self, drift: float = 0.0) -> list[dict]:
        words = []
        for segment in self.script["segments"]:
            tokens = segment["spoken_text"].split()
            start = float(segment["start_seconds"])
            duration = float(segment["end_seconds"]) - start
            for index, token in enumerate(tokens):
                midpoint = start + duration * (index + 0.5) / len(tokens) + drift
                words.append(
                    {
                        "text": token,
                        "start_seconds": round(midpoint - 0.1, 4),
                        "end_seconds": round(midpoint + 0.1, 4),
                        "confidence": 0.99,
                    }
                )
        return words

    def _measurement(self, *, drift: float = 0.0) -> dict:
        return {
            "status": "completed",
            "warnings": [],
            "language": "ru",
            "duration_seconds": 15,
            "engine": {
                "name": "fixture-asr",
                "version": "1.0.0",
                "run_id": "observer-run-001",
            },
            "completed_at": "2026-08-29T08:01:00Z",
            "words": self._observer_words(drift),
        }

    def _transcript_task(self) -> dict:
        return {
            "job_id": self.job_id,
            "role": "caption_transcript",
            "pod": "motivation",
            "payload": {
                "job_id": self.job_id,
                "lane_id": "motivation",
                "required_result_contract": "caption_transcript_manifest",
            },
            "upstream_results": [self._render_upstream()],
        }

    def _run_transcript(self, measurement: dict | None = None) -> dict:
        supplied = copy.deepcopy(measurement or self._measurement())

        def runner(executable: Path, request: dict, timeout: int) -> dict:
            self.assertEqual(executable, self.observer_executable)
            self.assertEqual(timeout, 30)
            self.assertEqual(request["render_sha256"], self.render["output_sha256"])
            self.assertEqual(request["render_path"], str(self.master))
            self.assertTrue(request["require_word_timestamps"])
            return supplied

        return observe_captions(self._transcript_task(), observer_runner=runner)

    def _analyzer_task(self, transcript_result: dict) -> dict:
        return {
            "job_id": self.job_id,
            "role": "captions_analyzer",
            "pod": "motivation",
            "payload": {
                "job_id": self.job_id,
                "lane_id": "motivation",
                "required_result_contract": "qc_analyzer_report",
            },
            "upstream_results": [
                {"role": "script", "result": {"artifact": self.script}},
                self._render_upstream(),
                {"role": "caption_transcript", "result": transcript_result},
            ],
        }

    def test_trusted_observer_to_checksum_bound_pass_report(self) -> None:
        transcript_result = self._run_transcript()
        transcript_manifest = transcript_result["artifact"]
        self.assertEqual(transcript_manifest["status"], "completed")
        self.assertEqual(transcript_manifest["word_count"], 14)
        transcript_path = Path(transcript_result["evidence"]["path"])
        self.assertEqual(transcript_result["evidence"]["sha256"], file_sha(transcript_path))

        result = analyze_captions(self._analyzer_task(transcript_result))
        report = result["artifact"]
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["needs_human_review"])
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["lane_id"], "motivation")
        self.assertEqual(
            report["bindings"],
            {
                "output_sha256": self.render["output_sha256"],
                "render_manifest_sha256": artifact_sha(self.render),
                "script_package_sha256": artifact_sha(self.script),
                "machine_evidence_sha256": file_sha(transcript_path),
            },
        )
        self.assertEqual(report["metrics"]["observed"]["caption_coverage_ratio"], 1.0)
        self.assertEqual(report["metrics"]["observed"]["alignment_ratio"], 1.0)
        report_path = Path(result["evidence"]["path"])
        self.assertEqual(result["evidence"]["sha256"], file_sha(report_path))

    def test_default_sync_limits_reject_visible_caption_drift(self) -> None:
        p95_measurement = self._measurement(drift=0.30)
        p95_measurement["engine"]["run_id"] = "observer-run-default-p95-drift"
        p95_report = analyze_captions(
            self._analyzer_task(self._run_transcript(p95_measurement))
        )["artifact"]
        self.assertEqual(p95_report["status"], "fail")
        self.assertEqual(
            p95_report["metrics"]["thresholds"]["p95_drift_seconds_max"],
            0.25,
        )
        self.assertEqual(
            p95_report["metrics"]["thresholds"]["absolute_drift_seconds_max"],
            0.45,
        )
        self.assertIn(
            "p95_drift_above_threshold",
            {item["code"] for item in p95_report["findings"]},
        )
        self.assertNotIn(
            "absolute_drift_above_threshold",
            {item["code"] for item in p95_report["findings"]},
        )

        absolute_measurement = self._measurement(drift=0.46)
        absolute_measurement["engine"]["run_id"] = "observer-run-default-max-drift"
        absolute_report = analyze_captions(
            self._analyzer_task(self._run_transcript(absolute_measurement))
        )["artifact"]
        self.assertEqual(absolute_report["status"], "fail")
        self.assertIn(
            "absolute_drift_above_threshold",
            {item["code"] for item in absolute_report["findings"]},
        )

    def test_alignment_coverage_drift_and_overflow_are_hard_failures(self) -> None:
        drifted = self._measurement(drift=0.6)
        drifted["engine"]["run_id"] = "observer-run-drift"
        drifted_result = self._run_transcript(drifted)
        drift_task = self._analyzer_task(drifted_result)
        drift_task["payload"]["thresholds"] = {
            "absolute_drift_seconds_max": 0.5,
            "p95_drift_seconds_max": 0.5,
        }
        drift_report = analyze_captions(drift_task)["artifact"]
        self.assertEqual(drift_report["status"], "fail")
        self.assertFalse(drift_report["needs_human_review"])
        drift_codes = {item["code"] for item in drift_report["findings"]}
        self.assertTrue(
            {
                "alignment_below_threshold",
                "p95_drift_above_threshold",
                "caption_coverage_below_threshold",
            }
            & drift_codes
        )

        clean_result = self._run_transcript()
        overflow_task = self._analyzer_task(clean_result)
        overflow_script = copy.deepcopy(self.script)
        overflow_script["segments"][0]["caption_text"] = (
            "Сверхответственность гиперконцентрация самодисциплина"
        )
        overflow_task["upstream_results"][0]["result"]["artifact"] = overflow_script
        overflow_report = analyze_captions(overflow_task)["artifact"]
        self.assertEqual(overflow_report["status"], "fail")
        self.assertIn(
            "caption_line_overflow",
            {item["code"] for item in overflow_report["findings"]},
        )

    def test_missing_warn_not_run_or_pass_only_transcript_never_passes(self) -> None:
        missing = self._analyzer_task(self._run_transcript())
        missing["upstream_results"] = [
            row for row in missing["upstream_results"] if row["role"] != "caption_transcript"
        ]
        with self.assertRaisesRegex(ValidationError, "exactly one upstream"):
            analyze_captions(missing)

        for status, warnings, message in (
            ("not_run", [], "status must be completed"),
            ("warn", ["low confidence"], "status must be completed"),
            ("failed", [], "status must be completed"),
        ):
            with self.subTest(status=status):
                measurement = self._measurement()
                measurement["status"] = status
                measurement["warnings"] = warnings
                with self.assertRaisesRegex(ValidationError, message):
                    self._run_transcript(measurement)

        pass_only = {"status": "pass"}
        with self.assertRaisesRegex(ValidationError, "word-level measurement"):
            self._run_transcript(pass_only)

    def test_tampered_master_or_transcript_bytes_fail_closed(self) -> None:
        transcript_result = self._run_transcript()
        self.master.write_bytes(self.master.read_bytes() + b"tamper")
        with self.assertRaisesRegex(ValidationError, "master checksum"):
            analyze_captions(self._analyzer_task(transcript_result))

        self.master.write_bytes(b"rendered-master-with-real-audio" * 128)
        transcript_result = self._run_transcript()
        evidence_path = Path(transcript_result["evidence"]["path"])
        evidence_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "checksum"):
            analyze_captions(self._analyzer_task(transcript_result))

    def test_manifest_binding_and_stdio_are_fail_closed(self) -> None:
        transcript_result = self._run_transcript()
        stale = copy.deepcopy(transcript_result)
        stale["artifact"]["render_sha256"] = "b" * 64
        task = self._analyzer_task(stale)
        with self.assertRaisesRegex(ValidationError, "not bound"):
            analyze_captions(task)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = analyzer_main(io.StringIO('{"role":"publisher"}'), stdout)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("caption_analyzer_error", stderr.getvalue())

    def test_observer_executable_must_be_absolute(self) -> None:
        with mock.patch.dict(
            "os.environ", {"VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE": "observer.exe"}
        ):
            with self.assertRaisesRegex(ValidationError, "must be absolute"):
                self._run_transcript()

    def test_default_observer_is_exact_json_stdio_without_shell(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout=json.dumps(self._measurement(), ensure_ascii=False),
            stderr="",
        )
        with mock.patch(
            "video_factory.caption_transcript_handler.subprocess.run",
            return_value=completed,
        ) as run:
            result = observe_captions(self._transcript_task())
        self.assertEqual(result["artifact"]["status"], "completed")
        args, kwargs = run.call_args
        self.assertEqual(args[0], [str(self.observer_executable)])
        self.assertFalse(kwargs["shell"])
        request = json.loads(kwargs["input"])
        self.assertEqual(request["render_sha256"], self.render["output_sha256"])
        self.assertTrue(request["require_word_timestamps"])


if __name__ == "__main__":
    unittest.main()
