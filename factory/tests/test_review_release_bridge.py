from __future__ import annotations

import copy
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from video_factory.db import Database
from video_factory.errors import ValidationError
from video_factory.review_release_bridge import (
    ReviewReleaseBridge,
    _verify_motivation_audio_chain,
    main,
)
from video_factory.validators import canonical_json, digest_text

from source_audio_fixtures import build_multisource_manifest, file_sha256


T0 = "2026-08-29T08:00:00.000Z"


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_sha(value: dict) -> str:
    return digest_text(canonical_json(value))


class ReviewReleaseBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "factory.sqlite3"
        self.outbox_root = self.root / "review-outbox"
        Database(self.db_path).initialize()
        self.job_id = "job_review_001"
        self.idea_id = "idea_review_001"
        self.lane = "motivation"
        self.render_path = self.root / "master.mp4"
        self.render_path.write_bytes(b"immutable-final-master")
        self._build_artifacts()
        self._insert_job_and_chain()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build_artifacts(self) -> None:
        project_root = self.root / "project"
        media = project_root / "assets" / "media" / "speaker.mp4"
        media.parent.mkdir(parents=True)
        narration = project_root / "assets" / "audio" / "narration.wav"
        narration.parent.mkdir(parents=True)
        program_mix = project_root / "assets" / "audio" / "program_mix.wav"
        (project_root / "index.html").write_text("<main>review</main>", encoding="utf-8")
        (project_root / "SCRIPT.md").write_text("Русский текст", encoding="utf-8")
        media.write_bytes(b"licensed-speaker")
        narration.write_bytes(b"RIFF" + b"licensed-source-audio" * 4)
        program_mix.write_bytes(b"RIFF" + b"checksum-bound-program-audio" * 4)
        files = []
        project_paths = [item for item in project_root.rglob("*") if item.is_file()]
        for path in sorted(
            project_paths, key=lambda item: item.relative_to(project_root).as_posix()
        ):
            files.append(
                {
                    "path": path.relative_to(project_root).as_posix(),
                    "sha256": file_sha(path),
                    "size_bytes": path.stat().st_size,
                }
            )
        self.project = {
            "schema_version": "1.0.0",
            "project_id": "project_review_001",
            "job_id": self.job_id,
            "idea_id": self.idea_id,
            "lane_id": self.lane,
            "project_root": str(project_root),
            "entrypoint": "index.html",
            "composition": {
                "composition_id": "main",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 30,
            },
            "bindings": {
                "shotlist": {
                    "contract": "shotlist",
                    "schema_version": "1.0.0",
                    "idea_id": self.idea_id,
                    "sha256": "1" * 64,
                },
                "script_package": {
                    "contract": "script_package",
                    "schema_version": "1.0.0",
                    "idea_id": self.idea_id,
                    "job_id": self.job_id,
                    "sha256": "2" * 64,
                },
                "frozen_media_manifest": {
                    "contract": "frozen_media_manifest",
                    "schema_version": "1.0.0",
                    "idea_id": self.idea_id,
                    "job_id": self.job_id,
                    "sha256": "3" * 64,
                },
                "authoritative_audio": {
                    "contract": "source_audio_manifest",
                    "schema_version": "1.0.0",
                    "job_id": self.job_id,
                    "sha256": "4" * 64,
                    "audio_sha256": file_sha(narration),
                },
                "program_audio": {
                    "contract": "program_audio_manifest",
                    "schema_version": "1.0.0",
                    "job_id": self.job_id,
                    "idea_id": self.idea_id,
                    "lane_id": self.lane,
                    "sha256": "5" * 64,
                    "audio_sha256": file_sha(program_mix),
                    "project_path": "assets/audio/program_mix.wav",
                    "size_bytes": program_mix.stat().st_size,
                },
            },
            "assets": [
                {
                    "asset_id": "speaker_001",
                    "frozen_path": "speaker.mp4",
                    "project_path": "assets/media/speaker.mp4",
                    "sha256": file_sha(media),
                    "size_bytes": media.stat().st_size,
                    "content_type": "video/mp4",
                    "shot_ids": ["shot_001"],
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
        receipt = self.root / "hyperframes-check.json"
        receipt.write_text(
            canonical_json(
                {"ok": True, "project_tree_sha256": self.project["project_tree_sha256"]}
            )
            + "\n",
            encoding="utf-8",
        )
        self.preview = {
            "schema_version": "1.0.0",
            "job_id": self.job_id,
            "project_id": self.project["project_id"],
            "approved": True,
            "approved_by": "owner@example.test",
            "approved_at": T0,
            "project_tree_sha256": self.project["project_tree_sha256"],
            "project_manifest_sha256": artifact_sha(self.project),
            "check_receipt_path": str(receipt),
            "check_receipt_sha256": file_sha(receipt),
            "studio_url": "http://127.0.0.1:3002/review",
            "review_notes": ["Проверено человеком"],
        }
        render_inputs = [
            {"path": "project_manifest.json", "sha256": artifact_sha(self.project)},
            {"path": "preview_approval.json", "sha256": artifact_sha(self.preview)},
            *[
                {"path": f"project/{item['path']}", "sha256": item["sha256"]}
                for item in self.project["files"]
            ],
        ]
        self.render = {
            "schema_version": "1.0.0",
            "render_id": "render_review_001",
            "job_id": self.job_id,
            "composition": "main",
            "output": self.render_path.name,
            "output_sha256": file_sha(self.render_path),
            "technical": {
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 30,
                "video_codec": "h264",
                "audio_codec": "aac",
                "audio_sample_rate_hz": 48000,
                "integrated_lufs": -14.0,
                "true_peak_dbtp": -1.0,
            },
            "input_hashes": render_inputs,
            "created_at": T0,
        }

        evidence_hashes: dict[str, str] = {}
        evidence_descriptors: dict[str, dict[str, str]] = {}
        checks = []
        categories = (
            "technical",
            "audio",
            "captions",
            "facts",
            "rights",
            "dedup",
            "policy",
            "visual",
        )
        contact_sheet = self.root / "contact-sheet.jpg"
        contact_sheet.write_bytes(b"contact-sheet")
        contact_sha = file_sha(contact_sheet)
        media_qc_sha = "a" * 64
        for category in categories:
            path = self.root / f"{category}-evidence.json"
            path.write_text(
                canonical_json(
                    {
                        "category": category,
                        "job_id": self.job_id,
                        "render_sha256": self.render["output_sha256"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            evidence_hashes[category] = file_sha(path)
            evidence_descriptors[category] = {
                "path": str(path),
                "sha256": evidence_hashes[category],
            }
            suffix = (
                f"; media_qc_sha256={media_qc_sha}"
                if category == "technical"
                else f"; contact_sheet={contact_sheet}#sha256={contact_sha}"
                if category == "visual"
                else ""
            )
            checks.append(
                {
                    "check_id": f"{category}_evidence",
                    "category": category,
                    "status": "pass",
                    "evidence": f"{path}#sha256={evidence_hashes[category]}{suffix}",
                    "artifact": str(path),
                }
            )
        shared_bindings = {
            "output_sha256": self.render["output_sha256"],
            "render_manifest_sha256": artifact_sha(self.render),
            "claim_ledger_sha256": "1" * 64,
            "script_package_sha256": "2" * 64,
            "shotlist_sha256": "3" * 64,
            "rights_manifest_sha256": "4" * 64,
            "frozen_media_manifest_sha256": "5" * 64,
            "corpus_snapshot_sha256": "6" * 64,
            "machine_evidence_sha256": evidence_hashes["captions"],
            "contact_sheet_sha256": contact_sha,
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
        self.analyzer_reports = {
            category: {
                "schema_version": "1.0.0",
                "category": category,
                "job_id": self.job_id,
                "lane_id": self.lane,
                "render_id": self.render["render_id"],
                "render_sha256": self.render["output_sha256"],
                "status": "pass",
                "needs_human_review": False,
                "warnings": [],
                "findings": [],
                "checker": {
                    "name": f"fixture-{category}",
                    "version": "1.0.0",
                    "run_id": f"run-{category}-001",
                },
                "completed_at": T0,
                "bindings": {
                    name: shared_bindings[name] for name in binding_names[category]
                },
                "metrics": {"observations": 1},
            }
            for category in categories
        }
        self.auto_evidence = {
            "schema_version": "1.0.0",
            "job_id": self.job_id,
            "lane_id": self.lane,
            "render_id": self.render["render_id"],
            "render_sha256": self.render["output_sha256"],
            "reports": [
                self.analyzer_reports[category]
                for category in ("technical", "audio", "rights")
            ],
            "evidence": {
                category: evidence_descriptors[category]
                for category in ("technical", "audio", "rights")
            },
            "created_at": T0,
        }
        self.caption_transcript = {
            "schema_version": "1.0.0",
            "job_id": self.job_id,
            "lane_id": self.lane,
            "render_id": self.render["render_id"],
            "render_sha256": self.render["output_sha256"],
            "status": "completed",
            "warnings": [],
            "observer": {
                "executable_sha256": "7" * 64,
                "engine_name": "fixture-observer",
                "engine_version": "1.0.0",
                "run_id": "run-caption-transcript-001",
            },
            "evidence": evidence_descriptors["captions"],
            "word_count": 1,
            "created_at": T0,
        }
        self.evidence_bundle = {
            "schema_version": "1.0.0",
            "job_id": self.job_id,
            "lane_id": self.lane,
            "render_id": self.render["render_id"],
            "render_sha256": self.render["output_sha256"],
            "reports": [
                {
                    "category": category,
                    "artifact_sha256": artifact_sha(self.analyzer_reports[category]),
                    "evidence": evidence_descriptors[category],
                }
                for category in categories
            ],
            "contact_sheet": {"path": str(contact_sheet), "sha256": contact_sha},
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "blocking_categories": [],
            },
            "created_at": T0,
        }
        self.qc = {
            "schema_version": "1.0.0",
            "job_id": self.job_id,
            "render_id": self.render["render_id"],
            "technical": {"audio_sample_rate_hz": 48000},
            "checks": checks,
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "blocking_check_ids": [],
                "review_notes": [],
            },
            "created_at": T0,
        }
        self.compiler_result = {"artifact": self.project}
        self.preview_result = {"artifact": self.preview}
        self.render_result = {"artifact": self.render, "output_path": str(self.render_path)}
        self.qc_result = {
            "artifact": self.qc,
            "render_output_path": str(self.render_path),
            "evidence_sha256": evidence_hashes,
            "visual_contact_sheet_sha256": contact_sha,
            "technical_media_qc_sha256": media_qc_sha,
        }

    def _payload(self, contract: str | None, **extra: object) -> str:
        value = {
            "job_id": self.job_id,
            "lane_id": self.lane,
            "required_result_contract": contract,
            **extra,
        }
        return canonical_json(value)

    def _insert_task(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        dependency: str | None,
        role: str,
        payload: str,
        status: str,
        result: dict | None,
    ) -> None:
        completed_at = T0 if status == "succeeded" else None
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
                self.job_id,
                dependency,
                role,
                self.lane,
                f"{role}_job",
                payload,
                status,
                f"idem:{task_id}",
                1 if status == "succeeded" else 0,
                T0,
                canonical_json(result) if result is not None else None,
                T0,
                T0,
                completed_at,
            ),
        )

    def _insert_job_and_chain(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                INSERT INTO ideas (
                    id, title, payload_json, source_file, source_digest,
                    source_index, status, created_at, updated_at
                ) VALUES (?, 'Review', '{}', 'test', ?, 0, 'approved', ?, ?)
                """,
                (self.idea_id, "a" * 64, T0, T0),
            )
            connection.execute(
                """
                INSERT INTO jobs (
                    id, idea_id, batch_id, state, rights_status, qc_status,
                    created_at, updated_at
                ) VALUES (?, ?, 'batch_test', 'approved', 'passed', 'passed', ?, ?)
                """,
                (self.job_id, self.idea_id, T0, T0),
            )
            self._insert_task(
                connection,
                task_id="task_compiler_001",
                dependency=None,
                role="compiler",
                payload=self._payload("project_manifest"),
                status="succeeded",
                result=self.compiler_result,
            )
            self._insert_task(
                connection,
                task_id="task_preview_001",
                dependency="task_compiler_001",
                role="preview_review",
                payload=self._payload(
                    "preview_approval", human_gate=True, checksum_bound=True
                ),
                status="succeeded",
                result=self.preview_result,
            )
            self._insert_task(
                connection,
                task_id="task_render_001",
                dependency="task_preview_001",
                role="render",
                payload=self._payload("render_manifest"),
                status="succeeded",
                result=self.render_result,
            )
            self._insert_task(
                connection,
                task_id="task_qc_auto_001",
                dependency="task_render_001",
                role="qc_auto_evidence",
                payload=self._payload("qc_auto_evidence_manifest"),
                status="succeeded",
                result={"artifact": self.auto_evidence},
            )
            self._insert_task(
                connection,
                task_id="task_caption_transcript_001",
                dependency="task_qc_auto_001",
                role="caption_transcript",
                payload=self._payload("caption_transcript_manifest"),
                status="succeeded",
                result={"artifact": self.caption_transcript},
            )
            previous_task = "task_caption_transcript_001"
            for category in ("captions", "facts", "policy", "dedup", "visual"):
                task_id = f"task_{category}_analyzer_001"
                result = {
                    "artifact": self.analyzer_reports[category],
                    "evidence": self.evidence_bundle["reports"][
                        list(self.analyzer_reports).index(category)
                    ]["evidence"],
                }
                if category == "visual":
                    result["contact_sheet"] = self.evidence_bundle["contact_sheet"]
                self._insert_task(
                    connection,
                    task_id=task_id,
                    dependency=previous_task,
                    role=f"{category}_analyzer",
                    payload=self._payload("qc_analyzer_report"),
                    status="succeeded",
                    result=result,
                )
                previous_task = task_id
            self._insert_task(
                connection,
                task_id="task_qc_evidence_gate_001",
                dependency=previous_task,
                role="qc_evidence_gate",
                payload=self._payload("qc_evidence_bundle"),
                status="succeeded",
                result={"artifact": self.evidence_bundle},
            )
            self._insert_task(
                connection,
                task_id="task_qc_001",
                dependency="task_qc_evidence_gate_001",
                role="qc",
                payload=self._payload("qc_report"),
                status="succeeded",
                result=self.qc_result,
            )
            self._insert_task(
                connection,
                task_id="task_final_001",
                dependency="task_qc_001",
                role="final_review",
                payload=self._payload(
                    None,
                    human_gate=True,
                    checksum_bound=True,
                    structured_gate_required=True,
                ),
                status="queued",
                result=None,
            )
            connection.commit()

    def _replace_result(self, task_id: str, result: dict) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE tasks SET result_json = ? WHERE id = ?",
                (canonical_json(result), task_id),
            )
            connection.commit()

    def test_materializes_checksum_bound_human_review_event_idempotently(self) -> None:
        bridge = ReviewReleaseBridge(self.db_path, self.outbox_root)
        first = bridge.materialize("task_final_001")
        second = bridge.materialize("task_final_001")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["bundle_id"], second["bundle_id"])
        bundle_path = Path(first["bundle_path"])
        event_path = Path(first["event_path"])
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        event = json.loads(event_path.read_text(encoding="utf-8"))
        self.assertEqual(bundle["job_id"], self.job_id)
        self.assertEqual(bundle["lane_id"], self.lane)
        self.assertEqual(bundle["render"]["sha256"], file_sha(self.render_path))
        self.assertEqual(bundle["qc"]["report_sha256"], artifact_sha(self.qc))
        self.assertTrue(bundle["manual_gate"]["approval_required"])
        self.assertFalse(bundle["manual_gate"]["automatic_approval"])
        self.assertEqual(event["status"], "pending_human_review")
        self.assertFalse(event["publish_outbox_created"])
        self.assertFalse(event["external_send_performed"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT status, result_json FROM tasks WHERE id = 'task_final_001'"
            ).fetchone()
        self.assertEqual(row, ("queued", None))

    def test_release_revalidates_multisource_chain_after_success(self) -> None:
        frozen_root = self.root / "release-frozen"
        frozen_root.mkdir()
        frozen_source = frozen_root / "speaker.mp4"
        frozen_source.write_bytes(b"licensed-speaker-source")
        frozen_item = {
            "asset_id": "speaker_001",
            "frozen_path": "speaker.mp4",
            "sha256": file_sha256(frozen_source),
        }
        source = build_multisource_manifest(
            self.root,
            job_id=self.job_id,
            frozen_root=frozen_root,
            frozen_assets=[frozen_item, frozen_item],
            transcript_parts=["Первый фрагмент.", "Второй фрагмент."],
            durations=[15.0, 15.0],
        )
        source_manifest_sha = artifact_sha(source)
        source_audio_sha = source["checksums"]["extracted_audio_sha256"]
        program_path = Path(source["extracted_audio_path"])
        program = {
            "schema_version": "1.0.0",
            "job_id": self.job_id,
            "idea_id": self.idea_id,
            "lane_id": "motivation",
            "source_authority": {
                "contract": "source_audio_manifest",
                "manifest_sha256": source_manifest_sha,
                "audio_sha256": source_audio_sha,
                "authority": "spoken_content_and_timing",
                "tts": False,
            },
            "immutable_output_path": str(program_path),
            "output_sha256": file_sha256(program_path),
            "output_bytes": program_path.stat().st_size,
        }
        program_manifest_sha = artifact_sha(program)
        project = copy.deepcopy(self.project)
        project["bindings"]["authoritative_audio"] = {
            "contract": "source_audio_manifest",
            "schema_version": "1.1.0",
            "job_id": self.job_id,
            "sha256": source_manifest_sha,
            "audio_sha256": source_audio_sha,
        }
        project["bindings"]["program_audio"] = {
            "contract": "program_audio_manifest",
            "schema_version": "1.0.0",
            "job_id": self.job_id,
            "idea_id": self.idea_id,
            "lane_id": "motivation",
            "sha256": program_manifest_sha,
            "audio_sha256": program["output_sha256"],
            "project_path": "assets/audio/program_mix.wav",
            "size_bytes": program["output_bytes"],
        }
        rights_report = copy.deepcopy(self.analyzer_reports["rights"])
        rights_report["bindings"].update(
            {
                "source_audio_manifest_sha256": source_manifest_sha,
                "source_audio_segment_bindings_sha256": source["checksums"]["segment_bindings_sha256"],
                "program_audio_manifest_sha256": program_manifest_sha,
                "project_manifest_sha256": artifact_sha(project),
            }
        )
        rights_path = self.root / "strict-rights-evidence.json"
        rights_path.write_text(canonical_json(rights_report) + "\n", encoding="utf-8")
        qc = copy.deepcopy(self.qc)
        next(check for check in qc["checks"] if check["category"] == "rights")[
            "artifact"
        ] = str(rights_path)
        chain = [
            {"role": "source_audio"},
            {"role": "audio_mix"},
            {"role": "compiler"},
        ]
        bindings = [
            {"contract": "source_audio_manifest"},
            {"contract": "program_audio_manifest"},
            {"contract": "project_manifest"},
        ]
        artifacts = [source, program, project]
        _verify_motivation_audio_chain(
            chain=chain,
            bindings=bindings,
            artifacts=artifacts,
            project_position=2,
            qc=qc,
        )

        source["segments"][1]["speaker_name"] = "post-success DB substitution"
        with self.assertRaisesRegex(
            ValidationError, "ProgramAudioManifest is not bound to exact SourceAudioManifest"
        ):
            _verify_motivation_audio_chain(
                chain=chain,
                bindings=bindings,
                artifacts=artifacts,
                project_position=2,
                qc=qc,
            )

    def test_rejects_tampered_render_bytes(self) -> None:
        self.render_path.write_bytes(b"changed-after-qc")
        with self.assertRaisesRegex(ValidationError, "render bytes changed"):
            ReviewReleaseBridge(self.db_path, self.outbox_root).materialize(
                "task_final_001"
            )
        self.assertFalse(self.outbox_root.exists())

    def test_rejects_nonpassing_qc(self) -> None:
        broken = json.loads(canonical_json(self.qc_result))
        broken["artifact"]["checks"][0]["status"] = "fail"
        broken["artifact"]["decision"] = {
            "passed": False,
            "needs_human_review": True,
            "blocking_check_ids": [broken["artifact"]["checks"][0]["check_id"]],
            "review_notes": ["blocked"],
        }
        self._replace_result("task_qc_001", broken)
        with self.assertRaisesRegex(ValidationError, "has not passed"):
            ReviewReleaseBridge(self.db_path, self.outbox_root).materialize(
                "task_final_001"
            )

    def test_rejects_tampered_semantic_evidence(self) -> None:
        evidence = Path(self.qc["checks"][3]["artifact"])
        evidence.write_text('{"tampered":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "evidence bytes changed"):
            ReviewReleaseBridge(self.db_path, self.outbox_root).materialize(
                "task_final_001"
            )

    def test_rejects_cross_lane_upstream_task(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE tasks SET pod = 'health' WHERE id = 'task_render_001'"
            )
            connection.commit()
        with self.assertRaisesRegex(ValidationError, "lane boundary"):
            ReviewReleaseBridge(self.db_path, self.outbox_root).materialize(
                "task_final_001"
            )

    def test_rejects_cross_job_qc_artifact(self) -> None:
        broken = json.loads(canonical_json(self.qc_result))
        broken["artifact"]["job_id"] = "job_review_999"
        self._replace_result("task_qc_001", broken)
        with self.assertRaisesRegex(ValidationError, "job boundary"):
            ReviewReleaseBridge(self.db_path, self.outbox_root).materialize(
                "task_final_001"
            )

    def test_rejects_non_direct_critical_binding(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            self._insert_task(
                connection,
                task_id="task_extra_001",
                dependency="task_qc_evidence_gate_001",
                role="editor",
                payload=self._payload("qc_analyzer_report"),
                status="succeeded",
                result={"artifact": self.analyzer_reports["visual"]},
            )
            connection.execute(
                "UPDATE tasks SET dependency_task_id = 'task_extra_001' WHERE id = 'task_qc_001'"
            )
            connection.commit()
        with self.assertRaisesRegex(ValidationError, "critical release chain"):
            ReviewReleaseBridge(self.db_path, self.outbox_root).materialize(
                "task_final_001"
            )

    def test_immutable_task_index_rejects_changed_qc_report_sha(self) -> None:
        bridge = ReviewReleaseBridge(self.db_path, self.outbox_root)
        bridge.materialize("task_final_001")
        changed = json.loads(canonical_json(self.qc_result))
        changed["artifact"]["created_at"] = "2026-08-29T08:01:00.000Z"
        self._replace_result("task_qc_001", changed)
        with self.assertRaisesRegex(ValidationError, "immutable outbox conflict"):
            bridge.materialize("task_final_001")

    def test_final_review_payload_is_part_of_immutable_identity(self) -> None:
        bridge = ReviewReleaseBridge(self.db_path, self.outbox_root)
        bridge.materialize("task_final_001")
        before = set((self.outbox_root / "bundles").iterdir())
        with closing(sqlite3.connect(self.db_path)) as connection:
            raw = connection.execute(
                "SELECT payload_json FROM tasks WHERE id = 'task_final_001'"
            ).fetchone()[0]
            payload = json.loads(raw)
            payload["operator_note"] = "changed after materialization"
            connection.execute(
                "UPDATE tasks SET payload_json = ? WHERE id = 'task_final_001'",
                (canonical_json(payload),),
            )
            connection.commit()
        with self.assertRaisesRegex(ValidationError, "immutable outbox conflict"):
            bridge.materialize("task_final_001")
        self.assertEqual(set((self.outbox_root / "bundles").iterdir()), before)

    def test_stdio_error_returns_nonzero_without_event(self) -> None:
        stdout = io.StringIO()
        code = main(io.StringIO('{"db_path":"missing"}'), stdout)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
