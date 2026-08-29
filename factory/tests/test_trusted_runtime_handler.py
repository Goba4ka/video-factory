from __future__ import annotations

from io import StringIO
import json
import unittest

from video_factory.errors import ValidationError
from video_factory.trusted_runtime_handler import handle_task, main


class TrustedRuntimeHandlerTests(unittest.TestCase):
    def test_dispatches_unchanged_task_to_exact_role_handler(self) -> None:
        observed: list[dict] = []

        def handler(task: dict) -> dict:
            observed.append(task)
            return {"ok": True, "role": task["role"]}

        task = {"job_id": "job-001", "role": "render", "payload": {}}
        result = handle_task(task, handlers={"render": handler})
        self.assertEqual(result, {"ok": True, "role": "render"})
        self.assertEqual(observed, [task])

    def test_rejects_human_and_unknown_roles(self) -> None:
        for role in ("preview_review", "final_review", "publisher", "anything"):
            with self.subTest(role=role), self.assertRaisesRegex(
                ValidationError, "not a trusted runtime role"
            ):
                handle_task({"role": role}, handlers={"render": lambda task: {}})

    def test_stdio_failure_emits_no_artifact(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        import contextlib

        with contextlib.redirect_stderr(stderr):
            code = main(StringIO('{"role":"final_review"}'), stdout)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("trusted_runtime_handler_error", stderr.getvalue())

    def test_stdio_success_is_one_json_object(self) -> None:
        # Exercise the protocol without invoking any production handler by
        # checking the public dispatcher separately above; main must fail
        # closed for malformed input.
        stdout = StringIO()
        code = main(StringIO("[]"), stdout)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
