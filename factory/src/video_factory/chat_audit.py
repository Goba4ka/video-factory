from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import FactoryError
from .lanes import load_lane_registry


_CHAT_ID_TOKEN = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
)
_PRODUCER_AGENT = re.compile(
    r"(?:\bproducer[-_ ]agent\b|\bproducer_agent\b|"
    r"(?<![A-Za-z0-9_])producer[-_ ]агент(?![A-Za-z0-9_А-Яа-яЁё])|"
    r"\bпродюсер(?:[- ]?агент)(?:а|у|ом|ы)?\b)",
    re.IGNORECASE,
)
_DELEGATION = re.compile(
    r"(?:\bdelegat(?:e|es|ed|ing|ion)\b|\bspawn(?:s|ed|ing)?\b|"
    r"\bsub[-_ ]?agents?\b|делегир|поруч|субагент)",
    re.IGNORECASE,
)
_READINESS = re.compile(
    r"(?:\bready\b|\breadiness\b|\bprepared\b|готов(?:а|ы|о|ность)?|"
    r"приступаю|можем\s+начинать|линия\s+запущена)",
    re.IGNORECASE,
)
_CODEX_DELEGATION = re.compile(r"<codex_delegation(?:\s|>)", re.IGNORECASE)


def _error(
    code: str,
    message: str,
    *,
    lane_id: str | None = None,
    chat_id: str | None = None,
    path: str | None = None,
    line: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if lane_id is not None:
        result["lane_id"] = lane_id
    if chat_id is not None:
        result["chat_id"] = chat_id
    if path is not None:
        result["path"] = path
    if line is not None:
        result["line"] = line
    return result


def _base_report(
    *, registry_path: Path, session_index: Path, sessions_root: Path
) -> dict[str, Any]:
    return {
        "ok": False,
        "command": "chat-audit",
        "read_only": True,
        "production_ready": False,
        "chat_topology_verified": False,
        "verification_scope": "five_codex_chat_topology_only",
        "inputs": {
            "registry": str(registry_path),
            "session_index": str(session_index),
            "sessions_root": str(sessions_root),
        },
        "summary": {
            "expected_lanes": 5,
            "registry_lanes": 0,
            "verified_lanes": 0,
        },
        "lanes": [],
        "errors": [],
    }


def _load_jsonl(path: Path, *, label: str) -> tuple[list[tuple[int, dict[str, Any]]], list[dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []
    errors: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as source:
            for line_no, raw in enumerate(source, start=1):
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    errors.append(
                        _error(
                            f"{label}_invalid_json",
                            f"{label} line is not valid JSON: {exc.msg}",
                            path=str(path),
                            line=line_no,
                        )
                    )
                    continue
                if not isinstance(value, dict):
                    errors.append(
                        _error(
                            f"{label}_record_not_object",
                            f"{label} line must be a JSON object",
                            path=str(path),
                            line=line_no,
                        )
                    )
                    continue
                records.append((line_no, value))
    except (OSError, UnicodeError) as exc:
        errors.append(
            _error(
                f"{label}_unreadable",
                f"cannot read {label}: {exc}",
                path=str(path),
            )
        )
    return records, errors


def _index_chat_id(record: Mapping[str, Any]) -> tuple[str | None, bool]:
    values = {
        value.strip().lower()
        for key in ("id", "session_id", "thread_id")
        if isinstance((value := record.get(key)), str) and value.strip()
    }
    if len(values) != 1:
        return None, len(values) > 1
    return next(iter(values)), False


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _text_content(item)))
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key in ("text", "message", "content"):
            if key in value:
                part = _text_content(value[key])
                if part:
                    parts.append(part)
        return "\n".join(parts)
    return ""


def _message(record: Mapping[str, Any]) -> tuple[str, str] | None:
    candidates: list[Mapping[str, Any]] = [record]
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        candidates.insert(0, payload)

    for candidate in candidates:
        role = candidate.get("role")
        if isinstance(role, str) and role.lower() in {"user", "assistant"}:
            text = _text_content(candidate.get("content"))
            if not text:
                text = _text_content(candidate.get("message"))
            return role.lower(), text.strip()

    if isinstance(payload, Mapping):
        event_type = payload.get("type")
        if event_type == "user_message":
            return "user", _text_content(payload.get("message")).strip()
        if event_type in {"agent_message", "assistant_message"}:
            return "assistant", _text_content(payload.get("message")).strip()
    return None


def _initialization_checks(text: str, lane_id: str) -> dict[str, bool]:
    exact_package = f"factory/lanes/{lane_id}"
    package_pattern = re.compile(
        rf"(?<![A-Za-z0-9_.-]){re.escape(exact_package)}(?![A-Za-z0-9_-])"
    )
    return {
        "exact_lane_package_reference": package_pattern.search(text) is not None,
        "producer_agent_named": _PRODUCER_AGENT.search(text) is not None,
        "producer_agent_delegation": (
            _CODEX_DELEGATION.search(text) is not None
            or _DELEGATION.search(text) is not None
        ),
    }


def _is_explicit_producer_delegation(text: str) -> bool:
    if _CODEX_DELEGATION.search(text) is not None:
        return True
    return (
        _PRODUCER_AGENT.search(text) is not None
        and _DELEGATION.search(text) is not None
    )


def _find_rollouts(
    sessions_root: Path, chat_id: str
) -> tuple[list[Path], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    matches: list[Path] = []
    root = sessions_root.resolve()
    try:
        candidates: Iterable[Path] = sessions_root.rglob("rollout-*.jsonl")
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError):
                errors.append(
                    _error(
                        "rollout_outside_sessions_root",
                        "rollout resolves outside the explicit sessions root",
                        chat_id=chat_id,
                        path=str(candidate),
                    )
                )
                continue
            tokens = [match.group(0) for match in _CHAT_ID_TOKEN.finditer(candidate.name.lower())]
            if chat_id in tokens:
                matches.append(candidate)
    except OSError as exc:
        errors.append(
            _error(
                "sessions_root_unreadable",
                f"cannot enumerate sessions root: {exc}",
                path=str(sessions_root),
            )
        )
    return sorted(matches), errors


def audit_chat_topology(
    *,
    registry_path: str | Path,
    session_index: str | Path,
    sessions_root: str | Path,
) -> dict[str, Any]:
    """Read-only, fail-closed verification of the five registered Codex chats.

    The caller must explicitly provide both Codex evidence paths. This function
    opens files only for reading and never discovers or writes a default
    ``.codex`` location.
    """

    registry_file = Path(registry_path).expanduser().resolve()
    index_file = Path(session_index).expanduser().resolve()
    sessions_dir = Path(sessions_root).expanduser().resolve()
    report = _base_report(
        registry_path=registry_file,
        session_index=index_file,
        sessions_root=sessions_dir,
    )
    errors: list[dict[str, Any]] = report["errors"]

    try:
        registry = load_lane_registry(registry_file)
    except FactoryError as exc:
        errors.append(
            _error(
                "lane_registry_invalid",
                str(exc),
                path=str(registry_file),
            )
        )
        return report

    lanes = [dict(lane) for lane in registry["lanes"] if lane["enabled"]]
    report["summary"]["registry_lanes"] = len(lanes)
    if len(lanes) != 5:
        errors.append(
            _error(
                "enabled_lane_count_mismatch",
                f"expected exactly 5 enabled lanes, found {len(lanes)}",
                path=str(registry_file),
            )
        )

    if not index_file.is_file():
        errors.append(
            _error(
                "session_index_missing",
                "explicit session index is not a readable file",
                path=str(index_file),
            )
        )
        index_records: list[tuple[int, dict[str, Any]]] = []
    else:
        index_records, index_errors = _load_jsonl(index_file, label="session_index")
        errors.extend(index_errors)

    if not sessions_dir.is_dir():
        errors.append(
            _error(
                "sessions_root_missing",
                "explicit sessions root is not a readable directory",
                path=str(sessions_dir),
            )
        )

    indexed: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for line_no, record in index_records:
        chat_id, conflicting = _index_chat_id(record)
        if conflicting:
            errors.append(
                _error(
                    "session_index_conflicting_ids",
                    "session index record contains conflicting exact ID fields",
                    path=str(index_file),
                    line=line_no,
                )
            )
        elif chat_id is None:
            errors.append(
                _error(
                    "session_index_id_missing",
                    "session index record has no non-empty id, session_id, or thread_id",
                    path=str(index_file),
                    line=line_no,
                )
            )
        elif chat_id is not None:
            indexed.setdefault(chat_id, []).append((line_no, record))

    for lane in lanes:
        lane_id = lane["id"]
        chat_id = lane["chat_id"].lower()
        lane_errors: list[dict[str, Any]] = []
        occurrences = indexed.get(chat_id, [])
        latest_line: int | None = None
        thread_name: str | None = None
        if not occurrences:
            lane_errors.append(
                _error(
                    "chat_id_missing_from_session_index",
                    "registered chat_id is absent from the explicit session index",
                    lane_id=lane_id,
                    chat_id=chat_id,
                    path=str(index_file),
                )
            )
        else:
            latest_line, latest_record = occurrences[-1]
            raw_name = latest_record.get("thread_name")
            if isinstance(raw_name, str) and raw_name.strip():
                thread_name = raw_name.strip()
            else:
                lane_errors.append(
                    _error(
                        "latest_thread_name_missing",
                        "latest exact session-index record has no non-empty thread_name",
                        lane_id=lane_id,
                        chat_id=chat_id,
                        path=str(index_file),
                        line=latest_line,
                    )
                )

        rollout_path: Path | None = None
        rollout_relative: str | None = None
        init_line: int | None = None
        readiness_line: int | None = None
        init_checks = {
            "exact_lane_package_reference": False,
            "producer_agent_named": False,
            "producer_agent_delegation": False,
        }
        if sessions_dir.is_dir():
            rollouts, rollout_search_errors = _find_rollouts(sessions_dir, chat_id)
            for row in rollout_search_errors:
                row.setdefault("lane_id", lane_id)
            lane_errors.extend(rollout_search_errors)
            if not rollouts:
                lane_errors.append(
                    _error(
                        "exact_rollout_missing",
                        "no rollout-*.jsonl filename contains the exact registered chat_id",
                        lane_id=lane_id,
                        chat_id=chat_id,
                        path=str(sessions_dir),
                    )
                )
            elif len(rollouts) > 1:
                lane_errors.append(
                    _error(
                        "exact_rollout_ambiguous",
                        f"found {len(rollouts)} rollout files containing the exact chat_id",
                        lane_id=lane_id,
                        chat_id=chat_id,
                        path=str(sessions_dir),
                    )
                )
            else:
                rollout_path = rollouts[0]
                rollout_relative = rollout_path.resolve().relative_to(sessions_dir).as_posix()
                rollout_records, rollout_errors = _load_jsonl(
                    rollout_path, label="rollout"
                )
                for row in rollout_errors:
                    row.setdefault("lane_id", lane_id)
                    row.setdefault("chat_id", chat_id)
                lane_errors.extend(rollout_errors)

                messages = [
                    (line_no, message)
                    for line_no, record in rollout_records
                    if (message := _message(record)) is not None
                ]
                user_messages = [
                    (line_no, text)
                    for line_no, (role, text) in messages
                    if role == "user"
                ]
                explicit_initializations = [
                    (line_no, text)
                    for line_no, text in user_messages
                    if _is_explicit_producer_delegation(text)
                ]
                if not explicit_initializations:
                    lane_errors.append(
                        _error(
                            "user_initialization_missing",
                            (
                                "rollout has no explicit producer delegation user message "
                                "or <codex_delegation> block"
                            ),
                            lane_id=lane_id,
                            chat_id=chat_id,
                            path=str(rollout_path),
                        )
                    )
                else:
                    init_line, initialization = explicit_initializations[0]
                    init_checks = _initialization_checks(initialization, lane_id)
                    if not init_checks["exact_lane_package_reference"]:
                        lane_errors.append(
                            _error(
                                "lane_package_reference_missing",
                                (
                                    "first explicit producer delegation must reference exact "
                                    f"factory/lanes/{lane_id}"
                                ),
                                lane_id=lane_id,
                                chat_id=chat_id,
                                path=str(rollout_path),
                                line=init_line,
                            )
                        )
                    if not (
                        init_checks["producer_agent_named"]
                        and init_checks["producer_agent_delegation"]
                    ):
                        lane_errors.append(
                            _error(
                                "producer_agent_delegation_missing",
                                (
                                    "first explicit delegation must name producer-agent and "
                                    "delegate work"
                                ),
                                lane_id=lane_id,
                                chat_id=chat_id,
                                path=str(rollout_path),
                                line=init_line,
                            )
                        )

                    for line_no, (role, text) in messages:
                        if (
                            line_no > init_line
                            and role == "assistant"
                            and text
                            and _READINESS.search(text) is not None
                        ):
                            readiness_line = line_no
                            break
                    if readiness_line is None:
                        lane_errors.append(
                            _error(
                                "assistant_readiness_missing",
                                "rollout has no assistant readiness response after initialization",
                                lane_id=lane_id,
                                chat_id=chat_id,
                                path=str(rollout_path),
                            )
                        )

        lane_report = {
            "lane_id": lane_id,
            "chat_id": chat_id,
            "verified": not lane_errors,
            "session_index": {
                "exact_id_present": bool(occurrences),
                "record_count": len(occurrences),
                "latest_record_line": latest_line,
                "latest_thread_name": thread_name,
            },
            "rollout": {
                "relative_path": rollout_relative,
                "exact_id_in_filename": rollout_path is not None,
                "user_initialization_line": init_line,
                "assistant_readiness_line": readiness_line,
            },
            "initialization_checks": init_checks,
            "errors": lane_errors,
        }
        report["lanes"].append(lane_report)
        errors.extend(lane_errors)

    verified_lanes = sum(1 for lane in report["lanes"] if lane["verified"])
    report["summary"]["verified_lanes"] = verified_lanes
    topology_verified = len(lanes) == 5 and verified_lanes == 5 and not errors
    report["chat_topology_verified"] = topology_verified
    report["ok"] = topology_verified
    return report


# Symmetric with other acceptance modules and convenient for API callers.
evaluate_chat_audit = audit_chat_topology
