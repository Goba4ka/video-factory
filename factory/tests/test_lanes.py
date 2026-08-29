from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from video_factory.analytics import PRODUCTION_STAGES
from video_factory.errors import ValidationError
from video_factory.lanes import (
    enabled_lane_ids,
    load_lane_registry,
    roles_for_lane,
    validate_lane_packages,
)


class LaneRegistryTests(unittest.TestCase):
    def test_canonical_registry_has_five_enabled_lanes_and_10_to_15_capacity(self) -> None:
        registry = load_lane_registry()
        self.assertEqual(
            enabled_lane_ids(registry),
            (
                "war_history",
                "celebrity_news",
                "motivation",
                "chinese_medicine",
                "health",
            ),
        )
        report = validate_lane_packages()
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["enabled_lanes"], 5)
        self.assertGreaterEqual(report["total_candidates"], 100)
        self.assertEqual(report["daily_allocation"], {"min": 10, "target": 15, "max": 15})

    def test_medical_lanes_use_the_medical_review_role(self) -> None:
        self.assertIn("medical_review", roles_for_lane("health"))
        self.assertIn("medical_review", roles_for_lane("chinese_medicine"))
        self.assertNotIn("medical_review", roles_for_lane("motivation"))

    def test_only_motivation_uses_source_audio_instead_of_voice(self) -> None:
        motivation_roles = roles_for_lane("motivation")
        self.assertIn("source_audio", motivation_roles)
        self.assertNotIn("voice", motivation_roles)
        for lane_id in (
            "war_history",
            "celebrity_news",
            "chinese_medicine",
            "health",
        ):
            lane_roles = roles_for_lane(lane_id)
            self.assertIn("voice", lane_roles, lane_id)
            self.assertNotIn("source_audio", lane_roles, lane_id)

    def test_every_lane_discovers_before_rights_and_freeze(self) -> None:
        for lane_id in (
            "war_history",
            "celebrity_news",
            "motivation",
            "chinese_medicine",
            "health",
        ):
            roles = roles_for_lane(lane_id)
            self.assertLess(roles.index("media_discovery"), roles.index("rights"))
            self.assertLess(roles.index("rights"), roles.index("media"))

    def test_every_lane_builds_licensed_bgm_and_program_mix_before_compiler(self) -> None:
        for lane_id in (
            "war_history",
            "celebrity_news",
            "motivation",
            "chinese_medicine",
            "health",
        ):
            roles = roles_for_lane(lane_id)
            self.assertLess(roles.index("editor"), roles.index("bgm"), lane_id)
            self.assertLess(roles.index("bgm"), roles.index("audio_mix"), lane_id)
            self.assertLess(roles.index("audio_mix"), roles.index("compiler"), lane_id)

    def test_analytics_covers_every_registered_lane_role(self) -> None:
        registry = load_lane_registry()
        registered_roles = {
            role
            for lane in registry["lanes"]
            if lane["enabled"]
            for role in roles_for_lane(lane["id"])
        }
        self.assertEqual(registered_roles - PRODUCTION_STAGES, set())

    def test_registry_rejects_allocation_drift(self) -> None:
        registry = copy.deepcopy(load_lane_registry())
        registry["lanes"][0]["daily"]["target"] = 2
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "registry.json"
            path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "do not match"):
                load_lane_registry(path)


if __name__ == "__main__":
    unittest.main()
