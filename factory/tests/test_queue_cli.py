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


class QueueCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "factory.sqlite3"
        self.payload = self.root / "payload.json"
        self.payload.write_text('{"shot": 3}', encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *args: str) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        code = main(list(args), out=out, err=err)
        return code, json.loads(out.getvalue() if code == 0 else err.getvalue())

    def test_enqueue_claim_complete_fail_status_and_simulation_commands(self) -> None:
        code, enqueued = self.run_cli(
            "enqueue",
            "--role", "editor",
            "--pod", "space_technology",
            "--kind", "edit",
            "--payload", str(self.payload),
            "--idempotency-key", "cli-enqueue-1",
            "--db", str(self.db),
        )
        self.assertEqual(code, 0)
        task_id = enqueued["task"]["id"]
        code, claimed = self.run_cli(
            "claim",
            "--worker", "cli-worker",
            "--role", "editor",
            "--idempotency-key", "cli-claim-1",
            "--db", str(self.db),
        )
        self.assertEqual(code, 0)
        token = claimed["task"]["lease_token"]
        code, completed = self.run_cli(
            "complete", task_id,
            "--lease-token", token,
            "--idempotency-key", "cli-complete-1",
            "--db", str(self.db),
        )
        self.assertEqual(code, 0)
        self.assertEqual(completed["task"]["status"], "succeeded")

        _, second = self.run_cli(
            "enqueue",
            "--role", "editor",
            "--pod", "space_technology",
            "--kind", "edit",
            "--idempotency-key", "cli-enqueue-2",
            "--db", str(self.db),
        )
        _, second_claim = self.run_cli(
            "claim",
            "--worker", "cli-worker",
            "--role", "editor",
            "--idempotency-key", "cli-claim-2",
            "--db", str(self.db),
        )
        code, failed = self.run_cli(
            "fail", second["task"]["id"],
            "--lease-token", second_claim["task"]["lease_token"],
            "--reason", "transient",
            "--idempotency-key", "cli-fail-2",
            "--db", str(self.db),
        )
        self.assertEqual(code, 0)
        self.assertTrue(failed["retried"])

        code, status = self.run_cli("queue-status", "--db", str(self.db))
        self.assertEqual(code, 0)
        self.assertEqual(status["tasks"], {"queued": 1, "succeeded": 1})
        code, status_alias = self.run_cli("status", "--queue", "--db", str(self.db))
        self.assertEqual(code, 0)
        self.assertEqual(status_alias["tasks"], status["tasks"])

        code, simulation = self.run_cli(
            "simulate-day",
            "--target", "10",
            "--pods", "space_technology,nature_animals,people_culture",
            "--roles", "qc",
            "--idempotency-key", "cli-day",
            "--db", str(self.db),
        )
        self.assertEqual(code, 0)
        self.assertEqual(simulation["completed_videos"], 10)
        self.assertTrue(simulation["simulation_only"])
        self.assertFalse(simulation["production_ready"])
        self.assertEqual(simulation["capacity_claim"], "queue_wip_mechanics_only")
        self.assertEqual(simulation["real_provider_calls"], 0)
        self.assertFalse(simulation["real_media_artifacts_created"])
        self.assertEqual(simulation["real_renders_created"], 0)
        self.assertEqual(simulation["real_publications"], 0)
        self.assertEqual(simulation["human_gate_roles_simulated"], [])
        self.assertFalse(simulation["background_processes_started"])


if __name__ == "__main__":
    unittest.main()
