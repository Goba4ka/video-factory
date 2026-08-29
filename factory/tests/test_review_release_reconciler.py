from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from video_factory.errors import ValidationError
from video_factory.review_release_reconciler import materialize_pending


class _FakeBridge:
    def __init__(self, calls: list[str], failing: set[str] | None = None):
        self.calls = calls
        self.failing = failing or set()

    def materialize(self, task_id: str) -> dict:
        self.calls.append(task_id)
        if task_id in self.failing:
            raise ValidationError("fixture rejection")
        return {
            "bundle_id": f"bundle-{task_id}",
            "event_id": f"event-{task_id}",
            "created": True,
            "status": "pending_human_review",
        }


class ReviewReleaseReconcilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.db = self.root / "factory.sqlite3"
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    dependency_task_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            rows = [
                ("qc-ready", "qc", "qc_job", "succeeded", "{}", None, "2026-08-29T08:00:00Z"),
                ("final-ready", "final_review", "final_review_job", "queued", None, "qc-ready", "2026-08-29T08:01:00Z"),
                ("qc-wait", "qc", "qc_job", "queued", None, None, "2026-08-29T08:02:00Z"),
                ("final-wait", "final_review", "final_review_job", "queued", None, "qc-wait", "2026-08-29T08:03:00Z"),
                ("render-ready", "render", "render_job", "succeeded", "{}", None, "2026-08-29T08:04:00Z"),
                ("final-wrong", "final_review", "final_review_job", "queued", None, "render-ready", "2026-08-29T08:05:00Z"),
            ]
            connection.executemany(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )
            connection.commit()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_materializes_only_final_review_with_successful_qc_dependency(self) -> None:
        calls: list[str] = []
        result = materialize_pending(
            self.db,
            self.root / "outbox",
            bridge_factory=lambda db, root: _FakeBridge(calls),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["final-ready"])
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["materialized_count"], 1)
        self.assertFalse(result["automatic_approval"])
        self.assertFalse(result["publish_outbox_created"])

    def test_reports_failures_without_approving_or_sending(self) -> None:
        calls: list[str] = []
        result = materialize_pending(
            self.db,
            self.root / "outbox",
            bridge_factory=lambda db, root: _FakeBridge(calls, {"final-ready"}),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["materialized_count"], 0)
        self.assertFalse(result["external_send_performed"])

    def test_limit_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValidationError, "1 to 1000"):
            materialize_pending(self.db, self.root / "outbox", limit=0)


if __name__ == "__main__":
    unittest.main()
