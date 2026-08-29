from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from video_factory.preflight import run_preflight


class PreflightTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.profiles = self.root / "profiles.json"
        self.profiles.write_text(
            json.dumps(
                {
                    "profiles": {
                        "test": {
                            "script_words": {"min": 2, "max": 10},
                            "voice_words_per_minute": {"min": 10, "max": 200},
                            "shot_count": {"min": 2, "max": 3},
                            "median_shot_seconds": {"min": 1, "max": 10},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.project / "idea_card.json").write_text(
            json.dumps(
                {
                    "production_authorized": False,
                    "format": {"quality_profile": "test", "target_duration_seconds": 4},
                }
            ),
            encoding="utf-8",
        )
        (self.project / "claim_ledger.json").write_text(
            json.dumps({"claims": [{"id": "C01"}, {"id": "I01"}]}),
            encoding="utf-8",
        )
        (self.project / "candidate_assets.json").write_text(
            json.dumps(
                {
                    "assets": [
                        {
                            "id": "A01",
                            "rights_status": "usable_with_nasa_media_guidelines",
                        },
                        {"id": "A02", "rights_status": "needs_file_specific_confirmation"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        (self.project / "SCRIPT_DRAFT.md").write_text(
            "# Draft\n\n## Narration\n\n**[0:00-0:02 · C01]** One true claim.\n\n"
            "**[0:02-0:04 · I01]** One inference.\n\n## Notes\n",
            encoding="utf-8",
        )
        (self.project / "SHOTLIST_DRAFT.md").write_text(
            "| Time | Shot |\n|---|---|\n"
            "| 0:00-0:02 | A01 C01 |\n"
            "| 0:02-0:04 | A02 I01 |\n",
            encoding="utf-8",
        )

    def test_preflight_passes_integrity_but_keeps_render_closed(self) -> None:
        result = run_preflight(self.project, self.profiles)
        self.assertTrue(result["ok"])
        self.assertTrue(result["ready_for_topic_approval"])
        self.assertFalse(result["render_authorized"])
        self.assertEqual(result["metrics"]["shot_count"], 2)
        self.assertEqual(result["evidence"]["rights_review_asset_ids"], ["A02"])

    def test_preflight_reports_unresolved_references(self) -> None:
        path = self.project / "SHOTLIST_DRAFT.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nC99 A99\n", encoding="utf-8")
        result = run_preflight(self.project, self.profiles)
        self.assertFalse(result["ok"])
        self.assertEqual(result["evidence"]["missing_claim_ids"], ["C99"])
        self.assertEqual(result["evidence"]["missing_asset_ids"], ["A99"])


if __name__ == "__main__":
    unittest.main()
