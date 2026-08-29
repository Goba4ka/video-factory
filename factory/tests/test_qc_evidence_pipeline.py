from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from factory.tests import test_semantic_qc_handler as semantic_fixture
from video_factory.contracts import validate_artifact
from video_factory.errors import ValidationError
from video_factory.qc_auto_evidence_handler import handle_task as auto_evidence
from video_factory.qc_evidence_gate import handle_task as evidence_gate
from video_factory.validators import canonical_json, digest_text

from source_audio_fixtures import build_multisource_manifest, file_sha256


class QCEvidencePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = semantic_fixture.SemanticQCHandlerTests(
            "test_builds_schema_valid_report_only_from_eight_bound_passes"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def _full_qc(self, source: Path, **kwargs: object) -> dict:
        self.assertEqual(source, self.fixture.output)
        self.assertEqual(kwargs["level"], "full")
        self.assertEqual(kwargs["profile_name"], "vertical_master")
        self.fixture.technical_report.write_text("{}\n", encoding="utf-8")
        return {
            "level": "full",
            "source": str(source),
            "technical_pass": True,
            "failures": [],
            "warnings": [],
            "media": {
                "video": {
                    "width": 1080,
                    "height": 1920,
                    "fps": 30.0,
                    "codec": "h264",
                },
                "audio": {
                    "sample_rate_hz": 48000,
                    "codec": "aac",
                    "channels": 2,
                },
            },
            "scan": {
                "black_durations_seconds": [],
                "freeze_durations_seconds": [],
                "silence_durations_seconds": [],
                "loudness": {
                    "integrated_lufs": -14.5,
                    "true_peak_dbtp": -1.5,
                    "lra_lu": 4.0,
                },
            },
            "cache": {"report_path": str(self.fixture.technical_report)},
        }

    def _auto_task(self) -> dict:
        return {
            "job_id": self.fixture.job_id,
            "role": "qc_auto_evidence",
            "pod": "motivation",
            "payload": {
                "job_id": self.fixture.job_id,
                "lane_id": "motivation",
                "required_result_contract": "qc_auto_evidence_manifest",
                "technical_profile": "vertical_master",
            },
            "upstream_results": [
                {"role": "rights", "result": {"artifact": self.fixture.rights}},
                {"role": "media", "result": {"artifact": self.fixture.frozen}},
                {"role": "editor", "result": {"artifact": self.fixture.shotlist}},
                {
                    "role": "render",
                    "result": {
                        "artifact": self.fixture.render,
                        "output_path": str(self.fixture.output),
                    },
                },
            ],
        }

    def _multisource_chain(self) -> tuple[dict, dict, dict]:
        source_audio = build_multisource_manifest(
            self.fixture.root,
            job_id=self.fixture.job_id,
            frozen_root=Path(self.fixture.frozen["frozen_root"]),
            frozen_assets=[self.fixture.frozen["assets"][0], self.fixture.frozen["assets"][0]],
            transcript_parts=["Первый лицензированный фрагмент.", "Второй лицензированный фрагмент."],
            durations=[7.5, 7.5],
        )
        program_path = Path(source_audio["extracted_audio_path"])
        source_sha = digest_text(canonical_json(source_audio))
        program = {
            "schema_version": "1.0.0",
            "job_id": self.fixture.job_id,
            "idea_id": self.fixture.idea_id,
            "lane_id": "motivation",
            "source_authority": {
                "contract": "source_audio_manifest",
                "manifest_sha256": source_sha,
                "audio_sha256": source_audio["checksums"]["extracted_audio_sha256"],
                "authority": "spoken_content_and_timing",
                "tts": False,
            },
            "bgm": {
                "asset_id": "music_001",
                "manifest_sha256": "1" * 64,
                "audio_sha256": "2" * 64,
                "license_evidence_sha256": "3" * 64,
                "human_approval_sha256": "4" * 64,
            },
            "mix": {
                "engine": "ffmpeg",
                "ffmpeg_version": "ffmpeg fixture",
                "recipe_version": "program-mix-1.0.0",
                "filtergraph_sha256": "5" * 64,
                "loudness_target_lufs": -15,
                "true_peak_max_dbtp": -1,
                "lra_target_lu": 7,
                "mix_profile_id": "speech-forward-audible-bgm-v1",
                "bgm_preduck_gain_db": -9,
                "sidechain_threshold_dbfs": -34,
                "sidechain_ratio": 10,
                "sidechain_attack_ms": 15,
                "sidechain_release_ms": 350,
                "sidechain_ducking": True,
                "broll_audio_muted": True,
                "deterministic": True,
            },
            "immutable_output_path": str(program_path),
            "output_sha256": file_sha256(program_path),
            "output_bytes": program_path.stat().st_size,
            "audio": {
                "sample_rate_hz": 48000,
                "channels": 2,
                "sample_width_bits": 16,
                "frames": 720000,
                "duration_seconds": 15,
                "integrated_loudness_lufs": -15,
                "loudness_range_lu": 3,
                "true_peak_dbtp": -1,
            },
            "created_at": "2026-08-30T10:00:01Z",
        }
        frozen_item = self.fixture.frozen["assets"][0]
        files = [
            {
                "path": "assets/audio/program_mix.wav",
                "sha256": program["output_sha256"],
                "size_bytes": program["output_bytes"],
            },
            {
                "path": "assets/media/speaker.mp4",
                "sha256": frozen_item["sha256"],
                "size_bytes": frozen_item["size_bytes"],
            },
            {"path": "index.html", "sha256": "6" * 64, "size_bytes": 1},
        ]
        project = {
            "schema_version": "1.0.0",
            "project_id": "project_qc_multi_001",
            "job_id": self.fixture.job_id,
            "idea_id": self.fixture.idea_id,
            "lane_id": "motivation",
            "project_root": str(self.fixture.root / "project"),
            "entrypoint": "index.html",
            "composition": {
                "composition_id": "main",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 15,
            },
            "bindings": {
                "shotlist": {
                    "contract": "shotlist", "schema_version": "1.0.0",
                    "idea_id": self.fixture.idea_id, "sha256": "7" * 64,
                },
                "script_package": {
                    "contract": "script_package", "schema_version": "1.0.0",
                    "idea_id": self.fixture.idea_id, "job_id": self.fixture.job_id,
                    "sha256": "8" * 64,
                },
                "frozen_media_manifest": {
                    "contract": "frozen_media_manifest", "schema_version": "1.0.0",
                    "idea_id": self.fixture.idea_id, "job_id": self.fixture.job_id,
                    "sha256": "9" * 64,
                },
                "authoritative_audio": {
                    "contract": "source_audio_manifest", "schema_version": "1.1.0",
                    "job_id": self.fixture.job_id, "sha256": source_sha,
                    "audio_sha256": source_audio["checksums"]["extracted_audio_sha256"],
                },
                "program_audio": {
                    "contract": "program_audio_manifest", "schema_version": "1.0.0",
                    "job_id": self.fixture.job_id, "idea_id": self.fixture.idea_id,
                    "lane_id": "motivation", "sha256": digest_text(canonical_json(program)),
                    "audio_sha256": program["output_sha256"],
                    "project_path": "assets/audio/program_mix.wav",
                    "size_bytes": program["output_bytes"],
                },
            },
            "assets": [
                {
                    "asset_id": frozen_item["asset_id"],
                    "frozen_path": frozen_item["frozen_path"],
                    "project_path": "assets/media/speaker.mp4",
                    "sha256": frozen_item["sha256"],
                    "size_bytes": frozen_item["size_bytes"],
                    "content_type": frozen_item["content_type"],
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
        validate_artifact("program_audio_manifest", program)
        validate_artifact("project_manifest", project)
        return source_audio, program, project

    def test_auto_stage_runs_one_scan_and_freezes_three_bound_reports(self) -> None:
        result = auto_evidence(self._auto_task(), media_qc_runner=self._full_qc)
        artifact = result["artifact"]
        self.assertIs(
            validate_artifact("qc_auto_evidence_manifest", artifact), artifact
        )
        self.assertEqual(
            {report["category"] for report in artifact["reports"]},
            {"technical", "audio", "rights"},
        )
        self.assertTrue(all(report["status"] == "pass" for report in artifact["reports"]))
        for category, descriptor in artifact["evidence"].items():
            path = Path(descriptor["path"])
            self.assertTrue(path.is_file(), category)
            self.assertEqual(semantic_fixture.file_sha(path), descriptor["sha256"])

    def test_motivation_rights_evidence_binds_multisource_program_and_project(self) -> None:
        source_audio, program, project = self._multisource_chain()
        task = self._auto_task()
        task["upstream_results"].extend(
            [
                {"role": "source_audio", "result": {"artifact": source_audio}},
                {"role": "audio_mix", "result": {"artifact": program}},
                {"role": "compiler", "result": {"artifact": project}},
            ]
        )
        result = auto_evidence(task, media_qc_runner=self._full_qc)
        rights = next(
            report for report in result["artifact"]["reports"]
            if report["category"] == "rights"
        )
        self.assertEqual(rights["status"], "pass")
        self.assertEqual(rights["metrics"]["source_audio_segments"], 2)
        self.assertEqual(
            rights["bindings"]["source_audio_segment_bindings_sha256"],
            source_audio["checksums"]["segment_bindings_sha256"],
        )
        self.assertEqual(
            rights["bindings"]["program_audio_manifest_sha256"],
            digest_text(canonical_json(program)),
        )
        self.assertEqual(
            rights["bindings"]["project_manifest_sha256"],
            digest_text(canonical_json(project)),
        )

        Path(source_audio["segments"][1]["extracted_audio_path"]).write_bytes(b"tampered")
        failed = auto_evidence(task, media_qc_runner=self._full_qc)
        rights = next(
            report for report in failed["artifact"]["reports"]
            if report["category"] == "rights"
        )
        self.assertEqual(rights["status"], "fail")
        self.assertTrue(any("multi-source program verification failed" in row for row in rights["findings"]))

    def test_media_warning_is_preserved_as_fail_not_synthetic_pass(self) -> None:
        def warned(source: Path, **kwargs: object) -> dict:
            report = self._full_qc(source, **kwargs)
            report["warnings"] = [
                {"code": "loudness_range", "message": "outside profile"}
            ]
            return report

        result = auto_evidence(self._auto_task(), media_qc_runner=warned)
        by_category = {
            report["category"]: report for report in result["artifact"]["reports"]
        }
        self.assertEqual(by_category["audio"]["status"], "fail")
        self.assertTrue(by_category["audio"]["warnings"])

    def _gate_task(self, auto_result: dict) -> dict:
        reports = {}
        for category in ("captions", "facts", "policy", "dedup", "visual"):
            descriptor = self.fixture.evidence[category]
            reports[category] = json.loads(
                Path(descriptor["path"]).read_text(encoding="utf-8")
            )
        return {
            "job_id": self.fixture.job_id,
            "role": "qc_evidence_gate",
            "pod": "motivation",
            "payload": {
                "job_id": self.fixture.job_id,
                "lane_id": "motivation",
                "required_result_contract": "qc_evidence_bundle",
            },
            "upstream_results": [
                {
                    "role": "render",
                    "result": {
                        "artifact": self.fixture.render,
                        "output_path": str(self.fixture.output),
                    },
                },
                {"role": "qc_auto_evidence", "result": auto_result},
                *[
                    {
                        "role": f"{category}_analyzer",
                        "result": {
                            "artifact": reports[category],
                            "evidence": self.fixture.evidence[category],
                            **(
                                {
                                    "contact_sheet": {
                                        "path": str(self.fixture.contact_sheet),
                                        "sha256": semantic_fixture.file_sha(self.fixture.contact_sheet),
                                    }
                                }
                                if category == "visual"
                                else {}
                            ),
                        },
                    }
                    for category in ("captions", "facts", "policy", "dedup", "visual")
                ],
            ],
        }

    def test_gate_accepts_exactly_eight_clean_immutable_reports(self) -> None:
        auto_result = auto_evidence(
            self._auto_task(), media_qc_runner=self._full_qc
        )
        result = evidence_gate(self._gate_task(auto_result))
        artifact = result["artifact"]
        self.assertIs(validate_artifact("qc_evidence_bundle", artifact), artifact)
        self.assertTrue(artifact["decision"]["passed"])
        self.assertEqual(len(artifact["reports"]), 8)

    def test_gate_rejects_tampered_report_bytes(self) -> None:
        auto_result = auto_evidence(
            self._auto_task(), media_qc_runner=self._full_qc
        )
        task = self._gate_task(auto_result)
        descriptor = task["upstream_results"][2]["result"]["evidence"]
        Path(descriptor["path"]).write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "checksum"):
            evidence_gate(task)

    def test_gate_rejects_nonpassing_category_even_with_fresh_hash(self) -> None:
        auto_result = auto_evidence(
            self._auto_task(), media_qc_runner=self._full_qc
        )
        task = self._gate_task(auto_result)
        visual_result = task["upstream_results"][-1]["result"]
        report = copy.deepcopy(visual_result["artifact"])
        report["status"] = "fail"
        report["needs_human_review"] = True
        report["findings"] = ["speaker face leaves safe zone"]
        path = Path(visual_result["evidence"]["path"])
        path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
        visual_result["artifact"] = report
        visual_result["evidence"]["sha256"] = semantic_fixture.file_sha(path)
        with self.assertRaisesRegex(ValidationError, "did not pass"):
            evidence_gate(task)


if __name__ == "__main__":
    unittest.main()
