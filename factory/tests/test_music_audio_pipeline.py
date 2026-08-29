from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from video_factory.bgm_handler import (
    _normalize_wav as normalize_bgm_wav,
    handle_task as freeze_bgm,
)
from video_factory.contracts import validate_artifact
from video_factory.errors import ValidationError
from video_factory.media_freeze import freeze_explicit_media
from video_factory.music_catalog import approved_track_sha256
from video_factory.program_audio_handler import (
    _authoritative_audio,
    _mix_program_wav,
    handle_task as mix_program_audio,
)
from video_factory.queue import Dispatcher
from video_factory.validators import canonical_json, digest_text

from source_audio_fixtures import build_multisource_manifest


_HOME_FFMPEG = Path.home() / "bin" / "ffmpeg.exe"
_FFMPEG = shutil.which("ffmpeg") or (str(_HOME_FFMPEG) if _HOME_FFMPEG.is_file() else None)


def write_tone(
    path: Path,
    *,
    duration: float,
    sample_rate: int = 48_000,
    channels: int = 2,
    frequency: float = 220.0,
    amplitude: float = 0.2,
) -> None:
    frames = int(duration * sample_rate)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        for index in range(frames):
            sample = int(
                max(-1.0, min(1.0, amplitude * math.sin(2 * math.pi * frequency * index / sample_rate)))
                * 32767
            )
            audio.writeframesraw(struct.pack("<h", sample) * channels)


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MusicAudioPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_root = self.root / "inputs"
        self.input_root.mkdir()
        self.evidence_root = self.root / "rights-evidence"
        self.evidence_root.mkdir()
        self.bgm_source = self.input_root / "licensed-bed.wav"
        write_tone(self.bgm_source, duration=2.0, frequency=110, amplitude=0.12)
        self.receipt = self.evidence_root / "music-license.pdf"
        self.receipt.write_bytes(b"immutable-commercial-music-license-evidence")
        self.rights = self._rights_manifest()
        self.catalog_path = self.root / "lane-music-catalog.json"
        catalog = json.loads(
            (Path(__file__).parents[1] / "music" / "lane_music_catalog.json").read_text(
                encoding="utf-8"
            )
        )
        track = {
            "track_id": "health-clean-licensed-001",
            "asset_id": "music-bed-001",
            "lane_id": "health",
            "archetype_id": "health_clean_explainer",
            "slot_id": "health_clean_cross_01",
            "status": "approved",
            "local_wav_path": str(self.bgm_source.resolve()),
            "sha256": file_sha(self.bgm_source),
            "reference_fingerprint_ids": [],
            "rights": {
                "creator": "Test Composer",
                "license_name": "Commercial social-video license",
                "license_source": "independent_commercial_license",
                "license_url": "https://license.example.test/terms",
                "license_evidence_path": str(self.receipt.resolve()),
                "license_evidence_sha256": file_sha(self.receipt),
                "commercial_use": True,
                "modification_allowed": True,
                "platform_scope": ["youtube_shorts", "instagram_reels", "tiktok"],
                "territories": ["worldwide"],
                "placements": ["organic_feed"],
                "expires_at": None,
                "attribution_required": False,
                "attribution_text": None,
            },
            "human_approval": {
                "approved": True,
                "approved_by": "music-curator@example.test",
                "approved_at": "2026-08-30T10:00:00Z",
                "approval_note": "Exact WAV, licence scope and lane vibe reviewed.",
                "reviewed_track_id": "health-clean-licensed-001",
                "approved_track_sha256": "pending",
            },
        }
        track["human_approval"]["approved_track_sha256"] = approved_track_sha256(track)
        catalog["tracks"].append(track)
        next(
            slot
            for slot in catalog["track_slots"]
            if slot["slot_id"] == "health_clean_cross_01"
        )["status"] = "ready"
        self.catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.frozen = freeze_explicit_media(
            self.rights,
            [{"asset_id": "music-bed-001", "local_path": str(self.bgm_source)}],
            self.root / "frozen",
            job_id="job_audio_001",
            allowed_local_roots=[self.input_root],
        )["artifact"]
        self.approval = {
            "approved": True,
            "approved_by": "rights-editor@example.test",
            "approved_at": "2026-08-30T10:00:00Z",
            "approval_note": "Exact music asset, platforms and receipt reviewed.",
            "rights_manifest_sha256": digest_text(canonical_json(self.rights)),
            "reviewed_asset_ids": ["music-bed-001"],
        }
        self.runtime = self.root / "runtime"

    def _rights_manifest(self) -> dict:
        return {
            "schema_version": "1.0.0",
            "idea_id": "idea_audio_001",
            "assets": [
                {
                    "asset_id": "music-bed-001",
                    "local_path": str(self.bgm_source.resolve()),
                    "download_url": None,
                    "landing_url": "https://license.example.test/music/001",
                    "creator": "Test Composer",
                    "license": "Commercial social-video license",
                    "license_url": "https://license.example.test/terms",
                    "license_receipt": str(self.receipt.resolve()),
                    "retrieved_at": "2026-08-30T09:00:00Z",
                    "commercial_use": True,
                    "modification_allowed": True,
                    "attribution_required": False,
                    "attribution_text": None,
                    "model_release": "not_applicable",
                    "property_release": "not_applicable",
                    "platforms": ["youtube_shorts", "instagram_reels", "tiktok"],
                    "territories": ["worldwide"],
                    "expires_at": None,
                    "rights_status": "approved",
                    "notes": "Local licensed fixture",
                }
            ],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "missing_asset_ids": [],
                "review_notes": [],
            },
        }

    def _bgm_task(
        self,
        *,
        rights: dict | None = None,
        approval: dict | None | object = ...,
    ) -> dict:
        rights_result: dict = {"artifact": rights or self.rights}
        if approval is ...:
            rights_result["human_approval"] = self.approval
        elif isinstance(approval, dict):
            rights_result["human_approval"] = approval
        return {
            "id": "task_bgm_001",
            "job_id": "job_audio_001",
            "role": "bgm",
            "pod": "health",
            "payload": {
                "job_id": "job_audio_001",
                "idea_id": "idea_audio_001",
                "lane_id": "health",
                "bgm_selection": {
                    "catalog_id": "video-factory-lane-music",
                    "catalog_version": "2026-08-30.1",
                    "track_id": "health-clean-licensed-001",
                    "asset_id": "music-bed-001",
                    "archetype_id": "health_clean_explainer",
                    "requested_platforms": [
                        "youtube_shorts", "instagram_reels", "tiktok"
                    ],
                    "requested_territories": ["worldwide"],
                    "requested_placements": ["organic_feed"],
                },
                "required_result_contract": "bgm_manifest",
            },
            "upstream_results": [
                {"role": "rights", "result": rights_result},
                {"role": "media", "result": {"artifact": self.frozen}},
            ],
        }

    def _env(self) -> dict[str, str]:
        return {
            "VIDEO_FACTORY_RUNTIME_ROOT": str(self.runtime),
            "VIDEO_FACTORY_BGM_OUTPUT_ROOT": str(self.runtime / "bgm"),
            "VIDEO_FACTORY_RIGHTS_EVIDENCE_ROOT": str(self.evidence_root),
            "VIDEO_FACTORY_MUSIC_CATALOG": str(self.catalog_path),
        }

    def test_bgm_freeze_is_local_checksum_bound_and_idempotent(self) -> None:
        with mock.patch.dict(os.environ, self._env(), clear=False):
            first = freeze_bgm(self._bgm_task())
            second = freeze_bgm(self._bgm_task())
        artifact = first["artifact"]
        self.assertFalse(first["bgm_execution"]["network_access"])
        self.assertFalse(first["bgm_execution"]["reused"])
        self.assertTrue(second["bgm_execution"]["reused"])
        self.assertEqual(artifact, second["artifact"])
        self.assertEqual(artifact["audio"]["sample_rate_hz"], 48_000)
        self.assertEqual(artifact["audio"]["channels"], 2)
        self.assertEqual(artifact["schema_version"], "1.2.0")
        self.assertEqual(
            artifact["music_selection"]["archetype_id"], "health_clean_explainer"
        )
        self.assertLessEqual(
            abs(artifact["audio"]["integrated_loudness_lufs"] + 14), 0.5
        )
        self.assertLessEqual(artifact["audio"]["true_peak_dbtp"], -1.4)
        self.assertEqual(
            artifact["normalization"]["recipe_version"], "bgm-freeze-1.1.0"
        )
        self.assertEqual(
            artifact["checksums"]["human_approval_sha256"],
            digest_text(canonical_json(self.approval)),
        )
        self.assertEqual(
            artifact["checksums"]["license_evidence_sha256"], file_sha(self.receipt)
        )
        self.assertIs(validate_artifact("bgm_manifest", artifact), artifact)

    def test_bgm_rejects_missing_or_stale_human_approval(self) -> None:
        with mock.patch.dict(os.environ, self._env(), clear=False):
            with self.assertRaisesRegex(ValidationError, "human rights approval"):
                freeze_bgm(self._bgm_task(approval=None))
            changed = copy.deepcopy(self.rights)
            changed["assets"][0]["license"] = "Substituted license"
            with self.assertRaisesRegex(ValidationError, "exact RightsManifest"):
                freeze_bgm(self._bgm_task(rights=changed))

    def test_bgm_rejects_asset_only_or_out_of_scope_selection(self) -> None:
        legacy = self._bgm_task()
        legacy["payload"]["bgm_selection"] = {"asset_id": "music-bed-001"}
        with mock.patch.dict(os.environ, self._env(), clear=False):
            with self.assertRaisesRegex(ValidationError, "exact lane music catalog"):
                freeze_bgm(legacy)

        paid = self._bgm_task()
        paid["payload"]["bgm_selection"]["requested_placements"] = ["paid_ads"]
        with mock.patch.dict(os.environ, self._env(), clear=False):
            with self.assertRaisesRegex(ValidationError, "requested placements"):
                freeze_bgm(paid)

    def test_bgm_rejects_tiktok_cml_cross_platform_inference(self) -> None:
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        track = catalog["tracks"][0]
        track["rights"]["license_source"] = "tiktok_commercial_music_library"
        track["human_approval"]["approved_track_sha256"] = approved_track_sha256(track)
        self.catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with mock.patch.dict(os.environ, self._env(), clear=False):
            with self.assertRaisesRegex(ValidationError, "cannot be expanded beyond TikTok"):
                freeze_bgm(self._bgm_task())

    def test_bgm_rejects_receipt_mutation_after_freeze(self) -> None:
        with mock.patch.dict(os.environ, self._env(), clear=False):
            freeze_bgm(self._bgm_task())
            self.receipt.write_bytes(b"substituted-rights-evidence")
            with self.assertRaisesRegex(
                ValidationError, "license evidence checksum changed|rights evidence changed"
            ):
                freeze_bgm(self._bgm_task())

    def test_queue_rejects_substituted_rights_approval_and_receipt_bytes(self) -> None:
        env = self._env()
        with mock.patch.dict(os.environ, env, clear=False):
            produced = freeze_bgm(self._bgm_task())
        queue = Dispatcher(self.root / "queue.sqlite3")
        rights_task = queue.enqueue(
            role="rights",
            pod="health",
            kind="rights_job",
            payload={
                "job_id": "job_audio_001",
                "idea_id": "idea_audio_001",
                "lane_id": "health",
                "required_result_contract": "rights_manifest",
                "human_gate": True,
                "rights_checksum_bound": True,
            },
            idempotency_key="music-rights",
        )["task"]
        rights_claim = queue.claim(
            worker_id="human-rights",
            role="rights",
            idempotency_key="claim-music-rights",
        )["task"]
        queue.complete(
            rights_task["id"],
            lease_token=rights_claim["lease_token"],
            result={"artifact": self.rights, "human_approval": self.approval},
            idempotency_key="complete-music-rights",
        )
        media_task = queue.enqueue(
            role="media",
            pod="health",
            kind="media_job",
            payload={
                "job_id": "job_audio_001",
                "idea_id": "idea_audio_001",
                "lane_id": "health",
                "required_result_contract": "frozen_media_manifest",
            },
            dependency_task_id=rights_task["id"],
            idempotency_key="music-media",
        )["task"]
        media_claim = queue.claim(
            worker_id="media-worker",
            role="media",
            idempotency_key="claim-music-media",
        )["task"]
        queue.complete(
            media_task["id"],
            lease_token=media_claim["lease_token"],
            result={"artifact": self.frozen},
            idempotency_key="complete-music-media",
        )
        bgm_task = queue.enqueue(
            role="bgm",
            pod="health",
            kind="bgm_job",
            payload={
                "job_id": "job_audio_001",
                "idea_id": "idea_audio_001",
                "lane_id": "health",
                "bgm_selection": {"asset_id": "music-bed-001"},
                "required_result_contract": "bgm_manifest",
            },
            dependency_task_id=media_task["id"],
            idempotency_key="music-bgm",
        )["task"]
        bgm_claim = queue.claim(
            worker_id="bgm-worker",
            role="bgm",
            idempotency_key="claim-music-bgm",
        )["task"]
        substituted = copy.deepcopy(produced["artifact"])
        substituted["rights"]["human_approval"]["approved_by"] = "attacker"
        with self.assertRaisesRegex(ValidationError, "approval checksum"):
            queue.complete(
                bgm_task["id"],
                lease_token=bgm_claim["lease_token"],
                result={**produced, "artifact": substituted},
                idempotency_key="reject-substituted-approval",
            )
        self.receipt.write_bytes(b"substituted-after-human-approval")
        with self.assertRaisesRegex(ValidationError, "license evidence checksum"):
            queue.complete(
                bgm_task["id"],
                lease_token=bgm_claim["lease_token"],
                result=produced,
                idempotency_key="reject-substituted-receipt",
            )

    def _voice_manifest(self, path: Path) -> dict:
        frames = int(15 * 44_100)
        return {
            "schema_version": "1.0.0",
            "provider": "fish_audio",
            "job_id": "job_audio_001",
            "video_id": "job_audio_001",
            "generation_no": 1,
            "generation_limit": 2,
            "request_hash": "a" * 64,
            "text_sha256": "b" * 64,
            "text_bytes": 100,
            "model": "s2.1-pro",
            "reference_id": "owned-voice-001",
            "voice_rights_status": "approved_owned_voice",
            "immutable_output_path": str(path.resolve()),
            "output_sha256": file_sha(path),
            "output_bytes": path.stat().st_size,
            "audio": {
                "sample_rate_hz": 44100,
                "channels": 1,
                "sample_width_bits": 16,
                "frames": frames,
                "duration_seconds": 15,
            },
            "render_target_sample_rate_hz": 48000,
            "estimated_cost_usd": 0,
            "retry_reason": None,
            "defect_reference": None,
            "defect_sha256": None,
            "retry_of_request_hash": None,
            "retry_of_output_sha256": None,
            "retry_of_generation_status": None,
            "created_at": "2026-08-30T10:00:00Z",
            "completed_at": "2026-08-30T10:00:01Z",
        }

    def _shotlist(self) -> dict:
        return {
            "schema_version": "1.0.0",
            "idea_id": "idea_audio_001",
            "duration_seconds": 15,
            "aspect": "9:16",
            "shots": [
                {
                    "shot_id": "shot_audio_001",
                    "start": 0,
                    "end": 15,
                    "narration": "Проверенная речь.",
                    "visual_intent": "Спикер",
                    "asset_id": "video-001",
                    "claim_ids": ["claim-001"],
                }
            ],
        }

    def _program_task(self, bgm: dict, voice: dict) -> dict:
        return {
            "id": "task_audio_mix_001",
            "job_id": "job_audio_001",
            "role": "audio_mix",
            "pod": "health",
            "payload": {
                "job_id": "job_audio_001",
                "idea_id": "idea_audio_001",
                "lane_id": "health",
                "required_result_contract": "program_audio_manifest",
            },
            "upstream_results": [
                {"role": "editor", "result": {"artifact": self._shotlist()}},
                {"role": "bgm", "result": {"artifact": bgm}},
                {"role": "voice", "result": {"artifact": voice}},
            ],
        }

    def test_program_mix_binds_authority_bgm_and_deterministic_recipe(self) -> None:
        voice_path = self.root / "voice.wav"
        write_tone(
            voice_path,
            duration=15,
            sample_rate=44_100,
            channels=1,
            frequency=330,
            amplitude=0.2,
        )
        voice = self._voice_manifest(voice_path)
        with mock.patch.dict(os.environ, self._env(), clear=False):
            bgm = freeze_bgm(self._bgm_task())["artifact"]

        def fake_mix(*, voice: Path, bgm: Path, duration: float, destination: Path):
            del voice, bgm
            write_tone(destination, duration=duration, frequency=440, amplitude=0.18)
            return (
                "ffmpeg fixture",
                "fixed-filtergraph",
                {
                    "integrated_loudness_lufs": -15.0,
                    "loudness_range_lu": 3.0,
                    "true_peak_dbtp": -1.0,
                },
            )

        env = {
            **self._env(),
            "VIDEO_FACTORY_PROGRAM_AUDIO_OUTPUT_ROOT": str(self.runtime / "program"),
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "video_factory.program_audio_handler._mix_program_wav",
            side_effect=fake_mix,
        ):
            first = mix_program_audio(self._program_task(bgm, voice))
            second = mix_program_audio(self._program_task(bgm, voice))
        artifact = first["artifact"]
        self.assertFalse(first["audio_mix_execution"]["reused"])
        self.assertTrue(second["audio_mix_execution"]["reused"])
        self.assertEqual(artifact["source_authority"]["audio_sha256"], file_sha(voice_path))
        self.assertEqual(
            artifact["bgm"]["human_approval_sha256"],
            bgm["checksums"]["human_approval_sha256"],
        )
        self.assertTrue(artifact["mix"]["sidechain_ducking"])
        self.assertTrue(artifact["mix"]["broll_audio_muted"])
        self.assertEqual(artifact["mix"]["bgm_preduck_gain_db"], -9)
        self.assertEqual(
            artifact["mix"]["mix_profile_id"], "speech-forward-audible-bgm-v1"
        )
        self.assertIs(validate_artifact("program_audio_manifest", artifact), artifact)

    def test_program_audio_uses_aggregate_multisource_duration_and_hash(self) -> None:
        source_audio = build_multisource_manifest(
            self.root,
            job_id="job_audio_001",
            frozen_root=Path(self.frozen["frozen_root"]),
            frozen_assets=[self.frozen["assets"][0], self.frozen["assets"][0]],
            transcript_parts=["Первый проверенный фрагмент.", "Второй проверенный фрагмент."],
            durations=[5.0, 10.0],
        )
        task = {
            "upstream_results": [
                {"role": "source_audio", "result": {"artifact": source_audio}}
            ]
        }
        contract, artifact, source, audio_sha, tts = _authoritative_audio(
            task,
            job_id="job_audio_001",
            lane="motivation",
            duration=15.0,
        )
        self.assertEqual(contract, "source_audio_manifest")
        self.assertIs(artifact, source_audio)
        self.assertEqual(source, Path(source_audio["extracted_audio_path"]))
        self.assertEqual(audio_sha, source_audio["checksums"]["extracted_audio_sha256"])
        self.assertFalse(tts)

        tampered = copy.deepcopy(source_audio)
        Path(tampered["segments"][1]["extracted_audio_path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValidationError, "segment.*extracted hash"):
            _authoritative_audio(
                {"upstream_results": [{"role": "source_audio", "result": {"artifact": tampered}}]},
                job_id="job_audio_001",
                lane="motivation",
                duration=15.0,
            )

    def test_program_mix_remeasures_bgm_instead_of_trusting_declared_loudness(self) -> None:
        voice_path = self.root / "voice-forged-bgm.wav"
        write_tone(
            voice_path,
            duration=15,
            sample_rate=44_100,
            channels=1,
            frequency=330,
            amplitude=0.2,
        )
        voice = self._voice_manifest(voice_path)
        with mock.patch.dict(os.environ, self._env(), clear=False):
            bgm = freeze_bgm(self._bgm_task())["artifact"]
        forged = copy.deepcopy(bgm)
        forged["audio"]["integrated_loudness_lufs"] = -13.5
        env = {
            **self._env(),
            "VIDEO_FACTORY_PROGRAM_AUDIO_OUTPUT_ROOT": str(self.runtime / "program"),
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "video_factory.program_audio_handler._mix_program_wav"
        ) as mixer:
            with self.assertRaisesRegex(ValidationError, "differs from manifest"):
                mix_program_audio(self._program_task(forged, voice))
        mixer.assert_not_called()

    @unittest.skipUnless(_FFMPEG, "FFmpeg is required")
    def test_bgm_normalization_removes_source_level_variance(self) -> None:
        quiet = self.root / "quiet-bed.wav"
        loud = self.root / "loud-bed.wav"
        quiet_out = self.root / "quiet-normalized.wav"
        loud_out = self.root / "loud-normalized.wav"
        write_tone(quiet, duration=3, frequency=110, amplitude=0.03)
        write_tone(loud, duration=3, frequency=110, amplitude=0.50)
        with mock.patch.dict(
            os.environ, {"VIDEO_FACTORY_FFMPEG": str(_FFMPEG)}, clear=False
        ):
            _, quiet_metrics = normalize_bgm_wav(quiet, quiet_out)
            _, loud_metrics = normalize_bgm_wav(loud, loud_out)
        for metrics in (quiet_metrics, loud_metrics):
            self.assertLessEqual(abs(metrics["integrated_loudness_lufs"] + 14), 0.5)
            self.assertLessEqual(metrics["true_peak_dbtp"], -1.4)
        self.assertLessEqual(
            abs(
                quiet_metrics["integrated_loudness_lufs"]
                - loud_metrics["integrated_loudness_lufs"]
            ),
            0.2,
        )

    @unittest.skipUnless(_FFMPEG, "FFmpeg is required")
    def test_real_ffmpeg_mix_is_byte_deterministic_and_hits_loudness_target(self) -> None:
        voice = self.root / "real-voice.wav"
        bgm = self.root / "real-bgm.wav"
        first = self.root / "mix-a.wav"
        second = self.root / "mix-b.wav"
        write_tone(voice, duration=15, frequency=330, amplitude=0.22)
        write_tone(bgm, duration=2, frequency=110, amplitude=0.18)
        with mock.patch.dict(
            os.environ, {"VIDEO_FACTORY_FFMPEG": str(_FFMPEG)}, clear=False
        ):
            _, graph_a, metrics_a = _mix_program_wav(
                voice=voice, bgm=bgm, duration=15, destination=first
            )
            _, graph_b, metrics_b = _mix_program_wav(
                voice=voice, bgm=bgm, duration=15, destination=second
            )
        self.assertEqual(graph_a, graph_b)
        self.assertIn("volume=0.35481339", graph_a)
        self.assertEqual(file_sha(first), file_sha(second))
        self.assertLessEqual(abs(metrics_a["integrated_loudness_lufs"] + 15), 0.5)
        self.assertLessEqual(metrics_a["true_peak_dbtp"], -0.9)
        self.assertEqual(metrics_a, metrics_b)


if __name__ == "__main__":
    unittest.main()
