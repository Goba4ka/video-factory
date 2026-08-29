from __future__ import annotations

import importlib.util
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock
import sys
import tempfile
import unittest
from pathlib import Path

from video_factory.contracts import CONTRACT_FILES


FACTORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = FACTORY_ROOT / "tools" / "server_preflight.py"
SPEC = importlib.util.spec_from_file_location("server_preflight", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
server_preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = server_preflight
SPEC.loader.exec_module(server_preflight)


class ServerPreflightTests(unittest.TestCase):
    def valid_config(self, root: Path) -> dict[str, str]:
        return {
            "VIDEO_FACTORY_RUNTIME_ROOT": str(root / "runtime"),
            "VIDEO_FACTORY_DB": str(root / "runtime" / "queue" / "factory.sqlite3"),
            "VIDEO_FACTORY_WORKSPACE": str(root / "workspace"),
            "VIDEO_FACTORY_CODEX_WORKSPACE": str(root / "codex-workspace"),
            "VIDEO_FACTORY_AGENT_OUTPUT_ROOT": str(root / "runtime" / "outputs"),
            "VIDEO_FACTORY_CODEX": str(root / "bin" / "codex"),
            "VIDEO_FACTORY_CODEX_VERSION": "0.151.0",
            "VIDEO_FACTORY_CODEX_MODEL": "gpt-5.4",
            "VIDEO_FACTORY_CODEX_TIMEOUT": "1800",
            "VIDEO_FACTORY_WORKER_LEASE_SECONDS": "900",
            "VIDEO_FACTORY_WORKER_HEARTBEAT_SECONDS": "300",
            "VIDEO_FACTORY_WORKER_POLL_SECONDS": "2",
            "VIDEO_FACTORY_FFMPEG": str(root / "bin" / "ffmpeg"),
            "VIDEO_FACTORY_FFPROBE": str(root / "bin" / "ffprobe"),
            "VIDEO_FACTORY_NODE": str(root / "bin" / "node"),
            "HYPERFRAMES_VERSION": "0.8.17",
            "HYPERFRAMES_BIN": str(root / "bin" / "hyperframes"),
            "VIDEO_FACTORY_RUNTIME_HANDLER_TIMEOUT": "7200",
            "VIDEO_FACTORY_RUNTIME_WORKER_LEASE_SECONDS": "7500",
            "VIDEO_FACTORY_RUNTIME_WORKER_HEARTBEAT_SECONDS": "300",
            "VIDEO_FACTORY_MEDIA_INPUT_ROOT": str(root / "runtime" / "media_inputs"),
            "VIDEO_FACTORY_MEDIA_OUTPUT_ROOT": str(root / "runtime" / "frozen_media"),
            "VIDEO_FACTORY_MEDIA_ALLOW_RIGHTS_DOWNLOADS": "true",
            "VIDEO_FACTORY_SOURCE_AUDIO_OUTPUT_ROOT": str(root / "runtime" / "source_audio"),
            "VIDEO_FACTORY_HYPERFRAMES_PROJECT_ROOT": str(root / "runtime" / "projects"),
            "VIDEO_FACTORY_GSAP_PATH": str(root / "bin" / "gsap.min.js"),
            "VIDEO_FACTORY_RENDER_OUTPUT_ROOT": str(root / "runtime" / "renders"),
            "VIDEO_FACTORY_QC_EVIDENCE_ROOT": str(root / "runtime" / "qc_evidence"),
            "VIDEO_FACTORY_QC_CACHE_ROOT": str(root / "runtime" / "qc_cache"),
            "VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE": str(root / "bin" / "caption-observer"),
            "VIDEO_FACTORY_CAPTION_OBSERVER_TIMEOUT_SECONDS": "600",
            "VIDEO_FACTORY_CAPTION_MODEL_PATH": str(root / "models" / "caption"),
            "VIDEO_FACTORY_CAPTION_MODEL_SHA256": "b" * 64,
            "VIDEO_FACTORY_CAPTION_DEVICE": "cuda",
            "VIDEO_FACTORY_CAPTION_DEVICE_INDEX": "0",
            "VIDEO_FACTORY_CAPTION_COMPUTE_TYPE": "float16",
            "VIDEO_FACTORY_CAPTION_CPU_THREADS": "0",
            "VIDEO_FACTORY_CAPTION_BEAM_SIZE": "5",
            "VIDEO_FACTORY_CAPTION_LANGUAGE_PROBABILITY_MIN": "0.65",
            "VIDEO_FACTORY_DEDUP_CORPUS_SNAPSHOT": str(root / "artifacts" / "dedup-corpus.json"),
            "VIDEO_FACTORY_FACE_OBSERVER": str(root / "bin" / "face-observer"),
            "VIDEO_FACTORY_FACE_ENGINE": "yunet",
            "VIDEO_FACTORY_FACE_MODEL_PATH": str(root / "models" / "yunet.onnx"),
            "VIDEO_FACTORY_FACE_MODEL_SHA256": "a" * 64,
            "VIDEO_FACTORY_REVIEW_OUTBOX_ROOT": str(root / "runtime" / "review_outbox"),
            "VIDEO_FACTORY_REVIEW_RECONCILE_LIMIT": "100",
            "VIDEO_FACTORY_PEXELS_CACHE_ROOT": str(root / "runtime" / "discovery" / "pexels"),
            "VIDEO_FACTORY_PROVIDER_HANDLER_TIMEOUT": "120",
        }

    def test_env_parser_does_not_evaluate_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "runtime.env"
            source.write_text(
                "SAFE=value\nLITERAL=$(touch should-not-exist)\nQUOTED='hello world'\n",
                encoding="utf-8",
            )
            parsed = server_preflight.load_env_file(source)
        self.assertEqual(parsed["LITERAL"], "$(touch should-not-exist)")
        self.assertEqual(parsed["QUOTED"], "hello world")

    def test_config_is_pinned_and_rejects_raw_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.valid_config(Path(tmp).resolve())
            server_preflight.validate_runtime_config(config)
            config["VIDEO_FACTORY_CODEX_MODEL"] = "latest"
            with self.assertRaisesRegex(server_preflight.PreflightError, "gpt-5.4"):
                server_preflight.validate_runtime_config(config)
            config = self.valid_config(Path(tmp).resolve())
            config["CODEX_API_KEY"] = "must-not-live-here"
            with self.assertRaisesRegex(server_preflight.PreflightError, "raw credentials"):
                server_preflight.validate_runtime_config(config)
            config = self.valid_config(Path(tmp).resolve())
            config["VIDEO_FACTORY_MEDIA_ALLOW_RIGHTS_DOWNLOADS"] = "sometimes"
            with self.assertRaisesRegex(server_preflight.PreflightError, "true or false"):
                server_preflight.validate_runtime_config(config)
            config = self.valid_config(Path(tmp).resolve())
            config["VIDEO_FACTORY_CAPTION_MODEL_SHA256"] = "latest"
            with self.assertRaisesRegex(server_preflight.PreflightError, "lowercase SHA-256"):
                server_preflight.validate_runtime_config(config)
            config = self.valid_config(Path(tmp).resolve())
            config["VIDEO_FACTORY_CAPTION_DEVICE"] = "auto"
            with self.assertRaisesRegex(server_preflight.PreflightError, "cuda or cpu"):
                server_preflight.validate_runtime_config(config)
            config = self.valid_config(Path(tmp).resolve())
            config["VIDEO_FACTORY_CAPTION_BEAM_SIZE"] = "11"
            with self.assertRaisesRegex(server_preflight.PreflightError, "within 1..10"):
                server_preflight.validate_runtime_config(config)
            config = self.valid_config(Path(tmp).resolve())
            config["VIDEO_FACTORY_CAPTION_LANGUAGE_PROBABILITY_MIN"] = "0.49"
            with self.assertRaisesRegex(server_preflight.PreflightError, "within 0.5..1"):
                server_preflight.validate_runtime_config(config)
            config = self.valid_config(Path(tmp).resolve())
            config["VIDEO_FACTORY_FACE_ENGINE"] = "haar"
            with self.assertRaisesRegex(server_preflight.PreflightError, "must be yunet"):
                server_preflight.validate_runtime_config(config)
            config = self.valid_config(Path(tmp).resolve())
            config["VIDEO_FACTORY_FACE_MODEL_SHA256"] = "latest"
            with self.assertRaisesRegex(server_preflight.PreflightError, "lowercase SHA-256"):
                server_preflight.validate_runtime_config(config)

    def test_config_rejects_unsafe_runtime_timing_and_reconcile_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.valid_config(Path(tmp).resolve())
            config["VIDEO_FACTORY_RUNTIME_WORKER_LEASE_SECONDS"] = "7499"
            with self.assertRaisesRegex(server_preflight.PreflightError, "lease must cover"):
                server_preflight.validate_runtime_config(config)
            config = self.valid_config(Path(tmp).resolve())
            config["VIDEO_FACTORY_REVIEW_RECONCILE_LIMIT"] = "0"
            with self.assertRaisesRegex(server_preflight.PreflightError, "1 to 1000"):
                server_preflight.validate_runtime_config(config)

    def test_caption_model_check_uses_the_observer_tree_fingerprint(self) -> None:
        from video_factory.caption_observer import model_tree_fingerprint

        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp).resolve() / "caption-model"
            model.mkdir()
            for name in (
                "config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.txt",
            ):
                (model / name).write_bytes(name.encode("utf-8"))
            expected = model_tree_fingerprint(model)
            self.assertTrue(
                server_preflight._caption_model_tree_check(model, expected).ok
            )
            self.assertFalse(
                server_preflight._caption_model_tree_check(model, "0" * 64).ok
            )
            (model / "model.bin").write_bytes(b"tampered")
            self.assertFalse(
                server_preflight._caption_model_tree_check(model, expected).ok
            )

    def test_systemd_layout_binds_sqlite_to_its_queue_scope(self) -> None:
        config = self.valid_config(Path("/var/lib/video-factory").resolve().parent)
        config["VIDEO_FACTORY_RUNTIME_ROOT"] = "/var/lib/video-factory"
        config["VIDEO_FACTORY_DB"] = "/var/lib/video-factory/queue/factory.sqlite3"
        config["VIDEO_FACTORY_REVIEW_OUTBOX_ROOT"] = "/var/lib/video-factory/review_outbox"
        config["VIDEO_FACTORY_PEXELS_CACHE_ROOT"] = "/var/lib/video-factory/discovery/pexels"
        self.assertTrue(server_preflight._systemd_layout_check(config).ok)
        config["VIDEO_FACTORY_DB"] = "/var/lib/video-factory/factory.sqlite3"
        check = server_preflight._systemd_layout_check(config)
        self.assertFalse(check.ok)
        self.assertIn("VIDEO_FACTORY_DB", check.detail)

    def test_only_allowlisted_editorial_roles_can_start(self) -> None:
        for role in server_preflight.ALLOWED_AUTONOMOUS_ROLES:
            server_preflight.validate_role(role)
        for role in (
            "medical_review",
            "rights",
            "final_review",
            "publisher",
            "render",
            "anything",
        ):
            with self.assertRaises(server_preflight.PreflightError):
                server_preflight.validate_role(role)

    def test_only_allowlisted_deterministic_runtime_roles_can_start(self) -> None:
        expected = {
            "media",
            "source_audio",
            "bgm",
            "audio_mix",
            "compiler",
            "render",
            "qc_auto_evidence",
            "caption_transcript",
            "captions_analyzer",
            "facts_analyzer",
            "policy_analyzer",
            "dedup_analyzer",
            "visual_analyzer",
            "qc_evidence_gate",
            "qc",
        }
        self.assertEqual(server_preflight.ALLOWED_TRUSTED_RUNTIME_ROLES, expected)
        for role in expected:
            server_preflight.validate_trusted_runtime_role(role)
        for role in ("research", "preview_review", "final_review", "publisher"):
            with self.assertRaises(server_preflight.PreflightError):
                server_preflight.validate_trusted_runtime_role(role)

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = server_preflight.main(
                ["--trusted-runtime-role", "render", "--trusted-runtime-role-only"]
            )
        self.assertEqual(code, 0)
        self.assertIn('"trusted_runtime_role": "render"', stdout.getvalue())

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = server_preflight.main(
                ["--trusted-runtime-role", "publisher", "--trusted-runtime-role-only"]
            )
        self.assertEqual(code, 2)
        self.assertIn('"ok": false', stdout.getvalue())

    def test_only_media_discovery_is_a_managed_provider_role(self) -> None:
        server_preflight.validate_provider_role("media_discovery")
        for role in ("rights", "render", "final_review", "publisher"):
            with self.assertRaises(server_preflight.PreflightError):
                server_preflight.validate_provider_role(role)

    def test_semver_parser_requires_a_real_version(self) -> None:
        self.assertEqual(server_preflight.parse_semver("codex-cli 0.151.0"), (0, 151, 0))
        with self.assertRaises(server_preflight.PreflightError):
            server_preflight.parse_semver("codex latest")

    def test_canonical_contract_check_passes_for_packaged_schemas(self) -> None:
        check = server_preflight._contracts_check()
        self.assertTrue(check.ok, check.detail)
        self.assertRegex(check.detail, rf"^{len(CONTRACT_FILES)} schemas at ")

    def test_canonical_contract_check_fails_closed_when_schema_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp)
            with mock.patch("video_factory.contracts.contracts_dir", return_value=empty):
                check = server_preflight._contracts_check()
        self.assertFalse(check.ok)
        self.assertIn("missing=", check.detail)

    def test_dedup_corpus_preflight_validates_schema_not_only_readability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corpus.json"
            path.write_text("{}\n", encoding="utf-8")
            check = server_preflight._artifact_file_check(
                "dedup_corpus_snapshot", "dedup_corpus_snapshot", path
            )
            self.assertFalse(check.ok)
            self.assertIn("invalid dedup_corpus_snapshot", check.detail)

            path.write_text(
                """{
                  "schema_version":"1.0.0",
                  "snapshot_id":"server-preflight-001",
                  "generated_at":"2026-08-29T08:00:00Z",
                  "algorithm":"dhash-64-v1",
                  "sample_interval_seconds":1,
                  "entries":[{
                    "comparison_id":"approved-master-001",
                    "job_id":"job-approved-001",
                    "render_id":"render-approved-001",
                    "render_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "frame_hashes":[
                      "0000000000000000","0000000000000001",
                      "0000000000000002","0000000000000003",
                      "0000000000000004","0000000000000005",
                      "0000000000000006","0000000000000007"
                    ]
                  }]
                }\n""",
                encoding="utf-8",
            )
            check = server_preflight._artifact_file_check(
                "dedup_corpus_snapshot", "dedup_corpus_snapshot", path
            )
            self.assertTrue(check.ok, check.detail)

    def test_face_model_preflight_binds_exact_read_only_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "yunet.onnx"
            path.write_bytes(b"approved-model-bytes")
            import hashlib

            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            check = server_preflight._sha256_file_check("face_model", path, expected)
            self.assertTrue(check.ok, check.detail)
            path.write_bytes(b"drifted-model-bytes")
            check = server_preflight._sha256_file_check("face_model", path, expected)
            self.assertFalse(check.ok)
            self.assertIn("sha256=", check.detail)


class SystemdUnitTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (FACTORY_ROOT / "deployment" / "systemd" / name).read_text(encoding="utf-8")

    def test_worker_is_real_heartbeat_worker_with_fail_closed_role_check(self) -> None:
        unit = self.read("video-factory-worker@.service")
        self.assertIn("ExecCondition=", unit)
        self.assertIn("--role %i --role-only", unit)
        self.assertIn("video-factory worker", unit)
        self.assertIn("--heartbeat-seconds", unit)
        self.assertIn("video_factory.editorial_handler", unit)
        self.assertNotIn("CODEX_API_KEY=", unit)
        self.assertNotIn("FISH_API_KEY=", unit)

    def test_runtime_worker_dispatches_only_allowlisted_local_handlers(self) -> None:
        unit = self.read("video-factory-runtime-worker@.service")
        self.assertIn("--trusted-runtime-role %i --trusted-runtime-role-only", unit)
        self.assertIn("--role %i", unit)
        self.assertIn("video_factory.trusted_runtime_handler", unit)
        self.assertIn("--resource-lock auto", unit)
        self.assertIn("--terminal-on-handler-error", unit)
        self.assertIn("ReadWritePaths=/var/lib/video-factory", unit)
        self.assertNotIn("final_review", unit)
        self.assertNotIn("publisher", unit)
        self.assertNotIn("CODEX_API_KEY=", unit)
        self.assertNotIn("FISH_API_KEY=", unit)

    def test_provider_worker_uses_systemd_credential_and_exact_handler(self) -> None:
        unit = self.read("video-factory-provider-worker@.service")
        self.assertIn("--provider-role %i --provider-role-only", unit)
        self.assertIn("--role %i", unit)
        self.assertIn("video_factory.pexels_discovery", unit)
        self.assertIn("LoadCredential=pexels_api_key:", unit)
        self.assertIn("PEXELS_API_KEY_FILE=%d/pexels_api_key", unit)
        self.assertIn(
            "ReadWritePaths=/var/lib/video-factory/queue /var/lib/video-factory/discovery",
            unit,
        )
        self.assertNotIn("ReadWritePaths=/var/lib/video-factory\n", unit)
        self.assertIn("PrivateDevices=true", unit)
        self.assertIn("CapabilityBoundingSet=", unit)
        self.assertNotIn("PEXELS_API_KEY=", unit)
        self.assertNotIn("publisher", unit)

    def test_metrics_timer_is_persistent_and_service_only_writes_runtime(self) -> None:
        timer = self.read("video-factory-metrics.timer")
        service = self.read("video-factory-metrics.service")
        collector = (FACTORY_ROOT / "tools" / "collect_server_metrics.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("collect_server_metrics.sh", service)
        self.assertIn("ReadWritePaths=/var/lib/video-factory", service)
        self.assertNotIn("source ", collector)
        self.assertNotIn("eval ", collector)

    def test_review_release_timer_cannot_approve_or_publish(self) -> None:
        timer = self.read("video-factory-review-release.timer")
        service = self.read("video-factory-review-release.service")
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("video_factory.review_release_reconciler", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", service)
        self.assertIn("ReadWritePaths=/var/lib/video-factory/review_outbox", service)
        self.assertIn("PrivateDevices=true", service)
        self.assertIn("CapabilityBoundingSet=", service)
        self.assertNotIn("publisher", service)
        self.assertNotIn("final_review_handler", service)

    def test_example_config_pins_codex_and_contains_no_raw_key(self) -> None:
        config = (FACTORY_ROOT / "deployment" / "server.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("VIDEO_FACTORY_CODEX_VERSION=0.151.0", config)
        self.assertIn("VIDEO_FACTORY_CODEX_MODEL=gpt-5.4", config)
        self.assertIn("VIDEO_FACTORY_QC_EVIDENCE_ROOT=", config)
        self.assertIn("VIDEO_FACTORY_HYPERFRAMES_PROJECT_ROOT=", config)
        self.assertIn("VIDEO_FACTORY_RENDER_OUTPUT_ROOT=", config)
        self.assertIn("VIDEO_FACTORY_FACE_ENGINE=yunet", config)
        self.assertIn("VIDEO_FACTORY_FACE_MODEL_PATH=", config)
        self.assertIn("VIDEO_FACTORY_FACE_MODEL_SHA256=", config)
        self.assertIn("VIDEO_FACTORY_MEDIA_ALLOW_RIGHTS_DOWNLOADS=true", config)
        self.assertIn(
            "VIDEO_FACTORY_DB=/var/lib/video-factory/queue/factory.sqlite3", config
        )
        self.assertNotIn("CODEX_API_KEY=", config)
        self.assertNotIn("OPENAI_API_KEY=", config)
        self.assertNotIn("PEXELS_API_KEY=", config)


if __name__ == "__main__":
    unittest.main()
