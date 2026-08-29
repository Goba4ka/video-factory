from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import wave
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
from video_factory.media_freeze import freeze_explicit_media  # noqa: E402
from video_factory.validators import canonical_json, digest_text  # noqa: E402


T0 = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)


def pexels_discovery_artifact(job_id: str, lane: str = "health") -> dict:
    landing_url = "https://www.pexels.com/video/sample-4812205/"
    creator_url = "https://www.pexels.com/@test-creator/"
    download_url = "https://videos.pexels.com/video-files/4812205/1080.mp4"
    attribution = f"Video by Test Creator on Pexels: {landing_url}"
    return {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "lane": lane,
        "provider": "pexels",
        "generated_at": "2026-08-29T12:00:00Z",
        "query": {
            "text": "здоровый сон",
            "orientation": "portrait",
            "size": "medium",
            "locale": "ru-RU",
            "page": 1,
            "per_page": 20,
            "minimum_width": 720,
            "minimum_height": 1280,
        },
        "cache": {
            "cache_key": "a" * 64,
            "hit": False,
            "ttl_seconds": 86400,
            "fetched_at": "2026-08-29T12:00:00Z",
            "expires_at": "2026-08-30T12:00:00Z",
            "payload_sha256": "b" * 64,
        },
        "rate_limit": {
            "local_hourly_limit": 180,
            "local_requests_in_window": 1,
            "local_window_started_at": "2026-08-29T12:00:00Z",
            "provider_limit": 200,
            "provider_remaining": 199,
            "provider_reset_at": "2026-08-29T13:00:00Z",
        },
        "candidates": [
            {
                "asset_id": "pexels_video_4812205",
                "provider_asset_id": "4812205",
                "media_type": "video",
                "duration_seconds": 14.0,
                "width": 1080,
                "height": 1920,
                "landing_url": landing_url,
                "thumbnail_url": "https://images.pexels.com/videos/4812205/free-video.jpg",
                "selected_file": {
                    "provider_file_id": "1002",
                    "download_url": download_url,
                    "content_type": "video/mp4",
                    "quality": "hd",
                    "width": 1080,
                    "height": 1920,
                    "fps": 30.0,
                },
                "ledger": {
                    "source": {
                        "provider": "pexels",
                        "provider_asset_id": "4812205",
                        "landing_url": landing_url,
                        "download_url": download_url,
                        "creator_name": "Test Creator",
                        "creator_url": creator_url,
                        "retrieved_at": "2026-08-29T12:00:00Z",
                    },
                    "license": {
                        "name": "Pexels License",
                        "url": "https://www.pexels.com/license/",
                        "commercial_use": True,
                        "modification_allowed": True,
                        "attribution_required_by_license": False,
                        "api_linkback_required": True,
                    },
                    "attribution": {
                        "apply": True,
                        "text": attribution,
                        "source_url": landing_url,
                        "creator_url": creator_url,
                    },
                    "clearance": {
                        "rights_status": "human_review",
                        "model_release": "unknown",
                        "property_release": "unknown",
                        "requires_item_level_review": True,
                        "review_reasons": ["Item-level rights review required."],
                    },
                },
            }
        ],
        "decision": {
            "discovery_passed": True,
            "rights_cleared": False,
            "needs_human_review": True,
            "candidate_count": 1,
            "duplicates_removed": 0,
        },
    }


def rights_from_discovery(discovery: dict, idea_id: str) -> dict:
    candidate = discovery["candidates"][0]
    source = candidate["ledger"]["source"]
    attribution = candidate["ledger"]["attribution"]
    return {
        "schema_version": "1.0.0",
        "idea_id": idea_id,
        "assets": [
            {
                "asset_id": candidate["asset_id"],
                "local_path": None,
                "download_url": candidate["selected_file"]["download_url"],
                "landing_url": candidate["landing_url"],
                "creator": source["creator_name"],
                "license": "Pexels License",
                "license_url": "https://www.pexels.com/license/",
                "license_receipt": "rights/pexels-video-4812205.json",
                "retrieved_at": "2026-08-29T12:05:00Z",
                "commercial_use": True,
                "modification_allowed": True,
                "attribution_required": True,
                "attribution_text": attribution["text"],
                "model_release": "not_applicable",
                "property_release": "not_applicable",
                "platforms": ["youtube_shorts", "instagram_reels", "tiktok"],
                "territories": ["worldwide"],
                "expires_at": None,
                "rights_status": "approved",
                "notes": "Fixture item-level review.",
            }
        ],
        "decision": {
            "passed": True,
            "needs_human_review": False,
            "missing_asset_ids": [],
            "review_notes": [],
        },
    }


