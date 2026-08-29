from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_ROOT / "src"))

from video_factory.errors import (  # noqa: E402
    IdeaConflictError,
    IdempotencyConflictError,
    StateTransitionError,
    ValidationError,
)
from video_factory.service import Factory  # noqa: E402
from video_factory.db import SCHEMA_VERSION  # noqa: E402


RIGHTS_PASS = {
    "items": [
        {
            "asset": "clip-01.mp4",
            "basis": "licensed",
            "reference": "license-123",
        }
    ]
}
QC_PASS = {
    "checks": {
        "duration": True,
        "aspect_ratio": True,
        "captions": True,
        "audio": True,
        "rights": True,
    }
}


class FactoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.factory = Factory(self.root / "factory.sqlite3")
        self.ideas_path = self.root / "ideas.json"
        self.ideas = [
            {"id": "idea-a", "title": "Idea A", "topic": "science"},
            {"id": "idea-b", "title": "Idea B", "topic": "nature"},
            {"title": "Idea without supplied ID", "summary": "Stored verbatim"},
        ]
        self.write_ideas(self.ideas)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_ideas(self, ideas: list[dict]) -> None:
        self.ideas_path.write_text(
            json.dumps({"ideas": ideas}, ensure_ascii=False), encoding="utf-8"
        )

    def first_job(self) -> str:
        return self.factory.start(self.ideas_path, batch_size=1)["jobs"][0]["id"]

    def test_init_is_idempotent(self) -> None:
        first = self.factory.init()
        second = self.factory.init()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["schema_version"], SCHEMA_VERSION)

    def test_start_imports_only_file_data_and_replays(self) -> None:
        first = self.factory.start(self.ideas_path, batch_size=2)
        second = self.factory.start(self.ideas_path, batch_size=2)
        self.assertEqual(first, second)
        self.assertEqual(first["imported_ideas"], 3)
        self.assertEqual(first["batch_size"], 2)

        status = self.factory.status()
        self.assertEqual(status["ideas"], {"candidate": 1, "in_review": 2})
        self.assertEqual(status["jobs"], {"review_pending": 2})

        next_batch = self.factory.start(
            self.ideas_path, batch_size=2, idempotency_key="batch-2"
        )
        self.assertEqual(next_batch["batch_size"], 1)
        self.assertEqual(self.factory.list(entity="jobs")["count"], 3)

        stored = self.factory.status("idea-a")["idea"]
        self.assertEqual(stored["payload"], self.ideas[0])

    def test_conflicting_idea_id_is_rejected(self) -> None:
        self.factory.start(self.ideas_path, batch_size=1)
        changed = list(self.ideas)
        changed[0] = {"id": "idea-a", "title": "Changed title"}
        self.write_ideas(changed)
        with self.assertRaises(IdeaConflictError):
            self.factory.start(
                self.ideas_path, batch_size=1, idempotency_key="changed-source"
            )

    def test_approve_and_reject_are_state_safe_and_idempotent(self) -> None:
        started = self.factory.start(self.ideas_path, batch_size=2)
        approved_job = started["jobs"][0]["id"]
        rejected_job = started["jobs"][1]["id"]

        approved = self.factory.approve(approved_job)
        self.assertEqual(approved["job"]["state"], "approved")
        self.assertEqual(approved, self.factory.approve(approved_job))

        rejected = self.factory.reject(rejected_job, reason="Not on brand")
        self.assertEqual(rejected["job"]["state"], "rejected")
        self.assertEqual(rejected["job"]["rejection_reason"], "Not on brand")
        self.assertEqual(
            rejected, self.factory.reject(rejected_job, reason="Not on brand")
        )
        with self.assertRaises(StateTransitionError):
            self.factory.approve(rejected_job, idempotency_key="approve-rejected")

    def test_rights_and_qc_gates(self) -> None:
        job_id = self.first_job()
        self.factory.approve(job_id)

        opened = self.factory.next(job_id, idempotency_key="open-rights")
        self.assertEqual(opened["to_state"], "rights_pending")
        self.assertEqual(opened, self.factory.next(job_id, idempotency_key="open-rights"))

        with self.assertRaises(ValidationError):
            self.factory.next(
                job_id,
                idempotency_key="bad-rights",
                gate_result="pass",
                evidence={"items": []},
            )

        rights = self.factory.next(
            job_id,
            idempotency_key="rights-pass",
            gate_result="pass",
            evidence=RIGHTS_PASS,
        )
        self.assertEqual(rights["to_state"], "production_pending")
        self.assertEqual(rights["job"]["rights_status"], "passed")

        qc_open = self.factory.next(job_id, idempotency_key="open-qc")
        self.assertEqual(qc_open["to_state"], "qc_pending")
        qc_failed = self.factory.next(
            job_id,
            idempotency_key="qc-fail",
            gate_result="fail",
            evidence={"reason": "Captions overlap UI"},
        )
        self.assertEqual(qc_failed["to_state"], "qc_failed")
        reopened = self.factory.next(job_id, idempotency_key="qc-reopen")
        self.assertEqual(reopened["to_state"], "qc_pending")

        ready = self.factory.next(
            job_id,
            idempotency_key="qc-pass",
            gate_result="pass",
            evidence=QC_PASS,
        )
        self.assertEqual(ready["to_state"], "ready")
        self.assertEqual(self.factory.status(job_id)["idea"]["status"], "ready")
        with self.assertRaises(StateTransitionError):
            self.factory.next(job_id, idempotency_key="past-ready")

    def test_rights_failure_can_be_remediated(self) -> None:
        job_id = self.first_job()
        self.factory.approve(job_id)
        self.factory.next(job_id, idempotency_key="rights-open")
        failed = self.factory.next(
            job_id,
            idempotency_key="rights-fail",
            gate_result="fail",
            evidence={"reason": "Missing license receipt"},
        )
        self.assertEqual(failed["to_state"], "rights_failed")
        reopened = self.factory.next(job_id, idempotency_key="rights-reopen")
        self.assertEqual(reopened["to_state"], "rights_pending")
        self.assertEqual(reopened["job"]["rights_status"], "pending")

    def test_idempotency_key_cannot_be_reused_for_different_request(self) -> None:
        started = self.factory.start(self.ideas_path, batch_size=2)
        first_job, second_job = (item["id"] for item in started["jobs"])
        self.factory.approve(first_job, idempotency_key="same-key")
        with self.assertRaises(IdempotencyConflictError):
            self.factory.approve(second_job, idempotency_key="same-key")

    def test_json_export_is_atomic_and_round_trips(self) -> None:
        result = self.factory.start(self.ideas_path, batch_size=1)
        destination = self.root / "exports" / "batch.json"
        exported = Factory.export_json(result, destination)
        self.assertEqual(exported, destination.resolve())
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), result)


if __name__ == "__main__":
    unittest.main()
