from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from factory.tests import test_semantic_qc_handler as semantic_fixture
from video_factory.errors import ValidationError
from video_factory.qc_evidence_gate import handle_task as build_evidence_bundle
from video_factory.queue import Dispatcher
from video_factory.validators import canonical_json


T0 = datetime(2026, 8, 29, 8, 0, tzinfo=UTC)


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QueueQCEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = semantic_fixture.SemanticQCHandlerTests(
            "test_builds_schema_valid_report_only_from_eight_bound_passes"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.queue = Dispatcher(fixture.root / "queue.sqlite3")

        self.transcript_path = fixture.evidence_root / "word-transcript.json"
        transcript = {
            "schema_version": "1.0.0",
            "job_id": fixture.job_id,
            "render_id": fixture.render_id,
            "render_sha256": fixture.render["output_sha256"],
            "status": "completed",
            "warnings": [],
            "language": "ru",
            "duration_seconds": 15,
            "engine": {
                "name": "fixture-observer",
                "version": "1.0.0",
                "run_id": "transcript-run-001",
            },
            "completed_at": "2026-08-29T08:02:00Z",
            "words": [
                {
                    "text": "Начни",
                    "start_seconds": 0,
                    "end_seconds": 0.5,
                    "confidence": 0.99,
                }
            ],
        }
        self.transcript_path.write_text(
            json.dumps(transcript, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.transcript_descriptor = {
            "path": str(self.transcript_path.resolve()),
            "sha256": file_sha(self.transcript_path),
        }

        self.corpus_path = fixture.evidence_root / "dedup-corpus.json"
        corpus = {
            "schema_version": "1.0.0",
            "snapshot_id": "fixture-corpus-001",
            "generated_at": "2026-08-29T08:01:00Z",
            "algorithm": "dhash-64-v1",
            "sample_interval_seconds": 1,
            "entries": [
                {
                    "comparison_id": "comparison-001",
                    "job_id": "job_previous_001",
                    "render_id": "render_previous_001",
                    "render_sha256": "d" * 64,
                    "frame_hashes": [f"{index:016x}" for index in range(8)],
                }
            ],
        }
        self.corpus_path.write_text(
            json.dumps(corpus, sort_keys=True) + "\n", encoding="utf-8"
        )
        environment = mock.patch.dict(
            os.environ,
            {"VIDEO_FACTORY_DEDUP_CORPUS_SNAPSHOT": str(self.corpus_path.resolve())},
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)

        self.reports = {}
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
            descriptor = fixture.evidence[category]
            path = Path(descriptor["path"])
            report = json.loads(path.read_text(encoding="utf-8"))
            if category == "captions":
                report["bindings"]["machine_evidence_sha256"] = (
                    self.transcript_descriptor["sha256"]
                )
            if category == "dedup":
                report["bindings"]["corpus_snapshot_sha256"] = file_sha(
                    self.corpus_path
                )
            path.write_text(
                json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            descriptor["sha256"] = file_sha(path)
            self.reports[category] = report

        self.auto_result = {
            "artifact": {
                "schema_version": "1.0.0",
                "job_id": fixture.job_id,
                "lane_id": "motivation",
                "render_id": fixture.render_id,
                "render_sha256": fixture.render["output_sha256"],
                "reports": [
                    self.reports[category]
                    for category in ("technical", "audio", "rights")
                ],
                "evidence": {
                    category: fixture.evidence[category]
                    for category in ("technical", "audio", "rights")
                },
                "created_at": "2026-08-29T08:02:00Z",
            }
        }
        self.caption_result = {
            "artifact": {
                "schema_version": "1.0.0",
                "job_id": fixture.job_id,
                "lane_id": "motivation",
                "render_id": fixture.render_id,
                "render_sha256": fixture.render["output_sha256"],
                "status": "completed",
                "warnings": [],
                "observer": {
                    "executable_sha256": "e" * 64,
                    "engine_name": "fixture-observer",
                    "engine_version": "1.0.0",
                    "run_id": "transcript-run-001",
                },
                "evidence": self.transcript_descriptor,
                "word_count": 1,
                "created_at": "2026-08-29T08:02:00Z",
            },
            "evidence": self.transcript_descriptor,
        }
        self.last_by_role = self._seed_upstream_chain()
        self.bundle_result = build_evidence_bundle(self._gate_handler_task())

    def _enqueue_succeeded(
        self,
        key: str,
        *,
        role: str,
        contract: str,
        result: dict,
        dependency: str | None,
    ) -> str:
        task = self.queue.enqueue(
            role=role,
            pod="motivation",
            kind=f"{role}_job",
            payload={
                "job_id": self.fixture.job_id,
                "lane_id": "motivation",
                "required_result_contract": contract,
            },
            dependency_task_id=dependency,
            idempotency_key=key,
            now=T0,
        )["task"]
        connection = self.queue.db.connect()
        try:
            connection.execute(
                "UPDATE tasks SET status='succeeded', result_json=? WHERE id=?",
                (canonical_json(result), task["id"]),
            )
            connection.commit()
        finally:
            connection.close()
        return task["id"]

    def _seed_upstream_chain(self) -> dict[str, str]:
        fixture = self.fixture
        dependency = None
        support = (
            ("research", "claim_ledger", fixture.claim_ledger),
            ("rights", "rights_manifest", fixture.rights),
            ("media", "frozen_media_manifest", fixture.frozen),
            ("script", "script_package", fixture.script),
            ("editor", "shotlist", fixture.shotlist),
        )
        last_by_role = {}
        for role, contract, artifact in support:
            dependency = self._enqueue_succeeded(
                f"seed-{role}",
                role=role,
                contract=contract,
                result={"artifact": artifact},
                dependency=dependency,
            )
            last_by_role[role] = dependency
        dependency = self._enqueue_succeeded(
            "seed-render",
            role="render",
            contract="render_manifest",
            result={"artifact": fixture.render, "output_path": str(fixture.output)},
            dependency=dependency,
        )
        last_by_role["render"] = dependency
        dependency = self._enqueue_succeeded(
            "seed-qc-auto",
            role="qc_auto_evidence",
            contract="qc_auto_evidence_manifest",
            result=self.auto_result,
            dependency=dependency,
        )
        last_by_role["qc_auto_evidence"] = dependency
        dependency = self._enqueue_succeeded(
            "seed-caption-transcript",
            role="caption_transcript",
            contract="caption_transcript_manifest",
            result=self.caption_result,
            dependency=dependency,
        )
        last_by_role["caption_transcript"] = dependency
        for category in ("captions", "facts", "policy", "dedup", "visual"):
            role = f"{category}_analyzer"
            result = {
                "artifact": self.reports[category],
                "evidence": self.fixture.evidence[category],
            }
            if category == "visual":
                result["contact_sheet"] = {
                    "path": str(self.fixture.contact_sheet.resolve()),
                    "sha256": file_sha(self.fixture.contact_sheet),
                }
            dependency = self._enqueue_succeeded(
                f"seed-{role}",
                role=role,
                contract="qc_analyzer_report",
                result=result,
                dependency=dependency,
            )
            last_by_role[role] = dependency
        return last_by_role

    def _gate_handler_task(self) -> dict:
        upstream = [
            {
                "role": "render",
                "result": {
                    "artifact": self.fixture.render,
                    "output_path": str(self.fixture.output),
                },
            },
            {"role": "qc_auto_evidence", "result": self.auto_result},
        ]
        for category in ("captions", "facts", "policy", "dedup", "visual"):
            result = {
                "artifact": self.reports[category],
                "evidence": self.fixture.evidence[category],
            }
            if category == "visual":
                result["contact_sheet"] = {
                    "path": str(self.fixture.contact_sheet.resolve()),
                    "sha256": file_sha(self.fixture.contact_sheet),
                }
            upstream.append({"role": f"{category}_analyzer", "result": result})
        return {
            "job_id": self.fixture.job_id,
            "role": "qc_evidence_gate",
            "pod": "motivation",
            "payload": {
                "job_id": self.fixture.job_id,
                "lane_id": "motivation",
                "required_result_contract": "qc_evidence_bundle",
            },
            "upstream_results": upstream,
        }

    def _queued_gate(self, key: str, dependency: str) -> tuple[dict, dict]:
        task = self.queue.enqueue(
            role="qc_evidence_gate",
            pod="motivation",
            kind="qc_evidence_gate_job",
            payload={
                "job_id": self.fixture.job_id,
                "lane_id": "motivation",
                "required_result_contract": "qc_evidence_bundle",
            },
            dependency_task_id=dependency,
            idempotency_key=key,
            now=T0,
        )["task"]
        claimed = self.queue.claim(
            worker_id=f"worker-{key}",
            role="qc_evidence_gate",
            idempotency_key=f"claim-{key}",
            lease_seconds=30,
            now=T0,
        )
        self.assertEqual(claimed["task"]["id"], task["id"])
        return task, claimed

    def test_bundle_completion_requires_all_eight_exact_evidence_artifacts(self) -> None:
        task, claimed = self._queued_gate(
            "gate-pass", self.last_by_role["visual_analyzer"]
        )
        completed = self.queue.complete(
            task["id"],
            lease_token=claimed["task"]["lease_token"],
            result=self.bundle_result,
            idempotency_key="complete-gate-pass",
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(completed["task"]["status"], "succeeded")

    def test_bundle_completion_rejects_missing_visual_analyzer(self) -> None:
        task, claimed = self._queued_gate(
            "gate-missing-visual", self.last_by_role["dedup_analyzer"]
        )
        with self.assertRaisesRegex(ValidationError, "lacks analyzer artifacts"):
            self.queue.complete(
                task["id"],
                lease_token=claimed["task"]["lease_token"],
                result=self.bundle_result,
                idempotency_key="complete-gate-missing-visual",
                now=T0 + timedelta(seconds=1),
            )

    def test_bundle_completion_rejects_tampered_report_bytes(self) -> None:
        task, claimed = self._queued_gate(
            "gate-tampered", self.last_by_role["visual_analyzer"]
        )
        facts_path = Path(self.fixture.evidence["facts"]["path"])
        facts_path.write_text('{"tampered":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "checksum"):
            self.queue.complete(
                task["id"],
                lease_token=claimed["task"]["lease_token"],
                result=copy.deepcopy(self.bundle_result),
                idempotency_key="complete-gate-tampered",
                now=T0 + timedelta(seconds=1),
            )

    def test_bundle_completion_rejects_stale_render_binding(self) -> None:
        task, claimed = self._queued_gate(
            "gate-stale-render", self.last_by_role["visual_analyzer"]
        )
        stale = copy.deepcopy(self.bundle_result)
        stale["artifact"]["render_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "stale or cross-job"):
            self.queue.complete(
                task["id"],
                lease_token=claimed["task"]["lease_token"],
                result=stale,
                idempotency_key="complete-gate-stale-render",
                now=T0 + timedelta(seconds=1),
            )


if __name__ == "__main__":
    unittest.main()
