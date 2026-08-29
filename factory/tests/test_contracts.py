from __future__ import annotations

import copy
import hashlib
import unittest

from video_factory.contracts import validate_artifact, validate_production_chain
from video_factory.errors import ValidationError
from video_factory.validators import canonical_json, digest_text


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


def valid_multisource_audio_manifest() -> dict:
    original_en = "Keep moving when motivation is gone."
    translated_ru = "Продолжай двигаться, когда мотивация исчезла."
    review = {
        "approved": True,
        "approved_by": "translator@example.test",
        "approved_at": "2026-08-30T10:00:00Z",
        "asset_id": "source_002",
        "source_in_seconds": 0.5,
        "source_out_seconds": 1.5,
        "original_transcript_sha256": digest_text(original_en),
        "russian_transcript_sha256": digest_text(translated_ru),
        "review_notes": "Точность перевода и контекст проверены.",
    }
    first_transcript = "Начни действовать сейчас."
    segments = [
        {
            "index": 0,
            "asset_id": "source_001",
            "source_video_uri_or_path": "C:/media/source-1.mp4",
            "source_in_seconds": 0,
            "source_out_seconds": 1,
            "program_in_seconds": 0,
            "program_out_seconds": 1,
            "speaker_name": "Спикер один",
            "source_language": "ru",
            "original_transcript": first_transcript,
            "transcript": first_transcript,
            "bilingual_review": None,
            "rights_status": "commercial_license_confirmed",
            "rights_evidence": "rights/source-1.pdf",
            "extracted_audio_path": "C:/audio/source-1.wav",
            "checksums": {
                "source_video_sha256": "1" * 64,
                "extracted_audio_sha256": "2" * 64,
                "original_transcript_sha256": digest_text(first_transcript),
                "transcript_sha256": digest_text(first_transcript),
                "bilingual_review_sha256": None,
            },
        },
        {
            "index": 1,
            "asset_id": "source_002",
            "source_video_uri_or_path": "C:/media/source-2.mp4",
            "source_in_seconds": 0.5,
            "source_out_seconds": 1.5,
            "program_in_seconds": 1,
            "program_out_seconds": 2,
            "speaker_name": "Speaker two",
            "source_language": "en",
            "original_transcript": original_en,
            "transcript": translated_ru,
            "bilingual_review": review,
            "rights_status": "commercial_license_confirmed",
            "rights_evidence": "rights/source-2.pdf",
            "extracted_audio_path": "C:/audio/source-2.wav",
            "checksums": {
                "source_video_sha256": "3" * 64,
                "extracted_audio_sha256": "4" * 64,
                "original_transcript_sha256": digest_text(original_en),
                "transcript_sha256": digest_text(translated_ru),
                "bilingual_review_sha256": digest_text(canonical_json(review)),
            },
        },
    ]
    bindings_sha = digest_text(canonical_json(segments))
    transcript = "\n".join(item["transcript"] for item in segments)
    return {
        "schema_version": "1.1.0",
        "job_id": "job_motivation_001",
        "lane": "motivation",
        "audio_asset_id": f"source-audio-program-{bindings_sha[:24]}",
        "segment_count": 2,
        "segments": segments,
        "transcript": transcript,
        "rights_status": "commercial_license_confirmed",
        "original_audio_only": True,
        "tts": False,
        "extracted_audio_path": "C:/audio/program.wav",
        "checksums": {
            "extracted_audio_sha256": "5" * 64,
            "transcript_sha256": digest_text(transcript),
            "segment_bindings_sha256": bindings_sha,
        },
        "created_at": "2026-08-30T10:00:00Z",
    }


def valid_qc_report() -> dict:
    categories = (
        "technical",
        "audio",
        "captions",
        "facts",
        "rights",
        "dedup",
        "policy",
        "visual",
    )
    return {
        "schema_version": "1.0.0",
        "job_id": "job_qc_001",
        "render_id": "render_qc_001",
        "technical": {"audio_sample_rate_hz": 48000},
        "checks": [
            {
                "check_id": f"{category}-pass",
                "category": category,
                "status": "pass",
                "evidence": f"{category} evidence",
            }
            for category in categories
        ],
        "decision": {
            "passed": True,
            "needs_human_review": False,
            "blocking_check_ids": [],
            "review_notes": [],
        },
    }


