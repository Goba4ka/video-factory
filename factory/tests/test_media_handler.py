from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from video_factory.errors import ValidationError
from video_factory.media_handler import handle_task, main


class MediaHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.output = self.root / "frozen"
        self.source = self.inputs / "speaker.mp4"
        self.source.write_bytes(b"\x00\x00\x00\x18ftypmp42local-media-fixture")
        self.rights = self._rights_manifest()

    def _rights_manifest(self) -> dict:
        return {
            "schema_version": "1.0.0",
            "idea_id": "idea_media_001",
            "assets": [
                {
                    "asset_id": "speaker-video-001",
                    "local_path": str(self.source.resolve()),
                    "download_url": None,
                    "landing_url": "https://example.test/speaker-video",
                    "creator": "Rights owner",
                    "license": "Commercial media license",
                    "license_url": "https://example.test/license",
                    "license_receipt": "rights/speaker-video-001.pdf",
                    "retrieved_at": "2026-08-29T10:00:00Z",
                    "commercial_use": True,
                    "modification_allowed": True,
                    "attribution_required": False,
                    "attribution_text": None,
                    "model_release": "confirmed",
                    "property_release": "not_applicable",
                    "platforms": [
                        "youtube_shorts",
                        "instagram_reels",
                        "tiktok",
                    ],
                    "territories": ["worldwide"],
                    "expires_at": None,
                    "rights_status": "approved",
                    "notes": "Local test fixture only",
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
            "id": "task_media_001",
            "job_id": "job_media_001",
            "role": "media",
            "pod": "motivation",
            "attempt_count": 1,
            "payload": {
                "job_id": "job_media_001",
                "idea_id": "idea_media_001",
                "lane_id": "motivation",
                "required_result_contract": "frozen_media_manifest",
                "media_inputs": [
                    {
                        "asset_id": "speaker-video-001",
                        "local_path": str(self.source.resolve()),
                    }
                ],
            },
            "upstream_results": [
                {
                    "task_id": "task_rights_001",
                    "role": "rights",
                    "result": {"artifact": copy.deepcopy(self.rights)},
                }
            ],
        }

    def remote_task(self) -> dict:
        body = self.task()
        asset = body["upstream_results"][0]["result"]["artifact"]["assets"][0]
        asset["local_path"] = None
        asset["download_url"] = (
            "https://videos.pexels.com/video-files/4812205/1080.mp4"
        )
        asset["landing_url"] = "https://www.pexels.com/video/sample-4812205/"
        asset["creator"] = "Test Creator"
        asset["license"] = "Pexels License"
        asset["license_url"] = "https://www.pexels.com/license/"
        asset["license_receipt"] = "rights/pexels-video-4812205.json"
        asset["attribution_required"] = True
        asset["attribution_text"] = (
            "Video by Test Creator on Pexels: " + asset["landing_url"]
        )
        asset["model_release"] = "not_applicable"
        asset["property_release"] = "not_applicable"
        del body["payload"]["media_inputs"]
        return body

    def environment(self) -> dict[str, str]:
        return {
            "VIDEO_FACTORY_RUNTIME_ROOT": str(self.root / "runtime"),
            "VIDEO_FACTORY_MEDIA_INPUT_ROOTS": str(self.inputs),
            "VIDEO_FACTORY_MEDIA_OUTPUT_ROOT": str(self.output),
        }

    def test_freezes_explicit_local_media_and_binds_actual_bytes(self) -> None:
        with mock.patch.dict(os.environ, self.environment(), clear=False):
            result = handle_task(self.task())

        artifact = result["artifact"]
        record = artifact["assets"][0]
        frozen_path = Path(artifact["frozen_root"]) / record["frozen_path"]
        self.assertTrue(frozen_path.is_file())
        self.assertEqual(frozen_path.read_bytes(), self.source.read_bytes())
        self.assertEqual(record["sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(record["source"]["kind"], "local_file")
        self.assertIsNone(record["source"]["final_url"])
        self.assertEqual(artifact["job_id"], "job_media_001")
        self.assertEqual(artifact["idea_id"], "idea_media_001")
        self.assertEqual(artifact["decision"]["network_downloads"], 0)
        self.assertEqual(artifact["decision"]["local_copies"], 1)
        self.assertFalse(result["media_execution"]["network_access"])
        manifest_path = Path(result["manifest_path"])
        self.assertEqual(manifest_path.parent, self.output / "job_media_001")
        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), artifact)

    def test_repeat_is_job_scoped_and_reuses_verified_manifest(self) -> None:
        with mock.patch.dict(os.environ, self.environment(), clear=False):
            first = handle_task(self.task())
            second = handle_task(self.task())
        self.assertEqual(first["manifest_path"], second["manifest_path"])
        self.assertEqual(first["artifact"], second["artifact"])
        self.assertFalse(first["media_execution"]["reused"])
        self.assertTrue(second["media_execution"]["reused"])

    def test_rejects_network_or_implicit_inputs_without_access(self) -> None:
        body = self.task()
        body["payload"]["media_inputs"] = [
            {
                "asset_id": "speaker-video-001",
                "download_url": "https://example.test/speaker.mp4",
            }
        ]
        with mock.patch("video_factory.media_handler.freeze_explicit_media") as freeze:
            with self.assertRaisesRegex(ValidationError, "network downloads are disabled"):
                handle_task(body)
        freeze.assert_not_called()

        body = self.task()
        del body["payload"]["media_inputs"]
        with mock.patch.dict(os.environ, self.environment(), clear=False):
            result = handle_task(body)
        self.assertEqual(result["artifact"]["decision"]["local_copies"], 1)
        self.assertFalse(result["media_execution"]["network_access"])

    def test_network_derivation_requires_explicit_switch_and_exact_rights_url(self) -> None:
        body = self.remote_task()
        with mock.patch.dict(os.environ, self.environment(), clear=False):
            with mock.patch("video_factory.media_handler.freeze_explicit_media") as freeze:
                with self.assertRaisesRegex(
                    ValidationError, "network downloads are disabled"
                ):
                    handle_task(body)
        freeze.assert_not_called()

        environment = {
            **self.environment(),
            "VIDEO_FACTORY_MEDIA_ALLOW_RIGHTS_DOWNLOADS": "true",
        }
        captured: list[list[dict[str, str]]] = []

        def fake_freeze(rights, inputs, output, **_kwargs):
            captured.append(copy.deepcopy(inputs))
            output = Path(output)
            output.mkdir(parents=True, exist_ok=True)
            artifact = {
                "assets": [
                    {
                        "source": {"kind": "direct_download"},
                        "reused_existing": False,
                    }
                ],
                "decision": {
                    "asset_count": 1,
                    "cache_hits": 0,
                    "network_downloads": 1,
                },
            }
            manifest_path = output / "frozen_media_manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            return {"artifact": artifact, "manifest_path": str(manifest_path)}

        with mock.patch.dict(os.environ, environment, clear=False):
            with mock.patch(
                "video_factory.media_handler.freeze_explicit_media",
                side_effect=fake_freeze,
            ), mock.patch("video_factory.media_handler.verify_frozen_media_manifest"):
                result = handle_task(body)
        self.assertEqual(
            captured,
            [
                [
                    {
                        "asset_id": "speaker-video-001",
                        "download_url": (
                            "https://videos.pexels.com/video-files/4812205/1080.mp4"
                        ),
                    }
                ]
            ],
        )
        self.assertTrue(result["media_execution"]["network_access"])

        with mock.patch.dict(
            os.environ,
            {**self.environment(), "VIDEO_FACTORY_MEDIA_ALLOW_RIGHTS_DOWNLOADS": "maybe"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValidationError, "must be a boolean"):
                handle_task(body)

    def test_network_freeze_rejects_unresolved_or_unevidenced_rights_before_http(self) -> None:
        environment = {
            **self.environment(),
            "VIDEO_FACTORY_MEDIA_ALLOW_RIGHTS_DOWNLOADS": "true",
        }
        mutations = (
            ("rights_status", "human_review", "rights_status must be approved"),
            ("license_receipt", None, "license_receipt"),
            ("model_release", "unknown", "model_release"),
            ("property_release", "required", "property_release"),
        )
        for field, value, expected in mutations:
            with self.subTest(field=field):
                body = self.remote_task()
                body["upstream_results"][0]["result"]["artifact"]["assets"][0][
                    field
                ] = value
                with mock.patch.dict(os.environ, environment, clear=False):
                    with mock.patch(
                        "video_factory.media_freeze._stage_download"
                    ) as download:
                        with self.assertRaisesRegex(ValidationError, expected):
                            handle_task(body)
                download.assert_not_called()

        body = self.remote_task()
        body["upstream_results"][0]["result"]["artifact"]["assets"][0][
            "download_url"
        ] = "http://public.example/media.mp4"
        with mock.patch.dict(os.environ, environment, clear=False):
            with mock.patch("video_factory.media_freeze._stage_download") as download:
                with self.assertRaisesRegex(ValidationError, "must use https"):
                    handle_task(body)
        download.assert_not_called()

    def test_rejects_outside_root_wrong_binding_and_unpassed_rights(self) -> None:
        body = self.task()
        outside = self.root / "outside.mp4"
        outside.write_bytes(self.source.read_bytes())
        body["payload"]["media_inputs"][0]["local_path"] = str(outside.resolve())
        body["upstream_results"][0]["result"]["artifact"]["assets"][0][
            "local_path"
        ] = str(outside.resolve())
        with mock.patch.dict(os.environ, self.environment(), clear=False):
            with self.assertRaisesRegex(ValidationError, "outside allowed local roots"):
                handle_task(body)

        body = self.task()
        body["payload"]["idea_id"] = "idea_other_001"
        with self.assertRaisesRegex(ValidationError, "not bound"):
            handle_task(body)

        body = self.task()
        body["upstream_results"][0]["result"]["artifact"]["decision"][
            "passed"
        ] = False
        with self.assertRaisesRegex(ValidationError, "has not passed"):
            handle_task(body)

    def test_stdio_returns_canonical_result_and_fails_closed(self) -> None:
        output = StringIO()
        with mock.patch.dict(os.environ, self.environment(), clear=False):
            code = main(StringIO(json.dumps(self.task())), output)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["artifact"]["job_id"], "job_media_001")

        with mock.patch("sys.stderr", new_callable=StringIO) as stderr:
            code = main(StringIO("[]"), StringIO())
        self.assertEqual(code, 2)
        self.assertIn("media_handler_error", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
