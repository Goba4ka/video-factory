from __future__ import annotations

import importlib.util
from unittest import mock
import sys
import tempfile
import unittest
from pathlib import Path


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
            "VIDEO_FACTORY_DB": str(root / "runtime" / "factory.sqlite3"),
            "VIDEO_FACTORY_WORKSPACE": str(root / "workspace"),
            "VIDEO_FACTORY_CODEX_WORKSPACE": str(root / "codex-workspace"),
            "VIDEO_FACTORY_AGENT_OUTPUT_ROOT": str(root / "runtime" / "outputs"),
            "VIDEO_FACTORY_CODEX": str(root / "bin" / "codex"),
            "VIDEO_FACTORY_CODEX_VERSION": "0.151.0",
            "VIDEO_FACTORY_CODEX_MODEL": "gpt-5.4",
            "VIDEO_FACTORY_FFMPEG": str(root / "bin" / "ffmpeg"),
            "VIDEO_FACTORY_FFPROBE": str(root / "bin" / "ffprobe"),
            "VIDEO_FACTORY_NODE": str(root / "bin" / "node"),
            "HYPERFRAMES_VERSION": "0.8.17",
            "HYPERFRAMES_BIN": str(root / "bin" / "hyperframes"),
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

    def test_only_allowlisted_editorial_roles_can_start(self) -> None:
        for role in server_preflight.ALLOWED_AUTONOMOUS_ROLES:
            server_preflight.validate_role(role)
        for role in ("final_review", "publisher", "render", "anything"):
            with self.assertRaises(server_preflight.PreflightError):
                server_preflight.validate_role(role)

    def test_semver_parser_requires_a_real_version(self) -> None:
        self.assertEqual(server_preflight.parse_semver("codex-cli 0.151.0"), (0, 151, 0))
        with self.assertRaises(server_preflight.PreflightError):
            server_preflight.parse_semver("codex latest")

    def test_canonical_contract_check_passes_for_packaged_schemas(self) -> None:
        check = server_preflight._contracts_check()
        self.assertTrue(check.ok, check.detail)
        self.assertRegex(check.detail, r"^16 schemas at ")

    def test_canonical_contract_check_fails_closed_when_schema_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp)
            with mock.patch("video_factory.contracts.contracts_dir", return_value=empty):
                check = server_preflight._contracts_check()
        self.assertFalse(check.ok)
        self.assertIn("missing=", check.detail)


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

    def test_example_config_pins_codex_and_contains_no_raw_key(self) -> None:
        config = (FACTORY_ROOT / "deployment" / "server.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("VIDEO_FACTORY_CODEX_VERSION=0.151.0", config)
        self.assertIn("VIDEO_FACTORY_CODEX_MODEL=gpt-5.4", config)
        self.assertNotIn("CODEX_API_KEY=", config)
        self.assertNotIn("OPENAI_API_KEY=", config)


if __name__ == "__main__":
    unittest.main()
