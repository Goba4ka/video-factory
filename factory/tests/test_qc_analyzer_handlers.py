from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_factory.dedup_analyzer_handler import (
    handle_task as handle_dedup,
    main as dedup_main,
)
from video_factory.errors import ValidationError
from video_factory.visual_analyzer_handler import (
    handle_task as handle_visual,
    main as visual_main,
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QCAnalyzerHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.evidence_root = self.root / "evidence"
        self.evidence_root.mkdir()
        self.face_observer = self.root / "face-observer.exe"
        self.face_observer.write_bytes(b"fixture executable")
        self.corpus = self.root / "corpus.json"
        self.corpus.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "snapshot_id": "corpus_handler_001",
                    "generated_at": "2026-08-29T12:00:00Z",
                    "algorithm": "dhash-64-v1",
                    "sample_interval_seconds": 1.0,
                    "entries": [
                        {
                            "comparison_id": "prior_001",
                            "job_id": "job_prior_001",
                            "render_id": "render_prior_001",
                            "render_sha256": "b" * 64,
                            "frame_hashes": ["c" * 16] * 8,
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        environment = mock.patch.dict(
            "os.environ",
            {
                "VIDEO_FACTORY_QC_EVIDENCE_ROOT": str(self.evidence_root),
                "VIDEO_FACTORY_DEDUP_CORPUS_SNAPSHOT": str(self.corpus),
                "VIDEO_FACTORY_FACE_OBSERVER": str(self.face_observer),
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.job_id = "job_handler_001"
        self.idea_id = "idea_handler_001"
        self.render_id = "render_handler_001"
        self.master = self.root / "master.mp4"
        self.master.write_bytes(b"handler render master" * 128)
        self.render = {
            "schema_version": "1.0.0",
            "render_id": self.render_id,
            "job_id": self.job_id,
            "composition": "main",
            "output": "master.mp4",
            "output_sha256": file_sha256(self.master),
            "technical": {
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "duration_seconds": 15,
                "video_codec": "h264",
                "audio_codec": "aac",
                "audio_sample_rate_hz": 48000,
            },
            "input_hashes": [{"path": "index.html", "sha256": "a" * 64}],
            "created_at": "2026-08-29T12:00:00Z",
        }
        self.shotlist = {
            "schema_version": "1.0.0",
            "idea_id": self.idea_id,
            "duration_seconds": 15,
            "aspect": "9:16",
            "shots": [
                {
                    "shot_id": "shot_001",
                    "start": 0,
                    "end": 15,
                    "narration": "speaker",
                    "caption": "caption",
                    "visual_intent": "speaker portrait",
                    "asset_id": "asset_001",
                    "claim_ids": [],
                    "transition": "hard_cut",
                }
            ],
        }

    @staticmethod
    def upstream(role: str, artifact: dict, **result_fields: str) -> dict:
        return {
            "role": role,
            "result": {"artifact": copy.deepcopy(artifact), **result_fields},
        }

    def task(self, role: str, *, lane: str = "motivation") -> dict:
        upstream = [
            self.upstream("render", self.render, output_path=str(self.master))
        ]
        if role == "visual_analyzer":
            upstream.append(self.upstream("editor", self.shotlist))
        return {
            "job_id": self.job_id,
            "role": role,
            "pod": lane,
            "payload": {
                "job_id": self.job_id,
                "idea_id": self.idea_id,
                "lane_id": lane,
                "required_result_contract": "qc_analyzer_report",
            },
            "upstream_results": upstream,
        }

    @staticmethod
    def persist_stub_report(
        *,
        category: str,
        job_id: str,
        lane_id: str,
        render: dict,
        report_path: Path,
        contact_sheet_path: Path | None = None,
    ) -> dict:
        bindings = {
            "output_sha256": render["output_sha256"],
            "render_manifest_sha256": "d" * 64,
        }
        contact_descriptor = None
        if contact_sheet_path is not None:
            contact_sheet_path.write_bytes(b"P5\n1 1\n255\n\x80")
            contact_descriptor = {
                "path": str(contact_sheet_path),
                "sha256": file_sha256(contact_sheet_path),
            }
            bindings["contact_sheet_sha256"] = contact_descriptor["sha256"]
        artifact = {
            "category": category,
            "job_id": job_id,
            "lane_id": lane_id,
            "render_id": render["render_id"],
            "render_sha256": render["output_sha256"],
            "bindings": bindings,
        }
        report_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
        result = {
            "artifact": artifact,
            "evidence": {"path": str(report_path), "sha256": file_sha256(report_path)},
        }
        if contact_descriptor is not None:
            result["contact_sheet"] = contact_descriptor
        return result

    def test_dedup_handler_uses_only_configured_corpus_and_derived_report_path(self) -> None:
        captured = {}

        def analyzer(output: Path, render: dict, corpus: dict, **kwargs: object) -> dict:
            captured.update(
                output=output, render=render, corpus=corpus, kwargs=kwargs
            )
            return self.persist_stub_report(
                category="dedup",
                job_id=self.job_id,
                lane_id="motivation",
                render=render,
                report_path=kwargs["report_path"],
            )

        result = handle_dedup(self.task("dedup_analyzer"), analyzer=analyzer)
        self.assertEqual(captured["output"], self.master)
        self.assertEqual(captured["corpus"]["path"], str(self.corpus))
        self.assertEqual(captured["corpus"]["sha256"], file_sha256(self.corpus))
        expected = self.evidence_root / self.job_id / self.render_id / "dedup.json"
        self.assertEqual(captured["kwargs"]["report_path"], expected)
        self.assertEqual(result["evidence"]["sha256"], file_sha256(expected))

    def test_dedup_rejects_payload_override_empty_corpus_and_duplicate_render(self) -> None:
        called = False

        def analyzer(*args: object, **kwargs: object) -> dict:
            nonlocal called
            called = True
            return {}

        overridden = self.task("dedup_analyzer")
        overridden["payload"]["corpus_snapshot"] = {
            "path": "C:/untrusted.json",
            "sha256": "a" * 64,
        }
        with self.assertRaisesRegex(ValidationError, "may not override"):
            handle_dedup(overridden, analyzer=analyzer)

        self.corpus.write_text('{"entries":[]}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "non-empty"):
            handle_dedup(self.task("dedup_analyzer"), analyzer=analyzer)

        self.corpus.write_text('{"entries":[{}]}\n', encoding="utf-8")
        duplicate = self.task("dedup_analyzer")
        duplicate["upstream_results"].append(
            self.upstream("render", self.render, output_path=str(self.master))
        )
        with self.assertRaisesRegex(ValidationError, "exactly one upstream"):
            handle_dedup(duplicate, analyzer=analyzer)
        self.assertFalse(called)

    def test_visual_handler_binds_shotlist_paths_and_trusted_speaker_policy(self) -> None:
        for lane, speaker_required in (("motivation", True), ("health", False)):
            with self.subTest(lane=lane):
                captured = {}

                def analyzer(
                    output: Path, render: dict, shotlist: dict, **kwargs: object
                ) -> dict:
                    captured.update(
                        output=output,
                        render=render,
                        shotlist=shotlist,
                        kwargs=kwargs,
                    )
                    return self.persist_stub_report(
                        category="visual",
                        job_id=self.job_id,
                        lane_id=lane,
                        render=render,
                        report_path=kwargs["report_path"],
                        contact_sheet_path=kwargs["contact_sheet_path"],
                    )

                result = handle_visual(
                    self.task("visual_analyzer", lane=lane), analyzer=analyzer
                )
                expected_directory = self.evidence_root / self.job_id / self.render_id
                self.assertEqual(
                    captured["shotlist"]["shots"][0]["shot_id"], "shot_001"
                )
                self.assertEqual(captured["kwargs"]["speaker_required"], speaker_required)
                self.assertEqual(
                    captured["kwargs"]["report_path"], expected_directory / "visual.json"
                )
                self.assertEqual(
                    captured["kwargs"]["contact_sheet_path"],
                    expected_directory / "visual-contact-sheet.pgm",
                )
                self.assertEqual(
                    result["contact_sheet"]["sha256"],
                    file_sha256(expected_directory / "visual-contact-sheet.pgm"),
                )

    def test_visual_missing_face_backend_or_wrong_shotlist_fails_before_analyzer(self) -> None:
        called = False

        def analyzer(*args: object, **kwargs: object) -> dict:
            nonlocal called
            called = True
            return {}

        with mock.patch.dict("os.environ", {"VIDEO_FACTORY_FACE_OBSERVER": ""}):
            with self.assertRaisesRegex(ValidationError, "must be configured"):
                handle_visual(self.task("visual_analyzer"), analyzer=analyzer)

        wrong = self.task("visual_analyzer")
        wrong["upstream_results"][1]["result"]["artifact"]["idea_id"] = "idea_other_001"
        with self.assertRaisesRegex(ValidationError, "not bound"):
            handle_visual(wrong, analyzer=analyzer)

        duplicate = self.task("visual_analyzer")
        duplicate["upstream_results"].append(self.upstream("editor", self.shotlist))
        with self.assertRaisesRegex(ValidationError, "exactly one upstream"):
            handle_visual(duplicate, analyzer=analyzer)
        self.assertFalse(called)

    def test_stdio_invalid_task_returns_nonzero_without_result(self) -> None:
        for main in (dedup_main, visual_main):
            with self.subTest(main=main.__module__):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch("sys.stderr", stderr):
                    code = main(io.StringIO('{"role":"publisher"}'), stdout)
                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("ValidationError", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
