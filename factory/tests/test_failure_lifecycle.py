from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from video_factory.cli import main
from video_factory.errors import LeaseConflictError, ValidationError
from video_factory.queue import Dispatcher


T0 = datetime(2026, 8, 29, 8, 0, tzinfo=UTC)


class FailureLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "factory.sqlite3"
        self.queue = Dispatcher(self.db)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def enqueue(self, key: str, **overrides):
        values = {
            "role": "editor",
            "pod": "health",
            "kind": "edit_job",
            "payload": {"revision": 1, "name": key},
            "idempotency_key": f"enqueue-{key}",
            "max_attempts": 1,
            "now": T0,
        }
        values.update(overrides)
        return self.queue.enqueue(**values)["task"]

    def claim(self, key: str, *, role: str = "editor", now=T0):
        return self.queue.claim(
            worker_id=f"worker-{key}",
            role=role,
            idempotency_key=f"claim-{key}",
            lease_seconds=30,
            now=now,
        )["task"]

    def kill(self, task: dict, key: str, *, now=T0 + timedelta(seconds=1)):
        leased = self.claim(key, role=task["role"], now=now - timedelta(seconds=1))
        self.assertEqual(leased["id"], task["id"])
        return self.queue.fail(
            task["id"],
            lease_token=leased["lease_token"],
            error={"code": "provider_outage", "message": "provider unavailable"},
            terminal=True,
            idempotency_key=f"fail-{key}",
            now=now,
        )

    def run_cli(self, *args: str) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        code = main(list(args), out=out, err=err)
        return code, json.loads(out.getvalue() if code == 0 else err.getvalue())

    def test_terminal_failure_creates_dlq_and_controlled_retry_resolves_it(self) -> None:
        task = self.enqueue("root")
        failed = self.kill(task, "root")
        self.assertEqual(failed["task"]["status"], "dead")
        self.assertEqual(failed["dead_letter"]["cause_code"], "provider_outage")

        open_letters = self.queue.dead_letters()
        self.assertEqual(open_letters["count"], 1)
        self.assertEqual(open_letters["items"][0]["task_snapshot"]["status"], "dead")

        retried = self.queue.retry_dead(
            task["id"],
            reason="provider incident is resolved",
            actor="oncall@example.test",
            additional_attempts=2,
            idempotency_key="retry-root",
            now=T0 + timedelta(minutes=1),
        )
        self.assertEqual(retried["task"]["status"], "queued")
        self.assertEqual(retried["task"]["attempt_count"], 1)
        self.assertEqual(retried["task"]["max_attempts"], 3)
        self.assertEqual(self.queue.dead_letters()["count"], 0)
        resolved = self.queue.dead_letters(status="resolved")
        self.assertEqual(resolved["items"][0]["resolution"]["action"], "retry")
        replay = self.queue.retry_dead(
            task["id"],
            reason="provider incident is resolved",
            actor="oncall@example.test",
            additional_attempts=2,
            idempotency_key="retry-root",
            now=T0 + timedelta(days=1),
        )
        self.assertEqual(replay, retried)

    def test_schema_upgrade_backfills_legacy_dead_tasks_once(self) -> None:
        task = self.enqueue("legacy")
        connection = self.queue.db.connect()
        try:
            connection.execute(
                """
                UPDATE tasks SET status = 'dead', last_error_json = ?,
                    completed_at = ?, updated_at = ? WHERE id = ?
                """,
                (
                    '{"code":"legacy_provider_failure"}',
                    "2026-08-29T08:00:01.000Z",
                    "2026-08-29T08:00:01.000Z",
                    task["id"],
                ),
            )
            connection.commit()
        finally:
            connection.close()
        first = self.queue.dead_letters()
        second = self.queue.dead_letters()
        self.assertEqual(first["count"], 1)
        self.assertEqual(second["count"], 1)
        self.assertEqual(first["items"][0]["cause_code"], "legacy_provider_failure")
        self.assertEqual(first["items"][0]["task_snapshot"]["id"], task["id"])

    def test_retry_can_revive_only_dependency_dead_descendants(self) -> None:
        parent = self.enqueue("parent")
        child = self.enqueue(
            "child",
            role="qc",
            kind="qc_job",
            dependency_task_id=parent["id"],
        )
        grandchild = self.enqueue(
            "grandchild",
            role="publisher",
            kind="publisher_job",
            dependency_task_id=child["id"],
        )
        self.kill(parent, "parent")
        self.assertIsNone(self.claim("propagate", role="qc", now=T0 + timedelta(seconds=2)))
        self.assertEqual(self.queue.dead_letters()["count"], 3)

        retried = self.queue.retry_dead(
            parent["id"],
            reason="transient root failure fixed",
            actor="operator-1",
            cascade_dependents=True,
            idempotency_key="retry-tree",
            now=T0 + timedelta(minutes=1),
        )
        self.assertEqual(
            retried["revived_dependents"], [child["id"], grandchild["id"]]
        )
        self.assertEqual(self.queue.dead_letters()["count"], 0)
        self.assertEqual(self.queue.status(child["id"])["task"]["status"], "queued")
        self.assertEqual(self.queue.status(grandchild["id"])["task"]["status"], "queued")

    def test_dependency_dead_retry_fails_closed_until_parent_succeeds(self) -> None:
        parent = self.enqueue("parent")
        child = self.enqueue(
            "child", role="qc", kind="qc_job", dependency_task_id=parent["id"]
        )
        self.kill(parent, "parent")
        self.assertIsNone(self.claim("propagate", role="qc", now=T0 + timedelta(seconds=2)))
        with self.assertRaisesRegex(ValidationError, "dependency must succeed"):
            self.queue.retry_dead(
                child["id"],
                reason="unsafe direct retry",
                actor="operator-1",
                idempotency_key="retry-child-too-soon",
                now=T0 + timedelta(minutes=1),
            )

    def test_rework_versions_root_and_rewires_entire_downstream_chain(self) -> None:
        root = self.enqueue("root", max_attempts=3)
        child = self.enqueue(
            "child",
            role="render",
            kind="render_job",
            dependency_task_id=root["id"],
            max_attempts=2,
        )
        leaf = self.enqueue(
            "leaf",
            role="qc",
            kind="qc_job",
            dependency_task_id=child["id"],
            max_attempts=2,
        )

        reworked = self.queue.rework_task(
            root["id"],
            reason="upstream source checksum changed",
            actor="research-lead",
            payload_patch={"revision": 2, "source_sha256": "b" * 64},
            idempotency_key="rework-chain-1",
            now=T0 + timedelta(minutes=2),
        )
        mapping = reworked["task_mapping"]
        self.assertEqual(len(mapping), 3)
        replacements = {item["id"]: item for item in reworked["created_tasks"]}
        new_root = replacements[mapping[root["id"]]]
        new_child = replacements[mapping[child["id"]]]
        new_leaf = replacements[mapping[leaf["id"]]]
        self.assertEqual(new_root["payload"]["revision"], 2)
        self.assertEqual(new_child["dependency_task_id"], new_root["id"])
        self.assertEqual(new_leaf["dependency_task_id"], new_child["id"])
        self.assertEqual(new_child["max_attempts"], 2)
        self.assertEqual(
            {self.queue.status(item["id"])["task"]["status"] for item in (root, child, leaf)},
            {"dead"},
        )
        self.assertEqual(self.queue.dead_letters()["count"], 0)
        self.assertEqual(self.queue.dead_letters(status="resolved")["count"], 3)
        detail = self.queue.status(root["id"])
        self.assertEqual(detail["reworks"][0]["replacement_root_task_id"], new_root["id"])
        self.assertEqual(
            self.queue.rework_task(
                root["id"],
                reason="upstream source checksum changed",
                actor="research-lead",
                payload_patch={"revision": 2, "source_sha256": "b" * 64},
                idempotency_key="rework-chain-1",
                now=T0 + timedelta(days=1),
            ),
            reworked,
        )

    def test_rework_rejects_active_lease_without_partial_clones(self) -> None:
        root = self.enqueue("leased")
        self.claim("leased")
        with self.assertRaises(LeaseConflictError):
            self.queue.rework_task(
                root["id"],
                reason="must wait",
                actor="operator-1",
                idempotency_key="rework-leased",
                now=T0 + timedelta(seconds=1),
            )
        status = self.queue.status(now=T0 + timedelta(seconds=1))
        self.assertEqual(status["tasks"], {"leased": 1})

    def test_rework_rejects_publisher_root_to_prevent_duplicate_delivery(self) -> None:
        publisher = self.enqueue("publisher", role="publisher", kind="publisher_job")
        with self.assertRaisesRegex(ValidationError, "cannot be reworked directly"):
            self.queue.rework_task(
                publisher["id"],
                reason="unsafe destination change",
                actor="operator-1",
                payload_patch={"destination": "other-account"},
                idempotency_key="rework-publisher",
                now=T0 + timedelta(seconds=1),
            )
        self.assertEqual(self.queue.status(publisher["id"])["task"]["status"], "queued")

    def test_cli_exposes_dead_list_retry_and_rework(self) -> None:
        task = self.enqueue("cli")
        self.kill(task, "cli")
        code, listed = self.run_cli("dead-list", "--db", str(self.db))
        self.assertEqual(code, 0)
        self.assertEqual(listed["count"], 1)
        code, retried = self.run_cli(
            "dead-retry",
            task["id"],
            "--reason",
            "incident resolved",
            "--actor",
            "cli-operator",
            "--idempotency-key",
            "cli-retry",
            "--db",
            str(self.db),
        )
        self.assertEqual(code, 0)
        self.assertEqual(retried["task"]["status"], "queued")

        patch_path = self.root / "patch.json"
        patch_path.write_text('{"revision": 3}', encoding="utf-8")
        code, reworked = self.run_cli(
            "task-rework",
            task["id"],
            "--reason",
            "editorial correction",
            "--actor",
            "cli-editor",
            "--payload-patch",
            str(patch_path),
            "--idempotency-key",
            "cli-rework",
            "--db",
            str(self.db),
        )
        self.assertEqual(code, 0)
        self.assertEqual(reworked["created_tasks"][0]["payload"]["revision"], 3)


if __name__ == "__main__":
    unittest.main()
