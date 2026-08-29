from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


FACTORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_ROOT / "src"))

from video_factory.cli import main  # noqa: E402
from video_factory.media_freeze import (  # noqa: E402
    MediaFreezeError,
    freeze_approved_media,
)


MEDIA = b"\x00\x00\x00\x18ftypmp42mock-video-payload"
BIG_MEDIA = b"x" * 256


class MockMediaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    head_requests = 0
    get_requests = 0

    def _send(self, body: bytes, content_type: str, *, send_body: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"mock-etag"')
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        type(self).head_requests += 1
        if self.path == "/media.mp4":
            self._send(MEDIA, "video/mp4", send_body=False)
        elif self.path == "/big.mp4":
            self._send(BIG_MEDIA, "video/mp4", send_body=False)
        elif self.path == "/html":
            self._send(b"not media", "text/html", send_body=False)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/media.mp4")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_error(404)

    def do_GET(self) -> None:  # noqa: N802
        type(self).get_requests += 1
        if self.path == "/media.mp4":
            self._send(MEDIA, "video/mp4", send_body=True)
        elif self.path == "/big.mp4":
            self._send(BIG_MEDIA, "video/mp4", send_body=True)
        elif self.path == "/html":
            self._send(b"not media", "text/html", send_body=True)
        elif self.path == "/slow.mp4":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(MEDIA)))
            self.end_headers()
            self.wfile.write(MEDIA[:1])
            self.wfile.flush()
            time.sleep(0.2)
            try:
                self.wfile.write(MEDIA[1:])
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/media.mp4")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_error(404)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


class MediaFreezeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockMediaHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "rights_manifest.json"
        MockMediaHandler.head_requests = 0
        MockMediaHandler.get_requests = 0

    def write_manifest(
        self,
        url: str,
        *,
        rights_status: str = "approved",
        passed: bool = True,
        needs_human_review: bool = False,
    ) -> Path:
        value = {
            "schema_version": "1.0.0",
            "idea_id": "idea-mock-001",
            "assets": [
                {
                    "asset_id": "AST-001",
                    "local_path": None,
                    "landing_url": f"{self.base_url}/landing",
                    "download_url": url,
                    "creator": "Mock creator",
                    "license": "Mock permissive license",
                    "license_url": f"{self.base_url}/license",
                    "license_receipt": "receipt/mock-001.txt",
                    "retrieved_at": "2026-08-27T00:00:00Z",
                    "commercial_use": True,
                    "modification_allowed": True,
                    "attribution_required": True,
                    "attribution_text": "Mock creator",
                    "model_release": "not_applicable",
                    "property_release": "not_applicable",
                    "platforms": ["youtube_shorts"],
                    "territories": ["worldwide"],
                    "expires_at": None,
                    "rights_status": rights_status,
                    "notes": "Local test fixture only",
                }
            ],
            "decision": {
                "passed": passed,
                "needs_human_review": needs_human_review,
                "missing_asset_ids": [],
                "review_notes": [],
            },
        }
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        return self.manifest

    def test_freezes_redirected_media_with_probe_hash_and_ledger(self) -> None:
        self.write_manifest(f"{self.base_url}/redirect")
        output = self.root / "frozen"

        result = freeze_approved_media(
            self.manifest,
            output,
            max_bytes=1024,
            timeout_seconds=5,
            probe=True,
            allow_private_hosts=True,
        )

        self.assertTrue(result["decision"]["passed"])
        self.assertEqual(result["decision"]["asset_count"], 1)
        record = result["assets"][0]
        expected_hash = hashlib.sha256(MEDIA).hexdigest()
        self.assertEqual(record["sha256"], expected_hash)
        self.assertEqual(record["size_bytes"], len(MEDIA))
        self.assertEqual(record["content_type"], "video/mp4")
        self.assertTrue(record["final_url"].endswith("/media.mp4"))
        self.assertEqual(record["head_probe"]["status"], 200)
        frozen_path = Path(result["ledger_path"]).parent / record["frozen_path"]
        self.assertEqual(frozen_path.read_bytes(), MEDIA)
        ledger = json.loads(Path(result["ledger_path"]).read_text(encoding="utf-8"))
        self.assertEqual(ledger["assets"][0]["sha256"], expected_hash)
        self.assertEqual(list(output.glob("*.part")), [])

    def test_repeat_is_idempotent_and_verifies_existing_hash(self) -> None:
        self.write_manifest(f"{self.base_url}/media.mp4")
        output = self.root / "frozen"
        first = freeze_approved_media(
            self.manifest, output, max_bytes=1024, allow_private_hosts=True
        )
        requests_after_first = (
            MockMediaHandler.head_requests,
            MockMediaHandler.get_requests,
        )
        second = freeze_approved_media(
            self.manifest, output, max_bytes=1024, allow_private_hosts=True
        )
        self.assertFalse(first["assets"][0]["reused_existing"])
        self.assertTrue(second["assets"][0]["reused_existing"])
        self.assertEqual(
            first["assets"][0]["sha256"], second["assets"][0]["sha256"]
        )
        self.assertTrue(second["assets"][0]["cache_revalidated"])
        self.assertEqual(second["decision"]["network_downloads"], 0)
        self.assertEqual(second["decision"]["cache_hits"], 1)
        self.assertEqual(
            (MockMediaHandler.head_requests, MockMediaHandler.get_requests),
            requests_after_first,
            "a verified repeat freeze must not make HEAD or GET requests",
        )

    def test_rejects_unsupported_scheme_before_creating_output(self) -> None:
        self.write_manifest("file:///etc/passwd")
        output = self.root / "frozen"
        with self.assertRaisesRegex(MediaFreezeError, "must use http or https"):
            freeze_approved_media(self.manifest, output)
        self.assertFalse(output.exists())

    def test_rejects_unapproved_or_human_review_manifest(self) -> None:
        self.write_manifest(
            f"{self.base_url}/media.mp4",
            rights_status="human_review",
            passed=True,
        )
        with self.assertRaisesRegex(MediaFreezeError, "rights_status must be approved"):
            freeze_approved_media(
                self.manifest, self.root / "one", allow_private_hosts=True
            )

        self.write_manifest(
            f"{self.base_url}/media.mp4",
            rights_status="approved",
            passed=False,
            needs_human_review=True,
        )
        with self.assertRaisesRegex(MediaFreezeError, "decision.passed must be true"):
            freeze_approved_media(
                self.manifest, self.root / "two", allow_private_hosts=True
            )

    def test_size_limit_removes_partial_file_and_does_not_write_ledger(self) -> None:
        self.write_manifest(f"{self.base_url}/big.mp4")
        output = self.root / "frozen"
        with self.assertRaisesRegex(MediaFreezeError, "exceeds max_bytes"):
            freeze_approved_media(
                self.manifest,
                output,
                max_bytes=32,
                allow_private_hosts=True,
            )
        self.assertEqual(list(output.glob("*")), [])

    def test_rejects_non_media_content_type(self) -> None:
        self.write_manifest(f"{self.base_url}/html")
        with self.assertRaisesRegex(MediaFreezeError, "unsupported Content-Type"):
            freeze_approved_media(
                self.manifest,
                self.root / "frozen",
                allow_private_hosts=True,
            )

    def test_timeout_removes_partial_file(self) -> None:
        self.write_manifest(f"{self.base_url}/slow.mp4")
        output = self.root / "frozen"
        with self.assertRaisesRegex(MediaFreezeError, "timed out"):
            freeze_approved_media(
                self.manifest,
                output,
                timeout_seconds=0.05,
                allow_private_hosts=True,
            )
        self.assertEqual(list(output.glob("*")), [])

    def test_private_network_is_blocked_by_default(self) -> None:
        self.write_manifest(f"{self.base_url}/media.mp4")
        with self.assertRaisesRegex(MediaFreezeError, "private, loopback"):
            freeze_approved_media(self.manifest, self.root / "frozen")

    def test_cli_uses_local_mock_server_and_emits_json(self) -> None:
        self.write_manifest(f"{self.base_url}/media.mp4")
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            [
                "freeze-media",
                str(self.manifest),
                "--output-dir",
                str(self.root / "frozen"),
                "--max-bytes",
                "1024",
                "--timeout",
                "5",
                "--probe",
                "--allow-private-hosts",
            ],
            out=stdout,
            err=stderr,
        )
        self.assertEqual(code, 0, stderr.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["decision"]["passed"])
        self.assertEqual(result["assets"][0]["content_type"], "video/mp4")


if __name__ == "__main__":
    unittest.main()
