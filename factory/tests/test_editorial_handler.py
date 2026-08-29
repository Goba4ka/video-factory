from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_factory.editorial_handler import build_editorial_prompt, handle_task
from video_factory.errors import ValidationError


class EditorialHandlerTests(unittest.TestCase):
    def test_prompt_has_bounded_public_context(self) -> None:
        prompt = build_editorial_prompt(
            {
                "id": "task_123456",
                "job_id": "job_123456",
                "role": "script",
                "pod": "health",
                "payload": {"required_result_contract": "script_package"},
                "upstream_results": [
                    {"task_id": "dep", "role": "research", "result": {"ok": True}}
                ],
            },
            "ROLE RULES",
        )
        self.assertIn("ROLE RULES", prompt)
        self.assertIn('"upstream_results"', prompt)
        self.assertNotIn("lease_token", prompt)

    def test_rejects_autonomous_final_review(self) -> None:
        with self.assertRaisesRegex(ValidationError, "not autonomous"):
            build_editorial_prompt(
                {
                    "id": "task_123456",
                    "role": "final_review",
                    "payload": {},
                    "upstream_results": [],
                },
                "rules",
            )

    def test_rejects_role_contract_mismatch(self) -> None:
        with self.assertRaisesRegex(ValidationError, "required_result_contract"):
            build_editorial_prompt(
                {
                    "id": "task_123456",
                    "role": "script",
                    "payload": {"required_result_contract": "claim_ledger"},
                    "upstream_results": [],
                },
                "rules",
            )

    def test_handle_task_returns_queue_compatible_artifact_wrapper(self) -> None:
        artifact = {
            "schema_version": "1.0.0",
            "idea_id": "idea_research_001",
            "sources": [
                {
                    "source_id": "src_001",
                    "url": "https://example.com/source",
                    "publisher": "Example",
                    "retrieved_at": "2026-08-29T10:00:00Z",
                    "primary": True,
                }
            ],
            "claims": [
                {
                    "claim_id": "claim_001",
                    "text": "Проверенный факт для теста контракта.",
                    "source_ids": ["src_001"],
                    "support": "direct",
                    "risk": "green",
                }
            ],
            "decision": {
                "passed": True,
                "needs_human_review": False,
                "review_notes": [],
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompts = root / "prompts"
            prompts.mkdir()
            (prompts / "research.md").write_text("Research safely.", encoding="utf-8")
            env = {
                "VIDEO_FACTORY_WORKSPACE": str(root),
                "VIDEO_FACTORY_CODEX_WORKSPACE": str(root),
                "VIDEO_FACTORY_AGENT_OUTPUT_ROOT": str(root / "outputs"),
                "VIDEO_FACTORY_PROMPT_ROOT": str(prompts),
            }
            backend = {
                "result": artifact,
                "backend": "codex_exec",
                "codex_version": "0.151.0",
                "model": "gpt-5.4",
                "output_path": str(root / "outputs" / "artifact.json"),
                "output_sha256": "a" * 64,
                "usage": {"input_tokens": 10},
                "elapsed_seconds": 1.25,
            }
            task = {
                "id": "task_research_001",
                "job_id": "job_research_001",
                "role": "research",
                "pod": "health",
                "attempt_count": 1,
                "payload": {"required_result_contract": "claim_ledger"},
                "upstream_results": [],
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch(
                "video_factory.editorial_handler.run_codex_json", return_value=backend
            ):
                result = handle_task(task)
        self.assertEqual(result["artifact"], artifact)
        self.assertEqual(result["agent_execution"]["backend"], "codex_exec")
        self.assertNotIn("thread_id", result["agent_execution"])


if __name__ == "__main__":
    unittest.main()
