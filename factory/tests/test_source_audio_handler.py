from __future__ import annotations

import hashlib
import json
import os
import copy
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from video_factory.errors import ValidationError
from video_factory.media_freeze import freeze_explicit_media
from video_factory.media_tools import resolve_media_binary
from video_factory.source_audio_handler import handle_task, main
from video_factory.source_audio import verify_multisource_program
from video_factory.validators import digest_text


class SourceAudioHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        try:
            self.ffmpeg = resolve_media_binary("ffmpeg")
            resolve_media_binary("ffprobe")
        except ValidationError as exc:
            self.skipTest(str(exc))
        self.source = self.root / "speaker.mp4"
        completed = subprocess.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x90:r=30:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=2",
                "-c:v",
                "mpeg4",
                "-q:v",
                "5",
                "-c:a",
                "aac",
                "-shortest",
                str(self.source),
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest("fixture FFmpeg cannot encode MPEG-4/AAC")
        self.rights = self._rights_manifest()
        frozen = freeze_explicit_media(
            self.rights,
            [{"asset_id": "speaker-video-001", "local_path": str(self.source)}],
            self.root / "frozen",
            job_id="job_motivation_001",
            allowed_local_roots=[self.root],
        )
        self.frozen = frozen["artifact"]

    def _rights_manifest(self) -> dict:
        return {
            "schema_version": "1.0.0",
            "idea_id": "idea_motivation_001",
            "assets": [
                {
                    "asset_id": "speaker-video-001",
                    "local_path": str(self.source.resolve()),
                    "download_url": None,
                    "landing_url": "https://example.test/speaker-video",
                    "creator": "Rights owner",
                    "license": "Commercial speaker license",
                    "license_url": "https://example.test/license",
                    "license_receipt": "rights/speaker-video-001.pdf",
                    "retrieved_at": "2026-08-29T10:00:00Z",
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
                    "notes": "Test fixture only",
                }
            ],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "missing_asset_ids": [],
                "review_notes": [],
            },
        }

    def task(self) -> dict:
        return {
            "id": "task_source_audio_001",
            "job_id": "job_motivation_001",
            "role": "source_audio",
            "pod": "motivation",
            "attempt_count": 1,
            "payload": {
                "job_id": "job_motivation_001",
                "lane_id": "motivation",
                "required_result_contract": "source_audio_manifest",
                "source_audio_selection": {
                    "asset_id": "speaker-video-001",
                    "source_in_seconds": 0.25,
                    "source_out_seconds": 1.25,
                    "speaker_name": "Тестовый спикер",
                    "transcript": "Дисциплина важнее настроения.",
                    "rights_status": "commercial_license_confirmed",
                },
            },
            "upstream_results": [
                {
                    "task_id": "task_rights_001",
                    "role": "rights",
                    "result": {"artifact": self.rights},
                },
                {
                    "task_id": "task_media_001",
                    "role": "media",
                    "result": {"artifact": self.frozen},
                },
            ],
        }

    def multi_task(self) -> dict:
        second_source = self.root / "speaker-second.mp4"
        completed = subprocess.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=160x90:r=30:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=660:sample_rate=48000:duration=2",
                "-c:v",
                "mpeg4",
                "-q:v",
                "5",
                "-c:a",
                "aac",
                "-shortest",
                str(second_source),
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest("fixture FFmpeg cannot encode second MPEG-4/AAC source")
        rights = copy.deepcopy(self.rights)
        second_right = copy.deepcopy(rights["assets"][0])
        second_right.update(
            {
                "asset_id": "speaker-video-002",
                "local_path": str(second_source.resolve()),
                "landing_url": "https://example.test/speaker-video-2",
                "license_receipt": "rights/speaker-video-002.pdf",
            }
        )
        rights["assets"].append(second_right)
        frozen = freeze_explicit_media(
            rights,
            [
                {"asset_id": "speaker-video-001", "local_path": str(self.source)},
                {"asset_id": "speaker-video-002", "local_path": str(second_source)},
            ],
            self.root / "frozen-multi",
            job_id="job_motivation_001",
            allowed_local_roots=[self.root],
        )["artifact"]
        original_en = "Discipline matters more than mood."
        translated_ru = "Дисциплина важнее настроения."
        review = {
            "approved": True,
            "approved_by": "translator@example.test",
            "approved_at": "2026-08-30T08:00:00Z",
            "asset_id": "speaker-video-002",
            "source_in_seconds": 0.25,
            "source_out_seconds": 1.25,
            "original_transcript_sha256": digest_text(original_en),
            "russian_transcript_sha256": digest_text(translated_ru),
            "review_notes": "Смысл и контекст сверены человеком.",
        }
        body = self.task()
        body["payload"].pop("source_audio_selection")
        body["payload"]["source_audio_selections"] = [
            {
                "asset_id": "speaker-video-001",
                "source_in_seconds": 0.25,
                "source_out_seconds": 1.25,
                "speaker_name": "Русский спикер",
                "source_language": "ru",
                "original_transcript": "Действуй даже без настроения.",
                "transcript": "Действуй даже без настроения.",
                "bilingual_review": None,
                "rights_status": "commercial_license_confirmed",
            },
            {
                "asset_id": "speaker-video-002",
                "source_in_seconds": 0.25,
                "source_out_seconds": 1.25,
                "speaker_name": "English speaker",
                "source_language": "en",
                "original_transcript": original_en,
                "transcript": translated_ru,
                "bilingual_review": review,
                "rights_status": "commercial_license_confirmed",
            },
        ]
        body["upstream_results"][0]["result"]["artifact"] = rights
        body["upstream_results"][1]["result"]["artifact"] = frozen
        return body

    def test_extracts_real_pcm_wav_and_binds_all_hashes(self) -> None:
        output_root = self.root / "source-audio"
        with mock.patch.dict(
            os.environ,
            {"VIDEO_FACTORY_SOURCE_AUDIO_OUTPUT_ROOT": str(output_root)},
            clear=False,
        ):
            result = handle_task(self.task())

        artifact = result["artifact"]
        output = Path(result["output_path"])
        self.assertTrue(output.is_file())
        self.assertEqual(output.parent, output_root / "job_motivation_001")
        with wave.open(str(output), "rb") as audio:
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.getsampwidth(), 2)
            self.assertEqual(audio.getframerate(), 48_000)
            self.assertAlmostEqual(audio.getnframes() / 48_000, 1.0, delta=0.1)
        self.assertEqual(
            artifact["checksums"]["source_video_sha256"],
            self.frozen["assets"][0]["sha256"],
        )
        self.assertEqual(
            artifact["checksums"]["extracted_audio_sha256"],
            hashlib.sha256(output.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            artifact["checksums"]["transcript_sha256"],
            digest_text("Дисциплина важнее настроения."),
        )
        self.assertEqual(artifact["rights_evidence"], "rights/speaker-video-001.pdf")
        self.assertTrue(artifact["original_audio_only"])
        self.assertFalse(artifact["tts"])
        stored = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(stored, artifact)

    def test_repeat_reuses_immutable_job_scoped_output(self) -> None:
        output_root = self.root / "source-audio"
        with mock.patch.dict(
            os.environ,
            {"VIDEO_FACTORY_SOURCE_AUDIO_OUTPUT_ROOT": str(output_root)},
            clear=False,
        ):
            first = handle_task(self.task())
            second = handle_task(self.task())
        self.assertEqual(first["output_path"], second["output_path"])
        self.assertFalse(first["source_audio_execution"]["reused"])
        self.assertTrue(second["source_audio_execution"]["reused"])

    def test_multisource_extracts_ordered_segments_and_program_without_hidden_premix(self) -> None:
        output_root = self.root / "source-audio-multi"
        task = self.multi_task()
        with mock.patch.dict(
            os.environ,
            {"VIDEO_FACTORY_SOURCE_AUDIO_OUTPUT_ROOT": str(output_root)},
            clear=False,
        ):
            first = handle_task(task)
            second = handle_task(task)
        artifact = first["artifact"]
        self.assertEqual(artifact["schema_version"], "1.1.0")
        self.assertEqual(artifact["segment_count"], 2)
        self.assertEqual(
            [row["asset_id"] for row in artifact["segments"]],
            ["speaker-video-001", "speaker-video-002"],
        )
        self.assertEqual(artifact["segments"][0]["program_in_seconds"], 0)
        self.assertAlmostEqual(
            artifact["segments"][0]["program_out_seconds"],
            artifact["segments"][1]["program_in_seconds"],
            delta=1 / 48_000,
        )
        self.assertEqual(verify_multisource_program(artifact), Path(first["output_path"]))
        self.assertEqual(first["output_path"], second["output_path"])
        self.assertTrue(second["source_audio_execution"]["reused"])

    def test_multisource_rejects_xor_bilingual_range_rights_and_segment_tampering(self) -> None:
        task = self.multi_task()
        task["payload"]["source_audio_selection"] = copy.deepcopy(
            self.task()["payload"]["source_audio_selection"]
        )
        with self.assertRaisesRegex(ValidationError, "exactly one"):
            handle_task(task)

        task = self.multi_task()
        task["payload"]["source_audio_selections"][1]["bilingual_review"][
            "russian_transcript_sha256"
        ] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "not bound"):
            handle_task(task)

        task = self.multi_task()
        task["payload"]["source_audio_selections"][1]["source_out_seconds"] = 5.0
        task["payload"]["source_audio_selections"][1]["bilingual_review"][
            "source_out_seconds"
        ] = 5.0
        with self.assertRaisesRegex(ValidationError, "exceeds the frozen source duration"):
            handle_task(task)

        task = self.multi_task()
        task["upstream_results"][0]["result"]["artifact"]["assets"][1][
            "license_receipt"
        ] = None
        task["payload"]["source_audio_selections"][1][
            "rights_status"
        ] = "consent_confirmed"
        rights = task["upstream_results"][0]["result"]["artifact"]
        task["upstream_results"][1]["result"]["artifact"] = freeze_explicit_media(
            rights,
            [
                {"asset_id": "speaker-video-001", "local_path": str(self.source)},
                {
                    "asset_id": "speaker-video-002",
                    "local_path": rights["assets"][1]["local_path"],
                },
            ],
            self.root / "frozen-multi-no-evidence",
            job_id="job_motivation_001",
            allowed_local_roots=[self.root],
        )["artifact"]
        with self.assertRaisesRegex(ValidationError, "lacks evidence"):
            handle_task(task)

        task = self.multi_task()
        output_root = self.root / "source-audio-multi-tamper"
        with mock.patch.dict(
            os.environ,
            {"VIDEO_FACTORY_SOURCE_AUDIO_OUTPUT_ROOT": str(output_root)},
            clear=False,
        ):
            result = handle_task(task)
            Path(result["artifact"]["segments"][0]["extracted_audio_path"]).write_bytes(
                b"tampered-segment"
            )
            with self.assertRaisesRegex(ValidationError, "immutable source-audio segment"):
                handle_task(task)

    def test_rejects_wrong_role_lane_job_and_non_explicit_selection(self) -> None:
        body = self.task()
        body["role"] = "voice"
        with self.assertRaisesRegex(ValidationError, "only role='source_audio'"):
            handle_task(body)

        body = self.task()
        body["pod"] = "health"
        with self.assertRaisesRegex(ValidationError, "only the motivation lane"):
            handle_task(body)

        body = self.task()
        body["payload"]["job_id"] = "job_other_001"
        with self.assertRaisesRegex(ValidationError, "not bound"):
            handle_task(body)

        body = self.task()
        body["payload"]["source_audio_selection"]["download_url"] = "https://evil.test/x"
        with self.assertRaisesRegex(ValidationError, "unsupported fields"):
            handle_task(body)

    def test_rejects_unpassed_rights_and_publishable_status_without_evidence(self) -> None:
        body = self.task()
        body["upstream_results"][0]["result"]["artifact"]["decision"]["passed"] = False
        with self.assertRaises(ValidationError):
            handle_task(body)

        body = self.task()
        rights = body["upstream_results"][0]["result"]["artifact"]
        rights["decision"]["passed"] = True
        rights["assets"][0]["license_receipt"] = None
        body["payload"]["source_audio_selection"]["rights_status"] = "consent_confirmed"
        # Rebuild a frozen artifact bound to the modified rights document so the
        # evidence failure, rather than a hash mismatch, is the blocking gate.
        body["upstream_results"][1]["result"]["artifact"] = freeze_explicit_media(
            rights,
            [{"asset_id": "speaker-video-001", "local_path": str(self.source)}],
            self.root / "frozen-no-receipt",
            job_id="job_motivation_001",
            allowed_local_roots=[self.root],
        )["artifact"]
        with self.assertRaisesRegex(ValidationError, "lacks evidence"):
            handle_task(body)

    def test_rejects_tampered_frozen_bytes_before_extraction(self) -> None:
        frozen_path = (
            Path(self.frozen["frozen_root"]) / self.frozen["assets"][0]["frozen_path"]
        )
        frozen_path.write_bytes(frozen_path.read_bytes() + b"tampered")
        with mock.patch(
            "video_factory.source_audio_handler._extract_pcm_wav"
        ) as extract:
            with self.assertRaisesRegex(ValidationError, "frozen media verification failed"):
                handle_task(self.task())
        extract.assert_not_called()

    def test_rejects_interval_outside_actual_media(self) -> None:
        body = self.task()
        body["payload"]["source_audio_selection"]["source_out_seconds"] = 5.0
        with self.assertRaisesRegex(ValidationError, "exceeds the frozen source duration"):
            handle_task(body)

    def test_stdio_returns_canonical_result_and_fails_closed(self) -> None:
        from io import StringIO

        output = StringIO()
        with mock.patch.dict(
            os.environ,
            {"VIDEO_FACTORY_SOURCE_AUDIO_OUTPUT_ROOT": str(self.root / "stdio-output")},
            clear=False,
        ):
            code = main(StringIO(json.dumps(self.task())), output)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["artifact"]["lane"], "motivation")

        with mock.patch("sys.stderr", new_callable=StringIO) as stderr:
            code = main(StringIO("[]"), StringIO())
        self.assertEqual(code, 2)
        self.assertIn("source_audio_handler_error", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
