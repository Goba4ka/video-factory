from __future__ import annotations

import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

FACTORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_ROOT / "src"))

from video_factory.errors import (  # noqa: E402
    IdempotencyConflictError,
    LeaseConflictError,
    ValidationError,
)
from video_factory.queue import Dispatcher  # noqa: E402
from video_factory.validators import canonical_json, digest_text  # noqa: E402


T0 = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)


class DispatcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Path(self.temporary.name) / "factory.sqlite3"
        self.queue = Dispatcher(self.db)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def enqueue(self, key: str, **overrides):
        values = {
            "role": "editor",
            "pod": "space_technology",
            "kind": "edit",
            "payload": {"key": key},
            "idempotency_key": key,
            "now": T0,
        }
        values.update(overrides)
        return self.queue.enqueue(**values)["task"]

    def claim(self, key: str, **overrides):
        values = {
            "worker_id": f"worker-{key}",
            "role": "editor",
            "idempotency_key": key,
            "lease_seconds": 30,
            "now": T0,
        }
        values.update(overrides)
        return self.queue.claim(**values)

    def test_enqueue_is_idempotent_but_rejects_key_reuse(self) -> None:
        first = self.queue.enqueue(
            role="editor",
            pod="space_technology",
            kind="edit",
            payload={"v": 1},
            idempotency_key="same",
            now=T0,
        )
        replay = self.queue.enqueue(
            role="editor",
            pod="space_technology",
            kind="edit",
            payload={"v": 1},
            idempotency_key="same",
            now=T0 + timedelta(days=1),
        )
        self.assertEqual(first, replay)
        with self.assertRaises(IdempotencyConflictError):
            self.queue.enqueue(
                role="editor",
                pod="space_technology",
                kind="edit",
                payload={"v": 2},
                idempotency_key="same",
                now=T0,
            )

    def test_priority_dependencies_completion_and_fencing(self) -> None:
        low = self.enqueue("low", priority=0)
        high = self.enqueue("high", priority=10)
        dependent = self.enqueue(
            "dependent",
            role="qc",
            kind="qc",
            dependency_task_id=high["id"],
        )
        claimed = self.claim("claim-high")
        self.assertEqual(claimed["task"]["id"], high["id"])
        self.assertIsNone(
            self.claim("blocked-dependency", role="qc")["task"]
        )
        token = claimed["task"]["lease_token"]
        completed = self.queue.complete(
            high["id"],
            lease_token=token,
            result={"asset": "approved.mp4"},
            idempotency_key="complete-high",
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(completed["task"]["status"], "succeeded")
        self.assertEqual(
            completed,
            self.queue.complete(
                high["id"],
                lease_token=token,
                result={"asset": "approved.mp4"},
                idempotency_key="complete-high",
                now=T0 + timedelta(hours=1),
            ),
        )
        with self.assertRaises(LeaseConflictError):
            self.queue.complete(
                high["id"],
                lease_token=token,
                result={},
                idempotency_key="stale-completion",
                now=T0 + timedelta(seconds=2),
            )
        self.assertEqual(self.claim("claim-low")["task"]["id"], low["id"])
        self.assertEqual(
            self.claim("claim-dependent", role="qc")["task"]["id"], dependent["id"]
        )

    def test_declared_safety_contract_cannot_complete_without_passed_artifact(self) -> None:
        research_task = self.enqueue(
            "medical-research",
            role="research",
            pod="health",
            kind="research_job",
            payload={
                "idea_id": "health_test_001",
                "required_result_contract": "claim_ledger",
            },
        )
        research_claim = self.claim("claim-medical-research", role="research")
        claim_ledger = {
            "schema_version": "1.0.0",
            "idea_id": "health_test_001",
            "sources": [
                {
                    "source_id": "src_health_001",
                    "url": "https://www.who.int/example",
                    "publisher": "WHO",
                    "retrieved_at": "2026-08-27T08:00:00Z",
                    "primary": True,
                }
            ],
            "claims": [
                {
                    "claim_id": "claim_health_001",
                    "text": "Supported health claim.",
                    "source_ids": ["src_health_001"],
                    "support": "direct",
                    "risk": "green",
                }
            ],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "review_notes": [],
            },
        }
        self.queue.complete(
            research_task["id"],
            lease_token=research_claim["task"]["lease_token"],
            result={"artifact": claim_ledger},
            idempotency_key="complete-medical-research",
            now=T0 + timedelta(seconds=1),
        )
        task = self.enqueue(
            "medical-gate",
            role="medical_review",
            pod="health",
            kind="medical_review_job",
            dependency_task_id=research_task["id"],
            payload={
                "lane_id": "health",
                "idea_id": "health_test_001",
                "risk_profile": "medical_safety",
                "required_result_contract": "safety_gate_report",
            },
        )
        claimed = self.claim("claim-medical", role="medical_review")
        token = claimed["task"]["lease_token"]
        with self.assertRaisesRegex(ValidationError, "result.artifact"):
            self.queue.complete(
                task["id"],
                lease_token=token,
                result={},
                idempotency_key="complete-medical-empty",
                now=T0 + timedelta(seconds=2),
            )
        artifact = {
            "schema_version": "1.0.0",
            "job_id": "job_health_test",
            "idea_id": "health_test_001",
            "lane": "health",
            "gate_type": "medical_safety",
            "checked_at": "2026-08-27T08:00:00Z",
            "reviewer": "medical-review-agent",
            "source_ids_checked": ["src_health_001"],
            "findings": [],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "review_notes": [],
            },
        }
        wrong_gate = {**artifact, "gate_type": "war_sensitivity"}
        with self.assertRaisesRegex(ValidationError, "does not match medical_review"):
            self.queue.complete(
                task["id"],
                lease_token=token,
                result={"artifact": wrong_gate},
                idempotency_key="complete-medical-wrong-gate",
                now=T0 + timedelta(seconds=3),
            )
        unknown_source = {**artifact, "source_ids_checked": ["src_unknown_001"]}
        with self.assertRaisesRegex(ValidationError, "unknown claim-ledger sources"):
            self.queue.complete(
                task["id"],
                lease_token=token,
                result={"artifact": unknown_source},
                idempotency_key="complete-medical-unknown-source",
                now=T0 + timedelta(seconds=3),
            )
        completed = self.queue.complete(
            task["id"],
            lease_token=token,
            result={"artifact": artifact},
            idempotency_key="complete-medical-passed",
            now=T0 + timedelta(seconds=4),
        )
        self.assertEqual(completed["task"]["status"], "succeeded")

    def test_human_gate_requires_attributable_approval(self) -> None:
        task = self.enqueue(
            "human-final",
            role="final_review",
            pod="motivation",
            kind="final_review_job",
            payload={"human_gate": True},
        )
        claimed = self.claim("claim-human-final", role="final_review")
        token = claimed["task"]["lease_token"]
        with self.assertRaisesRegex(ValidationError, "human_approval"):
            self.queue.complete(
                task["id"],
                lease_token=token,
                result={},
                idempotency_key="complete-human-empty",
                now=T0 + timedelta(seconds=1),
            )
        completed = self.queue.complete(
            task["id"],
            lease_token=token,
            result={
                "human_approval": {
                    "approved": True,
                    "approved_by": "owner",
                    "approved_at": "2026-08-27T08:00:01Z",
                }
            },
            idempotency_key="complete-human-approved",
            now=T0 + timedelta(seconds=2),
        )
        self.assertEqual(completed["task"]["status"], "succeeded")

    def test_qc_and_publish_are_bound_to_render_and_metadata_checksums(self) -> None:
        render_path = Path(self.temporary.name) / "final.mp4"
        render_path.write_bytes(b"render-bytes")
        render_sha = digest_text("render-bytes")
        render_task = self.enqueue(
            "bound-render",
            role="render",
            pod="motivation",
            kind="render_job",
            payload={"required_result_contract": "render_manifest"},
        )
        render_claim = self.claim("claim-bound-render", role="render")
        render_artifact = {
            "schema_version": "1.0.0",
            "render_id": "render_bound_001",
            "job_id": "job_bound_001",
            "composition": "index.html",
            "output": "final.mp4",
            "output_sha256": render_sha,
            "technical": {
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 60,
                "video_codec": "h264",
                "audio_codec": "aac",
                "audio_sample_rate_hz": 48000,
            },
            "input_hashes": [{"path": "index.html", "sha256": "b" * 64}],
        }
        with self.assertRaisesRegex(ValidationError, "actual output bytes"):
            self.queue.complete(
                render_task["id"],
                lease_token=render_claim["task"]["lease_token"],
                result={
                    "artifact": {**render_artifact, "output_sha256": "a" * 64},
                    "output_path": str(render_path),
                },
                idempotency_key="complete-bound-render-wrong-hash",
                now=T0 + timedelta(seconds=1),
            )
        actual_probe = {
            "width": 1080,
            "height": 1920,
            "fps": 30.0,
            "duration_seconds": 60.0,
            "video_codec": "h264",
            "audio_codec": "aac",
            "audio_sample_rate_hz": 48000,
        }
        with patch(
            "video_factory.queue._probe_render_output",
            return_value={**actual_probe, "audio_sample_rate_hz": 44100},
        ):
            with self.assertRaisesRegex(ValidationError, "audio_sample_rate_hz"):
                self.queue.complete(
                    render_task["id"],
                    lease_token=render_claim["task"]["lease_token"],
                    result={"artifact": render_artifact, "output_path": str(render_path)},
                    idempotency_key="complete-bound-render-wrong-sample-rate",
                    now=T0 + timedelta(seconds=1),
                )
        with patch("video_factory.queue._probe_render_output", return_value=actual_probe):
            self.queue.complete(
                render_task["id"],
                lease_token=render_claim["task"]["lease_token"],
                result={"artifact": render_artifact, "output_path": str(render_path)},
                idempotency_key="complete-bound-render",
                now=T0 + timedelta(seconds=1),
            )

        qc_task = self.enqueue(
            "bound-qc",
            role="qc",
            pod="motivation",
            kind="qc_job",
            dependency_task_id=render_task["id"],
            payload={"required_result_contract": "qc_report"},
        )
        qc_claim = self.claim("claim-bound-qc", role="qc")
        qc_artifact = {
            "schema_version": "1.0.0",
            "job_id": "job_bound_001",
            "render_id": "render_bound_001",
            "technical": {"audio_sample_rate_hz": 48000},
            "checks": [
                {
                    "check_id": "tech-pass",
                    "category": "technical",
                    "status": "pass",
                    "evidence": "ffprobe passed",
                }
            ],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "blocking_check_ids": [],
                "review_notes": [],
            },
        }
        self.queue.complete(
            qc_task["id"],
            lease_token=qc_claim["task"]["lease_token"],
            result={"artifact": qc_artifact},
            idempotency_key="complete-bound-qc",
            now=T0 + timedelta(seconds=2),
        )

        destinations = [
            {
                "platform": "youtube_shorts",
                "account_id": "account-1",
                "caption": "caption",
                "visibility": "private",
                "status": "pending",
            }
        ]
        disclosures = {
            "ai_generated": False,
            "altered_or_synthetic": False,
            "paid_promotion": False,
            "notes": [],
        }
        metadata_sha = digest_text(
            canonical_json({"destinations": destinations, "disclosures": disclosures})
        )
        approval = {
            "approved": True,
            "approved_by": "owner",
            "approved_at": "2026-08-27T08:00:03Z",
            "render_sha256": render_sha,
            "metadata_sha256": metadata_sha,
        }
        final_task = self.enqueue(
            "bound-final",
            role="final_review",
            pod="motivation",
            kind="final_review_job",
            dependency_task_id=qc_task["id"],
            payload={"human_gate": True, "checksum_bound": True},
        )
        final_claim = self.claim("claim-bound-final", role="final_review")
        self.queue.complete(
            final_task["id"],
            lease_token=final_claim["task"]["lease_token"],
            result={"human_approval": approval},
            idempotency_key="complete-bound-final",
            now=T0 + timedelta(seconds=3),
        )

        publish_task = self.enqueue(
            "bound-publish",
            role="publisher",
            pod="motivation",
            kind="publisher_job",
            dependency_task_id=final_task["id"],
            payload={"required_result_contract": "publish_manifest"},
        )
        publish_claim = self.claim("claim-bound-publish", role="publisher")
        manifest = {
            "schema_version": "1.0.0",
            "job_id": "job_bound_001",
            "render_id": "render_bound_001",
            "qc_report": "qc/QC_REPORT.json",
            "human_approval": approval,
            "destinations": destinations,
            "disclosures": disclosures,
        }
        completed = self.queue.complete(
            publish_task["id"],
            lease_token=publish_claim["task"]["lease_token"],
            result={"artifact": manifest},
            idempotency_key="complete-bound-publish",
            now=T0 + timedelta(seconds=4),
        )
        self.assertEqual(completed["task"]["status"], "succeeded")

    def test_voice_manifest_is_job_bound_and_rights_fail_closed(self) -> None:
        task = self.enqueue(
            "bound-voice",
            role="voice",
            kind="voice_job",
            payload={
                "job_id": "job_voice_001",
                "required_result_contract": "voice_manifest",
            },
        )
        claimed = self.claim("claim-bound-voice", role="voice")
        artifact = {
            "schema_version": "1.0.0",
            "provider": "fish_audio",
            "job_id": "job_voice_001",
            "video_id": "job_voice_001",
            "generation_no": 1,
            "generation_limit": 2,
            "request_hash": "a" * 64,
            "text_sha256": "b" * 64,
            "text_bytes": 10,
            "model": "s2.1-pro-free",
            "reference_id": "voice-001",
            "voice_rights_status": "user_confirmation_required",
            "immutable_output_path": "voice.wav",
            "output_sha256": "c" * 64,
            "output_bytes": 1000,
            "audio": {
                "sample_rate_hz": 44100,
                "channels": 1,
                "sample_width_bits": 16,
                "frames": 4410,
                "duration_seconds": 0.1,
            },
            "render_target_sample_rate_hz": 48000,
            "estimated_cost_usd": 0,
            "retry_reason": None,
            "defect_reference": None,
            "defect_sha256": None,
            "retry_of_request_hash": None,
            "retry_of_output_sha256": None,
            "retry_of_generation_status": None,
            "created_at": "2026-08-27T08:00:00Z",
            "completed_at": "2026-08-27T08:00:01Z",
        }
        token = claimed["task"]["lease_token"]
        with self.assertRaisesRegex(ValidationError, "not bound"):
            self.queue.complete(
                task["id"],
                lease_token=token,
                result={
                    "artifact": {
                        **artifact,
                        "video_id": "job_voice_002",
                    }
                },
                idempotency_key="complete-voice-wrong-job",
                now=T0 + timedelta(seconds=1),
            )
        with self.assertRaisesRegex(ValidationError, "rights are not approved"):
            self.queue.complete(
                task["id"],
                lease_token=token,
                result={"artifact": artifact},
                idempotency_key="complete-voice-unconfirmed-rights",
                now=T0 + timedelta(seconds=2),
            )
        approval = {
            "schema_version": "1.0.0",
            "job_id": "job_voice_001",
            "reference_id": "voice-001",
            "voice_rights_status": "approved_owned_voice",
            "basis": "voice_owner_confirmation",
            "evidence": "Owner confirmed voice and commercial publication rights.",
            "approved": True,
            "approved_by": "owner",
            "approved_at": "2026-08-27T08:00:02Z",
        }
        with self.assertRaisesRegex(ValidationError, "voice_rights_approval"):
            self.queue.complete(
                task["id"],
                lease_token=token,
                result={
                    "artifact": {
                        **artifact,
                        "voice_rights_status": "approved_owned_voice",
                    }
                },
                idempotency_key="complete-voice-missing-rights-approval",
                now=T0 + timedelta(seconds=3),
            )
        completed = self.queue.complete(
            task["id"],
            lease_token=token,
            result={
                "artifact": {
                    **artifact,
                    "voice_rights_status": "approved_owned_voice",
                },
                "voice_rights_approval": approval,
            },
            idempotency_key="complete-voice-approved-rights",
            now=T0 + timedelta(seconds=4),
        )
        self.assertEqual(completed["task"]["status"], "succeeded")

    def test_source_audio_manifest_is_motivation_only_and_completes_without_tts(self) -> None:
        transcript = "Продолжай, даже когда настроение закончилось."
        artifact = {
            "schema_version": "1.0.0",
            "job_id": "job_motivation_001",
            "lane": "motivation",
            "audio_asset_id": "audio_source_001",
            "source_video_uri_or_path": "C:/media/source.mp4",
            "source_in_seconds": 5.2,
            "source_out_seconds": 18.7,
            "speaker_name": None,
            "transcript": transcript,
            "rights_status": "internal_prototype",
            "rights_evidence": None,
            "original_audio_only": True,
            "tts": False,
            "extracted_audio_path": "audio/source.wav",
            "checksums": {
                "source_video_sha256": "a" * 64,
                "extracted_audio_sha256": "b" * 64,
                "transcript_sha256": digest_text(transcript),
            },
            "created_at": "2026-08-28T08:00:00Z",
        }
        task = self.enqueue(
            "source-audio-motivation",
            role="source_audio",
            pod="motivation",
            kind="source_audio_job",
            payload={
                "job_id": "job_motivation_001",
                "lane_id": "motivation",
                "required_result_contract": "source_audio_manifest",
            },
        )
        claimed = self.claim("claim-source-audio", role="source_audio")
        completed = self.queue.complete(
            task["id"],
            lease_token=claimed["task"]["lease_token"],
            result={"artifact": artifact},
            idempotency_key="complete-source-audio",
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(completed["task"]["status"], "succeeded")

        wrong_lane = self.enqueue(
            "source-audio-health",
            role="source_audio",
            pod="health",
            kind="source_audio_job",
            payload={
                "job_id": "job_health_001",
                "lane_id": "health",
                "required_result_contract": "source_audio_manifest",
            },
        )
        wrong_claim = self.claim("claim-source-audio-health", role="source_audio")
        with self.assertRaisesRegex(ValidationError, "restricted to the motivation lane"):
            self.queue.complete(
                wrong_lane["id"],
                lease_token=wrong_claim["task"]["lease_token"],
                result={"artifact": {**artifact, "job_id": "job_health_001"}},
                idempotency_key="complete-source-audio-health",
                now=T0 + timedelta(seconds=2),
            )

    def test_retry_backoff_attempt_history_and_dead_letter(self) -> None:
        task = self.enqueue("retry", max_attempts=2, retry_backoff_seconds=10)
        first = self.claim("claim-1")["task"]
        failed = self.queue.fail(
            task["id"],
            lease_token=first["lease_token"],
            error={"code": "temporary"},
            idempotency_key="fail-1",
            now=T0 + timedelta(seconds=1),
        )
        self.assertTrue(failed["retried"])
        self.assertEqual(failed["task"]["available_at"], "2026-08-27T08:00:11.000Z")
        self.assertIsNone(
            self.claim("too-early", now=T0 + timedelta(seconds=10))["task"]
        )
        second = self.claim("claim-2", now=T0 + timedelta(seconds=11))["task"]
        dead = self.queue.fail(
            task["id"],
            lease_token=second["lease_token"],
            error={"code": "still_bad"},
            idempotency_key="fail-2",
            now=T0 + timedelta(seconds=12),
        )
        self.assertFalse(dead["retried"])
        self.assertEqual(dead["task"]["status"], "dead")
        detail = self.queue.status(task["id"], now=T0 + timedelta(seconds=12))
        self.assertEqual([item["status"] for item in detail["attempts"]], ["failed", "failed"])

    def test_expired_lease_is_recovered_and_old_worker_is_fenced(self) -> None:
        task = self.enqueue("expires", max_attempts=2)
        first = self.claim("first", lease_seconds=5)["task"]
        recovered = self.queue.recover_expired(now=T0 + timedelta(seconds=5))
        self.assertEqual(recovered["items"], [{
            "task_id": task["id"], "attempt_no": 1, "status": "queued"
        }])
        with self.assertRaises(LeaseConflictError):
            self.queue.complete(
                task["id"],
                lease_token=first["lease_token"],
                result={},
                idempotency_key="late-worker",
                now=T0 + timedelta(seconds=6),
            )
        second = self.claim("second", now=T0 + timedelta(seconds=6))["task"]
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        self.assertEqual(second["attempt_count"], 2)
        detail = self.queue.status(task["id"], now=T0 + timedelta(seconds=6))
        self.assertEqual(detail["attempts"][0]["status"], "expired")

    def test_role_and_pod_wip_limits_are_atomic_under_concurrency(self) -> None:
        self.queue.configure_limit(role="editor", max_leased=2, now=T0)
        self.queue.configure_limit(pod="space_technology", max_leased=2, now=T0)
        for index in range(5):
            self.enqueue(f"parallel-{index}")

        def claim_once(index: int):
            queue = Dispatcher(self.db)
            return queue.claim(
                worker_id=f"parallel-worker-{index}",
                role="editor",
                pod="space_technology",
                lease_seconds=30,
                idempotency_key=f"parallel-claim-{index}",
                now=T0,
            )["task"]

        with ThreadPoolExecutor(max_workers=5) as pool:
            claimed = list(pool.map(claim_once, range(5)))
        actual = [item for item in claimed if item is not None]
        self.assertEqual(len(actual), 2)
        self.assertEqual(len({item["id"] for item in actual}), 2)
        status = self.queue.status(now=T0)
        self.assertEqual(status["tasks"], {"leased": 2, "queued": 3})

    def test_dead_dependency_is_propagated(self) -> None:
        parent = self.enqueue("parent", max_attempts=1)
        child = self.enqueue(
            "child", role="qc", kind="qc", dependency_task_id=parent["id"]
        )
        leased = self.claim("parent-claim")["task"]
        self.queue.fail(
            parent["id"],
            lease_token=leased["lease_token"],
            error={"code": "permanent"},
            idempotency_key="parent-fail",
            now=T0 + timedelta(seconds=1),
        )
        claim = self.claim("child-claim", role="qc", now=T0 + timedelta(seconds=1))
        self.assertIsNone(claim["task"])
        self.assertEqual(claim["dead_dependencies"], 1)
        self.assertEqual(self.queue.status(child["id"])["task"]["status"], "dead")

    def test_simulate_fifteen_video_day_obeys_default_wip(self) -> None:
        result = self.queue.simulate_day(
            target=15,
            pods=["space_technology", "nature_animals", "people_culture"],
            roles=[
                "research", "rights", "script", "editor", "render", "qc",
                "final_review", "publisher",
            ],
            idempotency_key="day-15",
            now=T0,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["completed_videos"], 15)
        self.assertEqual(result["task_counts"], {"succeeded": 120})
        self.assertEqual(
            result["max_observed_role_wip"],
            {
                "research": 4,
                "rights": 2,
                "script": 3,
                "editor": 2,
                "render": 1,
                "qc": 1,
                "final_review": 1,
                "publisher": 1,
            },
        )
        self.assertFalse(result["background_processes_started"])
        self.assertEqual(
            result,
            self.queue.simulate_day(
                target=15,
                pods=["space_technology", "nature_animals", "people_culture"],
                roles=[
                    "research", "rights", "script", "editor", "render", "qc",
                    "final_review", "publisher",
                ],
                idempotency_key="day-15",
                now=T0,
            ),
        )


if __name__ == "__main__":
    unittest.main()
