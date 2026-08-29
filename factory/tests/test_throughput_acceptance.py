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

from video_factory.cli import main
from video_factory.db import Database
from video_factory.lanes import load_lane_registry, roles_for_lane
from video_factory.throughput_acceptance import (
    evaluate_throughput_acceptance,
    expected_lane_distribution,
)
from video_factory.validators import canonical_json


T0 = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)
CATEGORIES = (
    "technical",
    "audio",
    "captions",
    "facts",
    "rights",
    "dedup",
    "policy",
    "visual",
)
ANALYZER_ROLES = {
    "captions": "captions_analyzer",
    "facts": "facts_analyzer",
    "dedup": "dedup_analyzer",
    "policy": "policy_analyzer",
    "visual": "visual_analyzer",
}


def iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_sha(value: dict) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ProductionEvidenceFixture:
    def __init__(self, root: Path, *, target: int = 10) -> None:
        self.root = root
        self.target = target
        self.db_path = root / "production.sqlite3"
        self.registry_path = Path("factory/lanes/registry.json").resolve()
        self.registry = load_lane_registry(self.registry_path)
        self.batch_id = "batch_production_20260830"
        self.cursor = T0
        self.master_paths: list[Path] = []
        Database(self.db_path).initialize()
        self._build()

    def _write(self, path: Path, content: bytes) -> dict[str, str]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"path": str(path), "sha256": file_sha(path)}

    def _artifacts(
        self, job_id: str, lane: str, ordinal: int
    ) -> dict[str, dict]:
        job_dir = self.root / "artifacts" / job_id
        job_dir.mkdir(parents=True)
        render_path = job_dir / "master.mp4"
        render_path.write_bytes(f"real-master:{job_id}".encode("utf-8"))
        self.master_paths.append(render_path)
        output_sha = file_sha(render_path)
        input_path = job_dir / "frozen-input.bin"
        input_path.write_bytes(f"input:{job_id}".encode("utf-8"))
        idea_id = f"idea_{ordinal:03d}"
        project_root = job_dir / "project"
        index_path = project_root / "index.html"
        media_path = project_root / "assets" / "media" / "clip.mp4"
        audio_path = project_root / "assets" / "audio" / "program_mix.wav"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text("<main>fixture</main>\n", encoding="utf-8")
        media_path.write_bytes(f"media:{job_id}".encode("utf-8"))
        audio_path.write_bytes(b"R" * 44)
        project_files = [
            {
                "path": "assets/audio/program_mix.wav",
                "sha256": file_sha(audio_path),
                "size_bytes": audio_path.stat().st_size,
            },
            {
                "path": "assets/media/clip.mp4",
                "sha256": file_sha(media_path),
                "size_bytes": media_path.stat().st_size,
            },
            {
                "path": "index.html",
                "sha256": file_sha(index_path),
                "size_bytes": index_path.stat().st_size,
            },
        ]
        project_tree_sha = hashlib.sha256(
            canonical_json(project_files).encode("utf-8")
        ).hexdigest()
        project_id = f"project_{ordinal:03d}"
        project = {
            "schema_version": "1.0.0",
            "project_id": project_id,
            "job_id": job_id,
            "idea_id": idea_id,
            "lane_id": lane,
            "project_root": str(project_root),
            "entrypoint": "index.html",
            "composition": {
                "composition_id": "main",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 31.0,
            },
            "bindings": {
                "shotlist": {
                    "contract": "shotlist",
                    "schema_version": "1.0.0",
                    "idea_id": idea_id,
                    "sha256": "1" * 64,
                },
                "script_package": {
                    "contract": "script_package",
                    "schema_version": "1.0.0",
                    "idea_id": idea_id,
                    "job_id": job_id,
                    "sha256": "2" * 64,
                },
                "frozen_media_manifest": {
                    "contract": "frozen_media_manifest",
                    "schema_version": "1.0.0",
                    "idea_id": idea_id,
                    "job_id": job_id,
                    "sha256": "3" * 64,
                },
                "authoritative_audio": {
                    "contract": (
                        "source_audio_manifest"
                        if lane == "motivation"
                        else "voice_manifest"
                    ),
                    "schema_version": "1.0.0",
                    "job_id": job_id,
                    "sha256": "4" * 64,
                    "audio_sha256": file_sha(audio_path),
                },
                "program_audio": {
                    "contract": "program_audio_manifest",
                    "schema_version": "1.0.0",
                    "job_id": job_id,
                    "idea_id": idea_id,
                    "lane_id": lane,
                    "sha256": "5" * 64,
                    "audio_sha256": file_sha(audio_path),
                    "project_path": "assets/audio/program_mix.wav",
                    "size_bytes": audio_path.stat().st_size,
                },
            },
            "assets": [
                {
                    "asset_id": f"asset_{ordinal:03d}",
                    "frozen_path": str(input_path),
                    "project_path": "assets/media/clip.mp4",
                    "sha256": file_sha(media_path),
                    "size_bytes": media_path.stat().st_size,
                    "content_type": "video/mp4",
                    "shot_ids": [f"shot_{ordinal:03d}"],
                }
            ],
            "files": project_files,
            "project_tree_sha256": project_tree_sha,
            "preview": {
                "status": "ready_for_human_review",
                "render_authorized": False,
                "human_approval_required": True,
            },
        }
        receipt_path = job_dir / "preview-check.json"
        receipt_path.write_text(
            canonical_json(
                {
                    "ok": True,
                    "project_tree_sha256": project_tree_sha,
                    "checks": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        approval = {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "project_id": project_id,
            "approved": True,
            "approved_by": "human-reviewer@example.test",
            "approved_at": iso(T0),
            "project_tree_sha256": project_tree_sha,
            "project_manifest_sha256": artifact_sha(project),
            "check_receipt_path": str(receipt_path),
            "check_receipt_sha256": file_sha(receipt_path),
            "studio_url": f"http://127.0.0.1:3002/#project/{project_id}",
            "review_notes": ["Reviewed the complete fixture timeline."],
        }
        render_id = f"render_{ordinal:03d}"
        render = {
            "schema_version": "1.0.0",
            "render_id": render_id,
            "job_id": job_id,
            "composition": f"project-{lane}",
            "output": render_path.name,
            "output_sha256": output_sha,
            "technical": {
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 31.0,
                "video_codec": "h264",
                "audio_codec": "aac",
                "audio_sample_rate_hz": 48000,
                "integrated_lufs": -15.0,
                "true_peak_dbtp": -1.1,
            },
            "input_hashes": [
                {"path": str(input_path), "sha256": file_sha(input_path)},
                {
                    "path": "project_manifest.json",
                    "sha256": artifact_sha(project),
                },
                {
                    "path": "preview_approval.json",
                    "sha256": artifact_sha(approval),
                },
            ],
            "created_at": iso(T0),
        }

        evidence: dict[str, dict[str, str]] = {}
        checks: list[dict] = []
        for category in CATEGORIES:
            descriptor = self._write(
                job_dir / f"{category}-evidence.json",
                (
                    canonical_json(
                        {
                            "category": category,
                            "job_id": job_id,
                            "render_sha256": output_sha,
                        }
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            evidence[category] = descriptor
            checks.append(
                {
                    "check_id": f"{category}_evidence",
                    "category": category,
                    "status": "pass",
                    "evidence": (
                        f"{descriptor['path']}#sha256={descriptor['sha256']}"
                    ),
                    "artifact": descriptor["path"],
                }
            )
        contact = self._write(
            job_dir / "contact-sheet.jpg", f"contact:{job_id}".encode("utf-8")
        )
        shared_bindings = {
            "output_sha256": output_sha,
            "render_manifest_sha256": artifact_sha(render),
            "claim_ledger_sha256": "1" * 64,
            "script_package_sha256": "2" * 64,
            "shotlist_sha256": "3" * 64,
            "rights_manifest_sha256": "4" * 64,
            "frozen_media_manifest_sha256": "5" * 64,
            "corpus_snapshot_sha256": "6" * 64,
            "machine_evidence_sha256": evidence["captions"]["sha256"],
            "contact_sheet_sha256": contact["sha256"],
            "safety_gate_report_sha256": "8" * 64,
        }
        binding_names = {
            "technical": {"output_sha256", "render_manifest_sha256"},
            "audio": {"output_sha256", "render_manifest_sha256"},
            "captions": {
                "output_sha256",
                "render_manifest_sha256",
                "script_package_sha256",
                "machine_evidence_sha256",
            },
            "facts": {
                "output_sha256",
                "render_manifest_sha256",
                "claim_ledger_sha256",
                "script_package_sha256",
                "shotlist_sha256",
            },
            "rights": {
                "output_sha256",
                "render_manifest_sha256",
                "rights_manifest_sha256",
                "frozen_media_manifest_sha256",
                "shotlist_sha256",
            },
            "dedup": {
                "output_sha256",
                "render_manifest_sha256",
                "corpus_snapshot_sha256",
            },
            "policy": {
                "output_sha256",
                "render_manifest_sha256",
                "claim_ledger_sha256",
                "script_package_sha256",
            },
            "visual": {
                "output_sha256",
                "render_manifest_sha256",
                "shotlist_sha256",
                "contact_sheet_sha256",
            },
        }
        reports = {
            category: {
                "schema_version": "1.0.0",
                "category": category,
                "job_id": job_id,
                "lane_id": lane,
                "render_id": render_id,
                "render_sha256": output_sha,
                "status": "pass",
                "needs_human_review": False,
                "warnings": [],
                "findings": [],
                "checker": {
                    "name": f"fixture-{category}",
                    "version": "1.0.0",
                    "run_id": f"run-{ordinal:03d}-{category}",
                },
                "completed_at": iso(T0),
                "bindings": {
                    name: shared_bindings[name]
                    for name in (
                        binding_names[category]
                        | (
                            {"safety_gate_report_sha256"}
                            if category == "policy" and lane != "motivation"
                            else set()
                        )
                    )
                },
                "metrics": {"observations": 1},
            }
            for category in CATEGORIES
        }
        auto = {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "lane_id": lane,
            "render_id": render_id,
            "render_sha256": output_sha,
            "reports": [reports[name] for name in ("technical", "audio", "rights")],
            "evidence": {
                name: evidence[name] for name in ("technical", "audio", "rights")
            },
            "created_at": iso(T0),
        }
        transcript = {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "lane_id": lane,
            "render_id": render_id,
            "render_sha256": output_sha,
            "status": "completed",
            "warnings": [],
            "observer": {
                "executable_sha256": "7" * 64,
                "engine_name": "fixture-observer",
                "engine_version": "1.0.0",
                "run_id": f"caption-run-{ordinal:03d}",
            },
            "evidence": evidence["captions"],
            "word_count": 8,
            "created_at": iso(T0),
        }
        bundle = {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "lane_id": lane,
            "render_id": render_id,
            "render_sha256": output_sha,
            "reports": [
                {
                    "category": category,
                    "artifact_sha256": artifact_sha(reports[category]),
                    "evidence": evidence[category],
                }
                for category in CATEGORIES
            ],
            "contact_sheet": contact,
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "blocking_categories": [],
            },
            "created_at": iso(T0),
        }
        qc = {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "render_id": render_id,
            "technical": {"audio_sample_rate_hz": 48000},
            "checks": checks,
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "blocking_check_ids": [],
                "review_notes": [],
            },
            "created_at": iso(T0),
        }
        results = {
            "compiler": {"artifact": project},
            "preview_review": {"artifact": approval},
            "render": {"artifact": render, "output_path": str(render_path)},
            "qc_auto_evidence": {"artifact": auto},
            "caption_transcript": {
                "artifact": transcript,
                "evidence": evidence["captions"],
            },
            "qc_evidence_gate": {"artifact": bundle},
            "qc": {
                "artifact": qc,
                "render_output_path": str(render_path),
                "evidence_sha256": {
                    category: evidence[category]["sha256"]
                    for category in CATEGORIES
                },
                "visual_contact_sheet_sha256": contact["sha256"],
                "technical_media_qc_sha256": "a" * 64,
            },
        }
        for category, role in ANALYZER_ROLES.items():
            results[role] = {
                "artifact": reports[category],
                "evidence": evidence[category],
            }
            if category == "visual":
                results[role]["contact_sheet"] = contact
        return results

    def _insert_task(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        job_id: str,
        dependency: str | None,
        role: str,
        lane: str,
        succeeded: bool,
        result: dict | None,
    ) -> None:
        if succeeded:
            dependency_completed = self.cursor
            claimed = dependency_completed + timedelta(seconds=1)
            finished = claimed + timedelta(seconds=2)
            self.cursor = finished
            status = "succeeded"
            attempt_count = 1
            completed_at = iso(finished)
            result_json = canonical_json(result)
            updated_at = completed_at
        else:
            status = "queued"
            attempt_count = 0
            completed_at = None
            result_json = None
            updated_at = iso(T0)
        payload = {
            "job_id": job_id,
            "lane_id": lane,
            "required_result_contract": None,
        }
        if role == "preview_review":
            payload.update(
                {
                    "required_result_contract": "preview_approval",
                    "human_gate": True,
                    "checksum_bound": True,
                }
            )
        connection.execute(
            """
            INSERT INTO tasks (
                id, job_id, dependency_task_id, role, pod, kind, payload_json,
                priority, status, idempotency_key, max_attempts, attempt_count,
                retry_backoff_seconds, available_at, result_json, created_at,
                updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 3, ?, 60, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                job_id,
                dependency,
                role,
                lane,
                f"{role}_job",
                canonical_json(payload),
                status,
                f"idem:{task_id}",
                attempt_count,
                iso(T0),
                result_json,
                iso(T0),
                updated_at,
                completed_at,
            ),
        )
        if succeeded:
            connection.execute(
                """
                INSERT INTO task_attempts (
                    task_id, attempt_no, worker_id, lease_token, status,
                    claimed_at, lease_expires_at, finished_at, result_json
                ) VALUES (?, 1, 'fixture-worker', ?, 'succeeded', ?, ?, ?, ?)
                """,
                (
                    task_id,
                    f"lease:{task_id}",
                    iso(claimed),
                    iso(finished + timedelta(seconds=60)),
                    iso(finished),
                    result_json,
                ),
            )

    def _build(self) -> None:
        allocation = expected_lane_distribution(self.registry, self.target)
        lanes = [
            lane
            for lane, count in allocation.items()
            for _ in range(count)
        ]
        with closing(sqlite3.connect(self.db_path)) as connection:
            for ordinal, lane in enumerate(lanes, start=1):
                idea_id = f"idea_{ordinal:03d}"
                job_id = f"job_{ordinal:03d}"
                connection.execute(
                    """
                    INSERT INTO ideas (
                        id, title, topic, payload_json, source_file, source_digest,
                        source_index, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'fixture', ?, ?, 'approved', ?, ?)
                    """,
                    (
                        idea_id,
                        f"Production {ordinal}",
                        lane,
                        canonical_json({"lane": lane, "production": True}),
                        hashlib.sha256(idea_id.encode()).hexdigest(),
                        ordinal,
                        iso(T0),
                        iso(T0),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO jobs (
                        id, idea_id, batch_id, state, rights_status, qc_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'qc_pending', 'passed', 'passed', ?, ?)
                    """,
                    (job_id, idea_id, self.batch_id, iso(T0), iso(T0)),
                )
                results = self._artifacts(job_id, lane, ordinal)
                roles = roles_for_lane(lane, registry=self.registry)
                qc_index = roles.index("qc")
                dependency = None
                for index, role in enumerate(roles):
                    task_id = f"task_{ordinal:03d}_{index:02d}_{role}"
                    succeeded = index <= qc_index
                    result = results.get(
                        role,
                        {
                            "artifact": {
                                "fixture_role": role,
                                "job_id": job_id,
                                "lane_id": lane,
                            }
                        },
                    )
                    self._insert_task(
                        connection,
                        task_id=task_id,
                        job_id=job_id,
                        dependency=dependency,
                        role=role,
                        lane=lane,
                        succeeded=succeeded,
                        result=result if succeeded else None,
                    )
                    dependency = task_id
            connection.commit()


class ThroughputAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = ProductionEvidenceFixture(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def evaluate(self, **overrides: object) -> dict:
        options = {
            "db_path": self.fixture.db_path,
            "target": 10,
            "deadline_hours": 1.0,
            "batch_id": self.fixture.batch_id,
            "registry_path": self.fixture.registry_path,
            "safety_margin": 0.20,
            "as_of": "2026-08-31T00:00:00Z",
        }
        options.update(overrides)
        return evaluate_throughput_acceptance(**options)

    def test_accepts_exact_production_batch_without_mutating_sqlite(self) -> None:
        before = file_sha(self.fixture.db_path)
        report = self.evaluate()
        after = file_sha(self.fixture.db_path)

        self.assertTrue(report["accepted"], report["errors"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["throughput_accepted"])
        self.assertFalse(report["production_ready"])
        self.assertEqual(report["counts"]["qc_passed_masters"], 10)
        self.assertEqual(
            report["actual_lane_distribution"],
            {
                "war_history": 2,
                "celebrity_news": 2,
                "motivation": 2,
                "chinese_medicine": 2,
                "health": 2,
            },
        )
        self.assertEqual(report["timing"]["handler_duration"]["overall"]["p95_seconds"], 2.0)
        self.assertGreater(report["gpu_heavy"]["successful_attempts"], 0)
        self.assertFalse(report["human_gates"]["gate_performed_actions"])
        self.assertEqual(
            report["human_gates"]["observed_final_review_status_counts"],
            {"queued": 10},
        )
        self.assertEqual(
            report["human_gates"]["observed_publisher_status_counts"],
            {"queued": 10},
        )
        self.assertEqual(before, after)

    def test_target_fifteen_distribution_follows_registry_order(self) -> None:
        self.assertEqual(
            expected_lane_distribution(self.fixture.registry, 15),
            {
                "war_history": 3,
                "celebrity_news": 3,
                "motivation": 3,
                "chinese_medicine": 3,
                "health": 3,
            },
        )

    def test_rejects_simulation_marker(self) -> None:
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            connection.execute(
                "UPDATE tasks SET kind = 'simulation.fixture' WHERE role = 'research'"
            )
            connection.commit()
        report = self.evaluate()
        self.assertFalse(report["accepted"])
        self.assertIn("simulation_evidence_rejected", {row["code"] for row in report["errors"]})

    def test_rejects_missing_registry_stage(self) -> None:
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            task_id = connection.execute(
                "SELECT id FROM tasks WHERE role = 'qc_auto_evidence' LIMIT 1"
            ).fetchone()[0]
            connection.execute("DELETE FROM task_attempts WHERE task_id = ?", (task_id,))
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            connection.commit()
        report = self.evaluate()
        self.assertFalse(report["accepted"])
        codes = {row["code"] for row in report["errors"]}
        self.assertIn("registry_dag_mismatch", codes)
        self.assertIn("qc_evidence_stage_missing", codes)

    def test_rejects_stale_render_checksum(self) -> None:
        self.fixture.master_paths[0].write_bytes(b"tampered-master")
        report = self.evaluate()
        self.assertFalse(report["accepted"])
        self.assertIn("production_evidence_invalid", {row["code"] for row in report["errors"]})

    def test_rejects_tampered_preview_receipt(self) -> None:
        receipt = next((self.root / "artifacts").glob("*/preview-check.json"))
        receipt.write_text('{"ok":false}\n', encoding="utf-8")
        report = self.evaluate()
        self.assertFalse(report["accepted"])
        self.assertIn("production_evidence_invalid", {row["code"] for row in report["errors"]})

    def test_rejects_symlinked_evidence_file(self) -> None:
        evidence = next((self.root / "artifacts").glob("*/facts-evidence.json"))
        target = evidence.with_name("facts-evidence-real.json")
        evidence.rename(target)
        try:
            evidence.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")
        report = self.evaluate()
        self.assertFalse(report["accepted"])
        self.assertIn("production_evidence_invalid", {row["code"] for row in report["errors"]})

    def test_rejects_open_dead_letter(self) -> None:
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            task_id = connection.execute(
                "SELECT id FROM tasks WHERE role = 'research' LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO dead_letters (
                    id, task_id, cycle_no, status, cause_code, error_json,
                    task_snapshot_json, created_at
                ) VALUES ('dlq_fixture_open', ?, 1, 'open', 'fixture_failure',
                          '{}', '{}', ?)
                """,
                (task_id, iso(T0)),
            )
            connection.commit()
        report = self.evaluate()
        self.assertFalse(report["accepted"])
        codes = {row["code"] for row in report["errors"]}
        self.assertIn("open_dead_letters", codes)
        self.assertIn("task_open_dead_letter", codes)

    def test_rejects_deadline_or_margin_overrun(self) -> None:
        report = self.evaluate(deadline_hours=0.10)
        self.assertFalse(report["accepted"])
        self.assertIn(
            "deadline_or_margin_exceeded", {row["code"] for row in report["errors"]}
        )
        out = io.StringIO()
        err = io.StringIO()
        code = main(
            [
                "throughput-acceptance",
                "--db",
                str(self.fixture.db_path),
                "--registry",
                str(self.fixture.registry_path),
                "--target",
                "10",
                "--deadline-hours",
                "0.10",
                "--batch-id",
                self.fixture.batch_id,
                "--as-of",
                "2026-08-31T00:00:00Z",
            ],
            out=out,
            err=err,
        )
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(json.loads(err.getvalue())["accepted"])

    def test_rejects_missing_successful_attempt(self) -> None:
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            task_id = connection.execute(
                "SELECT id FROM tasks WHERE role = 'research' LIMIT 1"
            ).fetchone()[0]
            connection.execute("DELETE FROM task_attempts WHERE task_id = ?", (task_id,))
            connection.commit()
        report = self.evaluate()
        self.assertFalse(report["accepted"])
        self.assertIn("successful_attempt_missing", {row["code"] for row in report["errors"]})

    def test_reports_and_rejects_completed_final_review(self) -> None:
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            row = connection.execute(
                """
                SELECT final.id, dependency.completed_at
                FROM tasks AS final
                JOIN tasks AS dependency ON dependency.id = final.dependency_task_id
                WHERE final.role = 'final_review' LIMIT 1
                """
            ).fetchone()
            claimed = datetime.fromisoformat(row[1].replace("Z", "+00:00")) + timedelta(
                seconds=1
            )
            finished = claimed + timedelta(seconds=2)
            result = canonical_json({"artifact": {"human_decision": "approved"}})
            connection.execute(
                """
                UPDATE tasks
                SET status='succeeded', attempt_count=1, result_json=?,
                    updated_at=?, completed_at=?
                WHERE id=?
                """,
                (result, iso(finished), iso(finished), row[0]),
            )
            connection.execute(
                """
                INSERT INTO task_attempts (
                    task_id, attempt_no, worker_id, lease_token, status,
                    claimed_at, lease_expires_at, finished_at, result_json
                ) VALUES (?, 1, 'human-reviewer', ?, 'succeeded', ?, ?, ?, ?)
                """,
                (
                    row[0],
                    f"lease:{row[0]}",
                    iso(claimed),
                    iso(finished + timedelta(seconds=60)),
                    iso(finished),
                    result,
                ),
            )
            connection.commit()
        report = self.evaluate()
        self.assertFalse(report["accepted"])
        self.assertIn(
            "final_review_already_completed", {row["code"] for row in report["errors"]}
        )
        self.assertEqual(
            report["human_gates"]["observed_final_review_status_counts"],
            {"queued": 9, "succeeded": 1},
        )

    def test_cli_emits_read_only_acceptance_report(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        code = main(
            [
                "throughput-acceptance",
                "--db",
                str(self.fixture.db_path),
                "--registry",
                str(self.fixture.registry_path),
                "--target",
                "10",
                "--deadline-hours",
                "1",
                "--batch-id",
                self.fixture.batch_id,
                "--as-of",
                "2026-08-31T00:00:00Z",
            ],
            out=out,
            err=err,
        )
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["accepted"])
        self.assertTrue(payload["read_only"])


if __name__ == "__main__":
    unittest.main()
