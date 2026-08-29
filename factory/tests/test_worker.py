from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_ROOT / "src"))

from video_factory.cli import main  # noqa: E402
from video_factory.errors import (  # noqa: E402
    LeaseConflictError,
    NotFoundError,
    ValidationError,
)
from video_factory.queue import Dispatcher  # noqa: E402
from video_factory.worker import (  # noqa: E402
    ExecutorError,
    ExecutorRegistry,
    HeadlessWorker,
    ResourceLock,
    SubprocessExecutor,
    WorkerConfig,
)


T0 = datetime(2026, 8, 29, 8, 0, tzinfo=UTC)


class RecordingLock:
    def __init__(self, *, available: bool = True):
        self.available = available
        self.held = False
        self.acquisitions = 0
        self.releases = 0

    def acquire(self, timeout_seconds: float) -> bool:
        self.acquisitions += 1
        if not self.available:
            return False
        self.held = True
        return True

    def release(self) -> None:
        if self.held:
            self.releases += 1
        self.held = False


class GuardedDispatcher(Dispatcher):
    def __init__(self, db_path: Path, lock: RecordingLock):
        super().__init__(db_path)
        self.lock = lock
        self.claim_calls = 0
        self.renew_calls = 0

    def claim(self, **kwargs):
        self.claim_calls += 1
        if not self.lock.held:
            raise AssertionError("claim happened before resource lock")
        return super().claim(**kwargs)

    def renew_lease(self, *args, **kwargs):
        self.renew_calls += 1
        return super().renew_lease(*args, **kwargs)


class BrokenHeartbeatDispatcher(GuardedDispatcher):
    def renew_lease(self, *args, **kwargs):
        self.renew_calls += 1
        raise LeaseConflictError("simulated fencing loss")


class AmbiguousAcknowledgementDispatcher(GuardedDispatcher):
    def __init__(self, db_path: Path, lock: RecordingLock, *, fail_path: bool = False):
        super().__init__(db_path, lock)
        self.fail_path = fail_path
        self.injected = False

    def complete(self, *args, **kwargs):
        response = super().complete(*args, **kwargs)
        if not self.fail_path and not self.injected:
            self.injected = True
            raise sqlite3.OperationalError("simulated lost complete response")
        return response

    def fail(self, *args, **kwargs):
        response = super().fail(*args, **kwargs)
        if self.fail_path and not self.injected:
            self.injected = True
            raise sqlite3.OperationalError("simulated lost fail response")
        return response


class WorkerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "factory.sqlite3"
        self.queue = Dispatcher(self.db)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def enqueue(self, key: str, *, max_attempts: int = 3) -> dict:
        return self.queue.enqueue(
            role="editor",
            pod="health",
            kind="test.edit",
            payload={"value": key},
            max_attempts=max_attempts,
            retry_backoff_seconds=0,
            idempotency_key=f"enqueue-{key}",
            now=T0,
        )["task"]

    def registry(self, executor) -> ExecutorRegistry:
        registry = ExecutorRegistry()
        registry.register("test.edit", executor)
        return registry

    def config(self, **overrides) -> WorkerConfig:
        values = {
            "worker_id": "worker-test-1",
            "role": "editor",
            "lease_seconds": 5,
            "heartbeat_seconds": 0.03,
            "poll_seconds": 0.01,
            "max_tasks": 1,
        }
        values.update(overrides)
        return WorkerConfig(**values)

    def test_renew_lease_is_monotonic_and_fenced(self) -> None:
        task = self.enqueue("renew")
        claimed = self.queue.claim(
            worker_id="renew-worker",
            role="editor",
            lease_seconds=5,
            idempotency_key="claim-renew",
            now=T0,
        )["task"]
        first = self.queue.renew_lease(
            task["id"],
            lease_token=claimed["lease_token"],
            worker_id="renew-worker",
            lease_seconds=5,
            now=T0 + timedelta(seconds=4),
        )
        self.assertEqual(first["task"]["lease_expires_at"], "2026-08-29T08:00:09.000Z")
        replay = self.queue.renew_lease(
            task["id"],
            lease_token=claimed["lease_token"],
            worker_id="renew-worker",
            lease_seconds=5,
            now=T0 + timedelta(seconds=4),
        )
        self.assertEqual(replay["task"]["lease_expires_at"], first["task"]["lease_expires_at"])
        detail = self.queue.status(task["id"], now=T0 + timedelta(seconds=4))
        self.assertEqual(detail["attempts"][0]["lease_expires_at"], first["task"]["lease_expires_at"])
        with self.assertRaises(LeaseConflictError):
            self.queue.renew_lease(
                task["id"],
                lease_token=claimed["lease_token"],
                worker_id="other-worker",
                lease_seconds=5,
                now=T0 + timedelta(seconds=4),
            )
        completed = self.queue.complete(
            task["id"],
            lease_token=claimed["lease_token"],
            result={},
            idempotency_key="complete-after-renew",
            now=T0 + timedelta(seconds=8),
        )
        self.assertEqual(completed["task"]["status"], "succeeded")

    def test_successful_claim_replays_but_empty_polls_do_not_grow_operations(self) -> None:
        empty_lock = RecordingLock()
        empty_dispatcher = GuardedDispatcher(self.db, empty_lock)
        idle = HeadlessWorker(
            empty_dispatcher,
            self.registry(lambda task, stop: {}),
            self.config(max_tasks=0, max_idle_polls=3),
            resource_lock=empty_lock,
        ).run()
        self.assertEqual(idle["idle_polls"], 3)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0],
                0,
            )

        self.enqueue("claim-replay")
        first = self.queue.claim(
            worker_id="replay-worker",
            role="editor",
            lease_seconds=5,
            idempotency_key="claim-replay-key",
            now=T0,
        )
        replay = self.queue.claim(
            worker_id="replay-worker",
            role="editor",
            lease_seconds=5,
            idempotency_key="claim-replay-key",
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(replay, first)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0],
                2,  # enqueue + the successful claim
            )

    def test_worker_locks_before_claim_heartbeats_and_completes(self) -> None:
        task = self.enqueue("happy")
        lock = RecordingLock()
        dispatcher = GuardedDispatcher(self.db, lock)
        seen: list[dict] = []

        def executor(public_task, stop_event):
            self.assertNotIn("lease_token", public_task)
            self.assertEqual(public_task["upstream_results"], [])
            self.assertEqual(public_task["id"], task["id"])
            time.sleep(0.12)
            return {"handled": public_task["id"]}

        result = HeadlessWorker(
            dispatcher,
            self.registry(executor),
            self.config(),
            resource_lock=lock,
            event_callback=seen.append,
        ).run()
        self.assertTrue(result["ok"])
        self.assertEqual(result["succeeded"], 1)
        self.assertGreaterEqual(dispatcher.renew_calls, 1)
        self.assertEqual(lock.acquisitions, 1)
        self.assertEqual(lock.releases, 1)
        detail = self.queue.status(task["id"])
        self.assertEqual(detail["task"]["status"], "succeeded")
        self.assertEqual(detail["task"]["result"], {"handled": task["id"]})
        self.assertEqual([event["event"] for event in seen], ["task_started", "task_succeeded"])

    def test_research_has_no_upstream_and_script_receives_root_first_chain(self) -> None:
        research = self.queue.enqueue(
            role="research",
            pod="health",
            kind="research_job",
            payload={},
            idempotency_key="context-research",
        )["task"]
        research_claim = self.queue.claim(
            worker_id="research-worker",
            role="research",
            lease_seconds=30,
            idempotency_key="context-research-claim",
        )["task"]
        empty = self.queue.execution_context(
            research["id"],
            lease_token=research_claim["lease_token"],
            worker_id="research-worker",
        )
        self.assertEqual(empty["upstream_results"], [])
        self.assertNotIn("lease_token", json.dumps(empty, ensure_ascii=False))
        self.queue.complete(
            research["id"],
            lease_token=research_claim["lease_token"],
            result={"claim_ledger": {"claim": "supported"}},
            idempotency_key="context-research-complete",
        )

        rights = self.queue.enqueue(
            role="rights",
            pod="health",
            kind="rights_job",
            payload={},
            dependency_task_id=research["id"],
            idempotency_key="context-rights",
        )["task"]
        rights_claim = self.queue.claim(
            worker_id="rights-worker",
            role="rights",
            lease_seconds=30,
            idempotency_key="context-rights-claim",
        )["task"]
        self.queue.complete(
            rights["id"],
            lease_token=rights_claim["lease_token"],
            result={"rights_manifest": {"passed": True}},
            idempotency_key="context-rights-complete",
        )

        script = self.queue.enqueue(
            role="script",
            pod="health",
            kind="script_job",
            payload={},
            dependency_task_id=rights["id"],
            idempotency_key="context-script",
        )["task"]
        registry = ExecutorRegistry()

        def script_executor(public_task, _stop_event):
            upstream = public_task["upstream_results"]
            self.assertEqual([item["role"] for item in upstream], ["research", "rights"])
            self.assertEqual(
                [item["result"] for item in upstream],
                [
                    {"claim_ledger": {"claim": "supported"}},
                    {"rights_manifest": {"passed": True}},
                ],
            )
            serialized = json.dumps(public_task, ensure_ascii=False)
            self.assertNotIn("lease_token", serialized)
            self.assertNotIn(research_claim["lease_token"], serialized)
            self.assertNotIn(rights_claim["lease_token"], serialized)
            return {"script": "ready"}

        registry.register("script_job", script_executor)
        lock = RecordingLock()
        result = HeadlessWorker(
            GuardedDispatcher(self.db, lock),
            registry,
            self.config(role="script"),
            resource_lock=lock,
        ).run()
        self.assertTrue(result["ok"])
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(self.queue.status(script["id"])["task"]["result"], {"script": "ready"})

    def test_execution_context_fails_closed_for_unsucceeded_unknown_and_secret(self) -> None:
        pending = self.queue.enqueue(
            role="rights",
            pod="health",
            kind="pending_rights",
            payload={},
            idempotency_key="context-pending",
        )["task"]
        leaf = self.queue.enqueue(
            role="script",
            pod="health",
            kind="context_leaf",
            payload={},
            idempotency_key="context-leaf",
        )["task"]
        leaf_claim = self.queue.claim(
            worker_id="context-worker",
            role="script",
            lease_seconds=30,
            idempotency_key="context-leaf-claim",
        )["task"]
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                "UPDATE tasks SET dependency_task_id = ? WHERE id = ?",
                (pending["id"], leaf["id"]),
            )
            connection.commit()
        with self.assertRaisesRegex(ValidationError, "has not succeeded"):
            self.queue.execution_context(
                leaf["id"],
                lease_token=leaf_claim["lease_token"],
                worker_id="context-worker",
            )

        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "UPDATE tasks SET dependency_task_id = ? WHERE id = ?",
                ("task_missing_upstream", leaf["id"]),
            )
            connection.commit()
        with self.assertRaisesRegex(ValidationError, "does not exist"):
            self.queue.execution_context(
                leaf["id"],
                lease_token=leaf_claim["lease_token"],
                worker_id="context-worker",
            )
        with self.assertRaises(NotFoundError):
            self.queue.execution_context(
                "task_unknown",
                lease_token="lt_unknown",
                worker_id="context-worker",
            )

        # Restore a valid, succeeded parent whose result itself attempts to
        # smuggle a fencing credential. The context refuses it, never redacts
        # silently or passes it to an executor.
        secret_parent = self.queue.enqueue(
            role="research",
            pod="health",
            kind="secret_parent",
            payload={},
            idempotency_key="context-secret-parent",
        )["task"]
        secret_claim = self.queue.claim(
            worker_id="secret-worker",
            role="research",
            lease_seconds=30,
            idempotency_key="context-secret-claim",
        )["task"]
        self.queue.complete(
            secret_parent["id"],
            lease_token=secret_claim["lease_token"],
            result={"lease_token": "lt_must_never_escape"},
            idempotency_key="context-secret-complete",
        )
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                "UPDATE tasks SET dependency_task_id = ? WHERE id = ?",
                (secret_parent["id"], leaf["id"]),
            )
            connection.commit()
        with self.assertRaisesRegex(ValidationError, "looks like a secret"):
            self.queue.execution_context(
                leaf["id"],
                lease_token=leaf_claim["lease_token"],
                worker_id="context-worker",
            )

    def test_retryable_executor_failure_is_acknowledged_once(self) -> None:
        task = self.enqueue("retry")
        lock = RecordingLock()
        dispatcher = GuardedDispatcher(self.db, lock)

        def executor(_task, _stop_event):
            raise ExecutorError("provider_busy", "temporary provider failure")

        result = HeadlessWorker(
            dispatcher,
            self.registry(executor),
            self.config(),
            resource_lock=lock,
        ).run()
        self.assertEqual((result["failed"], result["retried"], result["dead"]), (1, 1, 0))
        detail = self.queue.status(task["id"])
        self.assertEqual(detail["task"]["status"], "queued")
        self.assertEqual(detail["attempts"][0]["status"], "failed")
        self.assertEqual(detail["attempts"][0]["error"]["code"], "provider_busy")

    def test_ambiguous_complete_and_fail_replay_same_acknowledgement(self) -> None:
        completed_task = self.enqueue("ambiguous-complete")
        complete_lock = RecordingLock()
        complete_dispatcher = AmbiguousAcknowledgementDispatcher(
            self.db, complete_lock
        )
        completed = HeadlessWorker(
            complete_dispatcher,
            self.registry(lambda task, stop: {"done": task["id"]}),
            self.config(acknowledgement_retry_seconds=0),
            resource_lock=complete_lock,
        ).run()
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["succeeded"], 1)
        detail = self.queue.status(completed_task["id"])
        self.assertEqual(detail["task"]["status"], "succeeded")
        self.assertEqual(len(detail["attempts"]), 1)

        failed_task = self.enqueue("ambiguous-fail")
        fail_lock = RecordingLock()
        fail_dispatcher = AmbiguousAcknowledgementDispatcher(
            self.db, fail_lock, fail_path=True
        )

        def failing_executor(_task, _stop):
            raise ExecutorError("temporary", "try again")

        failed = HeadlessWorker(
            fail_dispatcher,
            self.registry(failing_executor),
            self.config(acknowledgement_retry_seconds=0),
            resource_lock=fail_lock,
        ).run()
        self.assertTrue(failed["ok"])
        self.assertEqual((failed["failed"], failed["retried"]), (1, 1))
        detail = self.queue.status(failed_task["id"])
        self.assertEqual(detail["task"]["status"], "queued")
        self.assertEqual(len(detail["attempts"]), 1)

    def test_lost_heartbeat_stops_without_stale_acknowledgement(self) -> None:
        task = self.enqueue("lost")
        lock = RecordingLock()
        dispatcher = BrokenHeartbeatDispatcher(self.db, lock)

        def executor(_task, _stop_event):
            time.sleep(0.08)
            return {"should_not_commit": True}

        result = HeadlessWorker(
            dispatcher,
            self.registry(executor),
            self.config(),
            resource_lock=lock,
        ).run()
        self.assertFalse(result["ok"])
        self.assertEqual(result["stop_reason"], "lease_lost")
        self.assertEqual(result["lease_lost"], 1)
        detail = self.queue.status(task["id"])
        self.assertEqual(detail["task"]["status"], "leased")
        self.assertIsNone(detail["task"]["result"])

    def test_busy_resource_never_claims(self) -> None:
        self.enqueue("busy")
        lock = RecordingLock(available=False)
        dispatcher = GuardedDispatcher(self.db, lock)
        result = HeadlessWorker(
            dispatcher,
            self.registry(lambda task, stop: {}),
            self.config(max_tasks=0, max_idle_polls=1),
            resource_lock=lock,
        ).run()
        self.assertEqual(result["stop_reason"], "max_idle_polls")
        self.assertEqual(dispatcher.claim_calls, 0)
        self.assertEqual(self.queue.status()["tasks"], {"queued": 1})

    def test_shutdown_drains_current_task_then_stops_claiming(self) -> None:
        first = self.enqueue("drain-1")
        self.enqueue("drain-2")
        lock = RecordingLock()
        stop = threading.Event()

        def executor(task, stop_event):
            self.assertEqual(task["id"], first["id"])
            stop_event.set()
            return {"drained": True}

        result = HeadlessWorker(
            GuardedDispatcher(self.db, lock),
            self.registry(executor),
            self.config(max_tasks=0),
            resource_lock=lock,
        ).run(stop)
        self.assertTrue(result["ok"])
        self.assertTrue(result["shutdown_requested"])
        self.assertEqual(result["claimed"], 1)
        self.assertEqual(self.queue.status(first["id"])["task"]["status"], "succeeded")
        self.assertEqual(self.queue.status()["tasks"], {"queued": 1, "succeeded": 1})

    def test_resource_lock_excludes_second_holder(self) -> None:
        path = self.root / "locks" / "gpu.lock"
        first, second = ResourceLock(path), ResourceLock(path)
        self.assertTrue(first.acquire(0))
        self.assertFalse(second.acquire(0.02))
        first.release()
        self.assertTrue(second.acquire(0))
        second.release()

    def test_cli_subprocess_handler_contract(self) -> None:
        task = self.enqueue("cli")
        handler = self.root / "handler.py"
        handler.write_text(
            "import json, sys\n"
            "task = json.load(sys.stdin)\n"
            "print(json.dumps({'handled': task['id'], "
            "'saw_lease_token': 'lease_token' in task}))\n",
            encoding="utf-8",
        )
        out, err = io.StringIO(), io.StringIO()
        code = main(
            [
                "worker",
                "--worker",
                "cli-worker",
                "--role",
                "editor",
                "--handler-executable",
                sys.executable,
                "--handler-arg",
                str(handler),
                "--resource-lock",
                "none",
                "--lease-seconds",
                "5",
                "--heartbeat-seconds",
                "1",
                "--poll-seconds",
                "0.01",
                "--max-tasks",
                "1",
                "--quiet-events",
                "--db",
                str(self.db),
            ],
            out=out,
            err=err,
        )
        self.assertEqual(code, 0, err.getvalue())
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["succeeded"], 1)
        result = self.queue.status(task["id"])["task"]["result"]
        self.assertEqual(result, {"handled": task["id"], "saw_lease_token": False})

    def test_max_runtime_stops_a_subprocess_after_grace_and_requeues(self) -> None:
        task = self.enqueue("runtime-bound")
        lock = RecordingLock()
        registry = ExecutorRegistry()
        registry.register(
            "test.edit",
            SubprocessExecutor(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_seconds=10,
                shutdown_grace_seconds=0.03,
                poll_seconds=0.01,
            ),
        )
        result = HeadlessWorker(
            GuardedDispatcher(self.db, lock),
            registry,
            self.config(
                max_tasks=0,
                max_runtime_seconds=0.05,
                heartbeat_seconds=0.02,
            ),
            resource_lock=lock,
        ).run()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stop_reason"], "max_runtime")
        self.assertFalse(result["shutdown_requested"])
        self.assertEqual((result["failed"], result["retried"]), (1, 1))
        self.assertEqual(self.queue.status(task["id"])["task"]["status"], "queued")

    def test_renew_lease_cli(self) -> None:
        task = self.queue.enqueue(
            role="editor",
            pod="health",
            kind="test.edit",
            payload={},
            idempotency_key="enqueue-cli-renew",
        )["task"]
        claimed = self.queue.claim(
            worker_id="cli-renew-worker",
            role="editor",
            lease_seconds=30,
            idempotency_key="claim-cli-renew",
        )["task"]
        out, err = io.StringIO(), io.StringIO()
        code = main(
            [
                "renew-lease",
                task["id"],
                "--worker",
                "cli-renew-worker",
                "--lease-token",
                claimed["lease_token"],
                "--lease-seconds",
                "30",
                "--db",
                str(self.db),
            ],
            out=out,
            err=err,
        )
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["command"], "renew-lease")


if __name__ == "__main__":
    unittest.main()
