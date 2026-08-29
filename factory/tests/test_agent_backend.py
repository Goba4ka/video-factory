from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_factory.agent_backend import CodexExecRequest, run_codex_json
from video_factory.errors import ValidationError


class AgentBackendTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.schema = self.root / "schema.json"
        self.schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                }
            ),
            encoding="utf-8",
        )
        self.binary = self.root / "codex.exe"
        self.binary.write_bytes(b"test")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, **overrides) -> CodexExecRequest:
        values = {
            "task_id": "task_001",
            "role": "script",
            "prompt": "Верни JSON.",
            "schema_path": self.schema,
            "workspace": self.root,
            "output_path": self.root / "result.json",
            "timeout_seconds": 60,
        }
        values.update(overrides)
        return CodexExecRequest(**values)

    def test_runs_read_only_with_prompt_on_stdin_and_atomic_json_output(self) -> None:
        def fake_run(command, **kwargs):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command, 0, stdout="codex-cli 0.151.0\n", stderr=""
                )
            output = Path(command[command.index("-o") + 1])
            output.write_text('{"answer":"готово"}', encoding="utf-8")
            stdout = "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "thread_123"}),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 10,
                                "cached_input_tokens": 2,
                                "output_tokens": 3,
                                "reasoning_output_tokens": 1,
                            },
                        }
                    ),
                ]
            )
            self.assertEqual(kwargs["input"], "Верни JSON.")
            self.assertNotIn("Верни JSON.", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertIn("--output-schema", command)
            self.assertIn("--ephemeral", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--ignore-rules", command)
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with mock.patch("video_factory.agent_backend.subprocess.run", side_effect=fake_run):
            result = run_codex_json(self.request(), codex_binary=self.binary)

        self.assertTrue(result["ok"])
        self.assertEqual(result["thread_id"], "thread_123")
        self.assertEqual(result["usage"]["input_tokens"], 10)
        self.assertEqual(result["result"], {"answer": "готово"})
        self.assertEqual(
            json.loads((self.root / "result.json").read_text(encoding="utf-8")),
            {"answer": "готово"},
        )

    def test_refuses_human_final_review_and_publisher_roles(self) -> None:
        for role in ("medical_review", "rights", "final_review", "publisher", "render"):
            with self.subTest(role=role), self.assertRaisesRegex(
                ValidationError, "not eligible"
            ):
                run_codex_json(
                    self.request(role=role, output_path=self.root / f"{role}.json"),
                    codex_binary=self.binary,
                )

    def test_web_search_is_explicit_and_global(self) -> None:
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command, 0, stdout="codex-cli 0.151.0\n", stderr=""
                )
            Path(command[command.index("-o") + 1]).write_text(
                '{"answer":"ok"}', encoding="utf-8"
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"type": "turn.completed", "usage": {}}),
                stderr="",
            )

        with mock.patch("video_factory.agent_backend.subprocess.run", side_effect=fake_run):
            run_codex_json(
                self.request(role="research", web_search=True), codex_binary=self.binary
            )
        self.assertEqual(commands[1][1:3], ["--search", "exec"])

    def test_refuses_to_overwrite_output(self) -> None:
        output = self.root / "exists.json"
        output.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "refusing to overwrite"):
            run_codex_json(
                self.request(output_path=output), codex_binary=self.binary
            )

    def test_deletes_partial_output_after_failed_turn(self) -> None:
        def fake_run(command, **kwargs):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command, 0, stdout="codex-cli 0.151.0\n", stderr=""
                )
            Path(command[command.index("-o") + 1]).write_text(
                '{"answer":"partial"}', encoding="utf-8"
            )
            return subprocess.CompletedProcess(
                command, 2, stdout="", stderr="authentication failed"
            )

        with mock.patch("video_factory.agent_backend.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(ValidationError, "authentication failed"):
                run_codex_json(self.request(), codex_binary=self.binary)
        self.assertFalse((self.root / "result.json").exists())
        self.assertEqual(list(self.root.glob(".result.json.*.json")), [])

    def test_rejects_outdated_codex_before_dispatch(self) -> None:
        with mock.patch(
            "video_factory.agent_backend.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [str(self.binary), "--version"],
                0,
                stdout="codex-cli 0.98.0\n",
                stderr="",
            ),
        ):
            with self.assertRaisesRegex(ValidationError, "too old"):
                run_codex_json(self.request(), codex_binary=self.binary)


if __name__ == "__main__":
    unittest.main()
