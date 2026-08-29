from __future__ import annotations

import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from video_factory.db import Database
from video_factory.queue import Dispatcher
from video_factory.worker import ExecutorRegistry, HeadlessWorker, WorkerConfig


class ProductionSoakTests(unittest.TestCase):
    def test_finite_worker_drains_shadow_batches_of_15_and_30(self) -> None:
        for target in (15, 30):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                database_path = Path(temporary) / "factory.sqlite3"
                dispatcher = Dispatcher(database_path)
                for index in range(target):
                    dispatcher.enqueue(
                        role="scout",
                        pod=("health" if index % 2 == 0 else "celebrity_news"),
                        kind="shadow_soak",
                        payload={"index": index, "shadow": True},
                        priority=target - index,
                        max_attempts=2,
                        retry_backoff_seconds=0,
                        idempotency_key=f"soak:{target}:enqueue:{index}",
                    )

                registry = ExecutorRegistry()
                registry.register(
                    "shadow_soak",
                    lambda task, stop: {
                        "shadow": True,
                        "task_id": task["id"],
                        "index": task["payload"]["index"],
                    },
                )
                result = HeadlessWorker(
                    dispatcher,
                    registry,
                    WorkerConfig(
                        worker_id=f"soak-{target}",
                        role="scout",
                        lease_seconds=5,
                        heartbeat_seconds=1,
                        poll_seconds=0.001,
                        max_tasks=target,
                        max_idle_polls=2,
                    ),
                ).run(threading.Event())

                self.assertTrue(result["ok"])
                self.assertEqual(result["claimed"], target)
                self.assertEqual(result["succeeded"], target)
                self.assertEqual(result["failed"], 0)
                status = dispatcher.status()
                self.assertEqual(status["tasks"].get("succeeded"), target)
                with closing(Database(database_path).connect()) as connection:
                    empty_claim_operations = connection.execute(
                        "SELECT COUNT(*) FROM operations "
                        "WHERE command = 'queue.claim' AND response_json LIKE '%\"claimed\":false%'"
                    ).fetchone()[0]
                self.assertEqual(empty_claim_operations, 0)


if __name__ == "__main__":
    unittest.main()
