from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from video_factory.caption_observer import (
    CaptionObserverError,
    main,
    model_tree_fingerprint,
    observe,
)


class _FakeWhisperModel:
    constructor_calls: list[tuple[tuple, dict]] = []
    transcribe_calls: list[tuple[tuple, dict]] = []
    language = "ru"
    language_probability = 0.98
    duration = 4.0
    segments = [
        SimpleNamespace(
            words=[
                SimpleNamespace(word=" Привет", start=0.20, end=0.68, probability=0.97),
                SimpleNamespace(word=" мир.", start=0.72, end=1.18, probability=0.95),
            ]
        )
    ]
    mutate_path: Path | None = None
    error: Exception | None = None

    def __init__(self, *args, **kwargs) -> None:
        type(self).constructor_calls.append((args, kwargs))

    def transcribe(self, *args, **kwargs):
        type(self).transcribe_calls.append((args, kwargs))
        # This print must never contaminate the observer JSON channel.
        print("third-party-progress")
        if type(self).error is not None:
            raise type(self).error
        if type(self).mutate_path is not None:
            type(self).mutate_path.write_bytes(b"changed-during-inference")
        info = SimpleNamespace(
            language=type(self).language,
            language_probability=type(self).language_probability,
            duration=type(self).duration,
        )
        return iter(type(self).segments), info


class CaptionObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.model = self.root / "faster-whisper-model"
        self.model.mkdir()
        for index, name in enumerate(
            (
                "config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.txt",
            )
        ):
            (self.model / name).write_bytes(f"fixture-{index}-{name}".encode())
        self.model_sha256 = model_tree_fingerprint(self.model)
        self.render = self.root / "master.mp4"
        self.render.write_bytes(b"real-rendered-master-audio" * 32)
        self.request = {
            "schema_version": "1.0.0",
            "job_id": "job_caption_001",
            "lane_id": "motivation",
            "render_id": "render_caption_001",
            "render_path": str(self.render),
            "render_sha256": hashlib.sha256(self.render.read_bytes()).hexdigest(),
            "duration_seconds": 4.0,
            "language": "ru",
            "require_word_timestamps": True,
        }
        environment = mock.patch.dict(
            "os.environ",
            {
                "VIDEO_FACTORY_CAPTION_MODEL_PATH": str(self.model),
                "VIDEO_FACTORY_CAPTION_MODEL_SHA256": self.model_sha256,
                "VIDEO_FACTORY_CAPTION_DEVICE": "cpu",
                "VIDEO_FACTORY_CAPTION_COMPUTE_TYPE": "int8",
                "VIDEO_FACTORY_CAPTION_CPU_THREADS": "2",
                "VIDEO_FACTORY_CAPTION_BEAM_SIZE": "3",
                "VIDEO_FACTORY_CAPTION_LANGUAGE_PROBABILITY_MIN": "0.65",
                "HF_TOKEN": "must-not-be-used-or-printed",
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)
        _FakeWhisperModel.constructor_calls = []
        _FakeWhisperModel.transcribe_calls = []
        _FakeWhisperModel.language = "ru"
        _FakeWhisperModel.language_probability = 0.98
        _FakeWhisperModel.duration = 4.0
        _FakeWhisperModel.segments = [
            SimpleNamespace(
                words=[
                    SimpleNamespace(
                        word=" Привет", start=0.20, end=0.68, probability=0.97
                    ),
                    SimpleNamespace(
                        word=" мир.", start=0.72, end=1.18, probability=0.95
                    ),
                ]
            )
        ]
        _FakeWhisperModel.mutate_path = None
        _FakeWhisperModel.error = None

    def _observe(self) -> dict:
        return observe(
            self.request,
            model_class=_FakeWhisperModel,
            version_getter=lambda name: "1.2.1",
            now=lambda: datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
        )

    def test_real_engine_adapter_contract_is_exact_offline_and_word_level(self) -> None:
        result = self._observe()

        self.assertEqual(
            set(result),
            {
                "status",
                "warnings",
                "language",
                "duration_seconds",
                "engine",
                "completed_at",
                "words",
            },
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["language"], "ru")
        self.assertEqual(result["words"][0]["text"], "Привет")
        self.assertEqual(result["words"][1]["start_seconds"], 0.72)
        self.assertTrue(result["engine"]["name"].endswith(self.model_sha256[:12]))
        self.assertEqual(result["engine"]["version"], "1.2.1")
        self.assertEqual(len(result["engine"]["run_id"]), 24)

        constructor_args, constructor_kwargs = _FakeWhisperModel.constructor_calls[0]
        self.assertEqual(constructor_args, (str(self.model),))
        self.assertTrue(constructor_kwargs["local_files_only"])
        self.assertFalse(constructor_kwargs["use_auth_token"])
        self.assertEqual(constructor_kwargs["device"], "cpu")
        self.assertEqual(constructor_kwargs["compute_type"], "int8")
        transcribe_args, transcribe_kwargs = _FakeWhisperModel.transcribe_calls[0]
        self.assertEqual(transcribe_args, (str(self.render),))
        self.assertIsNone(transcribe_kwargs["language"])
        self.assertTrue(transcribe_kwargs["word_timestamps"])
        self.assertFalse(transcribe_kwargs["condition_on_previous_text"])
        self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
        self.assertNotIn("HF_TOKEN", os.environ)

    def test_model_fingerprint_is_deterministic_and_tamper_evident(self) -> None:
        self.assertEqual(model_tree_fingerprint(self.model), self.model_sha256)
        stdout = io.StringIO()
        self.assertEqual(
            main(["--fingerprint-model", str(self.model)], stdout=stdout), 0
        )
        self.assertEqual(stdout.getvalue(), self.model_sha256 + "\n")

        (self.model / "model.bin").write_bytes(b"tampered-model")
        self.assertNotEqual(model_tree_fingerprint(self.model), self.model_sha256)
        with self.assertRaisesRegex(CaptionObserverError, "pinned fingerprint"):
            self._observe()
        self.assertEqual(_FakeWhisperModel.constructor_calls, [])

    def test_missing_local_tokenizer_or_version_drift_fails_before_inference(self) -> None:
        (self.model / "tokenizer.json").unlink()
        with self.assertRaisesRegex(CaptionObserverError, "incomplete"):
            model_tree_fingerprint(self.model)

        (self.model / "tokenizer.json").write_bytes(b"fixture-tokenizer")
        with mock.patch.dict(
            "os.environ",
            {"VIDEO_FACTORY_CAPTION_MODEL_SHA256": model_tree_fingerprint(self.model)},
        ):
            with self.assertRaisesRegex(CaptionObserverError, "version drift"):
                observe(
                    self.request,
                    model_class=_FakeWhisperModel,
                    version_getter=lambda name: "1.2.0",
                )
        self.assertEqual(_FakeWhisperModel.constructor_calls, [])

    def test_stale_render_and_unknown_request_field_fail_before_inference(self) -> None:
        stale = dict(self.request)
        stale["render_sha256"] = "a" * 64
        with self.assertRaisesRegex(CaptionObserverError, "render bytes"):
            observe(stale, model_class=_FakeWhisperModel)

        unknown = dict(self.request)
        unknown["script_text"] = "do not use this as a transcript fallback"
        with self.assertRaisesRegex(CaptionObserverError, "exact"):
            observe(unknown, model_class=_FakeWhisperModel)
        self.assertEqual(_FakeWhisperModel.constructor_calls, [])

    def test_non_russian_or_low_confidence_language_fails_closed(self) -> None:
        _FakeWhisperModel.language = "en"
        with self.assertRaisesRegex(CaptionObserverError, "Russian"):
            self._observe()

        _FakeWhisperModel.language = "ru"
        _FakeWhisperModel.language_probability = 0.50
        with self.assertRaisesRegex(CaptionObserverError, "Russian"):
            self._observe()

    def test_missing_or_synthetic_word_timing_is_never_accepted(self) -> None:
        _FakeWhisperModel.segments = [SimpleNamespace(words=None)]
        with self.assertRaisesRegex(CaptionObserverError, "word-level timestamps"):
            self._observe()

        _FakeWhisperModel.segments = [
            SimpleNamespace(
                words=[
                    SimpleNamespace(
                        word="два слова", start=0.2, end=0.8, probability=0.9
                    )
                ]
            )
        ]
        with self.assertRaisesRegex(CaptionObserverError, "non-atomic"):
            self._observe()

        _FakeWhisperModel.segments = [
            SimpleNamespace(
                words=[
                    SimpleNamespace(
                        word="слово", start=0.2, end=0.6, probability=0.9
                    ),
                    SimpleNamespace(word=" —", start=0.6, end=0.7, probability=0.9),
                    SimpleNamespace(
                        word=" дальше", start=0.72, end=1.1, probability=0.9
                    ),
                ]
            )
        ]
        result = self._observe()
        self.assertEqual([word["text"] for word in result["words"]], ["слово", "дальше"])

        _FakeWhisperModel.segments = [
            SimpleNamespace(
                words=[
                    SimpleNamespace(word="слово", start=0.2, end=0.2, probability=0.9)
                ]
            )
        ]
        with self.assertRaisesRegex(CaptionObserverError, "timing"):
            self._observe()

    def test_render_mutation_during_inference_fails_closed(self) -> None:
        _FakeWhisperModel.mutate_path = self.render
        with self.assertRaisesRegex(CaptionObserverError, "changed during"):
            self._observe()

    def test_model_mutation_during_inference_fails_closed(self) -> None:
        _FakeWhisperModel.mutate_path = self.model / "model.bin"
        with self.assertRaisesRegex(CaptionObserverError, "model files changed during"):
            self._observe()

    def test_stdio_is_single_json_and_engine_errors_do_not_leak_secrets(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                "video_factory.caption_observer._engine_class",
                return_value=_FakeWhisperModel,
            ),
            mock.patch(
                "video_factory.caption_observer._installed_engine_version",
                return_value="1.2.1",
            ),
            mock.patch("sys.stderr", stderr),
        ):
            code = main([], io.StringIO(json.dumps(self.request)), stdout)
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "completed")
        self.assertNotIn("third-party-progress", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

        _FakeWhisperModel.error = RuntimeError("sk-secret-must-never-leak")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                "video_factory.caption_observer._engine_class",
                return_value=_FakeWhisperModel,
            ),
            mock.patch(
                "video_factory.caption_observer._installed_engine_version",
                return_value="1.2.1",
            ),
            mock.patch("sys.stderr", stderr),
        ):
            code = main([], io.StringIO(json.dumps(self.request)), stdout)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("local caption inference failed", stderr.getvalue())
        self.assertNotIn("sk-secret", stderr.getvalue())

    def test_multiple_json_objects_or_oversized_stdin_are_rejected(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = main([], io.StringIO("{}\n{}"), stdout)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = main([], io.StringIO("x" * (64 * 1024 + 1)), stdout)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