def valid_preview_approval() -> dict:
    return {
        "schema_version": "1.0.0",
        "job_id": "job_preview_001",
        "project_id": "project-job_preview_001",
        "approved": True,
        "approved_by": "operator@example.test",
        "approved_at": "2026-08-29T12:00:00Z",
        "project_tree_sha256": "a" * 64,
        "project_manifest_sha256": "b" * 64,
        "check_receipt_path": "C:/receipts/job_preview_001-check.json",
        "check_receipt_sha256": "c" * 64,
        "studio_url": "http://127.0.0.1:3002/#project/job_preview_001",
        "review_notes": ["Timeline and captions reviewed in Studio."],
    }


class ContractTests(unittest.TestCase):
    def test_preview_approval_is_strict_and_http_studio_bound(self) -> None:
        approval = valid_preview_approval()
        self.assertIs(validate_artifact("preview_approval", approval), approval)

        approval = valid_preview_approval()
        approval["approved"] = False
        with self.assertRaises(ValidationError):
            validate_artifact("preview_approval", approval)

        approval = valid_preview_approval()
        approval["studio_url"] = "file:///tmp/index.html"
        with self.assertRaisesRegex(ValidationError, "HTTP"):
            validate_artifact("preview_approval", approval)

        approval = valid_preview_approval()
        approval["unbounded_extra"] = True
        with self.assertRaisesRegex(ValidationError, "not allowed"):
            validate_artifact("preview_approval", approval)

    def test_valid_qc_report_requires_all_eight_passing_categories(self) -> None:
        report = valid_qc_report()
        self.assertIs(validate_artifact("qc_report", report), report)

        report = valid_qc_report()
        report["checks"].pop()
        with self.assertRaisesRegex(ValidationError, "fewer items than minItems"):
            validate_artifact("qc_report", report)

    def test_qc_report_rejects_duplicate_ids_and_categories(self) -> None:
        report = valid_qc_report()
        report["checks"][-1]["check_id"] = report["checks"][0]["check_id"]
        with self.assertRaisesRegex(ValidationError, "duplicate check_id"):
            validate_artifact("qc_report", report)

        report = valid_qc_report()
        report["checks"][-1]["category"] = report["checks"][0]["category"]
        with self.assertRaisesRegex(ValidationError, "duplicate category"):
            validate_artifact("qc_report", report)

    def test_passing_qc_report_rejects_every_nonpass_status(self) -> None:
        for status in ("warn", "fail", "not_run"):
            with self.subTest(status=status):
                report = valid_qc_report()
                report["checks"][-1]["status"] = status
                with self.assertRaisesRegex(ValidationError, "non-pass checks"):
                    validate_artifact("qc_report", report)

    def test_passing_qc_report_rejects_review_and_blocking_flags(self) -> None:
        report = valid_qc_report()
        report["decision"]["needs_human_review"] = True
        with self.assertRaisesRegex(ValidationError, "needs_human_review"):
            validate_artifact("qc_report", report)

        report = valid_qc_report()
        report["decision"]["blocking_check_ids"] = ["technical-pass"]
        with self.assertRaisesRegex(ValidationError, "blocking_check_ids"):
            validate_artifact("qc_report", report)

    def test_failed_qc_report_can_preserve_fail_closed_evidence(self) -> None:
        report = valid_qc_report()
        report["checks"][-1]["status"] = "warn"
        report["decision"] = {
            "passed": False,
            "needs_human_review": True,
            "blocking_check_ids": ["visual-pass"],
            "review_notes": ["Visual review is required."],
        }
        self.assertIs(validate_artifact("qc_report", report), report)

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

    def test_multisource_audio_manifest_binds_order_transcripts_translation_and_no_prejoin(self) -> None:
        manifest = valid_multisource_audio_manifest()
        self.assertIs(validate_artifact("source_audio_manifest", manifest), manifest)

        reordered = copy.deepcopy(manifest)
        reordered["segments"].reverse()
        with self.assertRaises(ValidationError):
            validate_artifact("source_audio_manifest", reordered)

        tampered_translation = copy.deepcopy(manifest)
        tampered_translation["segments"][1]["transcript"] += " Лишнее."
        with self.assertRaisesRegex(ValidationError, "transcript hash|aggregate"):
            validate_artifact("source_audio_manifest", tampered_translation)

        hidden_prejoin = copy.deepcopy(manifest)
        hidden_prejoin["source_video_uri_or_path"] = "C:/media/hidden-prejoin.mp4"
        with self.assertRaises(ValidationError):
            validate_artifact("source_audio_manifest", hidden_prejoin)

        wrong_review = copy.deepcopy(manifest)
        wrong_review["segments"][1]["bilingual_review"]["asset_id"] = "source_001"
        with self.assertRaisesRegex(ValidationError, "not bound"):
            validate_artifact("source_audio_manifest", wrong_review)

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