def rights_human_approval(rights: dict) -> dict:
    return {
        "approved": True,
        "approved_by": "rights-reviewer@example.test",
        "approved_at": "2026-08-29T12:06:00Z",
        "approval_note": "Verified the exact asset, license receipt, releases, and reuse scope.",
        "rights_manifest_sha256": digest_text(canonical_json(rights)),
        "reviewed_asset_ids": [item["asset_id"] for item in rights["assets"]],
    }


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

    def seed_preview_gate(
        self, *, job_id: str, lane_id: str
    ) -> tuple[dict, list[dict[str, str]]]:
        project_root = Path(self.temporary.name) / f"project-{job_id}"
        (project_root / "assets" / "media").mkdir(parents=True)
        (project_root / "assets" / "audio").mkdir(parents=True)
        (project_root / "assets" / "vendor").mkdir(parents=True)
        (project_root / "index.html").write_bytes(b"<html></html>")
        (project_root / "assets" / "media" / "clip.mp4").write_bytes(b"clip")
        (project_root / "assets" / "audio" / "narration.wav").write_bytes(
            b"RIFF" + b"licensed-source-audio" * 4
        )
        (project_root / "assets" / "vendor" / "gsap.min.js").write_bytes(b"gsap")
        files = []
        for path in sorted(project_root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_file():
                files.append(
                    {
                        "path": path.relative_to(project_root).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size_bytes": path.stat().st_size,
                    }
                )
        media = next(item for item in files if item["path"].endswith("clip.mp4"))
        project = {
            "schema_version": "1.0.0",
            "project_id": f"project-{job_id}",
            "job_id": job_id,
            "idea_id": "idea_bound_001",
            "lane_id": lane_id,
            "project_root": str(project_root.resolve()),
            "entrypoint": "index.html",
            "composition": {
                "composition_id": "main",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 60,
            },
            "bindings": {
                "shotlist": {
                    "contract": "shotlist",
                    "schema_version": "1.0.0",
                    "idea_id": "idea_bound_001",
                    "sha256": "1" * 64,
                },
                "script_package": {
                    "contract": "script_package",
                    "schema_version": "1.0.0",
                    "idea_id": "idea_bound_001",
                    "job_id": job_id,
                    "sha256": "2" * 64,
                },
                "frozen_media_manifest": {
                    "contract": "frozen_media_manifest",
                    "schema_version": "1.0.0",
                    "idea_id": "idea_bound_001",
                    "job_id": job_id,
                    "sha256": "3" * 64,
                },
                "authoritative_audio": {
                    "contract": (
                        "source_audio_manifest"
                        if lane_id == "motivation"
                        else "voice_manifest"
                    ),
                    "schema_version": "1.0.0",
                    "job_id": job_id,
                    "sha256": "4" * 64,
                    "audio_sha256": next(
                        item["sha256"]
                        for item in files
                        if item["path"] == "assets/audio/narration.wav"
                    ),
                    "project_path": "assets/audio/narration.wav",
                    "size_bytes": next(
                        item["size_bytes"]
                        for item in files
                        if item["path"] == "assets/audio/narration.wav"
                    ),
                },
            },
            "assets": [
                {
                    "asset_id": "asset_bound_001",
                    "frozen_path": "asset/source.mp4",
                    "project_path": media["path"],
                    "sha256": media["sha256"],
                    "size_bytes": media["size_bytes"],
                    "content_type": "video/mp4",
                    "shot_ids": ["shot_bound_001"],
                }
            ],
            "files": files,
            "project_tree_sha256": digest_text(canonical_json(files)),
            "preview": {
                "status": "ready_for_human_review",
                "render_authorized": False,
                "human_approval_required": True,
            },
        }
        receipt = Path(self.temporary.name) / f"check-{job_id}.json"
        receipt.write_text(
            json.dumps(
                {
                    "ok": True,
                    "project_tree_sha256": project["project_tree_sha256"],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        approval = {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "project_id": project["project_id"],
            "approved": True,
            "approved_by": "queue-test-reviewer",
            "approved_at": "2026-08-29T12:00:00Z",
            "project_tree_sha256": project["project_tree_sha256"],
            "project_manifest_sha256": digest_text(canonical_json(project)),
            "check_receipt_path": str(receipt.resolve()),
            "check_receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            "studio_url": "http://127.0.0.1:3002/#project/bound",
            "review_notes": ["Fixture human preview approval."],
        }
        compiler = self.enqueue(
            f"seed-compiler-{job_id}",
            role="compiler",
            pod=lane_id,
            kind="compiler_job",
            payload={"required_result_contract": "project_manifest"},
        )
        preview = self.enqueue(
            f"seed-preview-{job_id}",
            role="preview_review",
            pod=lane_id,
            kind="preview_review_job",
            dependency_task_id=compiler["id"],
            payload={"required_result_contract": "preview_approval"},
        )
        connection = self.queue.db.connect()
        try:
            connection.execute(
                "UPDATE tasks SET status='succeeded', result_json=? WHERE id=?",
                (canonical_json({"artifact": project}), compiler["id"]),
            )
            connection.execute(
                "UPDATE tasks SET status='succeeded', result_json=? WHERE id=?",
                (canonical_json({"artifact": approval}), preview["id"]),
            )
            connection.commit()
        finally:
            connection.close()
        inputs = [
            {
                "path": "project_manifest.json",
                "sha256": digest_text(canonical_json(project)),
            },
            {
                "path": "preview_approval.json",
                "sha256": digest_text(canonical_json(approval)),
            },
            *[
                {"path": f"project/{item['path']}", "sha256": item["sha256"]}
                for item in files
            ],
        ]
        return preview, inputs

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
                "human_gate": True,
                "human_qualification_required": True,
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
            "reviewer": "doctor@example.test",
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
        with self.assertRaisesRegex(ValidationError, "qualified human_approval"):
            self.queue.complete(
                task["id"],
                lease_token=token,
                result={"artifact": artifact},
                idempotency_key="complete-medical-without-human",
                now=T0 + timedelta(seconds=3),
            )
        completed = self.queue.complete(
            task["id"],
            lease_token=token,
            result={
                "artifact": artifact,
                "human_approval": {
                    "approved": True,
                    "approved_by": "doctor@example.test",
                    "approved_at": "2026-08-27T08:00:03Z",
                    "qualification": "licensed physician",
                    "approval_note": "Reviewed source support and safety wording.",
                },
            },
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
        preview_task, render_inputs = self.seed_preview_gate(
            job_id="job_bound_001", lane_id="motivation"
        )
        render_task = self.enqueue(
            "bound-render",
            role="render",
            pod="motivation",
            kind="render_job",
            dependency_task_id=preview_task["id"],
            payload={
                "job_id": "job_bound_001",
                "lane_id": "motivation",
                "required_result_contract": "render_manifest",
            },
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
            "input_hashes": render_inputs,
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

        evidence_root = Path(self.temporary.name) / "qc-evidence"
        evidence_root.mkdir()
        evidence_descriptors = {}
        for category in (
            "technical",
            "audio",
            "captions",
            "facts",
            "rights",
            "dedup",
            "policy",
            "visual",
        ):
            path = evidence_root / f"{category}.json"
            path.write_text(
                json.dumps({"category": category, "status": "pass"}, sort_keys=True),
                encoding="utf-8",
            )
            evidence_descriptors[category] = {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        contact_sheet = evidence_root / "contact-sheet.jpg"
        contact_sheet.write_bytes(b"contact-sheet-bytes")
        contact_descriptor = {
            "path": str(contact_sheet.resolve()),
            "sha256": hashlib.sha256(contact_sheet.read_bytes()).hexdigest(),
        }
        evidence_bundle = {
            "schema_version": "1.0.0",
            "job_id": "job_bound_001",
            "lane_id": "motivation",
            "render_id": "render_bound_001",
            "render_sha256": render_sha,
            "reports": [
                {
                    "category": category,
                    "artifact_sha256": "a" * 64,
                    "evidence": evidence_descriptors[category],
                }
                for category in evidence_descriptors
            ],
            "contact_sheet": contact_descriptor,
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "blocking_categories": [],
            },
            "created_at": "2026-08-29T08:00:02Z",
        }
        evidence_task = self.enqueue(
            "bound-qc-evidence",
            role="qc_evidence_gate",
            pod="motivation",
            kind="qc_evidence_gate_job",
            dependency_task_id=render_task["id"],
            payload={"required_result_contract": "qc_evidence_bundle"},
        )
        connection = self.queue.db.connect()
        try:
            connection.execute(
                "UPDATE tasks SET status='succeeded', result_json=? WHERE id=?",
                (canonical_json({"artifact": evidence_bundle}), evidence_task["id"]),
            )
            connection.commit()
        finally:
            connection.close()

        qc_task = self.enqueue(
            "bound-qc",
            role="qc",
            pod="motivation",
            kind="qc_job",
            dependency_task_id=evidence_task["id"],
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
                    "check_id": f"{category}-pass",
                    "category": category,
                    "status": "pass",
                    "evidence": (
                        f"{category} passed "
                        f"#sha256={evidence_descriptors[category]['sha256']}"
                    ),
                    "artifact": evidence_descriptors[category]["path"],
                }
                for category in (
                    "technical",
                    "audio",
                    "captions",
                    "facts",
                    "rights",
                    "dedup",
                    "policy",
                    "visual",
                )
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
            result={
                "artifact": qc_artifact,
                "evidence_sha256": {
                    category: descriptor["sha256"]
                    for category, descriptor in evidence_descriptors.items()
                },
                "visual_contact_sheet_sha256": contact_descriptor["sha256"],
            },
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

    def test_discovery_to_rights_completion_preserves_exact_provider_binding(self) -> None:
        def complete_chain(
            suffix: str,
            mutate=None,
        ) -> tuple[dict, dict, dict]:
            job_id = f"job_discovery_{suffix}"
            idea_id = f"idea_discovery_{suffix}"
            discovery = pexels_discovery_artifact(job_id)
            discovery_task = self.enqueue(
                f"discovery-{suffix}",
                role="media_discovery",
                pod="health",
                kind="media_discovery_job",
                payload={
                    "job_id": job_id,
                    "lane_id": "health",
                    "required_result_contract": "media_discovery_manifest",
                },
            )
            discovery_claim = self.claim(
                f"claim-discovery-{suffix}", role="media_discovery"
            )["task"]
            self.queue.complete(
                discovery_task["id"],
                lease_token=discovery_claim["lease_token"],
                result={"artifact": discovery},
                idempotency_key=f"complete-discovery-{suffix}",
                now=T0 + timedelta(seconds=1),
            )
            rights = rights_from_discovery(discovery, idea_id)
            if mutate is not None:
                mutate(rights)
            rights_task = self.enqueue(
                f"rights-discovery-{suffix}",
                role="rights",
                pod="health",
                kind="rights_job",
                dependency_task_id=discovery_task["id"],
                payload={
                    "job_id": job_id,
                    "idea_id": idea_id,
                    "lane_id": "health",
                    "required_result_contract": "rights_manifest",
                    "human_gate": True,
                    "rights_checksum_bound": True,
                },
            )
            rights_claim = self.claim(
                f"claim-rights-discovery-{suffix}", role="rights"
            )["task"]
            return rights_task, rights_claim, rights

        task, claim, rights = complete_chain("valid")
        invalid_approval = rights_human_approval(rights)
        invalid_approval["rights_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "exact rights_manifest"):
            self.queue.complete(
                task["id"],
                lease_token=claim["lease_token"],
                result={"artifact": rights, "human_approval": invalid_approval},
                idempotency_key="complete-rights-discovery-stale-approval",
                now=T0 + timedelta(seconds=2),
            )
        completed = self.queue.complete(
            task["id"],
            lease_token=claim["lease_token"],
            result={
                "artifact": rights,
                "human_approval": rights_human_approval(rights),
            },
            idempotency_key="complete-rights-discovery-valid",
            now=T0 + timedelta(seconds=2),
        )
        self.assertEqual(completed["task"]["status"], "succeeded")

        mutations = (
            (
                "download",
                lambda value: value["assets"][0].__setitem__(
                    "download_url", "https://videos.pexels.com/other.mp4"
                ),
                "differs from media discovery: download_url",
            ),
            (
                "unknown-id",
                lambda value: value["assets"][0].__setitem__(
                    "asset_id", "pexels_video_9999999"
                ),
                "absent from media discovery",
            ),
            (
                "attribution",
                lambda value: value["assets"][0].__setitem__(
                    "attribution_required", False
                ),
                "does not preserve required attribution",
            ),
            (
                "receipt",
                lambda value: value["assets"][0].__setitem__(
                    "license_receipt", None
                ),
                "lacks item-level license evidence",
            ),
            (
                "release",
                lambda value: value["assets"][0].__setitem__(
                    "model_release", "unknown"
                ),
                "unresolved model_release",
            ),
        )
        for suffix, mutation, expected in mutations:
            with self.subTest(suffix=suffix):
                task, claim, rights = complete_chain(suffix, mutation)
                with self.assertRaisesRegex(ValidationError, expected):
                    self.queue.complete(
                        task["id"],
                        lease_token=claim["lease_token"],
                        result={
                            "artifact": rights,
                            "human_approval": rights_human_approval(rights),
                        },
                        idempotency_key=f"complete-rights-discovery-{suffix}",
                        now=T0 + timedelta(seconds=2),
                    )
                # A rejected completion deliberately keeps the task leased so the
                # worker may correct its result. End this fixture attempt explicitly;
                # otherwise failed subtests consume the pod's lease capacity and make
                # later cases observe claim=None instead of the validation under test.
                self.queue.fail(
                    task["id"],
                    lease_token=claim["lease_token"],
                    error={"fixture": "expected rights-binding rejection"},
                    idempotency_key=f"fail-rights-discovery-{suffix}",
                    terminal=True,
                    now=T0 + timedelta(seconds=3),
                )

    def test_media_discovery_completion_is_role_and_lane_bound(self) -> None:
        artifact = pexels_discovery_artifact("job_discovery_binding")
        wrong_role = self.enqueue(
            "discovery-wrong-role",
            role="editor",
            pod="health",
            kind="editor_job",
            payload={
                "job_id": "job_discovery_binding",
                "lane_id": "health",
                "required_result_contract": "media_discovery_manifest",
            },
        )
        claim = self.claim("claim-discovery-wrong-role", role="editor")["task"]
        with self.assertRaisesRegex(ValidationError, "may only complete"):
            self.queue.complete(
                wrong_role["id"],
                lease_token=claim["lease_token"],
                result={"artifact": artifact},
                idempotency_key="complete-discovery-wrong-role",
                now=T0 + timedelta(seconds=1),
            )

        wrong_lane = self.enqueue(
            "discovery-wrong-lane",
            role="media_discovery",
            pod="motivation",
            kind="media_discovery_job",
            payload={
                "job_id": "job_discovery_wrong_lane",
                "lane_id": "motivation",
                "required_result_contract": "media_discovery_manifest",
            },
        )
        claim = self.claim("claim-discovery-wrong-lane", role="media_discovery")["task"]
        artifact = pexels_discovery_artifact(
            "job_discovery_wrong_lane", lane="health"
        )
        with self.assertRaisesRegex(ValidationError, "lane does not match"):
            self.queue.complete(
                wrong_lane["id"],
                lease_token=claim["lease_token"],
                result={"artifact": artifact},
                idempotency_key="complete-discovery-wrong-lane",
                now=T0 + timedelta(seconds=1),
            )

    def test_source_audio_manifest_is_motivation_only_and_completes_without_tts(self) -> None:
        transcript = "Продолжай, даже когда настроение закончилось."
        source_input = Path(self.temporary.name) / "source.mp4"
        source_input.write_bytes(b"queue-source-video" * 64)
        rights = {
            "schema_version": "1.0.0",
            "idea_id": "idea_motivation_001",
            "assets": [
                {
                    "asset_id": "audio_source_001",
                    "local_path": str(source_input.resolve()),
                    "download_url": None,
                    "landing_url": "https://example.test/source",
                    "creator": "Fixture owner",
                    "license": "Owned fixture",
                    "license_url": "https://example.test/license",
                    "license_receipt": "receipt-queue-001",
                    "retrieved_at": "2026-08-28T08:00:00Z",
                    "commercial_use": True,
                    "modification_allowed": True,
                    "attribution_required": False,
                    "attribution_text": None,
                    "model_release": "confirmed",
                    "property_release": "not_applicable",
                    "platforms": ["youtube_shorts", "instagram_reels", "tiktok"],
                    "territories": ["worldwide"],
                    "expires_at": None,
                    "rights_status": "approved",
                    "notes": "Queue byte-binding fixture",
                }
            ],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "missing_asset_ids": [],
                "review_notes": [],
            },
        }
        rights_task = self.enqueue(
            "source-audio-rights",
            role="rights",
            pod="motivation",
            kind="rights_job",
            payload={
                "job_id": "job_motivation_001",
                "idea_id": "idea_motivation_001",
                "lane_id": "motivation",
                "required_result_contract": "rights_manifest",
                "human_gate": True,
                "rights_checksum_bound": True,
            },
        )
        rights_claim = self.claim("claim-source-audio-rights", role="rights")
        self.queue.complete(
            rights_task["id"],
            lease_token=rights_claim["task"]["lease_token"],
            result={
                "artifact": rights,
                "human_approval": rights_human_approval(rights),
            },
            idempotency_key="complete-source-audio-rights",
            now=T0 + timedelta(seconds=1),
        )
        frozen = freeze_explicit_media(
            rights,
            [{"asset_id": "audio_source_001", "local_path": str(source_input)}],
            Path(self.temporary.name) / "frozen",
            job_id="job_motivation_001",
            allowed_local_roots=[Path(self.temporary.name)],
        )["artifact"]
        media_task = self.enqueue(
            "source-audio-media",
            role="media",
            pod="motivation",
            kind="media_job",
            dependency_task_id=rights_task["id"],
            payload={
                "job_id": "job_motivation_001",
                "idea_id": "idea_motivation_001",
                "lane_id": "motivation",
                "required_result_contract": "frozen_media_manifest",
            },
        )
        media_claim = self.claim("claim-source-audio-media", role="media")
        self.queue.complete(
            media_task["id"],
            lease_token=media_claim["task"]["lease_token"],
            result={"artifact": frozen},
            idempotency_key="complete-source-audio-media",
            now=T0 + timedelta(seconds=2),
        )
        frozen_source = (
            Path(frozen["frozen_root"]) / frozen["assets"][0]["frozen_path"]
        ).resolve()
        extracted_audio = Path(self.temporary.name) / "source.wav"
        extracted_audio.write_bytes(b"queue-extracted-audio" * 64)
        artifact = {
            "schema_version": "1.0.0",
            "job_id": "job_motivation_001",
            "lane": "motivation",
            "audio_asset_id": "audio_source_001",
            "source_video_uri_or_path": str(frozen_source),
            "source_in_seconds": 5.2,
            "source_out_seconds": 18.7,
            "speaker_name": None,
            "transcript": transcript,
            "rights_status": "commercial_license_confirmed",
            "rights_evidence": "receipt-queue-001",
            "original_audio_only": True,
            "tts": False,
            "extracted_audio_path": str(extracted_audio.resolve()),
            "checksums": {
                "source_video_sha256": frozen["assets"][0]["sha256"],
                "extracted_audio_sha256": hashlib.sha256(
                    extracted_audio.read_bytes()
                ).hexdigest(),
                "transcript_sha256": digest_text(transcript),
            },
            "created_at": "2026-08-28T08:00:00Z",
        }
        task = self.enqueue(
            "source-audio-motivation",
            role="source_audio",
            pod="motivation",
            kind="source_audio_job",
            dependency_task_id=media_task["id"],
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
            result={"artifact": artifact, "output_path": str(extracted_audio)},
            idempotency_key="complete-source-audio",
            now=T0 + timedelta(seconds=3),
        )
        self.assertEqual(completed["task"]["status"], "succeeded")

        segment_paths = [
            Path(self.temporary.name) / "source-segment-1.wav",
            Path(self.temporary.name) / "source-segment-2.wav",
        ]
        pcm_parts = [b"\x00\x01" * 48_000, b"\x02\x03" * 48_000]
        for path, pcm in zip(segment_paths, pcm_parts, strict=True):
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(48_000)
                audio.writeframes(pcm)
        program_path = Path(self.temporary.name) / "source-program.wav"
        with wave.open(str(program_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(48_000)
            audio.writeframes(b"".join(pcm_parts))
        segment_transcripts = ["Первый спикер.", "Второй спикер."]
        multi_segments = []
        for index, (path, text) in enumerate(
            zip(segment_paths, segment_transcripts, strict=True)
        ):
            multi_segments.append(
                {
                    "index": index,
                    "asset_id": "audio_source_001",
                    "source_video_uri_or_path": str(frozen_source),
                    "source_in_seconds": 5.2 + index,
                    "source_out_seconds": 6.2 + index,
                    "program_in_seconds": float(index),
                    "program_out_seconds": float(index + 1),
                    "speaker_name": f"Спикер {index + 1}",
                    "source_language": "ru",
                    "original_transcript": text,
                    "transcript": text,
                    "bilingual_review": None,
                    "rights_status": "commercial_license_confirmed",
                    "rights_evidence": "receipt-queue-001",
                    "extracted_audio_path": str(path.resolve()),
                    "checksums": {
                        "source_video_sha256": frozen["assets"][0]["sha256"],
                        "extracted_audio_sha256": hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest(),
                        "original_transcript_sha256": digest_text(text),
                        "transcript_sha256": digest_text(text),
                        "bilingual_review_sha256": None,
                    },
                }
            )
        bindings_sha = digest_text(canonical_json(multi_segments))
        aggregate_transcript = "\n".join(segment_transcripts)
        multi_artifact = {
            "schema_version": "1.1.0",
            "job_id": "job_motivation_001",
            "lane": "motivation",
            "audio_asset_id": f"source-audio-program-{bindings_sha[:24]}",
            "segment_count": 2,
            "segments": multi_segments,
            "transcript": aggregate_transcript,
            "rights_status": "commercial_license_confirmed",
            "original_audio_only": True,
            "tts": False,
            "extracted_audio_path": str(program_path.resolve()),
            "checksums": {
                "extracted_audio_sha256": hashlib.sha256(
                    program_path.read_bytes()
                ).hexdigest(),
                "transcript_sha256": digest_text(aggregate_transcript),
                "segment_bindings_sha256": bindings_sha,
            },
            "created_at": "2026-08-28T08:00:00Z",
        }
        multi_task = self.enqueue(
            "source-audio-motivation-multi",
            role="source_audio",
            pod="motivation",
            kind="source_audio_job",
            dependency_task_id=media_task["id"],
            payload={
                "job_id": "job_motivation_001",
                "lane_id": "motivation",
                "required_result_contract": "source_audio_manifest",
            },
        )
        multi_claim = self.claim(
            "claim-source-audio-multi", role="source_audio"
        )["task"]
        with patch(
            "video_factory.queue._probe_source_audio_duration", return_value=20.0
        ):
            completed_multi = self.queue.complete(
                multi_task["id"],
                lease_token=multi_claim["lease_token"],
                result={
                    "artifact": multi_artifact,
                    "output_path": str(program_path),
                },
                idempotency_key="complete-source-audio-multi",
                now=T0 + timedelta(seconds=4),
            )
        self.assertEqual(completed_multi["task"]["status"], "succeeded")

        tampered_task = self.enqueue(
            "source-audio-motivation-multi-tamper",
            role="source_audio",
            pod="motivation",
            kind="source_audio_job",
            dependency_task_id=media_task["id"],
            payload={
                "job_id": "job_motivation_001",
                "lane_id": "motivation",
                "required_result_contract": "source_audio_manifest",
            },
        )
        tampered_claim = self.claim(
            "claim-source-audio-multi-tamper", role="source_audio"
        )["task"]
        segment_paths[1].write_bytes(b"tampered")
        with patch(
            "video_factory.queue._probe_source_audio_duration", return_value=20.0
        ):
            with self.assertRaisesRegex(ValidationError, "extracted hash differs"):
                self.queue.complete(
                    tampered_task["id"],
                    lease_token=tampered_claim["lease_token"],
                    result={
                        "artifact": multi_artifact,
                        "output_path": str(program_path),
                    },
                    idempotency_key="complete-source-audio-multi-tamper",
                    now=T0 + timedelta(seconds=5),
                )

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
        self.assertTrue(result["simulation_only"])
        self.assertFalse(result["production_ready"])
        self.assertEqual(result["capacity_claim"], "queue_wip_mechanics_only")
        self.assertEqual(result["real_provider_calls"], 0)
        self.assertFalse(result["real_media_artifacts_created"])
        self.assertEqual(result["real_renders_created"], 0)
        self.assertEqual(result["real_publications"], 0)
        self.assertEqual(
            result["human_gate_roles_simulated"], ["final_review", "publisher"]
        )
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
