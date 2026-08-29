from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from video_factory.chat_audit import audit_chat_topology
from video_factory.cli import main
from video_factory.lanes import load_lane_registry


def _jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


class ChatAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = self.root / "registry.json"
        self.session_index = self.root / "session_index.jsonl"
        self.sessions_root = self.root / "sessions"
        self.registry_data = copy.deepcopy(load_lane_registry())
        self.registry.write_text(
            json.dumps(self.registry_data, ensure_ascii=False), encoding="utf-8"
        )
        self._write_valid_evidence()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_valid_evidence(self) -> None:
        index_records: list[dict[str, Any]] = []
        for lane in self.registry_data["lanes"]:
            chat_id = lane["chat_id"]
            if lane["id"] == "war_history":
                index_records.append(
                    {"id": chat_id, "thread_name": "Старое имя, не использовать"}
                )
            index_records.append(
                {
                    "id": chat_id,
                    "thread_name": f"Продюсер — {lane['id']}",
                    "updated_at": "2026-08-30T08:00:00Z",
                }
            )
            rollout = (
                self.sessions_root
                / "2026"
                / "08"
                / "30"
                / f"rollout-2026-08-30T08-00-00-{chat_id}.jsonl"
            )
            _jsonl(
                rollout,
                [
                    {
                        "timestamp": "2026-08-30T08:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": (
                                f"Инициализируй producer-agent для "
                                f"factory/lanes/{lane['id']} и delegate задачи "
                                "профильным sub-agents."
                            ),
                        },
                    },
                    {
                        "timestamp": "2026-08-30T08:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "Готов. Линия запущена."}
                            ],
                        },
                    },
                ],
            )
        _jsonl(self.session_index, index_records)

    def _audit(self) -> dict[str, Any]:
        return audit_chat_topology(
            registry_path=self.registry,
            session_index=self.session_index,
            sessions_root=self.sessions_root,
        )

    def _snapshot(self) -> dict[str, str]:
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def test_accepts_five_exact_initialized_chats_read_only(self) -> None:
        before = self._snapshot()
        report = self._audit()
        after = self._snapshot()

        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(report["chat_topology_verified"])
        self.assertTrue(report["read_only"])
        self.assertFalse(report["production_ready"])
        self.assertEqual(report["verification_scope"], "five_codex_chat_topology_only")
        self.assertEqual(report["summary"]["verified_lanes"], 5)
        self.assertEqual(before, after)

        war = next(row for row in report["lanes"] if row["lane_id"] == "war_history")
        self.assertEqual(war["session_index"]["record_count"], 2)
        self.assertEqual(
            war["session_index"]["latest_thread_name"], "Продюсер — war_history"
        )
        self.assertIn(war["chat_id"], Path(war["rollout"]["relative_path"]).name)

    def test_missing_exact_index_id_fails_closed(self) -> None:
        records = [
            json.loads(line)
            for line in self.session_index.read_text(encoding="utf-8").splitlines()
        ]
        missing = self.registry_data["lanes"][1]["chat_id"]
        _jsonl(self.session_index, [record for record in records if record["id"] != missing])

        report = self._audit()
        self.assertFalse(report["chat_topology_verified"])
        self.assertFalse(report["production_ready"])
        self.assertIn(
            "chat_id_missing_from_session_index",
            {error["code"] for error in report["errors"]},
        )

    def test_duplicate_chat_id_in_registry_is_structured_failure(self) -> None:
        self.registry_data["lanes"][1]["chat_id"] = self.registry_data["lanes"][0][
            "chat_id"
        ]
        self.registry.write_text(
            json.dumps(self.registry_data, ensure_ascii=False), encoding="utf-8"
        )

        report = self._audit()
        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["code"], "lane_registry_invalid")
        self.assertIn("duplicate lane chat_id", report["errors"][0]["message"])

    def test_id_in_rollout_content_does_not_replace_exact_filename(self) -> None:
        lane = self.registry_data["lanes"][2]
        current = next(self.sessions_root.rglob(f"*{lane['chat_id']}*.jsonl"))
        wrong = current.with_name(
            f"rollout-2026-08-30T08-00-00-{lane['chat_id']}a.jsonl"
        )
        current.rename(wrong)

        report = self._audit()
        lane_report = next(
            row for row in report["lanes"] if row["lane_id"] == lane["id"]
        )
        self.assertFalse(lane_report["verified"])
        self.assertIn(
            "exact_rollout_missing", {error["code"] for error in lane_report["errors"]}
        )

    def test_first_explicit_delegation_must_initialize_exact_lane(self) -> None:
        lane = self.registry_data["lanes"][3]
        rollout = next(self.sessions_root.rglob(f"*{lane['chat_id']}*.jsonl"))
        _jsonl(
            rollout,
            [
                {
                    "role": "user",
                    "content": "Ambient browser context for an unrelated visible page.",
                },
                {
                    "role": "user",
                    "content": "factory/lanes/health producer-agent delegate sub-agents",
                },
                {
                    "role": "user",
                    "content": (
                        f"factory/lanes/{lane['id']} producer-agent delegate sub-agents"
                    ),
                },
                {"role": "assistant", "content": "Готов."},
            ],
        )

        report = self._audit()
        lane_report = next(row for row in report["lanes"] if row["lane_id"] == lane["id"])
        codes = {error["code"] for error in lane_report["errors"]}
        self.assertIn("lane_package_reference_missing", codes)

    def test_ambient_user_like_message_before_codex_delegation_is_ignored(self) -> None:
        lane = self.registry_data["lanes"][1]
        rollout = next(self.sessions_root.rglob(f"*{lane['chat_id']}*.jsonl"))
        _jsonl(
            rollout,
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "<in-app-browser-context>ambient only</in-app-browser-context>",
                            }
                        ],
                    },
                },
                {"role": "assistant", "content": "Контекст принят."},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "<codex_delegation>\n"
                                    f"Работай как producer-агент в factory/lanes/{lane['id']}.\n"
                                    "</codex_delegation>"
                                ),
                            }
                        ],
                    },
                },
                {"role": "assistant", "content": "Готов к работе."},
            ],
        )

        report = self._audit()
        self.assertTrue(report["ok"], report["errors"])
        lane_report = next(
            row for row in report["lanes"] if row["lane_id"] == lane["id"]
        )
        self.assertEqual(lane_report["rollout"]["user_initialization_line"], 3)
        self.assertEqual(lane_report["rollout"]["assistant_readiness_line"], 4)

    def test_user_message_without_explicit_delegation_is_not_initialization(self) -> None:
        lane = self.registry_data["lanes"][2]
        rollout = next(self.sessions_root.rglob(f"*{lane['chat_id']}*.jsonl"))
        _jsonl(
            rollout,
            [
                {
                    "role": "user",
                    "content": (
                        f"Покажи статус factory/lanes/{lane['id']}; producer-agent ожидает."
                    ),
                },
                {"role": "assistant", "content": "Готов."},
            ],
        )

        report = self._audit()
        lane_report = next(
            row for row in report["lanes"] if row["lane_id"] == lane["id"]
        )
        self.assertIn(
            "user_initialization_missing",
            {error["code"] for error in lane_report["errors"]},
        )
        self.assertIsNone(lane_report["rollout"]["user_initialization_line"])

    def test_assistant_message_without_readiness_signal_is_rejected(self) -> None:
        lane = self.registry_data["lanes"][4]
        rollout = next(self.sessions_root.rglob(f"*{lane['chat_id']}*.jsonl"))
        _jsonl(
            rollout,
            [
                {
                    "role": "user",
                    "content": (
                        f"factory/lanes/{lane['id']} producer-agent delegate sub-agents"
                    ),
                },
                {"role": "assistant", "content": "Инструкция получена."},
            ],
        )

        report = self._audit()
        lane_report = next(row for row in report["lanes"] if row["lane_id"] == lane["id"])
        self.assertIn(
            "assistant_readiness_missing",
            {error["code"] for error in lane_report["errors"]},
        )

    def test_latest_index_record_must_have_thread_name(self) -> None:
        lane = self.registry_data["lanes"][0]
        with self.session_index.open("a", encoding="utf-8") as target:
            target.write(json.dumps({"id": lane["chat_id"], "thread_name": ""}) + "\n")

        report = self._audit()
        lane_report = next(
            row for row in report["lanes"] if row["lane_id"] == lane["id"]
        )
        self.assertIsNone(lane_report["session_index"]["latest_thread_name"])
        self.assertIn(
            "latest_thread_name_missing",
            {error["code"] for error in lane_report["errors"]},
        )

    def test_multiple_exact_rollouts_are_ambiguous(self) -> None:
        lane = self.registry_data["lanes"][0]
        source = next(self.sessions_root.rglob(f"*{lane['chat_id']}*.jsonl"))
        duplicate = source.parent / f"rollout-copy-{lane['chat_id']}.jsonl"
        duplicate.write_bytes(source.read_bytes())

        report = self._audit()
        lane_report = next(
            row for row in report["lanes"] if row["lane_id"] == lane["id"]
        )
        self.assertIn(
            "exact_rollout_ambiguous",
            {error["code"] for error in lane_report["errors"]},
        )

    def test_malformed_jsonl_returns_structured_error_and_cli_code_three(self) -> None:
        self.session_index.write_text("{not-json}\n", encoding="utf-8")
        out = io.StringIO()
        err = io.StringIO()
        code = main(
            [
                "chat-audit",
                "--registry",
                str(self.registry),
                "--session-index",
                str(self.session_index),
                "--sessions-root",
                str(self.sessions_root),
            ],
            out=out,
            err=err,
        )
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), "")
        payload = json.loads(err.getvalue())
        self.assertFalse(payload["chat_topology_verified"])
        self.assertIn(
            "session_index_invalid_json", {error["code"] for error in payload["errors"]}
        )

    def test_cli_success_writes_only_stdout(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        code = main(
            [
                "chat-audit",
                "--registry",
                str(self.registry),
                "--session-index",
                str(self.session_index),
                "--sessions-root",
                str(self.sessions_root),
            ],
            out=out,
            err=err,
        )
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(err.getvalue(), "")
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["chat_topology_verified"])
        self.assertFalse(payload["production_ready"])


if __name__ == "__main__":
    unittest.main()
