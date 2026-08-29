from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_factory.artifact_store import ArtifactStore
from video_factory.daily import launch_approved, prepare_day
from video_factory.errors import IdempotencyConflictError, ValidationError
from video_factory.queue import Dispatcher
from video_factory.scout import run_scout
from video_factory.service import Factory


class DailyPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "factory.sqlite3"
        self.outputs = self.root / "days"
        self.artifacts = self.root / "artifacts"
        self.cache = self.root / "cache"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_offline_prepare_day_opens_review_jobs_and_versions_artifacts(self) -> None:
        result = prepare_day(
            db_path=self.db,
            output_root=self.outputs,
            artifact_root=self.artifacts,
            cache_dir=self.cache,
            target=10,
            production_date="2026-08-27",
            offline=True,
        )
        self.assertTrue(result["human_topic_gate_required"])
        self.assertGreaterEqual(result["accepted_for_review"], 1)
        self.assertEqual(len(result["jobs"]), result["accepted_for_review"])
        self.assertTrue(all(item["state"] == "review_pending" for item in result["jobs"]))
        self.assertTrue(
            all(
                Factory(self.db).status(item["idea_id"])["idea"]["source_digest"]
                == result["source_digest"]
                for item in result["jobs"]
            )
        )
        records = ArtifactStore(self.artifacts).list()
        self.assertEqual(len(records), result["accepted_for_review"] * 2)
        self.assertEqual({item["kind"] for item in records}, {"idea_card", "claim_ledger"})

    def test_prepare_day_replays_without_duplicate_jobs_or_artifacts(self) -> None:
        first = prepare_day(
            db_path=self.db,
            output_root=self.outputs,
            artifact_root=self.artifacts,
            cache_dir=self.cache,
            target=10,
            production_date="2026-08-27",
            offline=True,
        )
        replay = prepare_day(
            db_path=self.db,
            output_root=self.outputs,
            artifact_root=self.artifacts,
            cache_dir=self.cache,
            target=10,
            production_date="2026-08-27",
            offline=True,
        )
        self.assertEqual(first["batch_id"], replay["batch_id"])
        self.assertEqual(
            len(ArtifactStore(self.artifacts).list()), first["accepted_for_review"] * 2
        )

    def test_force_refresh_uses_source_digest_and_adds_only_new_candidates(self) -> None:
        discovery = run_scout(
            production_date="2026-08-27",
            limit=12,
            cache_dir=self.cache,
            offline=True,
        )
        first_discovery = {
            **discovery,
            "ideas": [discovery["ideas"][0]],
            "claim_ledgers": [discovery["claim_ledgers"][0]],
        }
        second_discovery = {
            **discovery,
            "ideas": [discovery["ideas"][0], discovery["ideas"][-1]],
            "claim_ledgers": [
                discovery["claim_ledgers"][0],
                discovery["claim_ledgers"][-1],
            ],
        }
        first = prepare_day(
            db_path=self.db,
            output_root=self.outputs,
            artifact_root=self.artifacts,
            cache_dir=self.cache,
            target=10,
            production_date="2026-08-27",
            scout_result=first_discovery,
        )
        refreshed = prepare_day(
            db_path=self.db,
            output_root=self.outputs,
            artifact_root=self.artifacts,
            cache_dir=self.cache,
            target=10,
            production_date="2026-08-27",
            scout_result=second_discovery,
            force_refresh=True,
        )
        self.assertTrue(refreshed["force_refreshed"])
        self.assertNotEqual(first["source_digest"], refreshed["source_digest"])
        self.assertNotEqual(
            first["paths"]["candidate_file"], refreshed["paths"]["candidate_file"]
        )
        self.assertNotEqual(first["batch_id"], refreshed["batch_id"])
        self.assertEqual(refreshed["blocked_as_duplicates"], 1)
        self.assertEqual(refreshed["accepted_for_review"], 1)
        self.assertEqual(Factory(self.db).list(entity="jobs")["count"], 2)

    def test_zero_candidate_day_is_replayed_without_running_scout_again(self) -> None:
        empty_discovery = {
            "mode": "offline",
            "warnings": ["empty fixture"],
            "ideas": [],
            "claim_ledgers": [],
        }
        with patch("video_factory.daily.run_scout", return_value=empty_discovery) as mocked:
            first = prepare_day(
                db_path=self.db,
                output_root=self.outputs,
                artifact_root=self.artifacts,
                cache_dir=self.cache,
                target=10,
                production_date="2026-08-27",
            )
            replay = prepare_day(
                db_path=self.db,
                output_root=self.outputs,
                artifact_root=self.artifacts,
                cache_dir=self.cache,
                target=10,
                production_date="2026-08-27",
            )
        self.assertIsNone(first["batch_id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(mocked.call_count, 1)

    def test_launch_approved_creates_fenced_dependency_chain(self) -> None:
        prepared = prepare_day(
            db_path=self.db,
            output_root=self.outputs,
            artifact_root=self.artifacts,
            cache_dir=self.cache,
            target=10,
            production_date="2026-08-27",
            offline=True,
        )
        job = prepared["jobs"][0]
        Factory(self.db).approve(job["id"])
        launched = launch_approved(db_path=self.db, batch_id=prepared["batch_id"])
        self.assertEqual(launched["chains_created"], 1)
        launched_chain = launched["chains"][0]
        chain = launched_chain["tasks"]
        roles = [task["role"] for task in chain]
        self.assertEqual(len(chain), len(launched_chain["roles"]))
        script_index = roles.index("script")
        self.assertEqual(roles[script_index : script_index + 3], ["script", "voice", "editor"])
        self.assertIsNone(chain[0]["dependency_task_id"])
        for parent, child in zip(chain, chain[1:]):
            self.assertEqual(child["dependency_task_id"], parent["id"])
        self.assertTrue(chain[-2]["payload"]["human_gate"])
        self.assertTrue(chain[-1]["payload"]["publish_requires_final_review"])

        replay = launch_approved(db_path=self.db, batch_id=prepared["batch_id"])
        self.assertEqual(
            [item["id"] for item in replay["chains"][0]["tasks"]],
            [item["id"] for item in chain],
        )

    def test_launch_rejects_changed_role_chain_without_creating_a_branch(self) -> None:
        prepared = prepare_day(
            db_path=self.db,
            output_root=self.outputs,
            artifact_root=self.artifacts,
            cache_dir=self.cache,
            target=10,
            production_date="2026-08-27",
            offline=True,
        )
        Factory(self.db).approve(prepared["jobs"][0]["id"])
        original = launch_approved(db_path=self.db, batch_id=prepared["batch_id"])
        changed_roles = list(original["roles"])
        changed_roles[1] = "alternate_rights"
        with self.assertRaises(IdempotencyConflictError):
            launch_approved(
                db_path=self.db,
                batch_id=prepared["batch_id"],
                roles=changed_roles,
            )
        queue_status = Dispatcher(self.db).status()
        self.assertEqual(sum(queue_status["tasks"].values()), len(original["roles"]))

    def test_lane_registry_inserts_medical_gate_into_health_chain(self) -> None:
        ideas_file = self.root / "health-ideas.json"
        ideas_file.write_text(
            '[{"id":"health_test_001","title":"Health test",'
            '"topic":"health","summary":"A sourced health explainer"}]',
            encoding="utf-8",
        )
        started = Factory(self.db).start(ideas_file, batch_size=1)
        job = started["jobs"][0]
        Factory(self.db).approve(job["id"])
        launched = launch_approved(db_path=self.db, batch_id=started["batch_id"])
        self.assertEqual(launched["roles_mode"], "lane_registry")
        self.assertIn("medical_review", launched["roles"])
        chain = launched["chains"][0]
        self.assertEqual(chain["risk_profile"], "medical_safety")
        medical_task = next(
            item for item in chain["tasks"] if item["role"] == "medical_review"
        )
        self.assertTrue(medical_task["payload"]["structured_gate_required"])

    def test_all_five_lanes_launch_their_independent_specialist_chains(self) -> None:
        ideas_file = self.root / "five-lanes.json"
        ideas_file.write_text(
            """{
              "ideas": [
                {"id":"war_test_001","title":"War history", "topic":"war_history"},
                {"id":"celeb_test_001","title":"Celebrity news", "topic":"celebrity_news"},
                {"id":"motivation_test_001","title":"Motivation", "topic":"motivation"},
                {"id":"chinese_med_test_001","title":"Chinese medicine", "topic":"chinese_medicine"},
                {"id":"health_test_002","title":"Health", "topic":"health"}
              ]
            }""",
            encoding="utf-8",
        )
        started = Factory(self.db).start(ideas_file, batch_size=5)
        for job in started["jobs"]:
            Factory(self.db).approve(job["id"])
        launched = launch_approved(db_path=self.db, batch_id=started["batch_id"])
        self.assertEqual(launched["chains_created"], 5)
        self.assertEqual(launched["tasks_created_or_replayed"], 49)
        self.assertIsNone(launched["roles"])
        self.assertIn("sensitivity_review", launched["roles_by_lane"]["war_history"])
        self.assertIn("privacy_review", launched["roles_by_lane"]["celebrity_news"])
        self.assertNotIn("medical_review", launched["roles_by_lane"]["motivation"])
        motivation_roles = launched["roles_by_lane"]["motivation"]
        self.assertIn("source_audio", motivation_roles)
        self.assertNotIn("voice", motivation_roles)
        motivation_chain = next(
            chain for chain in launched["chains"] if chain["lane_id"] == "motivation"
        )
        source_audio_task = next(
            task for task in motivation_chain["tasks"] if task["role"] == "source_audio"
        )
        self.assertEqual(
            source_audio_task["payload"]["required_result_contract"],
            "source_audio_manifest",
        )
        self.assertIn("medical_review", launched["roles_by_lane"]["chinese_medicine"])
        self.assertIn("medical_review", launched["roles_by_lane"]["health"])
        for lane_id in (
            "war_history",
            "celebrity_news",
            "chinese_medicine",
            "health",
        ):
            self.assertIn("voice", launched["roles_by_lane"][lane_id])
            self.assertNotIn("source_audio", launched["roles_by_lane"][lane_id])
        for chain in launched["chains"]:
            self.assertEqual(chain["roles"][-2:], ["final_review", "publisher"])
            self.assertTrue(chain["tasks"][-1]["payload"]["publish_requires_final_review"])

    def test_invalid_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "10 to 15"):
            prepare_day(
                db_path=self.db,
                output_root=self.outputs,
                artifact_root=self.artifacts,
                cache_dir=self.cache,
                target=9,
                offline=True,
            )


if __name__ == "__main__":
    unittest.main()
