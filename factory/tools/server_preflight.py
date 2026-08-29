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
import hashlib
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
        "script",
        "editor",
    }
)
ALLOWED_TRUSTED_RUNTIME_ROLES = frozenset(
    {
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
)
ALLOWED_PROVIDER_ROLES = frozenset({"media_discovery"})
FORBIDDEN_RUNTIME_KEYS = frozenset(
    {"OPENAI_API_KEY", "CODEX_API_KEY", "FISH_API_KEY", "PEXELS_API_KEY"}
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


def validate_trusted_runtime_role(role: str) -> None:
    """Allow only deterministic local handlers behind the runtime dispatcher."""

    if role not in ALLOWED_TRUSTED_RUNTIME_ROLES:
        allowed = ", ".join(sorted(ALLOWED_TRUSTED_RUNTIME_ROLES))
        raise PreflightError(
            f"role {role!r} is not a trusted runtime role; allowed: {allowed}"
        )


def validate_provider_role(role: str) -> None:
    if role not in ALLOWED_PROVIDER_ROLES:
        allowed = ", ".join(sorted(ALLOWED_PROVIDER_ROLES))
        raise PreflightError(
            f"role {role!r} is not a managed provider role; allowed: {allowed}"
        )


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
        "VIDEO_FACTORY_CODEX_TIMEOUT",
        "VIDEO_FACTORY_WORKER_LEASE_SECONDS",
        "VIDEO_FACTORY_WORKER_HEARTBEAT_SECONDS",
        "VIDEO_FACTORY_WORKER_POLL_SECONDS",
        "VIDEO_FACTORY_FFMPEG",
        "VIDEO_FACTORY_FFPROBE",
        "VIDEO_FACTORY_NODE",
        "HYPERFRAMES_VERSION",
        "HYPERFRAMES_BIN",
        "VIDEO_FACTORY_RUNTIME_HANDLER_TIMEOUT",
        "VIDEO_FACTORY_RUNTIME_WORKER_LEASE_SECONDS",
        "VIDEO_FACTORY_RUNTIME_WORKER_HEARTBEAT_SECONDS",
        "VIDEO_FACTORY_MEDIA_INPUT_ROOT",
        "VIDEO_FACTORY_MEDIA_OUTPUT_ROOT",
        "VIDEO_FACTORY_MEDIA_ALLOW_RIGHTS_DOWNLOADS",
        "VIDEO_FACTORY_SOURCE_AUDIO_OUTPUT_ROOT",
        "VIDEO_FACTORY_HYPERFRAMES_PROJECT_ROOT",
        "VIDEO_FACTORY_GSAP_PATH",
        "VIDEO_FACTORY_RENDER_OUTPUT_ROOT",
        "VIDEO_FACTORY_QC_EVIDENCE_ROOT",
        "VIDEO_FACTORY_QC_CACHE_ROOT",
        "VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE",
        "VIDEO_FACTORY_CAPTION_OBSERVER_TIMEOUT_SECONDS",
        "VIDEO_FACTORY_CAPTION_MODEL_PATH",
        "VIDEO_FACTORY_CAPTION_MODEL_SHA256",
        "VIDEO_FACTORY_CAPTION_DEVICE",
        "VIDEO_FACTORY_CAPTION_DEVICE_INDEX",
        "VIDEO_FACTORY_CAPTION_COMPUTE_TYPE",
        "VIDEO_FACTORY_CAPTION_CPU_THREADS",
        "VIDEO_FACTORY_CAPTION_BEAM_SIZE",
        "VIDEO_FACTORY_CAPTION_LANGUAGE_PROBABILITY_MIN",
        "VIDEO_FACTORY_DEDUP_CORPUS_SNAPSHOT",
        "VIDEO_FACTORY_FACE_OBSERVER",
        "VIDEO_FACTORY_FACE_ENGINE",
        "VIDEO_FACTORY_FACE_MODEL_PATH",
        "VIDEO_FACTORY_FACE_MODEL_SHA256",
        "VIDEO_FACTORY_REVIEW_OUTBOX_ROOT",
        "VIDEO_FACTORY_REVIEW_RECONCILE_LIMIT",
        "VIDEO_FACTORY_PEXELS_CACHE_ROOT",
        "VIDEO_FACTORY_PROVIDER_HANDLER_TIMEOUT",
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
    if not re.fullmatch(r"[a-f0-9]{64}", config["VIDEO_FACTORY_CAPTION_MODEL_SHA256"]):
        raise PreflightError(
            "VIDEO_FACTORY_CAPTION_MODEL_SHA256 must be lowercase SHA-256"
        )
    if config["VIDEO_FACTORY_CAPTION_DEVICE"] not in {"cuda", "cpu"}:
        raise PreflightError("VIDEO_FACTORY_CAPTION_DEVICE must be cuda or cpu")
    if config["VIDEO_FACTORY_CAPTION_COMPUTE_TYPE"] not in {
        "default",
        "float16",
        "float32",
        "bfloat16",
        "int8",
        "int8_float16",
        "int8_float32",
        "int16",
    }:
        raise PreflightError("VIDEO_FACTORY_CAPTION_COMPUTE_TYPE is unsupported")
    for key, minimum, maximum in (
        ("VIDEO_FACTORY_CAPTION_DEVICE_INDEX", 0, 15),
        ("VIDEO_FACTORY_CAPTION_CPU_THREADS", 0, 128),
        ("VIDEO_FACTORY_CAPTION_BEAM_SIZE", 1, 10),
    ):
        try:
            value = int(config[key])
        except ValueError as exc:
            raise PreflightError(f"{key} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise PreflightError(f"{key} must be within {minimum}..{maximum}")
    try:
        language_probability_min = float(
            config["VIDEO_FACTORY_CAPTION_LANGUAGE_PROBABILITY_MIN"]
        )
    except ValueError as exc:
        raise PreflightError(
            "VIDEO_FACTORY_CAPTION_LANGUAGE_PROBABILITY_MIN must be numeric"
        ) from exc
    if not 0.5 <= language_probability_min <= 1:
        raise PreflightError(
            "VIDEO_FACTORY_CAPTION_LANGUAGE_PROBABILITY_MIN must be within 0.5..1"
        )
    if config["VIDEO_FACTORY_FACE_ENGINE"] != "yunet":
        raise PreflightError("production VIDEO_FACTORY_FACE_ENGINE must be yunet")
    if not re.fullmatch(r"[a-f0-9]{64}", config["VIDEO_FACTORY_FACE_MODEL_SHA256"]):
        raise PreflightError(
            "VIDEO_FACTORY_FACE_MODEL_SHA256 must be lowercase SHA-256"
        )
    if config["VIDEO_FACTORY_MEDIA_ALLOW_RIGHTS_DOWNLOADS"].strip().lower() not in {
        "true",
        "false",
    }:
        raise PreflightError(
            "VIDEO_FACTORY_MEDIA_ALLOW_RIGHTS_DOWNLOADS must be true or false"
        )
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
        "VIDEO_FACTORY_MEDIA_INPUT_ROOT",
        "VIDEO_FACTORY_MEDIA_OUTPUT_ROOT",
        "VIDEO_FACTORY_SOURCE_AUDIO_OUTPUT_ROOT",
        "VIDEO_FACTORY_HYPERFRAMES_PROJECT_ROOT",
        "VIDEO_FACTORY_GSAP_PATH",
        "VIDEO_FACTORY_RENDER_OUTPUT_ROOT",
        "VIDEO_FACTORY_QC_EVIDENCE_ROOT",
        "VIDEO_FACTORY_QC_CACHE_ROOT",
        "VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE",
        "VIDEO_FACTORY_CAPTION_MODEL_PATH",
        "VIDEO_FACTORY_DEDUP_CORPUS_SNAPSHOT",
        "VIDEO_FACTORY_FACE_OBSERVER",
        "VIDEO_FACTORY_FACE_MODEL_PATH",
        "VIDEO_FACTORY_REVIEW_OUTBOX_ROOT",
        "VIDEO_FACTORY_PEXELS_CACHE_ROOT",
    ):
        if not Path(config[key]).is_absolute():
            raise PreflightError(f"{key} must be an absolute path")

    for key in (
        "VIDEO_FACTORY_RUNTIME_HANDLER_TIMEOUT",
        "VIDEO_FACTORY_RUNTIME_WORKER_LEASE_SECONDS",
        "VIDEO_FACTORY_RUNTIME_WORKER_HEARTBEAT_SECONDS",
        "VIDEO_FACTORY_PROVIDER_HANDLER_TIMEOUT",
        "VIDEO_FACTORY_CODEX_TIMEOUT",
        "VIDEO_FACTORY_WORKER_LEASE_SECONDS",
        "VIDEO_FACTORY_WORKER_HEARTBEAT_SECONDS",
        "VIDEO_FACTORY_WORKER_POLL_SECONDS",
        "VIDEO_FACTORY_CAPTION_OBSERVER_TIMEOUT_SECONDS",
    ):
        try:
            value = float(config[key])
        except ValueError as exc:
            raise PreflightError(f"{key} must be numeric") from exc
        if value <= 0:
            raise PreflightError(f"{key} must be positive")

    try:
        reconcile_limit = int(config["VIDEO_FACTORY_REVIEW_RECONCILE_LIMIT"])
    except ValueError as exc:
        raise PreflightError(
            "VIDEO_FACTORY_REVIEW_RECONCILE_LIMIT must be an integer"
        ) from exc
    if not 1 <= reconcile_limit <= 1000:
        raise PreflightError(
            "VIDEO_FACTORY_REVIEW_RECONCILE_LIMIT must be from 1 to 1000"
        )

    runtime_timeout = float(config["VIDEO_FACTORY_RUNTIME_HANDLER_TIMEOUT"])
    runtime_lease = float(config["VIDEO_FACTORY_RUNTIME_WORKER_LEASE_SECONDS"])
    runtime_heartbeat = float(
        config["VIDEO_FACTORY_RUNTIME_WORKER_HEARTBEAT_SECONDS"]
    )
    if runtime_lease < runtime_timeout + runtime_heartbeat:
        raise PreflightError(
            "runtime worker lease must cover handler timeout plus one heartbeat interval"
        )
    worker_lease = float(config["VIDEO_FACTORY_WORKER_LEASE_SECONDS"])
    worker_heartbeat = float(config["VIDEO_FACTORY_WORKER_HEARTBEAT_SECONDS"])
    if worker_heartbeat >= worker_lease:
        raise PreflightError("worker heartbeat must be shorter than its lease")
    if float(config["VIDEO_FACTORY_PROVIDER_HANDLER_TIMEOUT"]) >= worker_lease:
        raise PreflightError("provider handler timeout must be shorter than worker lease")


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


def _readable_directory(name: str, path: str | Path) -> Check:
    candidate = Path(path)
    return Check(
        name,
        candidate.is_dir() and os.access(candidate, os.R_OK | os.X_OK),
        str(candidate),
    )


def _readable_file(name: str, path: str | Path) -> Check:
    candidate = Path(path)
    return Check(
        name,
        candidate.is_file() and os.access(candidate, os.R_OK),
        str(candidate),
    )


def _sha256_file_check(name: str, path: str | Path, expected: str) -> Check:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file() or not os.access(candidate, os.R_OK):
        return Check(name, False, f"missing/unreadable regular file: {candidate}")
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        return Check(name, False, f"cannot hash {candidate}: {exc}"[:400])
    actual = digest.hexdigest()
    return Check(
        name,
        actual == expected,
        f"sha256={actual} path={candidate}",
    )


def _caption_model_tree_check(path: str | Path, expected: str) -> Check:
    from video_factory.caption_observer import CaptionObserverError
    from video_factory.caption_observer import model_tree_fingerprint

    candidate = Path(path)
    try:
        actual = model_tree_fingerprint(candidate)
    except (CaptionObserverError, OSError, ValueError) as exc:
        return Check("caption_model", False, f"invalid pinned model: {exc}"[:400])
    return Check(
        "caption_model",
        actual == expected,
        f"tree_sha256={actual} path={candidate}",
    )


def _artifact_file_check(name: str, contract: str, path: str | Path) -> Check:
    from video_factory.contracts import validate_artifact
    from video_factory.errors import FactoryError

    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink() or not os.access(candidate, os.R_OK):
        return Check(name, False, f"missing/unreadable regular file: {candidate}")
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
        validate_artifact(contract, value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, FactoryError) as exc:
        return Check(name, False, f"invalid {contract}: {exc}"[:400])
    return Check(name, True, f"{contract}: {candidate}")


def _executable_file(name: str, path: str | Path) -> Check:
    candidate = Path(path)
    return Check(
        name,
        candidate.is_file()
        and not candidate.is_symlink()
        and os.access(candidate, os.R_OK | os.X_OK),
        str(candidate),
    )


def _systemd_layout_check(config: Mapping[str, str]) -> Check:
    """Keep configured paths inside the exact scopes granted by systemd units."""

    runtime_root = Path(config["VIDEO_FACTORY_RUNTIME_ROOT"]).resolve()
    expected_runtime_root = Path("/var/lib/video-factory").resolve()
    expected = {
        "VIDEO_FACTORY_DB": runtime_root / "queue" / "factory.sqlite3",
        "VIDEO_FACTORY_REVIEW_OUTBOX_ROOT": runtime_root / "review_outbox",
        "VIDEO_FACTORY_PEXELS_CACHE_ROOT": runtime_root / "discovery" / "pexels",
    }
    mismatches = []
    if runtime_root != expected_runtime_root:
        mismatches.append(
            f"VIDEO_FACTORY_RUNTIME_ROOT={runtime_root} (expected {expected_runtime_root})"
        )
    for key, required_path in expected.items():
        actual = Path(config[key]).resolve()
        if actual != required_path:
            mismatches.append(f"{key}={actual} (expected {required_path})")
    return Check(
        "systemd_runtime_layout",
        not mismatches,
        "; ".join(mismatches) if mismatches else str(runtime_root),
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
    checks.append(_systemd_layout_check(config))
    checks.append(
        _writable_directory("runtime_root", config["VIDEO_FACTORY_RUNTIME_ROOT"])
    )
    checks.append(
        _writable_directory("agent_output_root", config["VIDEO_FACTORY_AGENT_OUTPUT_ROOT"])
    )
    checks.append(
        _readable_directory("media_input_root", config["VIDEO_FACTORY_MEDIA_INPUT_ROOT"])
    )
    for name, key in (
        ("media_output_root", "VIDEO_FACTORY_MEDIA_OUTPUT_ROOT"),
        ("source_audio_output_root", "VIDEO_FACTORY_SOURCE_AUDIO_OUTPUT_ROOT"),
        ("bgm_output_root", "VIDEO_FACTORY_BGM_OUTPUT_ROOT"),
        ("program_audio_output_root", "VIDEO_FACTORY_PROGRAM_AUDIO_OUTPUT_ROOT"),
        ("hyperframes_project_root", "VIDEO_FACTORY_HYPERFRAMES_PROJECT_ROOT"),
        ("render_output_root", "VIDEO_FACTORY_RENDER_OUTPUT_ROOT"),
        ("qc_cache_root", "VIDEO_FACTORY_QC_CACHE_ROOT"),
        ("review_outbox_root", "VIDEO_FACTORY_REVIEW_OUTBOX_ROOT"),
        ("pexels_cache_root", "VIDEO_FACTORY_PEXELS_CACHE_ROOT"),
    ):
        checks.append(_writable_directory(name, config[key]))
    checks.append(
        _readable_directory("qc_evidence_root", config["VIDEO_FACTORY_QC_EVIDENCE_ROOT"])
    )
    checks.append(
        _readable_directory(
            "rights_evidence_root", config["VIDEO_FACTORY_RIGHTS_EVIDENCE_ROOT"]
        )
    )
    checks.append(_readable_file("gsap_bundle", config["VIDEO_FACTORY_GSAP_PATH"]))
    checks.append(
        _executable_file(
            "caption_observer", config["VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE"]
        )
    )
    checks.append(
        _caption_model_tree_check(
            config["VIDEO_FACTORY_CAPTION_MODEL_PATH"],
            config["VIDEO_FACTORY_CAPTION_MODEL_SHA256"],
        )
    )
    checks.append(
        _artifact_file_check(
            "dedup_corpus_snapshot",
            "dedup_corpus_snapshot",
            config["VIDEO_FACTORY_DEDUP_CORPUS_SNAPSHOT"],
        )
    )
    checks.append(
        _executable_file("face_observer", config["VIDEO_FACTORY_FACE_OBSERVER"])
    )
    checks.append(
        _sha256_file_check(
            "face_model",
            config["VIDEO_FACTORY_FACE_MODEL_PATH"],
            config["VIDEO_FACTORY_FACE_MODEL_SHA256"],
        )
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
    parser.add_argument(
        "--trusted-runtime-role",
        help="Validate one deterministic runtime worker role",
    )
    parser.add_argument(
        "--trusted-runtime-role-only",
        action="store_true",
        help="Validate only --trusted-runtime-role for systemd ExecCondition",
    )
    parser.add_argument("--provider-role", help="Validate one managed provider role")
    parser.add_argument(
        "--provider-role-only",
        action="store_true",
        help="Validate only --provider-role for systemd ExecCondition",
    )
    parser.add_argument("--require-gpu", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        selected_role_modes = sum(
            value is not None
            for value in (args.role, args.trusted_runtime_role, args.provider_role)
        )
        if selected_role_modes > 1:
            raise PreflightError(
                "--role, --trusted-runtime-role and --provider-role are mutually exclusive"
            )
        if args.role:
            validate_role(args.role)
        elif args.role_only:
            raise PreflightError("--role-only requires --role")
        if args.trusted_runtime_role:
            validate_trusted_runtime_role(args.trusted_runtime_role)
        elif args.trusted_runtime_role_only:
            raise PreflightError(
                "--trusted-runtime-role-only requires --trusted-runtime-role"
            )
        if args.provider_role:
            validate_provider_role(args.provider_role)
        elif args.provider_role_only:
            raise PreflightError("--provider-role-only requires --provider-role")
        if args.role_only:
            result: dict[str, object] = {"ok": True, "role": args.role}
        elif args.trusted_runtime_role_only:
            result = {"ok": True, "trusted_runtime_role": args.trusted_runtime_role}
        elif args.provider_role_only:
            result = {"ok": True, "provider_role": args.provider_role}
        else:
            config = load_env_file(args.runtime_env)
            result = run_preflight(config, require_gpu=args.require_gpu)
    except (OSError, PreflightError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
