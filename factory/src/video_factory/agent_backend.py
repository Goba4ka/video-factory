"""Fail-closed non-interactive Codex backend for editorial queue roles.

The backend deliberately runs Codex read-only and only accepts a JSON object
that conforms to a caller-selected output schema.  Rendering, media downloads,
publishing, and the human final-review gate are not delegated to this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError
from .validators import require_nonempty_string


AUTOMATABLE_EDITORIAL_ROLES = frozenset(
    {
        "research",
        "privacy_review",
        "sensitivity_review",
        "medical_review",
        "rights",
        "script",
        "editor",
    }
)
MINIMUM_CODEX_VERSION = (0, 151, 0)


@dataclass(frozen=True)
class CodexExecRequest:
    task_id: str
    role: str
    prompt: str
    schema_path: Path
    workspace: Path
    output_path: Path
    timeout_seconds: int = 1800
    model: str = "gpt-5.4"
    web_search: bool = False


def _bounded_timeout(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 30 <= value <= 7200:
        raise ValidationError("timeout_seconds must be an integer from 30 to 7200")
    return value


def _regular_file(path: Path, name: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValidationError(f"{name} must be an existing regular file")
    return resolved


def _directory(path: Path, name: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise ValidationError(f"{name} must be an existing directory")
    return resolved


def _resolve_codex(binary: str | Path | None) -> str:
    configured = str(binary or os.environ.get("VIDEO_FACTORY_CODEX") or "codex")
    candidate = Path(configured).expanduser()
    if candidate.is_absolute():
        if not candidate.is_file():
            raise ValidationError(f"Codex executable does not exist: {candidate}")
        return str(candidate.resolve())
    found = shutil.which(configured)
    if found is None:
        raise ValidationError(
            "Codex CLI is required; install it or configure VIDEO_FACTORY_CODEX"
        )
    return str(Path(found).resolve())


def _verify_codex_version(binary: str) -> str:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError(f"cannot verify Codex CLI version: {exc}") from exc
    match = re.search(r"(?:codex-cli\s+)?(\d+)\.(\d+)\.(\d+)", completed.stdout)
    if completed.returncode != 0 or match is None:
        raise ValidationError("Codex CLI did not return a valid semantic version")
    version = tuple(int(item) for item in match.groups())
    if version < MINIMUM_CODEX_VERSION:
        required = ".".join(str(item) for item in MINIMUM_CODEX_VERSION)
        actual = ".".join(str(item) for item in version)
        raise ValidationError(
            f"Codex CLI {actual} is too old for unattended workers; require >= {required}"
        )
    return ".".join(str(item) for item in version)


def _parse_events(stdout: str) -> tuple[str | None, dict[str, int] | None]:
    thread_id: str | None = None
    usage: dict[str, int] | None = None
    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError("Codex --json emitted a non-JSON line") from exc
        if not isinstance(event, dict):
            raise ValidationError("Codex --json event must be an object")
        if event.get("type") == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str) and value:
                thread_id = value
        if event.get("type") == "turn.completed":
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                clean: dict[str, int] = {}
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                ):
                    value = raw_usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        clean[key] = value
                usage = clean
        if event.get("type") in {"turn.failed", "error"}:
            raise ValidationError("Codex reported a failed non-interactive turn")
    return thread_id, usage


def run_codex_json(
    request: CodexExecRequest,
    *,
    codex_binary: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run one schema-bound editorial turn and atomically persist its JSON result."""

    task_id = require_nonempty_string(request.task_id, "task_id")
    role = require_nonempty_string(request.role, "role")
    if role not in AUTOMATABLE_EDITORIAL_ROLES:
        raise ValidationError(
            f"role {role!r} is not eligible for autonomous editorial execution"
        )
    prompt = require_nonempty_string(request.prompt, "prompt")
    if len(prompt.encode("utf-8")) > 256_000:
        raise ValidationError("prompt exceeds the 256000-byte safety limit")
    schema_path = _regular_file(request.schema_path, "schema_path")
    workspace = _directory(request.workspace, "workspace")
    timeout_seconds = _bounded_timeout(request.timeout_seconds)
    output_path = request.output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise ValidationError(f"refusing to overwrite existing agent output: {output_path}")
    binary = _resolve_codex(codex_binary)
    codex_version = _verify_codex_version(binary)

    command = [binary]
    if request.web_search:
        command.append("--search")
    command.extend([
        "exec",
        "--sandbox",
        "read-only",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-C",
        str(workspace),
        "--output-schema",
        str(schema_path),
    ])
    command.extend(["--model", require_nonempty_string(request.model, "model")])

    temporary: Path | None = None
    started = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json", prefix=f".{output_path.name}.",
            dir=output_path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
        command.extend(["-o", str(temporary), "-"])
        child_env = os.environ.copy()
        child_env["NO_COLOR"] = "1"
        if environment:
            child_env.update({str(key): str(value) for key, value in environment.items()})
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValidationError(
                f"Codex editorial task exceeded {timeout_seconds}s"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip().replace("\r", " ").replace("\n", " ")[-1000:]
            raise ValidationError(f"Codex editorial task failed: {detail}")
        thread_id, usage = _parse_events(completed.stdout)
        try:
            raw = temporary.read_bytes()
            result = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Codex final output is not valid UTF-8 JSON") from exc
        if not isinstance(result, dict):
            raise ValidationError("Codex final output must be a JSON object")
        canonical = (
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        temporary.write_bytes(canonical)
        os.replace(temporary, output_path)
        temporary = None
        return {
            "ok": True,
            "backend": "codex_exec",
            "codex_version": codex_version,
            "model": request.model,
            "task_id": task_id,
            "role": role,
            "output_path": str(output_path),
            "output_sha256": hashlib.sha256(canonical).hexdigest(),
            "thread_id": thread_id,
            "usage": usage,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "result": result,
        }
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = [
    "AUTOMATABLE_EDITORIAL_ROLES",
    "CodexExecRequest",
    "run_codex_json",
]
