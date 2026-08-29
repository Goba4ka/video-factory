#!/usr/bin/env python3
"""Fail-closed production preflight for a single-host Video Factory deployment.

This script intentionally lives outside the application CLI so a broken Python
package entry point cannot make a deployment look healthy.  It never prints or
reads credential contents; authentication is verified through ``codex login
status`` executed as the service user.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


ALLOWED_AUTONOMOUS_ROLES = frozenset(
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
FORBIDDEN_RUNTIME_KEYS = frozenset(
    {"OPENAI_API_KEY", "CODEX_API_KEY", "FISH_API_KEY"}
)
NETWORK_FILESYSTEMS = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "sshfs", "fuse.sshfs", "fuse.rclone"}
)
VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")


class PreflightError(RuntimeError):
    """A required production invariant is not satisfied."""


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def load_env_file(path: str | Path) -> dict[str, str]:
    """Parse a systemd-compatible KEY=VALUE file without evaluating shell code."""

    result: dict[str, str] = {}
    source = Path(path)
    for number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PreflightError(f"{source}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise PreflightError(f"{source}:{number}: invalid environment key")
        value = value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise PreflightError(f"{source}:{number}: unterminated quoted value")
            value = value[1:-1]
        result[key] = value
    return result


def parse_semver(text: str) -> tuple[int, int, int]:
    match = VERSION_RE.search(text)
    if match is None:
        raise PreflightError(f"cannot parse semantic version from {text!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def validate_role(role: str) -> None:
    if role not in ALLOWED_AUTONOMOUS_ROLES:
        allowed = ", ".join(sorted(ALLOWED_AUTONOMOUS_ROLES))
        raise PreflightError(f"role {role!r} is not autonomous; allowed: {allowed}")


def validate_runtime_config(config: Mapping[str, str]) -> None:
    leaked = sorted(key for key in FORBIDDEN_RUNTIME_KEYS if config.get(key))
    if leaked:
        raise PreflightError(
            "raw credentials are forbidden in runtime.env: " + ", ".join(leaked)
        )
    required = {
        "VIDEO_FACTORY_RUNTIME_ROOT",
        "VIDEO_FACTORY_DB",
        "VIDEO_FACTORY_WORKSPACE",
        "VIDEO_FACTORY_CODEX_WORKSPACE",
        "VIDEO_FACTORY_AGENT_OUTPUT_ROOT",
        "VIDEO_FACTORY_CODEX",
        "VIDEO_FACTORY_CODEX_VERSION",
        "VIDEO_FACTORY_CODEX_MODEL",
        "VIDEO_FACTORY_FFMPEG",
        "VIDEO_FACTORY_FFPROBE",
        "VIDEO_FACTORY_NODE",
        "HYPERFRAMES_VERSION",
        "HYPERFRAMES_BIN",
    }
    missing = sorted(key for key in required if not config.get(key))
    if missing:
        raise PreflightError("missing runtime settings: " + ", ".join(missing))
    if config["VIDEO_FACTORY_CODEX_MODEL"] != "gpt-5.4":
        raise PreflightError("VIDEO_FACTORY_CODEX_MODEL must be explicitly pinned to gpt-5.4")
    if config["VIDEO_FACTORY_CODEX_VERSION"] != "0.151.0":
        raise PreflightError("VIDEO_FACTORY_CODEX_VERSION must be exactly 0.151.0")
    if config["HYPERFRAMES_VERSION"] != "0.8.17":
        raise PreflightError("HYPERFRAMES_VERSION must be exactly 0.8.17")
    for key in (
        "VIDEO_FACTORY_RUNTIME_ROOT",
        "VIDEO_FACTORY_DB",
        "VIDEO_FACTORY_WORKSPACE",
        "VIDEO_FACTORY_CODEX_WORKSPACE",
        "VIDEO_FACTORY_AGENT_OUTPUT_ROOT",
        "VIDEO_FACTORY_CODEX",
        "VIDEO_FACTORY_FFMPEG",
        "VIDEO_FACTORY_FFPROBE",
        "VIDEO_FACTORY_NODE",
        "HYPERFRAMES_BIN",
    ):
        if not Path(config[key]).is_absolute():
            raise PreflightError(f"{key} must be an absolute path")


def _run(argv: Sequence[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"cannot run {argv[0]!r}: {exc}") from exc


def _command_check(
    name: str,
    argv: Sequence[str],
    *,
    contains: str | None = None,
    timeout: int = 30,
) -> Check:
    completed = _run(argv, timeout=timeout)
    output = (completed.stdout + "\n" + completed.stderr).strip()
    ok = completed.returncode == 0 and (contains is None or contains in output)
    detail = output.splitlines()[0] if output else f"exit={completed.returncode}"
    return Check(name, ok, detail[:400])


def _sqlite_check(db_path: str | Path) -> list[Check]:
    path = Path(db_path)
    if not path.is_file():
        return [Check("sqlite_database", False, f"missing: {path}")]
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
            schema = connection.execute("PRAGMA user_version").fetchone()[0]
    except (OSError, sqlite3.Error) as exc:
        return [Check("sqlite_database", False, str(exc))]
    return [
        Check("sqlite_integrity", integrity == "ok", str(integrity)),
        Check("sqlite_journal", str(journal).lower() == "wal", str(journal)),
        Check("sqlite_schema", isinstance(schema, int) and schema > 0, f"user_version={schema}"),
    ]


def _filesystem_check(db_path: str | Path) -> Check:
    findmnt = shutil.which("findmnt")
    if not findmnt:
        return Check("sqlite_filesystem", False, "findmnt is unavailable")
    completed = _run([findmnt, "-T", str(Path(db_path).parent), "-no", "FSTYPE"])
    filesystem = completed.stdout.strip().lower()
    ok = completed.returncode == 0 and filesystem not in NETWORK_FILESYSTEMS
    return Check("sqlite_filesystem", ok, filesystem or f"exit={completed.returncode}")


def _writable_directory(name: str, path: str | Path) -> Check:
    candidate = Path(path)
    return Check(
        name,
        candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK),
        str(candidate),
    )


def _contracts_check() -> Check:
    """Verify that every canonical contract shipped with the Python package loads.

    This deliberately imports the installed package instead of consulting the
    deployment checkout.  A wheel missing package data must keep production
    closed even when a stale ``factory/contracts`` directory happens to exist.
    """

    try:
        from video_factory.contracts import CONTRACT_FILES, contracts_dir, load_contract

        root = contracts_dir()
        if not root.is_dir():
            return Check("canonical_contracts", False, f"missing directory: {root}")
        expected = set(CONTRACT_FILES.values())
        present = {path.name for path in root.glob("*.schema.json") if path.is_file()}
        missing = sorted(expected - present)
        unexpected = sorted(present - expected)
        if missing or unexpected:
            details: list[str] = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if unexpected:
                details.append("unexpected=" + ",".join(unexpected))
            return Check("canonical_contracts", False, "; ".join(details))
        for name in sorted(CONTRACT_FILES):
            schema = load_contract(name)
            if schema.get("type") != "object":
                return Check(
                    "canonical_contracts",
                    False,
                    f"{CONTRACT_FILES[name]}: root type must be object",
                )
            if schema.get("additionalProperties") is not False:
                return Check(
                    "canonical_contracts",
                    False,
                    f"{CONTRACT_FILES[name]}: additionalProperties must be false",
                )
    except Exception as exc:  # Package/import/JSON failures are all deployment blockers.
        return Check("canonical_contracts", False, f"{type(exc).__name__}: {exc}")
    return Check("canonical_contracts", True, f"{len(expected)} schemas at {root}")


def run_preflight(config: Mapping[str, str], *, require_gpu: bool = False) -> dict[str, object]:
    validate_runtime_config(config)
    codex = config["VIDEO_FACTORY_CODEX"]
    checks: list[Check] = []
    codex_version = _run([codex, "--version"])
    codex_output = (codex_version.stdout + "\n" + codex_version.stderr).strip()
    try:
        parsed_codex = parse_semver(codex_output)
    except PreflightError:
        parsed_codex = (-1, -1, -1)
    checks.append(
        Check(
            "codex_version",
            codex_version.returncode == 0 and parsed_codex == (0, 151, 0),
            codex_output[:400] or f"exit={codex_version.returncode}",
        )
    )
    checks.append(_command_check("codex_auth", [codex, "login", "status"]))
    checks.append(_command_check("ffmpeg", [config["VIDEO_FACTORY_FFMPEG"], "-version"]))
    checks.append(_command_check("ffprobe", [config["VIDEO_FACTORY_FFPROBE"], "-version"]))
    node_version = _run([config["VIDEO_FACTORY_NODE"], "--version"])
    node_output = (node_version.stdout + "\n" + node_version.stderr).strip()
    try:
        node_major = parse_semver(node_output)[0]
    except PreflightError:
        node_major = -1
    checks.append(
        Check(
            "node_version",
            node_version.returncode == 0 and node_major == 22,
            node_output[:400] or f"exit={node_version.returncode}",
        )
    )
    hyperframes_version = _run([config["HYPERFRAMES_BIN"], "--version"])
    hyperframes_output = (
        hyperframes_version.stdout + "\n" + hyperframes_version.stderr
    ).strip()
    try:
        parsed_hyperframes = parse_semver(hyperframes_output)
    except PreflightError:
        parsed_hyperframes = (-1, -1, -1)
    checks.append(
        Check(
            "hyperframes_version",
            hyperframes_version.returncode == 0 and parsed_hyperframes == (0, 8, 17),
            hyperframes_output[:400] or f"exit={hyperframes_version.returncode}",
        )
    )
    checks.append(
        Check(
            "python_version",
            sys.version_info >= (3, 11),
            ".".join(str(part) for part in sys.version_info[:3]),
        )
    )
    checks.append(_contracts_check())
    checks.extend(_sqlite_check(config["VIDEO_FACTORY_DB"]))
    checks.append(_filesystem_check(config["VIDEO_FACTORY_DB"]))
    checks.append(
        _writable_directory("runtime_root", config["VIDEO_FACTORY_RUNTIME_ROOT"])
    )
    checks.append(
        _writable_directory("agent_output_root", config["VIDEO_FACTORY_AGENT_OUTPUT_ROOT"])
    )
    workspace = Path(config["VIDEO_FACTORY_WORKSPACE"])
    checks.append(Check("workspace", workspace.is_dir(), str(workspace)))
    prompt_root = workspace / "factory" / "design" / "prompts"
    checks.append(Check("editorial_prompts", prompt_root.is_dir(), str(prompt_root)))
    codex_workspace = Path(config["VIDEO_FACTORY_CODEX_WORKSPACE"])
    checks.append(
        Check(
            "codex_workspace",
            codex_workspace.is_dir(),
            str(codex_workspace),
        )
    )
    if require_gpu:
        checks.append(_command_check("nvidia_smi", ["nvidia-smi", "-L"]))
        checks.append(
            _command_check(
                "nvenc",
                [config["VIDEO_FACTORY_FFMPEG"], "-hide_banner", "-encoders"],
                contains="h264_nvenc",
            )
        )
    failures = [check for check in checks if not check.ok]
    return {
        "ok": not failures,
        "expected": {
            "codex_cli": "0.151.0",
            "codex_model": "gpt-5.4",
            "hyperframes": "0.8.17",
            "node_major": 22,
        },
        "checks": [check.__dict__ for check in checks],
        "failure_count": len(failures),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-env", default="/etc/video-factory/runtime.env")
    parser.add_argument("--role", help="Validate one autonomous worker role")
    parser.add_argument(
        "--role-only", action="store_true", help="Validate only --role for systemd ExecCondition"
    )
    parser.add_argument("--require-gpu", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.role:
            validate_role(args.role)
        elif args.role_only:
            raise PreflightError("--role-only requires --role")
        if args.role_only:
            result: dict[str, object] = {"ok": True, "role": args.role}
        else:
            config = load_env_file(args.runtime_env)
            result = run_preflight(config, require_gpu=args.require_gpu)
    except (OSError, PreflightError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
