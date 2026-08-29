from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_factory.errors import ValidationError
from video_factory.hyperframes_compiler import compile_hyperframes_project, handle_task
from video_factory.media_freeze import freeze_explicit_media
from video_factory.validators import canonical_json, digest_text

from source_audio_fixtures import build_multisource_manifest


class HyperFramesCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"local-frozen-video-fixture" * 100)
        self.gsap = self.root / "gsap.min.js"
        self.gsap.write_text(
            "window.gsap={timeline:function(){return {paused:function(){}}}};",
            encoding="utf-8",
        )
        self.voice_path = self.root / "voice.wav"
        self.voice_path.write_bytes(b"RIFF" + b"authoritative-fish-voice" * 20)
        self.rights = self._rights_manifest()
        self.frozen = freeze_explicit_media(
            self.rights,
            [{"asset_id": "asset-video-001", "local_path": str(self.source)}],
            self.root / "frozen",
            job_id="job_compile_001",
            allowed_local_roots=[self.root],
        )["artifact"]
        self.script = self._script_package()
        self.shotlist = self._shotlist()
        self.voice = self._voice_manifest()
        self.bgm = self._bgm_manifest()

    def _bgm_manifest(self) -> dict:
        approval = {
            "approved": True,
            "approved_by": "rights-editor@example.test",
            "approved_at": "2026-08-29T10:00:00Z",
            "approval_note": "Licensed BGM reviewed.",
            "rights_manifest_sha256": "a" * 64,
            "reviewed_asset_ids": ["music-001"],
        }
        return {
            "schema_version": "1.1.0",
            "job_id": "job_compile_001",
            "idea_id": "idea_compile_001",
            "lane_id": self.script["lane_id"],
            "bgm_asset_id": "music-001",
            "immutable_wav_path": str(self.voice_path.resolve()),
            "checksums": {
                "rights_manifest_sha256": "a" * 64,
                "frozen_media_manifest_sha256": "b" * 64,
                "source_asset_sha256": hashlib.sha256(self.voice_path.read_bytes()).hexdigest(),
                "immutable_wav_sha256": hashlib.sha256(self.voice_path.read_bytes()).hexdigest(),
                "license_evidence_sha256": "c" * 64,
                "human_approval_sha256": digest_text(canonical_json(approval)),
            },
            "audio": {
                "sample_rate_hz": 48000,
                "channels": 2,
                "sample_width_bits": 16,
                "frames": 720000,
                "duration_seconds": 15,
                "integrated_loudness_lufs": -14.0,
                "loudness_range_lu": 2.0,
                "true_peak_dbtp": -1.5,
            },
            "normalization": {
                "engine": "ffmpeg",
                "ffmpeg_version": "ffmpeg test",
                "recipe_version": "bgm-freeze-1.1.0",
                "integrated_loudness_target_lufs": -14,
                "true_peak_target_dbtp": -1.5,
                "lra_target_lu": 7,
                "deterministic": True,
            },
            "rights": {
                "creator": "Composer",
                "license": "Commercial music license",
                "license_url": "https://example.test/music-license",
                "license_evidence_path": str((self.root / "license.pdf").resolve()),
                "commercial_use": True,
                "modification_allowed": True,
                "attribution_required": False,
                "attribution_text": None,
                "platforms": ["youtube_shorts", "instagram_reels", "tiktok"],
                "territories": ["worldwide"],
                "expires_at": None,
                "rights_status": "approved",
                "human_rights_gate_preserved": True,
                "human_approval": approval,
            },
            "created_at": "2026-08-29T10:00:00Z",
        }

    def _program_manifest(self, audio: dict) -> dict:
        contract = (
            "source_audio_manifest"
            if self.script["lane_id"] == "motivation"
            else "voice_manifest"
        )
        audio_sha = (
            audio["checksums"]["extracted_audio_sha256"]
            if contract == "source_audio_manifest"
            else audio["output_sha256"]
        )
        bgm = self._bgm_manifest()
        return {
            "schema_version": "1.0.0",
            "job_id": "job_compile_001",
            "idea_id": "idea_compile_001",
            "lane_id": self.script["lane_id"],
            "source_authority": {
                "contract": contract,
                "manifest_sha256": digest_text(canonical_json(audio)),
                "audio_sha256": audio_sha,
                "authority": "spoken_content_and_timing",
                "tts": contract == "voice_manifest",
            },
            "bgm": {
                "asset_id": bgm["bgm_asset_id"],
                "manifest_sha256": digest_text(canonical_json(bgm)),
                "audio_sha256": bgm["checksums"]["immutable_wav_sha256"],
                "license_evidence_sha256": bgm["checksums"]["license_evidence_sha256"],
                "human_approval_sha256": bgm["checksums"]["human_approval_sha256"],
            },
            "mix": {
                "engine": "ffmpeg",
                "ffmpeg_version": "ffmpeg test",
                "recipe_version": "program-mix-1.0.0",
                "filtergraph_sha256": "d" * 64,
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
            "immutable_output_path": str(self.voice_path.resolve()),
            "output_sha256": hashlib.sha256(self.voice_path.read_bytes()).hexdigest(),
            "output_bytes": self.voice_path.stat().st_size,
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
            "created_at": "2026-08-29T10:00:01Z",
        }

    def _voice_manifest(self) -> dict:
        spoken = " ".join(item["spoken_text"] for item in self.script["segments"])
        return {
            "schema_version": "1.0.0",
            "provider": "fish_audio",
            "job_id": "job_compile_001",
            "video_id": "job_compile_001",
            "generation_no": 1,
            "generation_limit": 2,
            "request_hash": "a" * 64,
            "text_sha256": hashlib.sha256(spoken.encode("utf-8")).hexdigest(),
            "text_bytes": len(spoken.encode("utf-8")),
            "model": "s2.1-pro",
            "reference_id": "owned-voice-001",
            "voice_rights_status": "approved_owned_voice",
            "immutable_output_path": str(self.voice_path.resolve()),
            "output_sha256": hashlib.sha256(self.voice_path.read_bytes()).hexdigest(),
            "output_bytes": self.voice_path.stat().st_size,
            "audio": {
                "sample_rate_hz": 44100,
                "channels": 1,
                "sample_width_bits": 16,
                "frames": 661500,
                "duration_seconds": 15,
            },
            "render_target_sample_rate_hz": 48000,
            "estimated_cost_usd": 0.001,
            "retry_reason": None,
            "defect_reference": None,
            "defect_sha256": None,
            "retry_of_request_hash": None,
            "retry_of_output_sha256": None,
            "retry_of_generation_status": None,
            "created_at": "2026-08-29T10:00:00Z",
            "completed_at": "2026-08-29T10:00:01Z",
        }

    def _rights_manifest(self) -> dict:
        return {
            "schema_version": "1.0.0",
            "idea_id": "idea_compile_001",
            "assets": [
                {
                    "asset_id": "asset-video-001",
                    "local_path": str(self.source.resolve()),
                    "download_url": None,
                    "landing_url": "https://example.test/video",
                    "creator": "Test owner",
                    "license": "Owned test media",
                    "license_url": "https://example.test/license",
                    "license_receipt": "receipt-001",
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
                    "notes": "Test fixture",
                }
            ],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "missing_asset_ids": [],
                "review_notes": [],
            },
        }

    def _script_package(self) -> dict:
        segments = []
        for index, (start, end, spoken, caption) in enumerate(
            (
                (0, 5, "Первый проверенный тезис.", "ПЕРВЫЙ ТЕЗИС"),
                (5, 10, "Второй проверенный тезис.", "ВТОРОЙ ТЕЗИС"),
                (10, 15, "Третий проверенный тезис.", "ТРЕТИЙ ТЕЗИС"),
            ),
            start=1,
        ):
            segments.append(
                {
                    "segment_id": f"seg_{index:02d}",
                    "start_seconds": start,
                    "end_seconds": end,
                    "spoken_text": spoken,
                    "caption_text": caption,
                    "visual_intent": "Крупный план спикера",
                    "claim_ids": [f"claim_{index:02d}"],
                }
            )
        return {
            "schema_version": "1.0.0",
            "idea_id": "idea_compile_001",
            "job_id": "job_compile_001",
            "lane_id": "health",
            "language": "ru",
            "target_duration_seconds": 15,
            "hook": {
                "spoken_text": "Три проверенных тезиса.",
                "first_frame_text": "ТРИ ТЕЗИСА",
                "duration_seconds": 2,
            },
            "segments": segments,
            "caption_style": {
                "max_lines": 2,
                "max_words_per_card": 4,
                "safe_zone": "center_lower_third",
                "side_labels": False,
            },
            "edit_direction": {
                "visual_world": "Контрастный документальный монтаж",
                "music_mood": "Сдержанная",
                "average_cut_seconds": 2,
                "speaker_scale": 0.9,
            },
            "disclaimer": "Информация не заменяет консультацию специалиста.",
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "review_notes": [],
            },
        }

    def _shotlist(self) -> dict:
        return {
            "schema_version": "1.0.0",
            "idea_id": "idea_compile_001",
            "duration_seconds": 15,
            "aspect": "9:16",
            "shots": [
                {
                    "shot_id": f"shot_{index:02d}",
                    "start": start,
                    "end": end,
                    "narration": f"Тезис {index}",
                    "caption": f"ТЕЗИС {index}",
                    "visual_intent": "Крупный план",
                    "asset_id": "asset-video-001",
                    "source_in": start,
                    "source_out": end,
                    "claim_ids": [f"claim_{index:02d}"],
                    "transition": "hard_cut",
                }
                for index, (start, end) in enumerate(((0, 5), (5, 10), (10, 15)), 1)
            ],
        }

    def compile(self, output: str = "out", *, audio: dict | None = None) -> dict:
        selected_audio = audio or self.voice
        selected_bgm = self._bgm_manifest()
        return compile_hyperframes_project(
            job_id="job_compile_001",
            shotlist=self.shotlist,
            script_package=self.script,
            frozen_media_manifest=self.frozen,
            authoritative_audio_manifest=selected_audio,
            bgm_manifest=selected_bgm,
            program_audio_manifest=self._program_manifest(selected_audio),
            output_root=self.root / output,
            gsap_source_path=self.gsap,
        )

    def test_compiles_deterministic_local_preview_project(self) -> None:
        first = self.compile("out-a")
        second = self.compile("out-b")
        self.assertEqual(
            first["artifact"]["project_tree_sha256"],
            second["artifact"]["project_tree_sha256"],
        )
        artifact = first["artifact"]
        self.assertFalse(artifact["preview"]["render_authorized"])
        self.assertEqual(artifact["composition"]["width"], 1080)
        self.assertEqual(artifact["composition"]["height"], 1920)
        index = (Path(first["project_root"]) / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("https://", index)
        self.assertIn('data-composition-id="main"', index)
        self.assertEqual(index.count("gsap.timeline({ paused: true })"), 1)
        self.assertEqual(index.count(" muted playsinline"), 3)
        self.assertEqual(index.count("<audio id="), 1)
        self.assertIn('id="vf-program-mix"', index)
        self.assertIn('src="assets/audio/program_mix.wav"', index)
        self.assertNotIn('src="assets/media/media-001-', index.split("<audio", 1)[1])
        self.assertEqual(
            artifact["bindings"]["authoritative_audio"]["contract"],
            "voice_manifest",
        )
        narration = Path(first["project_root"]) / "assets" / "audio" / "program_mix.wav"
        self.assertEqual(narration.read_bytes(), self.voice_path.read_bytes())
        self.assertIn('data-start="5" data-duration="5"', index)
        self.assertIn("ВТОРОЙ ТЕЗИС", index)

    def test_reuses_identical_output_and_rejects_timing_mismatch(self) -> None:
        first = self.compile()
        second = self.compile()
        self.assertFalse(first["compiler_execution"]["reused"])
        self.assertTrue(second["compiler_execution"]["reused"])

        self.script["target_duration_seconds"] = 16
        with self.assertRaisesRegex(ValidationError, "duration_seconds must equal"):
            self.compile("out-mismatch")

    def test_rejects_tampered_path_remote_dependency_and_bytes(self) -> None:
        remote = copy.deepcopy(self.frozen)
        remote["assets"][0]["frozen_path"] = "https://evil.test/source.mp4"
        with self.assertRaises(ValidationError):
            compile_hyperframes_project(
                job_id="job_compile_001",
                shotlist=self.shotlist,
                script_package=self.script,
                frozen_media_manifest=remote,
                authoritative_audio_manifest=self.voice,
                bgm_manifest=self._bgm_manifest(),
                program_audio_manifest=self._program_manifest(self.voice),
                output_root=self.root / "remote",
                gsap_source_path=self.gsap,
            )

        with self.assertRaisesRegex(ValidationError, "GSAP dependency must be a local file"):
            compile_hyperframes_project(
                job_id="job_compile_001",
                shotlist=self.shotlist,
                script_package=self.script,
                frozen_media_manifest=self.frozen,
                authoritative_audio_manifest=self.voice,
                bgm_manifest=self._bgm_manifest(),
                program_audio_manifest=self._program_manifest(self.voice),
                output_root=self.root / "remote-gsap",
                gsap_source_path="https://cdn.example.test/gsap.js",
            )

        frozen_file = Path(self.frozen["frozen_root"]) / self.frozen["assets"][0]["frozen_path"]
        frozen_file.write_bytes(frozen_file.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValidationError, "frozen media verification failed"):
            self.compile("tampered")

    def test_rejects_tampered_or_unbound_authoritative_audio(self) -> None:
        tampered = copy.deepcopy(self.voice)
        self.voice_path.write_bytes(self.voice_path.read_bytes() + b"tamper")
        with self.assertRaisesRegex(ValidationError, "output_bytes|checksum"):
            self.compile("tampered-audio", audio=tampered)

        self.voice_path.write_bytes(b"RIFF" + b"authoritative-fish-voice" * 20)
        wrong_text = copy.deepcopy(self.voice)
        wrong_text["output_sha256"] = hashlib.sha256(
            self.voice_path.read_bytes()
        ).hexdigest()
        wrong_text["output_bytes"] = self.voice_path.stat().st_size
        wrong_text["text_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "text hash"):
            self.compile("wrong-text", audio=wrong_text)

    def test_motivation_uses_cleared_source_audio_and_rejects_mixed_upstream(self) -> None:
        self.script["lane_id"] = "motivation"
        transcript = " ".join(item["spoken_text"] for item in self.script["segments"])
        frozen_item = self.frozen["assets"][0]
        frozen_source = (
            Path(self.frozen["frozen_root"]) / frozen_item["frozen_path"]
        ).resolve()
        source_audio = {
            "schema_version": "1.0.0",
            "job_id": "job_compile_001",
            "lane": "motivation",
            "audio_asset_id": "asset-video-001",
            "source_video_uri_or_path": str(frozen_source),
            "source_in_seconds": 0,
            "source_out_seconds": 15,
            "speaker_name": "Licensed speaker",
            "transcript": transcript,
            "rights_status": "commercial_license_confirmed",
            "rights_evidence": "receipt-001",
            "original_audio_only": True,
            "tts": False,
            "extracted_audio_path": str(self.voice_path.resolve()),
            "checksums": {
                "source_video_sha256": frozen_item["sha256"],
                "extracted_audio_sha256": hashlib.sha256(
                    self.voice_path.read_bytes()
                ).hexdigest(),
                "transcript_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
            },
            "created_at": "2026-08-29T10:00:00Z",
        }
        result = self.compile("motivation", audio=source_audio)
        self.assertEqual(
            result["artifact"]["bindings"]["authoritative_audio"]["contract"],
            "source_audio_manifest",
        )

        task = {
            "id": "task_compile_001",
            "job_id": "job_compile_001",
            "role": "compiler",
            "pod": "motivation",
            "payload": {
                "job_id": "job_compile_001",
                "lane_id": "motivation",
                "required_result_contract": "project_manifest",
            },
            "upstream_results": [
                {"role": "script", "result": {"artifact": self.script}},
                {"role": "editor", "result": {"artifact": self.shotlist}},
                {"role": "media", "result": {"artifact": self.frozen}},
                {"role": "source_audio", "result": {"artifact": source_audio}},
                {"role": "voice", "result": {"artifact": self.voice}},
            ],
        }
        with mock.patch.dict(
            os.environ,
            {
                "VIDEO_FACTORY_GSAP_PATH": str(self.gsap),
                "VIDEO_FACTORY_HYPERFRAMES_PROJECT_ROOT": str(self.root / "handler"),
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ValidationError, "exactly one upstream"):
                handle_task(task)

    def test_motivation_compiler_consumes_every_ordered_source_segment(self) -> None:
        self.script["lane_id"] = "motivation"
        spoken = [item["spoken_text"] for item in self.script["segments"]]
        source_audio = build_multisource_manifest(
            self.root,
            job_id="job_compile_001",
            frozen_root=Path(self.frozen["frozen_root"]),
            frozen_assets=[self.frozen["assets"][0], self.frozen["assets"][0]],
            transcript_parts=[spoken[0], " ".join(spoken[1:])],
            durations=[7.5, 7.5],
        )
        result = self.compile("motivation-multi", audio=source_audio)
        binding = result["artifact"]["bindings"]["authoritative_audio"]
        self.assertEqual(binding["contract"], "source_audio_manifest")
        self.assertEqual(binding["schema_version"], "1.1.0")
        self.assertEqual(binding["audio_sha256"], source_audio["checksums"]["extracted_audio_sha256"])

        bad_source_hash = copy.deepcopy(source_audio)
        bad_source_hash["segments"][1]["checksums"]["source_video_sha256"] = "f" * 64
        bindings_sha = digest_text(canonical_json(bad_source_hash["segments"]))
        bad_source_hash["checksums"]["segment_bindings_sha256"] = bindings_sha
        bad_source_hash["audio_asset_id"] = f"source-audio-program-{bindings_sha[:24]}"
        with self.assertRaisesRegex(ValidationError, "segment 1 source hash"):
            self.compile("motivation-multi-source-tamper", audio=bad_source_hash)

        wrong_pcm_order = copy.deepcopy(source_audio)
        first = wrong_pcm_order["segments"][0]
        second = wrong_pcm_order["segments"][1]
        first["extracted_audio_path"], second["extracted_audio_path"] = (
            second["extracted_audio_path"], first["extracted_audio_path"]
        )
        first["checksums"]["extracted_audio_sha256"], second["checksums"]["extracted_audio_sha256"] = (
            second["checksums"]["extracted_audio_sha256"],
            first["checksums"]["extracted_audio_sha256"],
        )
        bindings_sha = digest_text(canonical_json(wrong_pcm_order["segments"]))
        wrong_pcm_order["checksums"]["segment_bindings_sha256"] = bindings_sha
        wrong_pcm_order["audio_asset_id"] = f"source-audio-program-{bindings_sha[:24]}"
        with self.assertRaisesRegex(ValidationError, "ordered PCM concatenation"):
            self.compile("motivation-multi-order-tamper", audio=wrong_pcm_order)


if __name__ == "__main__":
    unittest.main()
