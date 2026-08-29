from __future__ import annotations

import io
import hashlib
import json
import struct
import sys
import tempfile
import threading
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_ROOT / "src"))

from video_factory.fish_audio import (  # noqa: E402
    FishAudioError,
    FishAudioInFlightError,
    FishAudioLimitError,
    FishAudioUnknownOutcomeError,
    FishTTSRequest,
    generate_tts,
    list_owned_voices,
    usage_status,
)
from video_factory.contracts import validate_artifact  # noqa: E402


def wav_payload(*, frames: int = 4410) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(44100)
        target.writeframes(b"\0\0" * frames)
    return output.getvalue()


class FishAudioTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.usage_db = self.root / "fish.sqlite3"
        self.output = self.root / "voice.wav"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(
        self,
        *,
        text: str = "Проверка озвучки",
        retry_reason: str | None = None,
        defect_reference: str | None = None,
    ) -> FishTTSRequest:
        return FishTTSRequest(
            video_id="video-001",
            text=text,
            output_path=self.output,
            reference_id="voice-001",
            retry_reason=retry_reason,
            defect_reference=defect_reference,
        )

    def write_defect(
        self,
        *,
        retry_reason: str,
        video_id: str = "video-001",
        filename: str = "voice-defect.json",
    ) -> str:
        first = usage_status(video_id, usage_db=self.usage_db)["generations"][0]
        path = self.root / filename
        payload = {
            "schema_version": "1.0.0",
            "defect_id": f"defect-{retry_reason}",
            "job_id": video_id,
            "video_id": video_id,
            "generation_no": 1,
            "generation_status": first["status"],
            "request_hash": first["request_hash"],
            "output_sha256": first["output_sha256"],
            "retry_reason": retry_reason,
            "description": f"Verified {retry_reason} defect requires regeneration.",
            "evidence": "automated unit-test evidence",
            "regeneration_required": True,
            "detected_by": "test-qa",
            "detected_at": "2026-08-28T08:00:00Z",
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_success_is_cached_without_second_api_call(self) -> None:
        calls = []

        def transport(request, timeout):
            calls.append((request, timeout))
            self.assertNotIn("secret", request.full_url)
            self.assertEqual(request.headers["Model"], "s2.1-pro")
            return 200, {"Content-Type": "audio/wav"}, wav_payload()

        first = generate_tts(
            self.request(),
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )
        second = generate_tts(
            self.request(),
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )

        self.assertEqual(len(calls), 1)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["generation_no"], 1)
        self.assertEqual(first["output_sha256"], second["output_sha256"])
        self.assertGreater(first["duration_seconds"], 0)
        manifest = json.loads(Path(first["voice_manifest_path"]).read_text(encoding="utf-8"))
        validate_artifact("voice_manifest", manifest)
        self.assertEqual(manifest["job_id"], "video-001")
        self.assertIsNone(manifest["retry_reason"])

    def test_third_generation_is_blocked_before_transport(self) -> None:
        calls = []

        def invalid_transport(request, timeout):
            calls.append(request)
            return 200, {}, b"not-a-wav"

        with self.assertRaises(FishAudioError):
            generate_tts(
                self.request(text="Первая версия"),
                usage_db=self.usage_db,
                api_key="secret",
                transport=invalid_transport,
            )
        defect = self.write_defect(retry_reason="provider_failure")
        with self.assertRaises(FishAudioError):
            generate_tts(
                self.request(
                    text="Вторая версия",
                    retry_reason="provider_failure",
                    defect_reference=defect,
                ),
                usage_db=self.usage_db,
                api_key="secret",
                transport=invalid_transport,
            )

        with self.assertRaises(FishAudioLimitError):
            generate_tts(
                self.request(
                    text="Третья версия",
                    retry_reason="provider_failure",
                    defect_reference=defect,
                ),
                usage_db=self.usage_db,
                api_key="secret",
                transport=invalid_transport,
            )

        self.assertEqual(len(calls), 2)
        status = usage_status("video-001", usage_db=self.usage_db)
        self.assertEqual(status["used"], 2)
        self.assertEqual(status["remaining"], 0)
        self.assertEqual(
            [item["status"] for item in status["generations"]],
            ["failed", "failed"],
        )

    def test_paid_utf8_cost_uses_bytes_and_free_model_is_zero(self) -> None:
        audio = wav_payload()

        def transport(request, timeout):
            return 200, {}, audio

        paid = generate_tts(
            self.request(text="Привет"),
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )
        self.assertAlmostEqual(
            paid["estimated_cost_usd"], len("Привет".encode("utf-8")) * 15 / 1_000_000
        )

        free_request = FishTTSRequest(
            video_id="video-free",
            text="Привет",
            output_path=self.root / "free.wav",
            model="s2.1-pro-free",
        )
        free = generate_tts(
            free_request,
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )
        self.assertEqual(free["estimated_cost_usd"], 0)

    def test_owned_voice_list_is_minimized(self) -> None:
        body = json.dumps(
            {
                "items": [
                    {
                        "_id": "private-id",
                        "title": "Narrator",
                        "type": "tts",
                        "state": "ready",
                        "visibility": "private",
                        "languages": ["ru"],
                        "licensed": False,
                        "samples": [{"audio": "not-returned"}],
                    }
                ]
            }
        ).encode()

        def transport(request, timeout):
            self.assertEqual(request.get_method(), "GET")
            self.assertIn("self=true", request.full_url)
            return 200, {"Content-Type": "application/json"}, body

        result = list_owned_voices(api_key="secret", transport=transport)
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["owned_voice_count"], 1)
        self.assertNotIn("samples", result["voices"][0])

    def test_two_versions_are_immutable_and_old_version_can_be_reused(self) -> None:
        calls = []

        def transport(request, timeout):
            calls.append(request)
            return 200, {}, wav_payload(frames=4410 + len(calls))

        first = generate_tts(
            self.request(text="Версия один"),
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )
        defect = self.write_defect(retry_reason="pronunciation")
        second = generate_tts(
            self.request(
                text="Версия два",
                retry_reason="pronunciation",
                defect_reference=defect,
            ),
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )
        reused = generate_tts(
            self.request(text="Версия один"),
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )

        self.assertEqual(len(calls), 2)
        self.assertNotEqual(first["immutable_output_path"], second["immutable_output_path"])
        self.assertEqual(
            Path(first["immutable_output_path"]).read_bytes(), self.output.read_bytes()
        )
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["remaining_generations"], 0)

    def test_same_payload_retry_creates_and_then_reuses_generation_two(self) -> None:
        calls = 0

        def transport(request, timeout):
            nonlocal calls
            calls += 1
            return 200, {}, wav_payload(frames=4410 + calls)

        first = generate_tts(
            self.request(),
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )
        defect = self.write_defect(retry_reason="pronunciation")
        retry = self.request(
            retry_reason="pronunciation",
            defect_reference=defect,
        )
        second = generate_tts(
            retry,
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )
        replay = generate_tts(
            retry,
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )

        self.assertEqual(first["generation_no"], 1)
        self.assertEqual(second["generation_no"], 2)
        self.assertFalse(second["reused"])
        self.assertEqual(replay["generation_no"], 2)
        self.assertTrue(replay["reused"])
        self.assertEqual(calls, 2)
        manifest = json.loads(
            Path(replay["voice_manifest_path"]).read_text(encoding="utf-8")
        )
        manifest["retry_reason"] = None
        manifest["defect_reference"] = None
        with self.assertRaisesRegex(Exception, "retry_reason|defect_reference"):
            validate_artifact("voice_manifest", manifest)

    def test_retry_defect_must_match_generation_one(self) -> None:
        def transport(request, timeout):
            return 200, {}, wav_payload()

        generate_tts(
            self.request(),
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )
        defect = Path(self.write_defect(retry_reason="pacing"))
        payload = json.loads(defect.read_text(encoding="utf-8"))
        payload["request_hash"] = "f" * 64
        defect.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(Exception, "does not match generation 1"):
            generate_tts(
                self.request(
                    text="second",
                    retry_reason="pacing",
                    defect_reference=str(defect),
                ),
                usage_db=self.usage_db,
                api_key="secret",
                transport=transport,
            )
        self.assertEqual(usage_status("video-001", usage_db=self.usage_db)["used"], 1)

    def test_retry_defect_is_frozen_against_in_flight_tampering(self) -> None:
        def first_transport(request, timeout):
            return 200, {}, wav_payload()

        generate_tts(
            self.request(),
            usage_db=self.usage_db,
            api_key="secret",
            transport=first_transport,
        )
        defect = Path(self.write_defect(retry_reason="pacing"))

        def tampering_transport(request, timeout):
            payload = json.loads(defect.read_text(encoding="utf-8"))
            payload["evidence"] = "schema-valid evidence changed during the TTS request"
            defect.write_text(json.dumps(payload), encoding="utf-8")
            return 200, {}, wav_payload(frames=4411)

        with self.assertRaisesRegex(FishAudioError, "retry evidence changed"):
            generate_tts(
                self.request(
                    text="second version",
                    retry_reason="pacing",
                    defect_reference=str(defect),
                ),
                usage_db=self.usage_db,
                api_key="secret",
                transport=tampering_transport,
            )
        status = usage_status("video-001", usage_db=self.usage_db)
        self.assertEqual(status["used"], 2)
        self.assertEqual(status["generations"][1]["status"], "succeeded")
        active_manifest = json.loads(
            self.output.with_suffix(".voice.json").read_text(encoding="utf-8")
        )
        self.assertEqual(active_manifest["generation_no"], 1)
        self.assertEqual(
            hashlib.sha256(self.output.read_bytes()).hexdigest(),
            active_manifest["output_sha256"],
        )

    def test_tampered_cache_consumes_second_slot_instead_of_false_reuse(self) -> None:
        calls = []

        def transport(request, timeout):
            calls.append(request)
            return 200, {}, wav_payload(frames=4410 + len(calls))

        first = generate_tts(
            self.request(),
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )
        Path(first["immutable_output_path"]).write_bytes(b"tampered")
        defect = self.write_defect(retry_reason="technical_failure")
        repaired = generate_tts(
            self.request(
                retry_reason="technical_failure",
                defect_reference=defect,
            ),
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(repaired["generation_no"], 2)
        self.assertEqual(repaired["remaining_generations"], 0)

    def test_any_second_request_is_blocked_while_first_is_in_flight(self) -> None:
        calls = 0
        lock = threading.Lock()
        entered = threading.Event()
        release = threading.Event()

        def transport(request, timeout):
            nonlocal calls
            with lock:
                calls += 1
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return 200, {}, wav_payload()

        first_request = FishTTSRequest(
            video_id="concurrent-video",
            text="Версия 1",
            output_path=self.root / "voice-1.wav",
        )
        second_request = FishTTSRequest(
            video_id="concurrent-video",
            text="Версия 2",
            output_path=self.root / "voice-2.wav",
            retry_reason="pacing",
            defect_reference="qa/voice-v1.json#pacing",
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                generate_tts,
                first_request,
                usage_db=self.usage_db,
                api_key="secret",
                transport=transport,
            )
            self.assertTrue(entered.wait(timeout=5))
            with self.assertRaises(FishAudioInFlightError):
                generate_tts(
                    second_request,
                    usage_db=self.usage_db,
                    api_key="secret",
                    transport=transport,
                )
            release.set()
            future.result(timeout=5)

        self.assertEqual(calls, 1)
        status = usage_status("concurrent-video", usage_db=self.usage_db)
        self.assertEqual(status["used"], 1)

    def test_second_generation_requires_qa_reason_and_defect_reference(self) -> None:
        def transport(request, timeout):
            return 200, {}, wav_payload()

        generate_tts(
            self.request(text="version one"),
            usage_db=self.usage_db,
            api_key="secret",
            transport=transport,
        )
        with self.assertRaisesRegex(Exception, "retry_reason and defect_reference"):
            generate_tts(
                self.request(text="version two"),
                usage_db=self.usage_db,
                api_key="secret",
                transport=transport,
            )
        self.assertEqual(usage_status("video-001", usage_db=self.usage_db)["used"], 1)

    def test_http_status_and_classification_are_persisted(self) -> None:
        def transport(request, timeout):
            from video_factory.fish_audio import _http_error

            raise _http_error(429, b'{"message":"rate limited"}')

        with self.assertRaises(FishAudioError):
            generate_tts(
                self.request(),
                usage_db=self.usage_db,
                api_key="secret",
                transport=transport,
            )
        generation = usage_status("video-001", usage_db=self.usage_db)["generations"][0]
        self.assertEqual(generation["http_status"], 429)
        self.assertEqual(generation["error_code"], "fish_audio_http_429")

    def test_truncated_wav_is_rejected_but_fish_streaming_header_is_allowed(self) -> None:
        truncated = bytearray(wav_payload())
        struct.pack_into("<I", truncated, 40, len(truncated))

        def bad_transport(request, timeout):
            return 200, {}, bytes(truncated)

        with self.assertRaisesRegex(FishAudioError, "truncated"):
            generate_tts(
                self.request(),
                usage_db=self.usage_db,
                api_key="secret",
                transport=bad_transport,
            )

        streaming = bytearray(wav_payload())
        struct.pack_into("<I", streaming, 4, 0xFFFFFF24)
        struct.pack_into("<I", streaming, 40, 0xFFFFFF00)

        def streaming_transport(request, timeout):
            return 200, {}, bytes(streaming)

        result = generate_tts(
            FishTTSRequest(
                video_id="streaming-video",
                text="streaming",
                output_path=self.root / "streaming.wav",
            ),
            usage_db=self.usage_db,
            api_key="secret",
            transport=streaming_transport,
        )
        self.assertTrue(result["ok"])

    def test_sub_10ms_wav_is_rejected(self) -> None:
        def transport(request, timeout):
            return 200, {}, wav_payload(frames=1)

        with self.assertRaisesRegex(FishAudioError, "0.01 seconds"):
            generate_tts(
                self.request(),
                usage_db=self.usage_db,
                api_key="secret",
                transport=transport,
            )

    def test_network_unknown_is_recorded_as_unknown_and_consumes_slot(self) -> None:
        def transport(request, timeout):
            raise FishAudioUnknownOutcomeError("timeout")

        with self.assertRaises(FishAudioUnknownOutcomeError):
            generate_tts(
                self.request(),
                usage_db=self.usage_db,
                api_key="secret",
                transport=transport,
            )
        status = usage_status("video-001", usage_db=self.usage_db)
        self.assertEqual(status["used"], 1)
        self.assertEqual(status["generations"][0]["status"], "failed_unknown")


if __name__ == "__main__":
    unittest.main()
