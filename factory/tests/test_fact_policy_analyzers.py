from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_factory.errors import ValidationError
from video_factory.facts_analyzer import handle_task as analyze_facts
from video_factory.policy_analyzer import handle_task as analyze_policy


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AnalyzerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.evidence_root = self.root / "evidence"
        environment = mock.patch.dict(
            "os.environ",
            {"VIDEO_FACTORY_QC_EVIDENCE_ROOT": str(self.evidence_root)},
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.job_id = "job_analyzer_001"
        self.idea_id = "idea_analyzer_001"
        self.master = self.root / "master.mp4"
        self.master.write_bytes(b"rendered-master" * 256)
        self.ledger = {
            "schema_version": "1.0.0",
            "idea_id": self.idea_id,
            "sources": [
                {
                    "source_id": "source_001",
                    "url": "https://example.test/primary",
                    "publisher": "Primary publisher",
                    "retrieved_at": "2026-08-29T08:00:00Z",
                    "primary": True,
                },
                {
                    "source_id": "source_002",
                    "url": "https://example.test/secondary",
                    "publisher": "Secondary publisher",
                    "retrieved_at": "2026-08-29T08:00:30Z",
                    "primary": False,
                },
            ],
            "claims": [
                {
                    "claim_id": "claim_001",
                    "text": "Р¤Р°РєС‚ РїРѕРґС‚РІРµСЂР¶РґС‘РЅ РґРІСѓРјСЏ РёСЃС‚РѕС‡РЅРёРєР°РјРё.",
                    "source_ids": ["source_001", "source_002"],
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
        self.script = {
            "schema_version": "1.0.0",
            "idea_id": self.idea_id,
            "job_id": self.job_id,
            "lane_id": "health",
            "language": "ru",
            "target_duration_seconds": 15,
            "hook": {
                "spoken_text": "Р’РѕС‚ С„Р°РєС‚, РєРѕС‚РѕСЂС‹Р№ РІР°Р¶РЅРѕ РїСЂРѕРІРµСЂРёС‚СЊ.",
                "first_frame_text": "РџСЂРѕРІРµСЂРµРЅРЅС‹Р№ С„Р°РєС‚",
                "duration_seconds": 2,
            },
            "segments": [
                {
                    "segment_id": f"s{index}",
                    "start_seconds": (index - 1) * 5,
                    "end_seconds": index * 5,
                    "spoken_text": "Р­С‚Рѕ РїСЂРѕРІРµСЂРµРЅРЅС‹Р№ СЂСѓСЃСЃРєРёР№ С‚РµРєСЃС‚.",
                    "caption_text": "РџСЂРѕРІРµСЂРµРЅРЅС‹Р№ С„Р°РєС‚",
                    "visual_intent": "РљСЂСѓРїРЅС‹Р№ РїР»Р°РЅ Рё РёРЅС„РѕРіСЂР°С„РёРєР°",
                    "claim_ids": ["claim_001"],
                }
                for index in range(1, 4)
            ],
            "caption_style": {
                "max_lines": 2,
                "max_words_per_card": 5,
                "safe_zone": "center_lower_third",
                "side_labels": False,
            },
            "edit_direction": {
                "visual_world": "Р§РёСЃС‚С‹Р№ РґРѕРєСѓРјРµРЅС‚Р°Р»СЊРЅС‹Р№ СЃС‚РёР»СЊ",
                "music_mood": "РЎРїРѕРєРѕР№РЅС‹Р№ СЂРёС‚Рј",
                "average_cut_seconds": 2,
                "speaker_scale": 0.9,
            },
            "disclaimer": "РќРµ РјРµРґРёС†РёРЅСЃРєР°СЏ СЂРµРєРѕРјРµРЅРґР°С†РёСЏ.",
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
                    "narration": "РџСЂРѕРІРµСЂРµРЅРЅС‹Р№ С„Р°РєС‚.",
                    "caption": "РџСЂРѕРІРµСЂРµРЅРѕ",
                    "visual_intent": "РЎРїРёРєРµСЂ РІ Р±РµР·РѕРїР°СЃРЅРѕРј РєР°РґСЂРµ",
                    "asset_id": "asset_001",
                    "claim_ids": ["claim_001"],
                    "transition": "hard_cut",
                }
            ],
        }
        self.render = {
            "schema_version": "1.0.0",
            "render_id": "render_analyzer_001",
            "job_id": self.job_id,
            "composition": "main",
            "output": "master.mp4",
            "output_sha256": _file_sha256(self.master),
            "technical": {
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 15,
                "video_codec": "h264",
                "audio_codec": "aac",
                "audio_sample_rate_hz": 48000,
                "integrated_lufs": -14.0,
                "true_peak_dbtp": -1.0,
            },
            "input_hashes": [{"path": "index.html", "sha256": "a" * 64}],
            "created_at": "2026-08-29T08:03:00Z",
        }
        self.safety = {
            "schema_version": "1.0.0",
            "job_id": self.job_id,
            "idea_id": self.idea_id,
            "lane": "health",
            "gate_type": "medical_safety",
            "checked_at": "2026-08-29T08:02:00Z",
            "reviewer": "policy checker",
            "source_ids_checked": ["source_001", "source_002"],
            "findings": [],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "review_notes": [],
            },
        }

    @staticmethod
    def _upstream(role: str, artifact: dict, **extra: str) -> dict:
        return {"role": role, "result": {"artifact": copy.deepcopy(artifact), **extra}}

    def task(self, role: str, *, include_safety: bool = True) -> dict:
        upstream = [
            self._upstream("research", self.ledger),
            self._upstream("script", self.script),
            self._upstream("editor", self.shotlist),
            self._upstream("render", self.render, output_path=str(self.master)),
        ]
        if include_safety:
            upstream.append(self._upstream("medical_review", self.safety))
        return {
            "job_id": self.job_id,
            "role": role,
            "pod": self.script["lane_id"],
            "payload": {
                "job_id": self.job_id,
                "lane_id": self.script["lane_id"],
                "required_result_contract": "qc_analyzer_report",
            },
            "upstream_results": upstream,
        }


class FactsAnalyzerTests(AnalyzerFixture):
    def test_recomputes_complete_coverage_and_persists_evidence(self) -> None:
        result = analyze_facts(self.task("facts_analyzer"))
        report = result["artifact"]
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["metrics"]["script_segments_verified"], 3)
        self.assertEqual(report["metrics"]["used_claims_verified"], 1)
        path = Path(result["evidence"]["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(result["evidence"]["sha256"], _file_sha256(path))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)

    def test_non_green_or_unapproved_used_claim_fails_closed(self) -> None:
        for field, value, message in (
            ("risk", "yellow", "green-risk"),
            ("support", "disputed", "adequately supported"),
            ("script_usage", "qualify", "explicitly allowed"),
        ):
            with self.subTest(field=field):
                task = self.task("facts_analyzer")
                task["upstream_results"][0]["result"]["artifact"]["claims"][0][field] = value
                with self.assertRaisesRegex(ValidationError, message):
                    analyze_facts(task)

    def test_tampered_master_fails_closed(self) -> None:
        task = self.task("facts_analyzer")
        self.master.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValidationError, "checksum"):
            analyze_facts(task)


class PolicyAnalyzerTests(AnalyzerFixture):
    def test_recomputes_policy_and_safety_coverage(self) -> None:
        result = analyze_policy(self.task("policy_analyzer"))
        report = result["artifact"]
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["metrics"]["safety_gate_required"])
        self.assertEqual(report["metrics"]["safety_sources_verified"], 2)
        self.assertIn("safety_gate_report_sha256", report["bindings"])
        self.assertEqual(
            result["evidence"]["sha256"], _file_sha256(Path(result["evidence"]["path"]))
        )

    def test_required_safety_gate_may_not_be_missing_or_warn(self) -> None:
        with self.assertRaisesRegex(ValidationError, "exactly one upstream"):
            analyze_policy(self.task("policy_analyzer", include_safety=False))

        warned = self.task("policy_analyzer")
        warned_safety = warned["upstream_results"][-1]["result"]["artifact"]
        warned_safety["findings"] = [
            {"code": "medical_warning", "severity": "warning", "message": "review"}
        ]
        with self.assertRaisesRegex(ValidationError, "clean hard gate"):
            analyze_policy(warned)

    def test_incomplete_safety_source_coverage_fails_closed(self) -> None:
        task = self.task("policy_analyzer")
        task["upstream_results"][-1]["result"]["artifact"]["source_ids_checked"] = [
            "source_001"
        ]
        with self.assertRaisesRegex(ValidationError, "coverage is incomplete"):
            analyze_policy(task)


if __name__ == "__main__":
    unittest.main()
