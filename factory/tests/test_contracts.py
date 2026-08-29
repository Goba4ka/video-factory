from __future__ import annotations

import copy
import hashlib
import unittest

from video_factory.contracts import validate_artifact, validate_production_chain
from video_factory.errors import ValidationError


def valid_chain() -> dict[str, dict]:
    idea_id = "moon_trees_001"
    return {
        "idea_card": {
            "schema_version": "1.0.0",
            "idea_id": idea_id,
            "pod": "space_technology",
            "title": "Лунные деревья существуют",
            "hook": "На Земле растут деревья, побывавшие у Луны.",
            "message": "Семена Apollo 14 стали живыми памятниками космической истории.",
            "why_now": "Вечнозелёная история с сильным визуальным контрастом.",
            "source_candidates": [
                {
                    "source_id": "src_nasa",
                    "url": "https://www.nasa.gov/history/moon-trees-2/",
                    "publisher": "NASA",
                    "source_type": "primary",
                }
            ],
            "visual_plan": {
                "target_shots": 8,
                "rights_feasibility": "green",
                "visual_world": "Архив Apollo и современные деревья",
            },
            "risk": "green",
            "status": "approved",
        },
        "claim_ledger": {
            "schema_version": "1.0.0",
            "idea_id": idea_id,
            "sources": [
                {
                    "source_id": "src_nasa",
                    "url": "https://www.nasa.gov/history/moon-trees-2/",
                    "publisher": "NASA",
                    "retrieved_at": "2026-08-27T10:00:00Z",
                    "primary": True,
                }
            ],
            "claims": [
                {
                    "claim_id": "claim_01",
                    "text": "Семена летали на Apollo 14.",
                    "source_ids": ["src_nasa"],
                    "support": "direct",
                    "risk": "green",
                }
            ],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "review_notes": [],
            },
        },
        "rights_manifest": {
            "schema_version": "1.0.0",
            "idea_id": idea_id,
            "assets": [
                {
                    "asset_id": "asset_01",
                    "landing_url": "https://images.nasa.gov/details-as14-66-9301",
                    "creator": "NASA",
                    "license": "NASA Media Usage Guidelines",
                    "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
                    "retrieved_at": "2026-08-27T10:00:00Z",
                    "commercial_use": True,
                    "modification_allowed": True,
                    "attribution_required": False,
                    "platforms": ["youtube_shorts", "instagram_reels", "tiktok"],
                    "rights_status": "approved",
                }
            ],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "missing_asset_ids": [],
                "review_notes": [],
            },
        },
        "shotlist": {
            "schema_version": "1.0.0",
            "idea_id": idea_id,
            "duration_seconds": 60,
            "aspect": "9:16",
            "shots": [
                {
                    "shot_id": "shot_01",
                    "start": 0,
                    "end": 60,
                    "narration": "Семена летали на Apollo 14.",
                    "visual_intent": "Архивный кадр Apollo",
                    "asset_id": "asset_01",
                    "claim_ids": ["claim_01"],
                }
            ],
        },
    }


