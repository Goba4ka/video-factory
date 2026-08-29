from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_factory.contracts import validate_artifact
from video_factory.errors import ValidationError
from video_factory.media_freeze import freeze_explicit_media
from video_factory.semantic_qc_handler import handle_task, main
from video_factory.validators import canonical_json, digest_text


CATEGORIES = {
    "technical",
    "audio",
    "captions",
    "facts",
    "rights",
    "dedup",
    "policy",
    "visual",
}


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_sha(value: dict) -> str:
    return digest_text(canonical_json(value))


class SemanticQCHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.evidence_root = self.root / "evidence"
        self.evidence_root.mkdir()
        environment = mock.patch.dict(
            "os.environ",
            {"VIDEO_FACTORY_QC_EVIDENCE_ROOT": str(self.evidence_root)},
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)

        self.job_id = "job_qc_001"
        self.idea_id = "idea_qc_001"
        self.render_id = "render_qc_001"
        self.claim_ledger = {
            "schema_version": "1.0.0",
            "idea_id": self.idea_id,
            "sources": [
                {
                    "source_id": "source_001",
                    "url": "https://example.test/research",
                    "publisher": "Fixture publisher",
                    "retrieved_at": "2026-08-29T08:00:00Z",
                    "primary": True,
                }
            ],
            "claims": [
                {
                    "claim_id": "claim_001",
                    "text": "Тестовый подтверждённый тезис.",
                    "source_ids": ["source_001"],
                    "support": "direct",
                    "risk": "green",
                    "script_usage": "allowed",
                }
            ],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "review_notes": [],
            },
        }
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"owned-source-bytes" * 64)
        self.rights = {
            "schema_version": "1.0.0",
            "idea_id": self.idea_id,
            "assets": [
                {
                    "asset_id": "asset_001",
                    "local_path": str(self.source),
                    "download_url": None,
                    "landing_url": "https://example.test/source",
                    "creator": "Fixture owner",
                    "license": "Owned fixture",
                    "license_url": "https://example.test/license",
                    "license_receipt": "receipt-001",
                    "retrieved_at": "2026-08-29T08:00:00Z",
                    "commercial_use": True,
                    "modification_allowed": True,
                    "attribution_required": False,
                    "attribution_text": None,
                    "model_release": "confirmed",
                    "property_release": "not_applicable",
                    "platforms": ["youtube_shorts", "instagram_reels", "tiktok"],
                    "territories": ["worldwide"],
                    "expires_at": None,
                    "rights_status": "approved",
                    "notes": "test fixture",
                }
            ],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "missing_asset_ids": [],
                "review_notes": [],
            },
        }
        self.frozen = freeze_explicit_media(
            self.rights,
            [{"asset_id": "asset_001", "local_path": str(self.source)}],
            self.root / "frozen",
            job_id=self.job_id,
            allowed_local_roots=[self.root],
        )["artifact"]
        self.script = {
            "schema_version": "1.0.0",
            "idea_id": self.idea_id,
            "job_id": self.job_id,
            "lane_id": "motivation",
            "language": "ru",
            "target_duration_seconds": 15,
            "hook": {
                "spoken_text": "Начни действовать прямо сейчас.",
                "first_frame_text": "Начни сейчас",
                "duration_seconds": 2,
            },
            "segments": [
                {
                    "segment_id": "s1",
                    "start_seconds": 0,
                    "end_seconds": 5,
                    "spoken_text": "Первый шаг всегда самый сложный.",
                    "caption_text": "Первый шаг самый сложный",
                    "visual_intent": "Крупный план спикера",
                    "claim_ids": [],
                },
                {
                    "segment_id": "s2",
                    "start_seconds": 5,
                    "end_seconds": 10,
                    "spoken_text": "Дисциплина сильнее случайного настроения.",
                    "caption_text": "Дисциплина сильнее настроения",
                    "visual_intent": "Динамичный средний план",
                    "claim_ids": [],
                },
                {
                    "segment_id": "s3",
                    "start_seconds": 10,
                    "end_seconds": 15,
                    "spoken_text": "Сделай сегодня то, что откладывал.",
                    "caption_text": "Сделай это сегодня",
                    "visual_intent": "Финальный крупный план",
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
        self.shotlist = {
            "schema_version": "1.0.0",
            "idea_id": self.idea_id,
            "duration_seconds": 15,
            "aspect": "9:16",
            "shots": [
                {
                    "shot_id": "shot_001",
                    "start": 0,
                    "end": 15,
                    "narration": "Мотивационная речь",
                    "caption": "Начни сейчас",
                    "visual_intent": "Спикер в безопасном кадре",
                    "asset_id": "asset_001",
                    "source_in": 0,
                    "source_out": 15,
                    "claim_ids": [],
                    "transition": "hard_cut",
                }
            ],
        }
        self.output = self.root / "master.mp4"
        self.output.write_bytes(b"render-master-bytes" * 128)
        self.render = {
            "schema_version": "1.0.0",
            "render_id": self.render_id,
            "job_id": self.job_id,
            "composition": "main",
            "output": "master.mp4",
            "output_sha256": file_sha(self.output),
            "technical": {
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 15,
                "video_codec": "h264",
                "audio_codec": "aac",
                "audio_sample_rate_hz": 48000,
                "integrated_lufs": -14.5,
                "true_peak_dbtp": -1.5,
            },
            "input_hashes": [{"path": "index.html", "sha256": "a" * 64}],
            "created_at": "2026-08-29T08:01:00Z",
        }
        self.contact_sheet = self.evidence_root / "contact-sheet.jpg"
        self.contact_sheet.write_bytes(b"contact-sheet" * 64)
        self.evidence = self._write_evidence()
        self.task = self._task()
        self.technical_report = self.root / "technical-report.json"
        self.technical_report.write_text("{}\n", encoding="utf-8")

    def _bindings(self, category: str) -> dict[str, str]:
        common = {
            "output_sha256": self.render["output_sha256"],
            "render_manifest_sha256": artifact_sha(self.render),
            "claim_ledger_sha256": artifact_sha(self.claim_ledger),
            "script_package_sha256": artifact_sha(self.script),
            "shotlist_sha256": artifact_sha(self.shotlist),
            "rights_manifest_sha256": artifact_sha(self.rights),
            "frozen_media_manifest_sha256": artifact_sha(self.frozen),
            "corpus_snapshot_sha256": "b" * 64,
            "contact_sheet_sha256": file_sha(self.contact_sheet),
        }
        names = {
            "technical": {"output_sha256", "render_manifest_sha256"},
            "audio": {"output_sha256", "render_manifest_sha256"},
            "captions": {
                "output_sha256",
                "render_manifest_sha256",
                "script_package_sha256",
                "machine_evidence_sha256",
            },
            "facts": {
                "output_sha256",
                "render_manifest_sha256",
                "claim_ledger_sha256",
                "script_package_sha256",
                "shotlist_sha256",
            },
            "rights": {
                "output_sha256",
                "render_manifest_sha256",
                "rights_manifest_sha256",
                "frozen_media_manifest_sha256",
                "shotlist_sha256",
            },
            "dedup": {
                "output_sha256",
                "render_manifest_sha256",
                "corpus_snapshot_sha256",
            },
            "policy": {
                "output_sha256",
                "render_manifest_sha256",
                "claim_ledger_sha256",
                "script_package_sha256",
            },
            "visual": {
                "output_sha256",
                "render_manifest_sha256",
                "shotlist_sha256",
                "contact_sheet_sha256",
            },
        }
        common["machine_evidence_sha256"] = "c" * 64
        return {name: common[name] for name in names[category]}

    def _write_evidence(self) -> dict[str, dict[str, str]]:
        descriptors = {}
        for category in sorted(CATEGORIES):
            report = {
                "schema_version": "1.0.0",
                "category": category,
                "job_id": self.job_id,
                "lane_id": "motivation",
                "render_id": self.render_id,
                "render_sha256": self.render["output_sha256"],
                "status": "pass",
                "needs_human_review": False,
                "warnings": [],
                "findings": [],
                "checker": {
                    "name": f"fixture-{category}",
                    "version": "1.0.0",
                    "run_id": f"run-{category}-001",
                },
                "completed_at": "2026-08-29T08:02:00Z",
                "bindings": self._bindings(category),
                "metrics": {"fixture_observations": 1},
            }
            path = self.evidence_root / f"{category}.json"
            path.write_text(
                json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            descriptors[category] = {"path": str(path), "sha256": file_sha(path)}
        return descriptors

    def _task(self) -> dict:
        def upstream(role: str, artifact: dict, **result_fields: str) -> dict:
            return {
                "role": role,
                "result": {"artifact": artifact, **result_fields},
            }

        return {
            "job_id": self.job_id,
            "role": "qc",
            "pod": "motivation",
            "payload": {
                "job_id": self.job_id,
                "lane_id": "motivation",
                "required_result_contract": "qc_report",
                "technical_profile": "motivation_v3_master",
                "evidence": copy.deepcopy(self.evidence),
                "visual_contact_sheet": {
                    "path": str(self.contact_sheet),
                    "sha256": file_sha(self.contact_sheet),
                },
            },
            "upstream_results": [
                upstream("research", self.claim_ledger),
                upstream("rights", self.rights),
                upstream("media", self.frozen),
                upstream("script", self.script),
                upstream("editor", self.shotlist),
                upstream("render", self.render, output_path=str(self.output)),
            ],
        }

    def _technical_pass(self, source: Path, **kwargs: object) -> dict:
        self.assertEqual(source, self.output)
        self.assertEqual(kwargs["level"], "full")
        self.assertEqual(kwargs["profile_name"], "motivation_v3_master")
        return {
            "level": "full",
            "source": str(self.output),
            "technical_pass": True,
            "failures": [],
            "warnings": [],
            "media": {
                "video": {"width": 1080, "height": 1920, "fps": 30.0},
                "audio": {"sample_rate_hz": 48000},
            },
            "cache": {"report_path": str(self.technical_report)},
        }

    def _rewrite_report(self, task: dict, category: str, **changes: object) -> None:
        descriptor = task["payload"]["evidence"][category]
        path = Path(descriptor["path"])
        report = json.loads(path.read_text(encoding="utf-8"))
        report.update(changes)
        path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
        descriptor["sha256"] = file_sha(path)

    def test_builds_schema_valid_report_only_from_eight_bound_passes(self) -> None:
        result = handle_task(self.task, media_qc_runner=self._technical_pass)
        self.assertIs(validate_artifact("qc_report", result["artifact"]), result["artifact"])
        self.assertTrue(result["artifact"]["decision"]["passed"])
        self.assertEqual(
            {item["category"] for item in result["artifact"]["checks"]}, CATEGORIES
        )
        self.assertTrue(all(item["status"] == "pass" for item in result["artifact"]["checks"]))
        self.assertEqual(result["visual_contact_sheet_sha256"], file_sha(self.contact_sheet))

    def test_missing_or_tampered_evidence_fails_closed(self) -> None:
        missing = copy.deepcopy(self.task)
        del missing["payload"]["evidence"]["facts"]
        with self.assertRaisesRegex(ValidationError, "exactly the eight"):
            handle_task(missing, media_qc_runner=self._technical_pass)

        tampered = copy.deepcopy(self.task)
        Path(tampered["payload"]["evidence"]["captions"]["path"]).write_text(
            "{}\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValidationError, "checksum"):
            handle_task(tampered, media_qc_runner=self._technical_pass)

    def test_warn_not_run_or_human_review_never_becomes_pass(self) -> None:
        for category, changes, message in (
            ("policy", {"status": "warn"}, "status is not pass"),
            ("dedup", {"status": "not_run"}, "status is not pass"),
            ("visual", {"needs_human_review": True}, "needs human review"),
            ("facts", {"warnings": ["stale source"]}, "contains warnings"),
        ):
            with self.subTest(category=category):
                self.evidence = self._write_evidence()
                task = self._task()
                self._rewrite_report(task, category, **changes)
                with self.assertRaisesRegex(ValidationError, message):
                    handle_task(task, media_qc_runner=self._technical_pass)

    def test_technical_warning_or_stale_binding_fails_closed(self) -> None:
        def warned(source: Path, **kwargs: object) -> dict:
            result = self._technical_pass(source, **kwargs)
            result["warnings"] = [{"code": "loudness_range", "message": "wide"}]
            return result

        with self.assertRaisesRegex(ValidationError, "failures or warnings"):
            handle_task(self.task, media_qc_runner=warned)

        self.evidence = self._write_evidence()
        stale = self._task()
        visual_path = Path(stale["payload"]["evidence"]["visual"]["path"])
        visual = json.loads(visual_path.read_text(encoding="utf-8"))
        visual["bindings"]["contact_sheet_sha256"] = "c" * 64
        visual_path.write_text(json.dumps(visual, sort_keys=True) + "\n", encoding="utf-8")
        stale["payload"]["evidence"]["visual"]["sha256"] = file_sha(visual_path)
        with self.assertRaisesRegex(ValidationError, "not bound to the contact sheet"):
            handle_task(stale, media_qc_runner=self._technical_pass)

    def test_stdio_invalid_task_returns_nonzero_without_artifact(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = main(io.StringIO('{"role":"publisher"}'), stdout)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("semantic_qc_handler_error", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
