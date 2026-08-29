from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path

from video_factory.errors import ValidationError
from video_factory.voice_profile_gate import load_approved_voice_profile


def fixture(root: Path) -> tuple[Path, Path, dict, dict]:
    golden_dir = root / "golden_samples"
    golden_dir.mkdir(parents=True)
    golden = golden_dir / "approved.wav"
    with wave.open(str(golden), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(44100)
        writer.writeframes(b"\x00\x00" * 2205)
    payload = golden.read_bytes()
    profile = {
        "schema_version": "1.0.0",
        "profile_id": "fish-approved-russian-001",
        "provider": "fish_audio",
        "reference_id": "reference-russian-001",
        "state": "approved",
        "languages": ["ru"],
        "eligible_lanes": ["celebrity_news", "health"],
        "rights_status": "approved_licensed_voice",
        "rights_review": {
            "decision": "approved",
            "basis": "commercial_license",
            "evidence": "Commercial license receipt voice-001 is archived.",
            "reviewed_by": "rights_reviewer",
            "reviewed_at": "2026-08-30T08:00:00+03:00",
        },
        "golden_sample": {
            "path": "golden_samples/approved.wav",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "immutable": True,
        },
        "quality_review": {
            "decision": "approved",
            "reviewed_by": "creative_reviewer",
            "reviewed_at": "2026-08-30T08:15:00+03:00",
            "timbre_approved": True,
            "diction_approved": True,
            "pacing_approved": True,
            "emotional_range_approved": True,
            "russian_pronunciation_approved": True,
            "note": "Golden sample passed every Russian narration rubric field.",
        },
    }
    catalog = {
        "schema_version": "1.0.0",
        "selection_policy": {
            "automatic_fallback_allowed": False,
            "eligible_state": "approved",
            "golden_sample_sha256_required": True,
            "human_quality_approval_required": True,
            "job_bound_rights_approval_required": True,
            "maximum_fish_generations_per_video": 2,
        },
        "profiles": [profile],
    }
    catalog_path = root / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    return catalog_path, golden, profile, catalog


class VoiceProfileGateTests(unittest.TestCase):
    def select(self, root: Path, catalog: Path):
        return load_approved_voice_profile(
            reference_id="reference-russian-001",
            lane_id="celebrity_news",
            language="ru",
            catalog_path=catalog,
            profile_root=root,
        )

    def test_exact_approved_profile_binds_catalog_profile_and_golden_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path, golden, profile, _ = fixture(root)
            selected = self.select(root, catalog_path)
            raw_catalog = catalog_path.read_bytes()

        self.assertEqual(selected.approval, profile)
        self.assertEqual(selected.binding["reference_id"], profile["reference_id"])
        self.assertEqual(selected.binding["lane_id"], "celebrity_news")
        self.assertEqual(selected.binding["language"], "ru")
        self.assertEqual(
            selected.binding["catalog_sha256"],
            hashlib.sha256(raw_catalog).hexdigest(),
        )
        self.assertEqual(
            selected.binding["golden_sample_sha256"],
            profile["golden_sample"]["sha256"],
        )
        self.assertEqual(selected.binding["golden_sample_path"], str(golden.resolve()))
        self.assertRegex(
            selected.binding["profile_approval_sha256"], r"^[a-f0-9]{64}$"
        )

    def test_mismatched_golden_hash_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path, _, _, catalog = fixture(root)
            catalog["profiles"][0]["golden_sample"]["sha256"] = "0" * 64
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "SHA-256"):
                self.select(root, catalog_path)

    def test_missing_golden_wav_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path, golden, _, _ = fixture(root)
            golden.unlink()
            with self.assertRaisesRegex(ValidationError, "WAV is missing"):
                self.select(root, catalog_path)

    def test_lane_and_language_must_be_individually_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path, _, _, _ = fixture(root)
            with self.assertRaisesRegex(ValidationError, "lane 'war_history'"):
                load_approved_voice_profile(
                    reference_id="reference-russian-001",
                    lane_id="war_history",
                    language="ru",
                    catalog_path=catalog_path,
                    profile_root=root,
                )
            with self.assertRaisesRegex(ValidationError, "language 'en'"):
                load_approved_voice_profile(
                    reference_id="reference-russian-001",
                    lane_id="celebrity_news",
                    language="en",
                    catalog_path=catalog_path,
                    profile_root=root,
                )

    def test_quality_rubric_and_timezone_aware_human_review_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path, _, _, catalog = fixture(root)
            catalog["profiles"][0]["quality_review"]["diction_approved"] = False
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "diction_approved"):
                self.select(root, catalog_path)

            catalog["profiles"][0]["quality_review"]["diction_approved"] = True
            catalog["profiles"][0]["quality_review"]["reviewed_at"] = (
                "2026-08-30T08:15:00"
            )
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "include a timezone"):
                self.select(root, catalog_path)

    def test_rights_basis_must_match_approved_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path, _, _, catalog = fixture(root)
            catalog["profiles"][0]["rights_review"]["basis"] = (
                "voice_owner_confirmation"
            )
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "basis contradicts"):
                self.select(root, catalog_path)

    def test_duplicate_or_missing_exact_reference_never_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path, _, profile, catalog = fixture(root)
            catalog["profiles"].append(dict(profile))
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate exact"):
                self.select(root, catalog_path)

            catalog["profiles"] = [profile]
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "no voice profile exists"):
                load_approved_voice_profile(
                    reference_id="some-other-reference",
                    lane_id="celebrity_news",
                    language="ru",
                    catalog_path=catalog_path,
                    profile_root=root,
                )

    def test_rejected_exact_reference_is_not_replaced_by_approved_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path, _, profile, catalog = fixture(root)
            rejected = {
                "profile_id": "fish-rejected-russian-001",
                "provider": "fish_audio",
                "reference_id": "rejected-reference-001",
                "state": "rejected",
            }
            catalog["profiles"].append(rejected)
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "rejected.*automatic fallback is forbidden"
            ):
                load_approved_voice_profile(
                    reference_id="rejected-reference-001",
                    lane_id="celebrity_news",
                    language="ru",
                    catalog_path=catalog_path,
                    profile_root=root,
                )
            self.assertEqual(profile["state"], "approved")

    def test_contract_schema_copies_are_identical(self) -> None:
        factory_root = Path(__file__).resolve().parents[1]
        public = factory_root / "contracts" / "voice_profile_approval.schema.json"
        packaged = (
            factory_root
            / "src"
            / "video_factory"
            / "schemas"
            / "voice_profile_approval.schema.json"
        )
        self.assertEqual(public.read_bytes(), packaged.read_bytes())


if __name__ == "__main__":
    unittest.main()
