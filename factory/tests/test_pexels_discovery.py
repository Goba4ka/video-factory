from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest import mock

from video_factory.contracts import validate_artifact
from video_factory.errors import ValidationError
from video_factory.pexels_discovery import (
    HttpResponse,
    PEXELS_VIDEO_SEARCH_URL,
    PexelsDiscoveryClient,
    handle_task,
    main,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def pexels_video(
    provider_id: int = 4812205,
    *,
    landing_url: str | None = None,
) -> dict[str, object]:
    return {
        "id": provider_id,
        "width": 1080,
        "height": 1920,
        "duration": 14,
        "url": landing_url
        or f"https://www.pexels.com/video/sample-{provider_id}/",
        "image": f"https://images.pexels.com/videos/{provider_id}/free-video.jpg",
        "user": {
            "id": 77,
            "name": "Test Creator",
            "url": "https://www.pexels.com/@test-creator/",
        },
        "video_files": [
            {
                "id": 1001,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 720,
                "height": 1280,
                "fps": 30,
                "link": f"https://videos.pexels.com/video-files/{provider_id}/720.mp4",
            },
            {
                "id": 1002,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "link": f"https://videos.pexels.com/video-files/{provider_id}/1080.mp4",
            },
            {
                "id": 1003,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 2160,
                "height": 3840,
                "fps": 60,
                "link": f"https://videos.pexels.com/video-files/{provider_id}/4k.mp4",
            },
            {
                "id": 1004,
                "quality": "hd",
                "file_type": "video/mp4",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "link": f"https://videos.pexels.com/video-files/{provider_id}/wide.mp4",
            },
        ],
    }


class FakeTransport:
    def __init__(self, payload: dict[str, object], *, now: datetime) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str], float]] = []
        self.now = now

    def __call__(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        self.calls.append((url, dict(headers), timeout))
        return HttpResponse(
            status=200,
            headers={
                "X-Ratelimit-Limit": "200",
                "X-Ratelimit-Remaining": "199",
                "X-Ratelimit-Reset": str(int((self.now + timedelta(hours=1)).timestamp())),
            },
            body=json.dumps(self.payload).encode("utf-8"),
        )


class PexelsDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        self.clock = MutableClock(self.now)
        self.environment = mock.patch.dict(
            os.environ, {"PEXELS_API_KEY": "pexels-test-secret"}, clear=False
        )
        self.environment.start()
        self.transport = FakeTransport(
            {"page": 1, "per_page": 20, "videos": [pexels_video()]},
            now=self.now,
        )

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def client(
        self,
        *,
        transport=None,
        cache_ttl_seconds: int = 86400,
        requests_per_hour: int = 180,
    ) -> PexelsDiscoveryClient:
        return PexelsDiscoveryClient(
            cache_root=self.root / "cache",
            cache_ttl_seconds=cache_ttl_seconds,
            requests_per_hour=requests_per_hour,
            transport=transport or self.transport,
            clock=self.clock,
        )

    def search(self, client: PexelsDiscoveryClient | None = None, **overrides):
        arguments = {
            "job_id": "job_pexels_001",
            "lane": "health",
            "query": "здоровый сон",
            "size": "medium",
            "locale": "ru-RU",
            "page": 1,
            "per_page": 20,
        }
        arguments.update(overrides)
        return (client or self.client()).search(**arguments)

    def task(self, **payload_overrides) -> dict[str, object]:
        payload = {
            "job_id": "job_pexels_001",
            "lane_id": "health",
            "required_result_contract": "media_discovery_manifest",
            "query": "здоровый сон",
            "orientation": "portrait",
            "size": "medium",
            "locale": "ru-RU",
            "page": 1,
            "per_page": 20,
        }
        payload.update(payload_overrides)
        return {
            "id": "task_pexels_001",
            "job_id": "job_pexels_001",
            "pod": "health",
            "role": "media_discovery",
            "payload": payload,
        }

    def test_network_search_is_portrait_secret_free_and_rights_fail_closed(self) -> None:
        artifact = self.search()
        self.assertIs(
            validate_artifact("media_discovery_manifest", artifact), artifact
        )
        self.assertFalse(artifact["cache"]["hit"])
        self.assertEqual(artifact["rate_limit"]["provider_remaining"], 199)
        self.assertEqual(artifact["decision"]["candidate_count"], 1)
        self.assertFalse(artifact["decision"]["rights_cleared"])
        self.assertTrue(artifact["decision"]["needs_human_review"])

        candidate = artifact["candidates"][0]
        self.assertEqual(candidate["selected_file"]["width"], 1080)
        self.assertEqual(candidate["selected_file"]["height"], 1920)
        self.assertEqual(
            candidate["ledger"]["clearance"]["rights_status"], "human_review"
        )
        self.assertEqual(
            candidate["ledger"]["clearance"]["model_release"], "unknown"
        )
        self.assertTrue(candidate["ledger"]["attribution"]["apply"])

        self.assertEqual(len(self.transport.calls), 1)
        url, headers, timeout = self.transport.calls[0]
        query = parse_qs(urlparse(url).query)
        self.assertEqual(
            urlparse(url).path, "/v1/videos/search"
        )
        self.assertEqual(
            PEXELS_VIDEO_SEARCH_URL,
            "https://api.pexels.com/v1/videos/search",
        )
        self.assertEqual(query["orientation"], ["portrait"])
        self.assertEqual(query["query"], ["здоровый сон"])
        self.assertEqual(query["locale"], ["ru-RU"])
        self.assertEqual(headers["Authorization"], "pexels-test-secret")
        self.assertGreater(timeout, 0)

        serialized = json.dumps(artifact, ensure_ascii=False)
        cached = "".join(
            path.read_text(encoding="utf-8")
            for path in (self.root / "cache").rglob("*.json")
        )
        self.assertNotIn("pexels-test-secret", url)
        self.assertNotIn("pexels-test-secret", serialized)
        self.assertNotIn("pexels-test-secret", cached)

    def test_fresh_cache_is_reused_without_transport_or_api_key(self) -> None:
        first = self.search()
        self.clock.value += timedelta(hours=1)

        def forbidden_transport(*_args, **_kwargs):
            raise AssertionError("cache hit must not access the network")

        cached_client = self.client(transport=forbidden_transport)
        with mock.patch.dict(os.environ, {}, clear=True):
            second = self.search(cached_client, job_id="job_pexels_002")
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(second["job_id"], "job_pexels_002")
        self.assertEqual(
            second["cache"]["payload_sha256"], first["cache"]["payload_sha256"]
        )

    def test_expired_cache_obeys_durable_local_hourly_limit(self) -> None:
        client = self.client(cache_ttl_seconds=30, requests_per_hour=1)
        self.search(client)
        self.clock.value += timedelta(seconds=31)
        with self.assertRaisesRegex(ValidationError, "local hourly request limit"):
            self.search(client)
        self.assertEqual(len(self.transport.calls), 1)

    def test_deduplicates_exact_provider_id_and_url(self) -> None:
        duplicate = pexels_video()
        self.transport.payload["videos"] = [duplicate, copy.deepcopy(duplicate)]
        artifact = self.search()
        self.assertEqual(len(artifact["candidates"]), 1)
        self.assertEqual(artifact["decision"]["duplicates_removed"], 1)

    def test_conflicting_provider_id_or_url_fails_closed(self) -> None:
        self.transport.payload["videos"] = [
            pexels_video(),
            pexels_video(
                landing_url="https://www.pexels.com/video/conflicting-url-4812205/"
            ),
        ]
        with self.assertRaisesRegex(ValidationError, "conflicting duplicate"):
            self.search()

    def test_missing_creator_rights_metadata_fails_closed(self) -> None:
        malformed = pexels_video()
        del malformed["user"]["url"]  # type: ignore[index]
        self.transport.payload["videos"] = [malformed]
        with self.assertRaisesRegex(ValidationError, "user.url"):
            self.search()

    def test_tampered_cache_is_rejected_without_network_fallback(self) -> None:
        self.search()
        cache_file = next((self.root / "cache" / "metadata").glob("*.json"))
        record = json.loads(cache_file.read_text(encoding="utf-8"))
        record["payload"]["videos"][0]["duration"] = 99
        cache_file.write_text(json.dumps(record), encoding="utf-8")

        def forbidden_transport(*_args, **_kwargs):
            raise AssertionError("corrupt cache must fail, not silently refetch")

        cached_client = self.client(transport=forbidden_transport)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValidationError, "checksum does not match"):
                self.search(cached_client)

    def test_missing_api_key_fails_before_transport(self) -> None:
        calls = 0

        def forbidden_transport(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("transport must not run without an API key")

        client = self.client(transport=forbidden_transport)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValidationError, "required for a cache miss"):
                self.search(client)
        self.assertEqual(calls, 0)

    def test_runtime_credential_file_takes_precedence_over_raw_environment(self) -> None:
        credential = self.root / "pexels_api_key"
        credential.write_text("file-secret\n", encoding="utf-8")
        client = self.client()
        with mock.patch.dict(
            os.environ,
            {
                "PEXELS_API_KEY": "wrong-env-secret",
                "PEXELS_API_KEY_FILE": str(credential),
            },
            clear=False,
        ):
            self.search(client, query="уникальный запрос для файла")
        self.assertEqual(self.transport.calls[-1][1]["Authorization"], "file-secret")

    def test_runtime_credential_file_is_strict_and_never_falls_back(self) -> None:
        credential = self.root / "pexels_api_key"
        cases = (
            (b"", "small credential file"),
            (b"x" * 4097, "small credential file"),
            (b"\xff\xfe", "unreadable"),
            (b" leading-space", "required for a cache miss"),
        )
        for index, (content, expected) in enumerate(cases):
            with self.subTest(index=index):
                credential.write_bytes(content)
                with mock.patch.dict(
                    os.environ,
                    {
                        "PEXELS_API_KEY": "must-not-be-used",
                        "PEXELS_API_KEY_FILE": str(credential),
                    },
                    clear=False,
                ):
                    with self.assertRaisesRegex(ValidationError, expected):
                        self.search(
                            self.client(),
                            query=f"уникальный invalid credential {index}",
                        )

        with mock.patch.dict(
            os.environ,
            {
                "PEXELS_API_KEY": "must-not-be-used",
                "PEXELS_API_KEY_FILE": "relative/pexels_api_key",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ValidationError, "absolute regular"):
                self.search(self.client(), query="уникальный relative credential")

    def test_json_stdio_handler_and_portrait_guard(self) -> None:
        result = handle_task(self.task(), client=self.client())
        self.assertEqual(result["artifact"]["job_id"], "job_pexels_001")
        self.assertFalse(result["discovery_execution"]["rights_cleared"])

        body = self.task(query="утренняя зарядка")
        output = io.StringIO()
        code = main(io.StringIO(json.dumps(body)), output, client=self.client())
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(output.getvalue())["artifact"]["provider"], "pexels"
        )

        with self.assertRaisesRegex(ValidationError, "only portrait"):
            handle_task(self.task(orientation="landscape"), client=self.client())

    def test_transport_exception_cannot_leak_api_key_to_stderr(self) -> None:
        secret = "pexels-super-secret"

        def leaking_transport(*_args, **_kwargs):
            raise RuntimeError(f"Authorization: {secret}")

        client = self.client(transport=leaking_transport)
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", {"PEXELS_API_KEY": secret}, clear=False):
            with redirect_stderr(stderr):
                code = main(
                    io.StringIO(json.dumps(self.task(query="другая тема"))),
                    io.StringIO(),
                    client=client,
                )
        self.assertEqual(code, 2)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn("Pexels API request failed", stderr.getvalue())

    def test_transport_validation_error_cannot_leak_file_credential(self) -> None:
        secret = "pexels-file-super-secret"
        credential = self.root / "pexels_api_key"
        credential.write_text(secret + "\n", encoding="utf-8")

        def leaking_transport(*_args, **_kwargs):
            raise ValidationError(f"Authorization: {secret}")

        stderr = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"PEXELS_API_KEY_FILE": str(credential)},
            clear=True,
        ):
            with redirect_stderr(stderr):
                code = main(
                    io.StringIO(
                        json.dumps(self.task(query="файловый секрет другая тема"))
                    ),
                    io.StringIO(),
                    client=self.client(transport=leaking_transport),
                )
        self.assertEqual(code, 2)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn("[redacted]", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