def valid_source_audio_manifest() -> dict:
    transcript = "Дисциплина начинается там, где заканчивается настроение."
    return {
        "schema_version": "1.0.0",
        "job_id": "job_motivation_001",
        "lane": "motivation",
        "audio_asset_id": "audio_source_001",
        "source_video_uri_or_path": "C:/media/source-speaker.mp4",
        "source_in_seconds": 12.4,
        "source_out_seconds": 28.9,
        "speaker_name": "Speaker Name",
        "transcript": transcript,
        "rights_status": "commercial_license_confirmed",
        "rights_evidence": "rights/licenses/source-speaker-license.pdf",
        "original_audio_only": True,
        "tts": False,
        "extracted_audio_path": "audio/source-speaker-12.4-28.9.wav",
        "checksums": {
            "source_video_sha256": "a" * 64,
            "extracted_audio_sha256": "b" * 64,
            "transcript_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        },
        "created_at": "2026-08-28T10:00:00Z",
    }


class ContractTests(unittest.TestCase):
    def test_valid_source_audio_manifest_passes(self) -> None:
        manifest = valid_source_audio_manifest()
        self.assertIs(validate_artifact("source_audio_manifest", manifest), manifest)

    def test_source_audio_manifest_rejects_tts_and_invalid_binding(self) -> None:
        manifest = valid_source_audio_manifest()
        manifest["tts"] = True
        with self.assertRaisesRegex(ValidationError, "must equal False"):
            validate_artifact("source_audio_manifest", manifest)

        manifest = valid_source_audio_manifest()
        manifest["source_out_seconds"] = manifest["source_in_seconds"]
        with self.assertRaisesRegex(ValidationError, "greater than source_in_seconds"):
            validate_artifact("source_audio_manifest", manifest)

        manifest = valid_source_audio_manifest()
        manifest["checksums"]["transcript_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValidationError, "does not match transcript"):
            validate_artifact("source_audio_manifest", manifest)

    def test_valid_chain_passes(self) -> None:
        result = validate_production_chain(**valid_chain())
        self.assertTrue(result["production_ready"])
        self.assertEqual(result["counts"]["shots"], 1)

    def test_shotlist_rejects_timeline_and_source_range_defects(self) -> None:
        document = copy.deepcopy(valid_chain()["shotlist"])
        document["shots"] = [
            {
                "shot_id": "shot_01",
                "start": 0,
                "end": 10,
                "narration": "Первый фрагмент.",
                "visual_intent": "Первый архивный кадр",
                "asset_id": "asset_01",
                "source_in": 0,
                "source_out": 10,
                "claim_ids": ["claim_01"],
            },
            {
                "shot_id": "shot_02",
                "start": 11,
                "end": 60,
                "narration": "Второй фрагмент.",
                "visual_intent": "Второй архивный кадр",
                "asset_id": "asset_01",
                "source_in": 0,
                "source_out": 49,
                "claim_ids": ["claim_01"],
            },
        ]
        with self.assertRaisesRegex(ValidationError, "unexplained gap"):
            validate_artifact("shotlist", document)

        document["shots"][1]["start"] = 10
        document["shots"][1]["source_out"] = 20
        with self.assertRaisesRegex(ValidationError, "source range is shorter"):
            validate_artifact("shotlist", document)

    def test_schema_rejects_unknown_fields(self) -> None:
        chain = valid_chain()
        chain["idea_card"]["invented"] = True
        with self.assertRaisesRegex(ValidationError, "not allowed"):
            validate_artifact("idea_card", chain["idea_card"])

    def test_schema_rejects_bad_uri_and_duplicate_platform(self) -> None:
        chain = valid_chain()
        asset = chain["rights_manifest"]["assets"][0]
        asset["license_url"] = "not a uri"
        with self.assertRaisesRegex(ValidationError, "valid URI"):
            validate_artifact("rights_manifest", chain["rights_manifest"])

        chain = valid_chain()
        chain["rights_manifest"]["assets"][0]["platforms"] = [
            "tiktok",
            "tiktok",
        ]
        with self.assertRaisesRegex(ValidationError, "unique"):
            validate_artifact("rights_manifest", chain["rights_manifest"])

    def test_chain_catches_unknown_claim_and_asset(self) -> None:
        chain = valid_chain()
        shot = chain["shotlist"]["shots"][0]
        shot["asset_id"] = "asset_missing"
        shot["claim_ids"] = ["claim_missing"]
        result = validate_production_chain(**chain)
        self.assertFalse(result["production_ready"])
        self.assertEqual(len(result["errors"]), 2)

    def test_chain_is_fail_closed_on_rights_review(self) -> None:
        chain = valid_chain()
        chain["rights_manifest"]["assets"][0]["rights_status"] = "human_review"
        chain["rights_manifest"]["decision"]["passed"] = False
        chain["rights_manifest"]["decision"]["needs_human_review"] = True
        result = validate_production_chain(**chain)
        self.assertFalse(result["production_ready"])
        self.assertTrue(any("hard gate" in error for error in result["errors"]))

    def test_schema_rejects_non_finite_number(self) -> None:
        document = copy.deepcopy(valid_chain()["idea_card"])
        document["score"] = {"relevance": float("nan")}
        with self.assertRaisesRegex(ValidationError, "must be number"):
            validate_artifact("idea_card", document)

    def test_medical_lane_is_fail_closed_without_its_safety_gate(self) -> None:
        chain = valid_chain()
        chain["idea_card"]["pod"] = "health"
        result = validate_production_chain(**chain)
        self.assertFalse(result["production_ready"])
        self.assertTrue(any("medical_safety" in item for item in result["errors"]))

    def test_medical_lane_passes_with_matching_safety_gate(self) -> None:
        chain = valid_chain()
        chain["idea_card"]["pod"] = "health"
        chain["safety_gate_report"] = {
            "schema_version": "1.0.0",
            "job_id": "job_health_001",
            "idea_id": chain["idea_card"]["idea_id"],
            "lane": "health",
            "gate_type": "medical_safety",
            "checked_at": "2026-08-27T10:00:00Z",
            "reviewer": "medical-review-agent",
            "source_ids_checked": ["src_nasa"],
            "findings": [],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "review_notes": [],
            },
        }
        result = validate_production_chain(**chain)
        self.assertTrue(result["production_ready"], result["errors"])


if __name__ == "__main__":
    unittest.main()
