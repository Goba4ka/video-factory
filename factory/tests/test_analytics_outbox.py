from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from video_factory.analytics import AnalyticsStore
from video_factory.cli import main
from video_factory.db import Database
from video_factory.errors import (
    IdempotencyConflictError,
    LeaseConflictError,
    StateTransitionError,
    ValidationError,
)
from video_factory.outbox import HUMAN_CONFIRMATION, PublishOutbox
from video_factory.queue import Dispatcher


T0 = datetime(2026, 8, 29, 8, 0, tzinfo=UTC)


class AnalyticsOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "factory.sqlite3"
        self.render = self.root / "final.mp4"
        self.render.write_bytes(b"immutable-render-v1")
        self.render_sha = hashlib.sha256(self.render.read_bytes()).hexdigest()
        database = Database(self.db_path)
        database.initialize()
        with closing(database.connect()) as connection:
            timestamp = "2026-08-29T07:59:00.000Z"
            connection.execute(
                """
                INSERT INTO ideas (
                    id, title, topic, summary, payload_json, source_file,
                    source_digest, source_index, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "idea_analytics_001",
                    "Analytics test",
                    "health",
                    "test",
                    "{}",
                    "test.json",
                    "a" * 64,
                    0,
                    "ready",
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO jobs (
                    id, idea_id, batch_id, state, rights_status, qc_status,
                    rights_json, qc_json, rejection_reason, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'ready', 'passed', 'passed', ?, ?, NULL, 1, ?, ?)
                """,
                (
                    "job_analytics_001",
                    "idea_analytics_001",
                    "batch_analytics_001",
                    "{}",
                    "{}",
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        self.outbox = PublishOutbox(self.db_path)
        self.analytics = AnalyticsStore(self.db_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, *, caption: str = "Проверенный ролик") -> dict:
        return {
            "schema_version": "1.0.0",
            "job_id": "job_analytics_001",
            "render_id": "render_analytics_001",
            "render_path": str(self.render),
            "render_sha256": self.render_sha,
            "qc_report": "qc/final.json",
            "destination": {
                "platform": "tiktok",
                "account_id": "account-ru-1",
                "caption": caption,
                "visibility": "draft",
            },
            "disclosures": {
                "ai_generated": False,
                "altered_or_synthetic": False,
                "paid_promotion": False,
                "notes": [],
            },
        }

    def create(self, key: str = "create-1") -> dict:
        return self.outbox.create(self.request(), idempotency_key=key, now=T0)

    def approve(self, created: dict, key: str = "approve-1") -> dict:
        item = created["outbox"]
        return self.outbox.approve(
            item["id"],
            render_sha256=item["render_sha256"],
            metadata_sha256=item["metadata_sha256"],
            approved_by="owner@example.test",
            approval_note="Reviewed the exact render and destination metadata",
            human_confirmation=HUMAN_CONFIRMATION,
            idempotency_key=key,
            now=T0 + timedelta(seconds=1),
        )

    def publish(self) -> dict:
        created = self.create()
        self.approve(created)
        claim = self.outbox.claim(
            worker_id="connector-1",
            platform="tiktok",
            lease_seconds=300,
            idempotency_key="claim-1",
            now=T0 + timedelta(seconds=2),
        )
        return self.outbox.complete(
            created["outbox"]["id"],
            lease_token=claim["outbox"]["lease_token"],
            remote_id="post-001",
            receipt={"provider_request_id": "request-001", "status": "accepted"},
            published_at="2026-08-29T08:00:03Z",
            idempotency_key="complete-1",
            now=T0 + timedelta(seconds=3),
        )

    def test_outbox_requires_ready_job_and_exact_render_checksum(self) -> None:
        bad = self.request()
        bad["render_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "actual render bytes"):
            self.outbox.create(bad, idempotency_key="bad-checksum", now=T0)

        database = Database(self.db_path)
        with closing(database.connect()) as connection:
            connection.execute(
                "UPDATE jobs SET state = 'qc_pending', qc_status = 'pending' WHERE id = ?",
                ("job_analytics_001",),
            )
            connection.commit()
        with self.assertRaisesRegex(StateTransitionError, "ready job"):
            self.outbox.create(self.request(), idempotency_key="not-ready", now=T0)

    def test_human_approval_is_separate_checksum_bound_and_idempotent(self) -> None:
        created = self.create()
        self.assertEqual(created["outbox"]["status"], "pending_approval")
        self.assertFalse(created["external_send_performed"])
        with self.assertRaisesRegex(ValidationError, "human_confirmation"):
            self.outbox.approve(
                created["outbox"]["id"],
                render_sha256=self.render_sha,
                metadata_sha256=created["outbox"]["metadata_sha256"],
                approved_by="owner",
                approval_note="reviewed",
                human_confirmation="yes",
                idempotency_key="bad-human",
                now=T0,
            )
        with self.assertRaisesRegex(ValidationError, "metadata_sha256"):
            self.outbox.approve(
                created["outbox"]["id"],
                render_sha256=self.render_sha,
                metadata_sha256="0" * 64,
                approved_by="owner",
                approval_note="reviewed",
                human_confirmation=HUMAN_CONFIRMATION,
                idempotency_key="bad-metadata",
                now=T0,
            )
        approved = self.approve(created)
        replay = self.approve(created)
        self.assertEqual(approved, replay)
        self.assertEqual(approved["outbox"]["status"], "approved")
        self.assertEqual(approved["outbox"]["approved_by"], "owner@example.test")

    def test_tampered_render_blocks_claim(self) -> None:
        created = self.create()
        self.approve(created)
        self.render.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValidationError, "checksum mismatch"):
            self.outbox.claim(
                worker_id="connector-1",
                platform="tiktok",
                lease_seconds=300,
                idempotency_key="claim-tampered",
                now=T0 + timedelta(seconds=2),
            )

    def test_claim_complete_and_stale_token_are_fenced(self) -> None:
        created = self.create()
        self.approve(created)
        claim = self.outbox.claim(
            worker_id="connector-1",
            platform="tiktok",
            lease_seconds=300,
            idempotency_key="claim-1",
            now=T0 + timedelta(seconds=2),
        )
        replay = self.outbox.claim(
            worker_id="connector-1",
            platform="tiktok",
            lease_seconds=300,
            idempotency_key="claim-1",
            now=T0 + timedelta(seconds=30),
        )
        self.assertEqual(claim, replay)
        token = claim["outbox"]["lease_token"]
        self.assertTrue(token)
        listed = self.outbox.list(status="dispatching")
        self.assertIsNone(listed["items"][0]["lease_token"])
        with self.assertRaises(LeaseConflictError):
            self.outbox.complete(
                created["outbox"]["id"],
                lease_token="stale-token",
                remote_id="post-x",
                receipt={},
                idempotency_key="complete-stale",
                now=T0 + timedelta(seconds=3),
            )
        completed = self.outbox.complete(
            created["outbox"]["id"],
            lease_token=token,
            remote_id="post-001",
            receipt={"provider_request_id": "req-1"},
            idempotency_key="complete-1",
            now=T0 + timedelta(seconds=3),
        )
        self.assertEqual(completed["outbox"]["status"], "published")
        self.assertFalse(completed["external_send_performed"])

    def test_expired_delivery_becomes_unknown_never_auto_retries(self) -> None:
        created = self.create()
        self.approve(created)
        self.outbox.claim(
            worker_id="connector-1",
            platform=None,
            lease_seconds=30,
            idempotency_key="claim-expiring",
            now=T0,
        )
        recovered = self.outbox.recover_expired(
            idempotency_key="recover-1", now=T0 + timedelta(seconds=31)
        )
        self.assertEqual(recovered["recovered"], 1)
        self.assertEqual(recovered["new_state"], "unknown")
        self.assertFalse(recovered["automatic_retry"])
        next_claim = self.outbox.claim(
            worker_id="connector-2",
            platform=None,
            lease_seconds=30,
            idempotency_key="claim-after-unknown",
            now=T0 + timedelta(seconds=32),
        )
        self.assertFalse(next_claim["claimed"])

    def test_global_idempotency_key_conflict_is_fail_closed(self) -> None:
        self.create("same-key")
        with self.assertRaises(IdempotencyConflictError):
            self.outbox.create(
                self.request(caption="Different metadata"),
                idempotency_key="same-key",
                now=T0,
            )

    def test_production_metrics_validate_deduplicate_and_summarize(self) -> None:
        event = {
            "schema_version": "1.0.0",
            "event_id": "evt-render-001",
            "job_id": "job_analytics_001",
            "lane": "health",
            "stage": "render",
            "status": "succeeded",
            "occurred_at": "2026-08-29T08:01:00+00:00",
            "duration_seconds": 42.5,
            "attempts": 1,
            "estimated_cost_usd": 0.12,
            "gpu_seconds": 40,
            "output_bytes": 123456,
            "metadata": {"profile": "high"},
        }
        first = self.analytics.record_metric(event, idempotency_key="metric-1", now=T0)
        replay = self.analytics.record_metric(event, idempotency_key="metric-1", now=T0)
        self.assertEqual(first, replay)
        duplicate = self.analytics.record_metric(
            event, idempotency_key="metric-duplicate", now=T0
        )
        self.assertFalse(duplicate["created"])
        altered = {**event, "duration_seconds": 99}
        with self.assertRaisesRegex(ValidationError, "different metrics"):
            self.analytics.record_metric(
                altered, idempotency_key="metric-altered", now=T0
            )
        summary = self.analytics.summary(lane="health", stage="render")
        self.assertEqual(summary["totals"]["events"], 1)
        self.assertEqual(summary["totals"]["output_bytes"], 123456)
        self.assertAlmostEqual(summary["totals"]["estimated_cost_usd"], 0.12)

    def test_metrics_reject_secrets(self) -> None:
        event = {
            "schema_version": "1.0.0",
            "event_id": "evt-secret",
            "lane": "health",
            "stage": "system",
            "status": "failed",
            "occurred_at": "2026-08-29T08:01:00Z",
            "duration_seconds": 0,
            "metadata": {"api_key": "do-not-store"},
        }
        with self.assertRaisesRegex(ValidationError, "secret"):
            self.analytics.record_metric(event, idempotency_key="metric-secret")

    def test_queue_metrics_collector_is_incremental_and_idempotent(self) -> None:
        queue = Dispatcher(self.db_path)
        task = queue.enqueue(
            role="render",
            pod="health",
            kind="render_job",
            payload={},
            job_id="job_analytics_001",
            idempotency_key="enqueue-metric-task",
            now=T0,
        )["task"]
        claimed = queue.claim(
            worker_id="render-worker",
            role="render",
            pod="health",
            lease_seconds=300,
            idempotency_key="claim-metric-task",
            now=T0 + timedelta(seconds=1),
        )["task"]
        queue.complete(
            task["id"],
            lease_token=claimed["lease_token"],
            result={},
            idempotency_key="complete-metric-task",
            now=T0 + timedelta(seconds=13),
        )
        first = self.analytics.collect_queue_metrics(
            idempotency_key="collect-queue-1", now=T0 + timedelta(seconds=20)
        )
        second = self.analytics.collect_queue_metrics(
            idempotency_key="collect-queue-2", now=T0 + timedelta(seconds=21)
        )
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["existing"], 1)
        summary = self.analytics.summary(lane="health", stage="render")
        self.assertEqual(summary["totals"]["events"], 1)
        self.assertEqual(summary["totals"]["duration_seconds"], 12)

    def feedback_bundle(self, outbox_id: str, *, render_sha: str | None = None) -> dict:
        return {
            "schema_version": "1.0.0",
            "snapshots": [
                {
                    "outbox_id": outbox_id,
                    "render_sha256": render_sha or self.render_sha,
                    "cohort": {"lane": "health", "duration_seconds": 28.0},
                    "snapshot": {
                        "schema_version": "1.0.0",
                        "job_id": "job_analytics_001",
                        "platform": "tiktok",
                        "remote_id": "post-001",
                        "captured_at": "2026-08-29T09:00:03Z",
                        "age_hours": 1,
                        "metrics": {
                            "views": 1000,
                            "engaged_views": 800,
                            "stayed_to_watch_rate": 0.71,
                            "average_view_duration_seconds": 17.4,
                            "average_percentage_viewed": 0.68,
                            "completion_rate": 0.52,
                            "likes": 90,
                            "comments": 12,
                            "shares": 22,
                            "saves": 18,
                            "follows": 7,
                            "negative_feedback": 0,
                        },
                        "policy_events": [],
                        "production": {
                            "minutes_to_produce": 18,
                            "estimated_cost": 0.42,
                            "currency": "USD",
                        },
                    },
                }
            ],
        }

    def seed_evaluation_snapshot(
        self,
        index: int,
        *,
        hook: float,
        hold: float,
        shares: int,
        saves: int,
        follows: int,
        lane: str = "health",
        duration_seconds: float = 28,
        canonical_hour: int = 72,
        candidate: bool = False,
        policy_events: list[dict] | None = None,
    ) -> str:
        idea_id = f"idea_eval_{index:03d}"
        job_id = f"job_eval_{index:03d}"
        outbox_id = f"out_eval_{index:03d}"
        feedback_id = f"fb_eval_{index:03d}"
        captured = T0 if candidate else T0 - timedelta(minutes=index + 1)
        captured_text = captured.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        timestamp = "2026-08-29T07:00:00.000Z"
        destination = {
            "platform": "tiktok",
            "account_id": "account-ru-1",
            "caption": f"evaluation {index}",
            "visibility": "draft",
            "scheduled_at": None,
            "status": "pending",
            "remote_id": None,
        }
        disclosures = {
            "ai_generated": False,
            "altered_or_synthetic": False,
            "paid_promotion": False,
            "notes": [],
        }
        metadata_sha = hashlib.sha256(
            json.dumps(
                {"destinations": [destination], "disclosures": disclosures},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        metrics = {
            "views": 1200,
            "engaged_views": 1000,
            "stayed_to_watch_rate": hook,
            "average_view_duration_seconds": duration_seconds * hold,
            "average_percentage_viewed": hold,
            "completion_rate": hold,
            "likes": 80,
            "comments": 10,
            "shares": shares,
            "saves": saves,
            "follows": follows,
            "negative_feedback": 0,
        }
        if duration_seconds < 20:
            duration_band = "under_20"
        elif duration_seconds < 35:
            duration_band = "20_34"
        elif duration_seconds < 60:
            duration_band = "35_59"
        else:
            duration_band = "60_plus"
        database = Database(self.db_path)
        with closing(database.connect()) as connection:
            connection.execute(
                """
                INSERT INTO ideas (
                    id, title, topic, summary, payload_json, source_file,
                    source_digest, source_index, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '{}', 'eval.json', ?, 0, 'ready', ?, ?)
                """,
                (idea_id, f"Evaluation {index}", lane, "test", "e" * 64, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO jobs (
                    id, idea_id, batch_id, state, rights_status, qc_status,
                    rights_json, qc_json, rejection_reason, version,
                    created_at, updated_at
                ) VALUES (?, ?, 'batch_eval', 'ready', 'passed', 'passed',
                          '{}', '{}', NULL, 1, ?, ?)
                """,
                (job_id, idea_id, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO publish_outbox (
                    id, job_id, render_id, render_path, render_sha256,
                    metadata_sha256, qc_report, platform, account_id,
                    destination_json, disclosures_json, status, approved_by,
                    approved_at, approval_note, remote_id, published_at,
                    receipt_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'qc/eval.json', 'tiktok',
                          'account-ru-1', ?, ?, 'published', 'owner', ?,
                          'reviewed', ?, ?, '{}', ?, ?)
                """,
                (
                    outbox_id,
                    job_id,
                    f"render_eval_{index:03d}",
                    str(self.render),
                    self.render_sha,
                    metadata_sha,
                    json.dumps(destination, ensure_ascii=False, sort_keys=True),
                    json.dumps(disclosures, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    f"post-eval-{index:03d}",
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO performance_feedback (
                    id, outbox_id, job_id, render_sha256, platform, account_id,
                    remote_id, captured_at, age_hours, metrics_json,
                    policy_events_json, production_json, source_file,
                    source_digest, imported_at
                ) VALUES (?, ?, ?, ?, 'tiktok', 'account-ru-1', ?, ?, ?, ?, ?,
                          NULL, 'seed.json', ?, ?)
                """,
                (
                    feedback_id,
                    outbox_id,
                    job_id,
                    self.render_sha,
                    f"post-eval-{index:03d}",
                    captured_text,
                    float(canonical_hour),
                    json.dumps(metrics, sort_keys=True, separators=(",", ":")),
                    json.dumps(policy_events or [], sort_keys=True, separators=(",", ":")),
                    "f" * 64,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO performance_feedback_dimensions (
                    feedback_id, lane, duration_seconds, duration_band,
                    canonical_snapshot_hour
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (feedback_id, lane, duration_seconds, duration_band, canonical_hour),
            )
            connection.commit()
        return outbox_id

    def test_feedback_import_requires_published_checksum_attribution(self) -> None:
        published = self.publish()
        outbox_id = published["outbox"]["id"]
        source = self.root / "feedback.json"
        source.write_text(
            json.dumps(self.feedback_bundle(outbox_id), ensure_ascii=False),
            encoding="utf-8",
        )
        imported = self.analytics.import_feedback(
            source, idempotency_key="feedback-1", now=T0 + timedelta(hours=2)
        )
        replay = self.analytics.import_feedback(
            source, idempotency_key="feedback-1", now=T0 + timedelta(hours=3)
        )
        self.assertEqual(imported, replay)
        self.assertEqual(imported["created"], 1)
        self.assertTrue(imported["checksum_attribution_verified"])
        rows = self.analytics.list_feedback(job_id="job_analytics_001")
        self.assertEqual(rows["items"][0]["render_sha256"], self.render_sha)

    def test_feedback_bundle_is_atomic_on_checksum_mismatch(self) -> None:
        published = self.publish()
        outbox_id = published["outbox"]["id"]
        good = self.feedback_bundle(outbox_id)["snapshots"][0]
        second = json.loads(json.dumps(good))
        second["snapshot"]["captured_at"] = "2026-08-29T14:00:03Z"
        second["snapshot"]["age_hours"] = 6
        second["render_sha256"] = "0" * 64
        source = self.root / "bad-feedback.json"
        source.write_text(
            json.dumps({"snapshots": [good, second]}), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValidationError, "render_sha256"):
            self.analytics.import_feedback(source, idempotency_key="feedback-bad")
        self.assertEqual(self.analytics.list_feedback()["items"], [])

    def test_editorial_feedback_insufficient_cohort_uses_nearest_canonical(self) -> None:
        for index in range(1, 4):
            self.seed_evaluation_snapshot(
                index,
                hook=0.5,
                hold=0.6,
                shares=10,
                saves=5,
                follows=3,
                canonical_hour=24,
            )
        candidate = self.seed_evaluation_snapshot(
            90,
            hook=0.7,
            hold=0.75,
            shares=20,
            saves=10,
            follows=6,
            canonical_hour=24,
            candidate=True,
        )
        result = self.analytics.evaluate_editorial_feedback(
            candidate,
            minimum_cohort=5,
            idempotency_key="evaluate-insufficient",
            now=T0 + timedelta(hours=1),
        )
        recommendation = result["recommendation"]
        self.assertEqual(recommendation["evaluation"]["status"], "insufficient_cohort")
        self.assertEqual(recommendation["evaluation"]["snapshot_resolution"], "nearest_canonical")
        self.assertEqual(recommendation["canonical_snapshot_hour"], 24)
        self.assertEqual(recommendation["recommendations"]["maximum_followups"], 0)
        self.assertEqual(recommendation["recommendations"]["actions"], [])

    def test_editorial_winner_is_bounded_deterministic_and_same_cohort_only(self) -> None:
        cohort_values = [
            (0.40, 0.50, 6, 3, 1),
            (0.45, 0.55, 8, 4, 2),
            (0.50, 0.60, 10, 5, 3),
            (0.55, 0.65, 12, 6, 4),
            (0.60, 0.70, 14, 7, 5),
        ]
        for index, values in enumerate(cohort_values, start=1):
            self.seed_evaluation_snapshot(
                index,
                hook=values[0],
                hold=values[1],
                shares=values[2],
                saves=values[3],
                follows=values[4],
            )
        # These attractive but non-comparable rows must never enter the cohort.
        self.seed_evaluation_snapshot(
            50,
            hook=0.05,
            hold=0.05,
            shares=0,
            saves=0,
            follows=0,
            lane="motivation",
        )
        self.seed_evaluation_snapshot(
            51,
            hook=0.05,
            hold=0.05,
            shares=0,
            saves=0,
            follows=0,
            duration_seconds=45,
        )
        candidate = self.seed_evaluation_snapshot(
            90,
            hook=0.85,
            hold=0.88,
            shares=30,
            saves=20,
            follows=10,
            candidate=True,
        )
        first = self.analytics.evaluate_editorial_feedback(
            candidate,
            idempotency_key="evaluate-winner",
            now=T0 + timedelta(hours=1),
        )
        replay = self.analytics.evaluate_editorial_feedback(
            candidate,
            idempotency_key="evaluate-winner",
            now=T0 + timedelta(hours=2),
        )
        same_evidence = self.analytics.evaluate_editorial_feedback(
            candidate,
            idempotency_key="evaluate-winner-second-key",
            now=T0 + timedelta(hours=3),
        )
        self.assertEqual(first, replay)
        self.assertEqual(
            first["recommendation"]["id"], same_evidence["recommendation"]["id"]
        )
        self.assertFalse(same_evidence["created"])
        recommendation = first["recommendation"]
        self.assertTrue(recommendation["evaluation"]["winner"])
        self.assertEqual(recommendation["evaluation"]["cohort_count"], 5)
        self.assertEqual(recommendation["recommendations"]["maximum_followups"], 2)
        self.assertLessEqual(len(recommendation["recommendations"]["actions"]), 2)
        self.assertFalse(recommendation["recommendations"]["automatic_publish"])
        self.assertFalse(first["automatic_mutation_performed"])
        self.assertEqual(
            set(action["signal"] for action in recommendation["recommendations"]["actions"])
            - {"hook", "hold", "value", "conversion"},
            set(),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            [
                "feedback-evaluate",
                candidate,
                "--minimum-cohort",
                "5",
                "--idempotency-key",
                "evaluate-winner-cli",
                "--db",
                str(self.db_path),
            ],
            out=stdout,
            err=stderr,
        )
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(
            json.loads(stdout.getvalue())["recommendation"]["id"],
            recommendation["id"],
        )

    def test_editorial_nonwinner_gets_tests_but_no_followups(self) -> None:
        for index in range(1, 6):
            self.seed_evaluation_snapshot(
                index,
                hook=0.65 + index / 100,
                hold=0.70 + index / 100,
                shares=20 + index,
                saves=10 + index,
                follows=5 + index,
            )
        candidate = self.seed_evaluation_snapshot(
            90,
            hook=0.25,
            hold=0.30,
            shares=1,
            saves=0,
            follows=0,
            candidate=True,
        )
        result = self.analytics.evaluate_editorial_feedback(
            candidate,
            idempotency_key="evaluate-nonwinner",
            now=T0 + timedelta(hours=1),
        )["recommendation"]
        self.assertFalse(result["evaluation"]["winner"])
        self.assertEqual(result["recommendations"]["status"], "nonwinner")
        self.assertEqual(result["recommendations"]["maximum_followups"], 0)
        self.assertLessEqual(len(result["recommendations"]["actions"]), 2)

    def test_policy_event_blocks_winner_and_all_followups(self) -> None:
        for index in range(1, 6):
            self.seed_evaluation_snapshot(
                index,
                hook=0.4 + index / 100,
                hold=0.5 + index / 100,
                shares=5 + index,
                saves=2 + index,
                follows=1 + index,
            )
        candidate = self.seed_evaluation_snapshot(
            90,
            hook=0.9,
            hold=0.9,
            shares=40,
            saves=30,
            follows=20,
            candidate=True,
            policy_events=[
                {"kind": "copyright", "status": "claim", "notes": "claim received"}
            ],
        )
        result = self.analytics.evaluate_editorial_feedback(
            candidate,
            idempotency_key="evaluate-safety-blocked",
            now=T0 + timedelta(hours=1),
        )["recommendation"]
        self.assertFalse(result["evaluation"]["winner"])
        self.assertFalse(result["evaluation"]["safety_clear"])
        self.assertEqual(result["recommendations"]["status"], "safety_blocked")
        self.assertEqual(result["recommendations"]["maximum_followups"], 0)
        self.assertEqual(result["recommendations"]["actions"], [])
        self.assertIn("rights_confidence", result["recommendations"]["immutable_boundaries"])

    def test_cli_exposes_metrics_and_outbox_without_sending(self) -> None:
        metric_path = self.root / "metric.json"
        metric_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "event_id": "evt-cli-001",
                    "job_id": "job_analytics_001",
                    "lane": "health",
                    "stage": "qc",
                    "status": "succeeded",
                    "occurred_at": "2026-08-29T08:00:00Z",
                    "duration_seconds": 5,
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            [
                "metrics-record",
                str(metric_path),
                "--idempotency-key",
                "cli-metric",
                "--db",
                str(self.db_path),
            ],
            out=stdout,
            err=stderr,
        )
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["metric"]["stage"], "qc")

        stdout = io.StringIO()
        code = main(
            ["outbox-list", "--db", str(self.db_path)], out=stdout, err=stderr
        )
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(stdout.getvalue())["external_send_performed"])


if __name__ == "__main__":
    unittest.main()
