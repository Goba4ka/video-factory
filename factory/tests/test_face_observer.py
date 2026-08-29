from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_factory.errors import ValidationError
from video_factory.face_observer import (
    FaceDetection,
    GrayImage,
    build_backend,
    main,
    observe_request,
)


class _FixtureBackend:
    name = "fixture_face_detector"
    version = "1.2.3"
    model_sha256 = "d" * 64

    def __init__(self, detections: list[FaceDetection]) -> None:
        self.detections = detections
        self.seen: list[GrayImage] = []

    def detect(self, frame: GrayImage) -> list[FaceDetection]:
        self.seen.append(frame)
        return list(self.detections)


class FaceObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.render = self.root / "master.mp4"
        self.render.write_bytes(b"measured-render" * 128)
        self.width = 18
        self.height = 32
        self.request = {
            "schema_version": "1.0.0",
            "render_path": str(self.render),
            "render_sha256": self._sha(self.render.read_bytes()),
            "frame_width": self.width,
            "frame_height": self.height,
            "frames": [],
        }
        for index in range(2):
            pixels = bytes((position + index * 17) % 256 for position in range(576))
            path = self.root / f"frame-{index}.pgm"
            path.write_bytes(f"P5\n{self.width} {self.height}\n255\n".encode() + pixels)
            self.request["frames"].append(
                {
                    "frame_index": index,
                    "timestamp_seconds": float(index),
                    "frame_sha256": self._sha(pixels),
                    "path": str(path),
                }
            )

    @staticmethod
    def _sha(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    def test_single_measured_face_is_unambiguous_speaker_candidate(self) -> None:
        backend = _FixtureBackend(
            [
                FaceDetection(
                    bbox=(3.0, 4.0, 9.0, 16.0),
                    confidence=0.94,
                    landmarks=((5.0, 7.0), (10.0, 18.0)),
                )
            ]
        )
        response = observe_request(self.request, backend=backend)

        self.assertEqual(response["render_sha256"], self.request["render_sha256"])
        self.assertEqual(response["checker"]["model_sha256"], "d" * 64)
        self.assertEqual(len(response["observations"]), 2)
        self.assertEqual(len(backend.seen), 2)
        face = response["observations"][0]["faces"][0]
        self.assertEqual(face["bbox"], [0.16666667, 0.125, 0.5, 0.5])
        self.assertTrue(face["speaker"])
        self.assertEqual(face["occlusion_fraction"], 0.0)

    def test_multiple_faces_are_not_guessed_and_clipping_is_measured(self) -> None:
        backend = _FixtureBackend(
            [
                FaceDetection(bbox=(-3.0, 4.0, 9.0, 16.0), confidence=0.91),
                FaceDetection(bbox=(10.0, 7.0, 7.0, 12.0), confidence=0.89),
            ]
        )
        response = observe_request(self.request, backend=backend)
        faces = response["observations"][0]["faces"]

        self.assertEqual(len(faces), 2)
        self.assertFalse(any(face["speaker"] for face in faces))
        clipped = next(face for face in faces if face["bbox"][0] == 0.0)
        self.assertAlmostEqual(clipped["occlusion_fraction"], 1 / 3, places=7)

    def test_input_bytes_and_identity_are_fail_closed(self) -> None:
        cases: list[tuple[str, dict, str]] = []
        stale_render = copy.deepcopy(self.request)
        stale_render["render_sha256"] = "a" * 64
        cases.append(("render", stale_render, "render_sha256 does not match"))
        stale_frame = copy.deepcopy(self.request)
        stale_frame["frames"][0]["frame_sha256"] = "b" * 64
        cases.append(("frame", stale_frame, "SHA-256 does not match pixels"))
        unordered = copy.deepcopy(self.request)
        unordered["frames"][1]["frame_index"] = 3
        cases.append(("index", unordered, "contiguous and ordered"))
        traversal = copy.deepcopy(self.request)
        traversal["frames"][0]["path"] = "frame-0.pgm"
        cases.append(("path", traversal, "must be absolute"))

        for label, request, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValidationError, message):
                    observe_request(request, backend=_FixtureBackend([]))

    def test_cli_supports_stdin_and_absolute_request_file(self) -> None:
        for label, argv, stdin in (
            ("stdin", [], io.StringIO(json.dumps(self.request))),
            ("path", [str(self.root / "request.json")], io.StringIO("")),
        ):
            with self.subTest(label=label):
                if argv:
                    Path(argv[0]).write_text(json.dumps(self.request), encoding="utf-8")
                stdout, stderr = io.StringIO(), io.StringIO()
                exit_code = main(
                    argv,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    backend_factory=lambda: _FixtureBackend([]),
                )
                self.assertEqual(exit_code, 0, stderr.getvalue())
                response = json.loads(stdout.getvalue())
                self.assertEqual(len(response["observations"]), 2)
                self.assertEqual(stderr.getvalue(), "")

    def test_missing_engine_and_model_drift_fail_before_inference(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValidationError, "must explicitly be"):
                build_backend()

        fake_cv2 = mock.Mock()
        with mock.patch.dict(
            os.environ,
            {
                "VIDEO_FACTORY_FACE_ENGINE": "yunet",
                "VIDEO_FACTORY_FACE_MODEL_SHA256": "0" * 64,
            },
            clear=True,
        ), mock.patch("video_factory.face_observer._import_opencv", return_value=fake_cv2):
            with self.assertRaisesRegex(ValidationError, "must be a non-empty path"):
                build_backend()

        fake_cv2.data.haarcascades = str(self.root) + os.sep
        model = self.root / "haarcascade_frontalface_default.xml"
        model.write_bytes(b"model")
        with mock.patch.dict(
            os.environ,
            {
                "VIDEO_FACTORY_FACE_ENGINE": "haar",
                "VIDEO_FACTORY_FACE_MODEL_SHA256": "0" * 64,
            },
            clear=True,
        ), mock.patch("video_factory.face_observer._import_opencv", return_value=fake_cv2):
            with self.assertRaisesRegex(ValidationError, "does not match model bytes"):
                build_backend()

    def test_cli_failure_returns_no_json_artifact(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        exit_code = main(
            [],
            stdin=io.StringIO("{}"),
            stdout=stdout,
            stderr=stderr,
            backend_factory=lambda: _FixtureBackend([]),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("failed closed", stderr.getvalue())


@unittest.skipUnless(importlib.util.find_spec("cv2") is not None, "OpenCV extra absent")
class OpenCVFaceObserverSmokeTests(unittest.TestCase):
    def test_packaged_haar_backend_runs_real_inference_without_network(self) -> None:
        import cv2  # type: ignore[import-not-found]

        model = (
            Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        ).resolve()
        model_sha = hashlib.sha256(model.read_bytes()).hexdigest()
        with mock.patch.dict(
            os.environ,
            {
                "VIDEO_FACTORY_FACE_ENGINE": "haar",
                "VIDEO_FACTORY_FACE_MODEL_PATH": str(model),
                "VIDEO_FACTORY_FACE_MODEL_SHA256": model_sha,
            },
            clear=True,
        ):
            backend = build_backend()
            faces = backend.detect(GrayImage(180, 320, b"\x80" * (180 * 320)))
        self.assertEqual(backend.model_sha256, model_sha)
        self.assertEqual(faces, [])


if __name__ == "__main__":
    unittest.main()
