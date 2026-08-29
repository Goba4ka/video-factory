from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_ROOT / "src"))

from video_factory.cli import main  # noqa: E402
from video_factory.db import SCHEMA_VERSION  # noqa: E402


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "factory.sqlite3"
        self.ideas = self.root / "ideas.json"
        self.ideas.write_text(
            json.dumps([{"id": "ru-1", "title": "Русская команда"}], ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(list(arguments), out=stdout, err=stderr)
        stream = stdout.getvalue() if code == 0 else stderr.getvalue()
        return code, json.loads(stream), stderr.getvalue()

    def write_json(self, name: str, value) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_russian_aliases_and_export(self) -> None:
        code, initialized, _ = self.run_cli("init", "--db", str(self.db))
        self.assertEqual(code, 0)
        self.assertTrue(initialized["ok"])

        export_path = self.root / "review-batch.json"
        code, started, _ = self.run_cli(
            "начинаем",
            str(self.ideas),
            "--db",
            str(self.db),
            "--export",
            str(export_path),
        )
        self.assertEqual(code, 0)
        self.assertEqual(started["batch_size"], 1)
        self.assertEqual(json.loads(export_path.read_text(encoding="utf-8")), started)

        job_id = started["jobs"][0]["id"]
        code, approved, _ = self.run_cli("одобрить", job_id, "--db", str(self.db))
        self.assertEqual(code, 0)
        self.assertEqual(approved["job"]["state"], "approved")

    def test_expected_errors_are_json(self) -> None:
        code, error, stderr = self.run_cli(
            "approve", "missing-job", "--db", str(self.db)
        )
        self.assertEqual(code, 2)
        self.assertEqual(error["error"]["code"], "not_found")
        self.assertTrue(stderr)

    def test_runtime_optimizer_creates_a_clean_resource_limited_database(self) -> None:
        runtime_root = self.root / "runtime"
        runtime_db = runtime_root / "factory-v3.sqlite3"
        plan_path = runtime_root / "active-plan.json"
        code, result, stderr = self.run_cli(
            "optimize-runtime",
            "--profile",
            "balanced",
            "--target",
            "10",
            "--runtime-root",
            str(runtime_root),
            "--db",
            str(runtime_db),
            "--legacy-db",
            str(self.root / "missing-legacy.sqlite3"),
            "--plan-output",
            str(plan_path),
            "--apply",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["selected_profile"], "balanced")
        self.assertEqual(result["applied"]["render_limit"], 1)
        self.assertEqual(
            result["applied"]["database_status"]["schema_version"], SCHEMA_VERSION
        )
        self.assertEqual([item["job_count"] for item in result["waves"]], [5, 5])
        self.assertTrue(plan_path.is_file())

        code, cache, stderr = self.run_cli(
            "cache-status", "--cache-root", str(runtime_root / "cache")
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(cache["entries"], 0)

    def test_planning_dedup_quality_and_metrics_commands(self) -> None:
        plan_input = self.write_json(
            "plan.json",
            {
                "target": 1,
                "pod_targets": {"space": 1},
                "pod_capacities": {"space": 1},
                "expected_attrition": {},
                "render_slots": 1,
                "human_review_minutes": 10,
            },
        )
        code, plan, _ = self.run_cli("plan-day", str(plan_input))
        self.assertEqual(code, 0)
        self.assertTrue(plan["feasible"])

        card = {"title": "same", "hook": "same hook", "message": "same message"}
        candidate = self.write_json("candidate.json", card)
        existing = self.write_json("existing.json", {"ideas": [card]})
        code, dedup, _ = self.run_cli(
            "dedup", "--candidate", str(candidate), "--existing", str(existing)
        )
        self.assertEqual(code, 0)
        self.assertEqual(dedup["decision"], "block")

        preflight = self.write_json("preflight.json", {"checks": {"all": True}})
        editorial = self.write_json(
            "editorial.json",
            {
                "visual_relevance": 1,
                "narrative_turn": 1,
                "opening_truthfulness": 1,
                "payoff": 1,
                "factual_review_passed": True,
                "freshness_review_passed": True,
                "rights_manifest_passed": True,
                "caption_review_passed": True,
                "technical_qc_passed": True,
                "visual_provenance_passed": True,
                "human_editor_approved": True,
            },
        )
        originality = self.write_json(
            "originality.json", {"decision": "allow", "similarity": 0}
        )
        code, quality, _ = self.run_cli(
            "quality-score",
            "--preflight",
            str(preflight),
            "--editorial",
            str(editorial),
            "--originality",
            str(originality),
        )
        self.assertEqual(code, 0)
        self.assertTrue(quality["reference_quality"])

        code, freshness, _ = self.run_cli(
            "freshness-gate",
            "--lane",
            "celebrity_news",
            "--checked-at",
            "2026-08-29T08:00:00+03:00",
            "--now",
            "2026-08-29T09:30:00+03:00",
        )
        self.assertEqual(code, 0)
        self.assertEqual(freshness["decision"], "pass")

        snapshot = {
            "engaged_views": 100,
            "stayed_to_watch_rate": 0.8,
            "completion_rate": 0.8,
            "shares": 10,
            "saves": 5,
            "follows": 2,
            "policy_events": [],
        }
        candidate_metrics = self.write_json("metrics.json", snapshot)
        cohort = self.write_json("cohort.json", {"snapshots": [snapshot]})
        code, metrics, _ = self.run_cli(
            "evaluate-performance",
            "--candidate",
            str(candidate_metrics),
            "--cohort",
            str(cohort),
            "--minimum-cohort",
            "1",
        )
        self.assertEqual(code, 0)
        self.assertEqual(metrics["status"], "evaluated")


if __name__ == "__main__":
    unittest.main()
