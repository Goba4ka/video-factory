from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_factory.errors import ValidationError
from video_factory.voice_handler import handle_task


def script_package() -> dict:
    return {
        "schema_version": "1.0.0",
        "idea_id": "idea_health_001",
        "job_id": "job_health_001",
        "lane_id": "health",
        "language": "ru",
        "target_duration_seconds": 15,
        "hook": {
            "spoken_text": "Хук не должен звучать дважды.",
            "first_frame_text": "Проверяем миф",
            "duration_seconds": 2,
        },
        "segments": [
            {
                "segment_id": "s1",
                "start_seconds": 0,
                "end_seconds": 5,
                "spoken_text": "Первый русский сегмент.",
                "caption_text": "Первый сегмент",
                "visual_intent": "Крупный план предмета",
                "claim_ids": ["claim_001"],
            },
            {
                "segment_id": "s2",
                "start_seconds": 5,
                "end_seconds": 10,
                "spoken_text": "Второй русский сегмент.",
                "caption_text": "Второй сегмент",
                "visual_intent": "Показываем механизм",
                "claim_ids": ["claim_001"],
            },
            {
                "segment_id": "s3",
                "start_seconds": 10,
                "end_seconds": 15,
                "spoken_text": "Третий русский сегмент.",
                "caption_text": "Третий сегмент",
                "visual_intent": "Безопасный итог",
                "claim_ids": ["claim_001"],
            },
        ],
        "caption_style": {
            "max_lines": 2,
            "max_words_per_card": 5,
            "safe_zone": "center_lower_third",
            "side_labels": False,
        },
        "edit_direction": {
            "visual_world": "Чистое медицинское объяснение",
            "music_mood": "спокойная",
            "average_cut_seconds": 2,
            "speaker_scale": 0.8,
        },
        "disclaimer": "Информация не заменяет консультацию врача.",
        "decision": {"passed": True, "needs_human_review": False, "review_notes": []},
    }


def approval() -> dict:
    return {
        "schema_version": "1.0.0",
        "job_id": "job_health_001",
        "reference_id": "voice-owned-001",
        "voice_rights_status": "approved_owned_voice",
        "basis": "voice_owner_confirmation",
        "evidence": "Владелец голоса подтвердил коммерческое использование.",
        "approved": True,
        "approved_by": "owner",
        "approved_at": "2026-08-29T10:00:00Z",
    }


def task(*, lane: str = "health") -> dict:
    return {
        "id": "task_voice_001",
        "job_id": "job_health_001",
        "role": "voice",
        "pod": lane,
        "attempt_count": 1,
        "payload": {
            "job_id": "job_health_001",
            "lane_id": lane,
            "required_result_contract": "voice_manifest",
            "voice_rights_approval": approval(),
        },
        "upstream_results": [
            {
                "task_id": "task_script_001",
                "role": "script",
                "result": {"artifact": script_package()},
            }
        ],
    }


class VoiceHandlerTests(unittest.TestCase):
    def test_generates_queue_compatible_result_without_duplicate_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "voice.voice.json"
            manifest = {
                "schema_version": "1.0.0",
                "provider": "fish_audio",
                "job_id": "job_health_001",
                "video_id": "job_health_001",
                "generation_no": 1,
                "generation_limit": 2,
                "request_hash": "a" * 64,
                "text_sha256": "b" * 64,
                "text_bytes": 60,
                "model": "s2.1-pro",
                "reference_id": "voice-owned-001",
                "voice_rights_status": "approved_owned_voice",
                "immutable_output_path": str(root / "voice.g1.wav"),
                "output_sha256": "c" * 64,
                "output_bytes": 100,
                "audio": {
                    "sample_rate_hz": 44100,
                    "channels": 1,
                    "sample_width_bits": 16,
                    "frames": 4410,
                    "duration_seconds": 0.1,
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
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            captured = {}

            def fake_generate(request, **kwargs):
                captured["request"] = request
                return {
                    "voice_manifest_path": str(manifest_path),
                    "generation_no": 1,
                    "reused": False,
                    "remaining_generations": 1,
                    "estimated_cost_usd": 0.001,
                }

            with mock.patch.dict(
                os.environ, {"VIDEO_FACTORY_RUNTIME_ROOT": str(root)}, clear=False
            ), mock.patch(
                "video_factory.voice_handler.generate_tts", side_effect=fake_generate
            ):
                result = handle_task(task())

        self.assertEqual(result["artifact"], manifest)
        self.assertEqual(result["voice_rights_approval"], approval())
        spoken = captured["request"].text
        self.assertEqual(spoken.count("Первый русский сегмент."), 1)
        self.assertNotIn("Хук не должен звучать дважды", spoken)

    def test_fails_closed_without_job_approval_before_fish_call(self) -> None:
        body = task()
        del body["payload"]["voice_rights_approval"]
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"VIDEO_FACTORY_RUNTIME_ROOT": temporary}, clear=False
        ), mock.patch("video_factory.voice_handler.generate_tts") as generate:
            with self.assertRaisesRegex(ValidationError, "approval is missing"):
                handle_task(body)
        generate.assert_not_called()

    def test_rejects_motivation_lane(self) -> None:
        with self.assertRaisesRegex(ValidationError, "source_audio"):
            handle_task(task(lane="motivation"))


if __name__ == "__main__":
    unittest.main()
