from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import validate_artifact, validate_production_chain
from .db import Database
from .errors import (
    IdempotencyConflictError,
    LeaseConflictError,
    NotFoundError,
    ValidationError,
)
from .validators import canonical_json, digest_text, require_nonempty_string


TASK_STATES = frozenset({"queued", "leased", "succeeded", "dead"})
ATTEMPT_STATES = frozenset({"leased", "succeeded", "failed", "expired"})

_SECRET_RESULT_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "lease_token",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "session_token",
        "token",
    }
)
_SECRET_RESULT_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/=-]{8,}|sk-(?:fish-)?[A-Za-z0-9_-]{12,})",
    re.IGNORECASE,
)
_MAX_UPSTREAM_TASKS = 128
_MAX_UPSTREAM_RESULT_BYTES = 4 * 1024 * 1024
_MAX_UPSTREAM_CONTEXT_BYTES = 16 * 1024 * 1024
_MAX_UPSTREAM_JSON_DEPTH = 32


def _ffprobe_path() -> str:
    configured = os.environ.get("VIDEO_FACTORY_FFPROBE")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(shutil.which("ffprobe")) if shutil.which("ffprobe") else None,
        Path.home() / "bin" / ("ffprobe.exe" if os.name == "nt" else "ffprobe"),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate.resolve())
    raise ValidationError(
        "ffprobe is required for render completion; configure VIDEO_FACTORY_FFPROBE"
    )


def _probe_render_output(path: Path) -> dict[str, Any]:
    command = [
        _ffprobe_path(),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,avg_frame_rate,sample_rate:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError(f"ffprobe could not inspect render output: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().replace("\r", " ").replace("\n", " ")[:300]
        raise ValidationError(f"render output is not ffprobe-decodable: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError("ffprobe returned invalid JSON") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise ValidationError("ffprobe output has no streams array")
    video = next(
        (item for item in streams if item.get("codec_type") == "video"), None
    )
    audio = next(
        (item for item in streams if item.get("codec_type") == "audio"), None
    )
    if not isinstance(video, dict) or not isinstance(audio, dict):
        raise ValidationError("render output must contain video and audio streams")
    try:
        frame_rate = float(Fraction(str(video["avg_frame_rate"])))
        duration = float(payload["format"]["duration"])
        result = {
            "width": int(video["width"]),
            "height": int(video["height"]),
            "fps": frame_rate,
            "duration_seconds": duration,
            "video_codec": str(video["codec_name"]).lower(),
            "audio_codec": str(audio["codec_name"]).lower(),
            "audio_sample_rate_hz": int(audio["sample_rate"]),
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValidationError("ffprobe output is missing required technical values") from exc
    return result


def _validate_render_probe(
    manifest_technical: Mapping[str, Any], actual: Mapping[str, Any]
) -> None:
    mismatches: list[str] = []
    for key in ("width", "height", "audio_sample_rate_hz"):
        if actual[key] != manifest_technical[key]:
            mismatches.append(key)
    if abs(float(actual["fps"]) - float(manifest_technical["fps"])) > 0.01:
        mismatches.append("fps")
    if (
        abs(
            float(actual["duration_seconds"])
            - float(manifest_technical["duration_seconds"])
        )
        > (1 / 30)
    ):
        mismatches.append("duration_seconds")
    for key in ("video_codec", "audio_codec"):
        if str(actual[key]).lower() not in str(manifest_technical[key]).lower():
            mismatches.append(key)
    if mismatches:
        raise ValidationError(
            "render_manifest technical values do not match ffprobe: "
            + ", ".join(mismatches)
        )


def _timestamp(value: datetime | str | None = None) -> str:
    if value is None:
        parsed = datetime.now(UTC)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("timestamp must be ISO-8601") from exc
    else:
        raise ValidationError("timestamp must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None:
        raise ValidationError("timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _plus_seconds(timestamp: str, seconds: int) -> str:
    value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return _timestamp(value + timedelta(seconds=seconds))


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _safe_upstream_value(
    value: Any, path: str = "result", *, depth: int = 0
) -> Any:
    """Copy JSON while failing closed on credential-shaped fields or values."""

    if depth > _MAX_UPSTREAM_JSON_DEPTH:
        raise ValidationError(
            f"{path} exceeds the execution-context nesting limit"
        )
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"{path} contains a non-string key")
            normalized = key.casefold().replace("-", "_")
            if normalized in _SECRET_RESULT_KEYS or normalized.endswith(
                (
                    "_api_key",
                    "_authorization",
                    "_credential",
                    "_lease_token",
                    "_password",
                    "_refresh_token",
                    "_secret",
                    "_token",
                )
            ):
                raise ValidationError(
                    f"{path}.{key} looks like a secret and cannot enter execution context"
                )
            safe[key] = _safe_upstream_value(
                child, f"{path}.{key}", depth=depth + 1
            )
        return safe
    if isinstance(value, list):
        return [
            _safe_upstream_value(child, f"{path}[{index}]", depth=depth + 1)
            for index, child in enumerate(value)
        ]
    if isinstance(value, str) and _SECRET_RESULT_VALUE.search(value):
        raise ValidationError(
            f"{path} contains a credential-shaped value and cannot enter execution context"
        )
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValidationError(f"{path} contains a non-JSON value")


def _ancestor_results(
    connection: sqlite3.Connection, task: sqlite3.Row
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    artifacts: dict[str, dict[str, Any]] = {}
    results_by_role: dict[str, dict[str, Any]] = {}
    task_id = task["dependency_task_id"]
    seen: set[str] = set()
    while task_id is not None:
        if task_id in seen:
            raise ValidationError("task dependency cycle detected")
        seen.add(task_id)
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValidationError(f"dependency task {task_id!r} does not exist")
        if row["status"] != "succeeded" or row["result_json"] is None:
            raise ValidationError(f"dependency task {task_id!r} has not succeeded")
        try:
            result = json.loads(row["result_json"])
            ancestor_payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"dependency task {task_id!r} has invalid JSON") from exc
        if not isinstance(result, dict) or not isinstance(ancestor_payload, dict):
            raise ValidationError(f"dependency task {task_id!r} result is invalid")
        results_by_role.setdefault(row["role"], result)
        contract = ancestor_payload.get("required_result_contract")
        artifact = result.get("artifact")
        if isinstance(contract, str) and isinstance(artifact, dict):
            artifacts.setdefault(contract, artifact)
        task_id = row["dependency_task_id"]
    return artifacts, results_by_role


def _validate_success_result(
    connection: sqlite3.Connection, task: sqlite3.Row, body: dict[str, Any]
) -> None:
    """Fail closed when a production task declares an output contract or gate."""

    try:
        payload = json.loads(task["payload_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("task payload_json is invalid") from exc
    if not isinstance(payload, dict):
        raise ValidationError("task payload must be an object")

    contract = payload.get("required_result_contract")
    ancestors: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]] | None = None

    def history() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        nonlocal ancestors
        if ancestors is None:
            ancestors = _ancestor_results(connection, task)
        return ancestors

    if contract is not None:
        if not isinstance(contract, str) or not contract:
            raise ValidationError("required_result_contract must be a non-empty string")
        artifact = body.get("artifact")
        if not isinstance(artifact, dict):
            raise ValidationError(
                f"successful {task['role']} task requires result.artifact for {contract}"
            )
        validate_artifact(contract, artifact)
        expected_idea = payload.get("idea_id")
        if expected_idea is not None and artifact.get("idea_id") not in {None, expected_idea}:
            raise ValidationError("result artifact idea_id does not match task payload")
        expected_job = payload.get("job_id")
        if expected_job is not None and artifact.get("job_id") not in {None, expected_job}:
            raise ValidationError("result artifact job_id does not match task payload")

        decision = artifact.get("decision")
        if contract in {"claim_ledger", "rights_manifest", "safety_gate_report"}:
            if (
                not isinstance(decision, dict)
                or decision.get("passed") is not True
                or decision.get("needs_human_review") is not False
            ):
                raise ValidationError(f"{contract} has not passed its hard gate")
        if contract == "rights_manifest" and decision.get("missing_asset_ids"):
            raise ValidationError("rights_manifest still has missing assets")
        if contract == "source_audio_manifest":
            if task["role"] != "source_audio":
                raise ValidationError(
                    "source_audio_manifest may only complete a source_audio task"
                )
            if payload.get("lane_id") != "motivation":
                raise ValidationError(
                    "source_audio_manifest is restricted to the motivation lane"
                )
        if contract == "voice_manifest":
            if not isinstance(expected_job, str) or not expected_job:
                raise ValidationError("voice task requires payload.job_id")
            if artifact["job_id"] != expected_job or artifact["video_id"] != expected_job:
                raise ValidationError("voice_manifest is not bound to the task job_id")
            if artifact["voice_rights_status"] not in {
                "approved_owned_voice",
                "approved_licensed_voice",
            }:
                raise ValidationError("voice_manifest publication rights are not approved")
            if artifact["generation_no"] == 2 and (
                artifact["retry_reason"] is None
                or artifact["defect_reference"] is None
            ):
                raise ValidationError(
                    "voice_manifest generation 2 has no verified retry evidence"
                )
            if artifact["generation_no"] == 2:
                defect_path = Path(artifact["defect_reference"]).expanduser().resolve()
                try:
                    defect_bytes = defect_path.read_bytes()
                    defect = json.loads(defect_bytes.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValidationError(
                        "voice_manifest retry defect artifact is unreadable"
                    ) from exc
                validate_artifact("voice_defect", defect)
                if hashlib.sha256(defect_bytes).hexdigest() != artifact["defect_sha256"]:
                    raise ValidationError("voice_manifest retry defect hash does not match")
                retry_bindings = {
                    "job_id": expected_job,
                    "video_id": expected_job,
                    "retry_reason": artifact["retry_reason"],
                    "request_hash": artifact["retry_of_request_hash"],
                    "output_sha256": artifact["retry_of_output_sha256"],
                    "generation_status": artifact["retry_of_generation_status"],
                }
                if any(defect.get(key) != value for key, value in retry_bindings.items()):
                    raise ValidationError(
                        "voice_manifest retry defect is not bound to generation 1"
                    )
            approval = body.get("voice_rights_approval")
            if not isinstance(approval, dict):
                raise ValidationError(
                    "approved voice_manifest requires result.voice_rights_approval"
                )
            validate_artifact("voice_rights_approval", approval)
            if approval["job_id"] != expected_job:
                raise ValidationError("voice rights approval is not bound to task job_id")
            if artifact["reference_id"] is None or (
                approval["reference_id"] != artifact["reference_id"]
            ):
                raise ValidationError("voice rights approval reference_id does not match")
            if approval["voice_rights_status"] != artifact["voice_rights_status"]:
                raise ValidationError("voice rights approval status does not match manifest")
            expected_basis = (
                "voice_owner_confirmation"
                if artifact["voice_rights_status"] == "approved_owned_voice"
                else "commercial_license"
            )
            if approval["basis"] != expected_basis:
                raise ValidationError("voice rights approval basis does not match status")
        if contract == "safety_gate_report" and any(
            item.get("severity") == "blocking" for item in artifact["findings"]
        ):
            raise ValidationError("safety_gate_report contains blocking findings")
        if contract == "safety_gate_report":
            expected_lane = payload.get("lane_id")
            if artifact["lane"] != expected_lane:
                raise ValidationError("safety_gate_report lane does not match task lane_id")
            expected_gate_by_role = {
                "sensitivity_review": "war_sensitivity",
                "privacy_review": "privacy_defamation",
                "medical_review": "medical_safety",
            }
            expected_gate = expected_gate_by_role.get(task["role"])
            if expected_gate is None or artifact["gate_type"] != expected_gate:
                raise ValidationError(
                    f"safety_gate_report gate_type does not match {task['role']}"
                )
            risk_profile = payload.get("risk_profile")
            if risk_profile is not None and artifact["gate_type"] != risk_profile:
                raise ValidationError(
                    "safety_gate_report gate_type does not match task risk_profile"
                )
            prior, _ = history()
            claim_ledger = prior.get("claim_ledger")
            if claim_ledger is None:
                raise ValidationError(
                    "safety_gate_report requires an upstream claim_ledger"
                )
            known_source_ids = {
                item["source_id"] for item in claim_ledger.get("sources", [])
            }
            unknown_sources = sorted(
                set(artifact["source_ids_checked"]) - known_source_ids
            )
            if unknown_sources:
                raise ValidationError(
                    "safety_gate_report references unknown claim-ledger sources: "
                    + ", ".join(unknown_sources)
                )
        if contract == "script_package":
            if artifact["lane_id"] != payload.get("lane_id"):
                raise ValidationError("script_package lane_id does not match task lane_id")
            prior, _ = history()
            claim_ledger = prior.get("claim_ledger")
            rights_manifest = prior.get("rights_manifest")
            if claim_ledger is None or rights_manifest is None:
                raise ValidationError(
                    "script_package requires upstream claim_ledger and rights_manifest"
                )
            known_claim_ids = {
                item["claim_id"] for item in claim_ledger.get("claims", [])
            }
            used_claim_ids = {
                claim_id
                for segment in artifact["segments"]
                for claim_id in segment["claim_ids"]
            }
            unknown_claim_ids = sorted(used_claim_ids - known_claim_ids)
            if unknown_claim_ids:
                raise ValidationError(
                    "script_package references unknown claim ids: "
                    + ", ".join(unknown_claim_ids)
                )
            if artifact["decision"]["passed"] is not True:
                raise ValidationError("script_package has not passed its editorial gate")
            if payload.get("lane_id") in {"health", "chinese_medicine"} and not artifact.get(
                "disclaimer"
            ):
                raise ValidationError("medical script_package requires a disclaimer")
        if contract == "qc_report" and (
            artifact["decision"]["passed"] is not True
            or artifact["decision"]["blocking_check_ids"]
        ):
            raise ValidationError("qc_report has not passed its hard gate")
        if contract == "render_manifest":
            output_path_value = body.get("output_path")
            if not isinstance(output_path_value, str) or not output_path_value.strip():
                raise ValidationError(
                    "render completion requires result.output_path for byte-level hashing"
                )
            output_path = Path(output_path_value).expanduser().resolve()
            if not output_path.is_file():
                raise ValidationError(f"render output file does not exist: {output_path}")
            if output_path.name != Path(artifact["output"]).name:
                raise ValidationError("render output_path name does not match manifest output")
            digest = hashlib.sha256()
            try:
                with output_path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError as exc:
                raise ValidationError(f"cannot hash render output: {exc}") from exc
            if digest.hexdigest() != artifact["output_sha256"]:
                raise ValidationError(
                    "render_manifest.output_sha256 does not match actual output bytes"
                )
            _validate_render_probe(
                artifact["technical"], _probe_render_output(output_path)
            )
        if contract == "shotlist":
            prior, _ = history()
            idea_card = payload.get("idea_card")
            if not isinstance(idea_card, dict):
                raise ValidationError(
                    "editor task requires a canonical idea_card for chain validation"
                )
            missing = [
                name
                for name in ("claim_ledger", "rights_manifest", "script_package")
                if name not in prior
            ]
            if missing:
                raise ValidationError(
                    f"editor task is missing upstream artifacts: {', '.join(missing)}"
                )
            chain = validate_production_chain(
                idea_card=idea_card,
                claim_ledger=prior["claim_ledger"],
                rights_manifest=prior["rights_manifest"],
                shotlist=artifact,
                safety_gate_report=prior.get("safety_gate_report"),
            )
            if not chain["production_ready"]:
                raise ValidationError(
                    "production chain integrity failed: " + "; ".join(chain["errors"])
                )
        if contract == "qc_report":
            prior, _ = history()
            render = prior.get("render_manifest")
            if render is None:
                raise ValidationError("qc_report requires an upstream render_manifest")
            if artifact["render_id"] != render["render_id"]:
                raise ValidationError("qc_report.render_id does not match rendered artifact")
            if (
                artifact["technical"]["audio_sample_rate_hz"]
                != render["technical"]["audio_sample_rate_hz"]
            ):
                raise ValidationError("qc_report audio sample rate does not match render")
        if contract == "publish_manifest":
            prior, role_results = history()
            render = prior.get("render_manifest")
            qc = prior.get("qc_report")
            source_audio = prior.get("source_audio_manifest")
            final_review = role_results.get("final_review")
            if render is None or qc is None or final_review is None:
                raise ValidationError(
                    "publish_manifest requires render, QC, and final human review"
                )
            if artifact["render_id"] != render["render_id"]:
                raise ValidationError("publish_manifest.render_id does not match render")
            if qc["render_id"] != render["render_id"]:
                raise ValidationError("upstream QC does not match render")
            if (
                source_audio is not None
                and source_audio["rights_status"] == "internal_prototype"
            ):
                raise ValidationError(
                    "internal_prototype source audio is not eligible for publication"
                )
            approval = artifact["human_approval"]
            reviewed = final_review.get("human_approval")
            if approval != reviewed:
                raise ValidationError(
                    "publish_manifest human approval differs from final_review"
                )
            if approval["render_sha256"] != render["output_sha256"]:
                raise ValidationError("human approval is not bound to render checksum")
            metadata_sha = digest_text(
                canonical_json(
                    {
                        "destinations": artifact["destinations"],
                        "disclosures": artifact["disclosures"],
                    }
                )
            )
            if approval["metadata_sha256"] != metadata_sha:
                raise ValidationError("human approval is not bound to publish metadata")

    if payload.get("human_gate") is True:
        approval = body.get("human_approval")
        if not isinstance(approval, dict) or approval.get("approved") is not True:
            raise ValidationError("human-gated task requires approved human_approval")
        require_nonempty_string(approval.get("approved_by"), "human_approval.approved_by")
        _timestamp(approval.get("approved_at"))
        if payload.get("checksum_bound") is True:
            prior, _ = history()
            render = prior.get("render_manifest")
            if render is None:
                raise ValidationError("human approval requires an upstream render_manifest")
            checksum = require_nonempty_string(
                approval.get("render_sha256"), "human_approval.render_sha256"
            )
            if checksum != render["output_sha256"]:
                raise ValidationError("human approval is not bound to render checksum")
            metadata_sha = require_nonempty_string(
                approval.get("metadata_sha256"), "human_approval.metadata_sha256"
            )
            if len(metadata_sha) != 64 or any(ch not in "0123456789abcdef" for ch in metadata_sha):
                raise ValidationError("human_approval.metadata_sha256 must be lowercase sha256")


def _json_object(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be a JSON object")
    return value


class Dispatcher:
    """Transactional SQLite task dispatcher with fenced leases and durable attempts.

    SQLite serializes dispatch decisions with ``BEGIN IMMEDIATE``. A completion or
    failure is accepted only with the current random lease token, so a worker whose
    lease expired cannot overwrite a newer attempt.
    """

    def __init__(self, db_path: str | Path):
        self.db = Database(db_path)

    def _connection(self) -> sqlite3.Connection:
        self.db.initialize()
        connection = self.db.connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    @staticmethod
    def _operation_replay(
        connection: sqlite3.Connection,
        *,
        key: str,
        command: str,
        request: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        key = require_nonempty_string(key, "idempotency_key")
        request_hash = digest_text(canonical_json(request))
        row = connection.execute(
            "SELECT command, request_hash, response_json FROM operations WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None, request_hash
        if row["command"] != command or row["request_hash"] != request_hash:
            raise IdempotencyConflictError(
                f"idempotency key {key!r} was already used for a different request"
            )
        return json.loads(row["response_json"]), request_hash

    @staticmethod
    def _store_operation(
        connection: sqlite3.Connection,
        *,
        key: str,
        command: str,
        request_hash: str,
        response: dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO operations(idempotency_key, command, request_hash, response_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, command, request_hash, canonical_json(response), now),
        )

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "dependency_task_id": row["dependency_task_id"],
            "role": row["role"],
            "pod": row["pod"],
            "kind": row["kind"],
            "payload": json.loads(row["payload_json"]),
            "priority": row["priority"],
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "max_attempts": row["max_attempts"],
            "attempt_count": row["attempt_count"],
            "retry_backoff_seconds": row["retry_backoff_seconds"],
            "available_at": row["available_at"],
            "lease_owner": row["lease_owner"],
            "lease_token": row["lease_token"],
            "lease_expires_at": row["lease_expires_at"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "last_error": json.loads(row["last_error_json"])
            if row["last_error_json"]
            else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "attempt_no": row["attempt_no"],
            "worker_id": row["worker_id"],
            "lease_token": row["lease_token"],
            "status": row["status"],
            "claimed_at": row["claimed_at"],
            "lease_expires_at": row["lease_expires_at"],
            "finished_at": row["finished_at"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
        }

    @staticmethod
    def _dead_letter_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "cycle_no": row["cycle_no"],
            "status": row["status"],
            "cause_code": row["cause_code"],
            "error": json.loads(row["error_json"]),
            "task_snapshot": json.loads(row["task_snapshot_json"]),
            "resolution": json.loads(row["resolution_json"])
            if row["resolution_json"]
            else None,
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
        }

    @classmethod
    def _record_dead_letter(
        cls,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        error: Mapping[str, Any],
        now_text: str,
    ) -> sqlite3.Row:
        """Append exactly one DLQ record for a task's current dead cycle."""

        task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise NotFoundError(f"task {task_id!r} does not exist")
        if task["status"] != "dead":
            raise ValidationError("a dead letter may only be recorded for a dead task")
        open_record = connection.execute(
            "SELECT * FROM dead_letters WHERE task_id = ? AND status = 'open' "
            "ORDER BY cycle_no DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if open_record is not None:
            return open_record
        cycle_no = connection.execute(
            "SELECT COALESCE(MAX(cycle_no), 0) + 1 AS next_cycle "
            "FROM dead_letters WHERE task_id = ?",
            (task_id,),
        ).fetchone()["next_cycle"]
        cause_code = error.get("code", "unknown_failure")
        if not isinstance(cause_code, str) or not cause_code.strip():
            cause_code = "unknown_failure"
        letter_id = f"dlq_{digest_text(f'{task_id}:{cycle_no}')[:24]}"
        connection.execute(
            """
            INSERT INTO dead_letters(
                id, task_id, cycle_no, status, cause_code, error_json,
                task_snapshot_json, created_at
            ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (
                letter_id,
                task_id,
                cycle_no,
                cause_code.strip(),
                canonical_json(dict(error)),
                canonical_json(cls._task_dict(task)),
                now_text,
            ),
        )
        return connection.execute(
            "SELECT * FROM dead_letters WHERE id = ?", (letter_id,)
        ).fetchone()

    @staticmethod
    def _resolve_open_dead_letter(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        resolution: Mapping[str, Any],
        now_text: str,
    ) -> str | None:
        row = connection.execute(
            "SELECT id FROM dead_letters WHERE task_id = ? AND status = 'open' "
            "ORDER BY cycle_no DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            """
            UPDATE dead_letters
            SET status = 'resolved', resolution_json = ?, resolved_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (canonical_json(dict(resolution)), now_text, row["id"]),
        )
        return row["id"]

    def enqueue(
        self,
        *,
        role: str,
        pod: str,
        kind: str,
        payload: dict[str, Any] | None,
        idempotency_key: str,
        job_id: str | None = None,
        dependency_task_id: str | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        retry_backoff_seconds: int = 60,
        available_at: datetime | str | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        role = require_nonempty_string(role, "role")
        pod = require_nonempty_string(pod, "pod")
        kind = require_nonempty_string(kind, "kind")
        key = require_nonempty_string(idempotency_key, "idempotency_key")
        payload = _json_object(payload, "payload")
        priority = _bounded_int(priority, "priority", -1000, 1000)
        max_attempts = _bounded_int(max_attempts, "max_attempts", 1, 100)
        retry_backoff_seconds = _bounded_int(
            retry_backoff_seconds, "retry_backoff_seconds", 0, 86400
        )
        now_text = _timestamp(now)
        requested_available_at = (
            _timestamp(available_at) if available_at is not None else None
        )
        available_text = requested_available_at or now_text
        request = {
            "role": role,
            "pod": pod,
            "kind": kind,
            "payload": payload,
            "job_id": job_id,
            "dependency_task_id": dependency_task_id,
            "priority": priority,
            "max_attempts": max_attempts,
            "retry_backoff_seconds": retry_backoff_seconds,
            # An omitted scheduling time is part of the request as ``null``. The
            # first execution freezes the actual timestamp in the stored response,
            # so replaying the same key tomorrow is still the same request.
            "available_at": requested_available_at,
        }

        connection = self._connection()
        try:
            replay, request_hash = self._operation_replay(
                connection, key=key, command="queue.enqueue", request=request
            )
            if replay is not None:
                connection.rollback()
                return replay
            if job_id is not None and connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone() is None:
                raise NotFoundError(f"job {job_id!r} does not exist")
            if dependency_task_id is not None and connection.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (dependency_task_id,)
            ).fetchone() is None:
                raise NotFoundError(f"dependency task {dependency_task_id!r} does not exist")
            task_id = f"task_{digest_text(key + ':' + request_hash)[:24]}"
            connection.execute(
                """
                INSERT INTO tasks(
                    id, job_id, dependency_task_id, role, pod, kind, payload_json,
                    priority, status, idempotency_key, max_attempts, attempt_count,
                    retry_backoff_seconds, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    job_id,
                    dependency_task_id,
                    role,
                    pod,
                    kind,
                    canonical_json(payload),
                    priority,
                    key,
                    max_attempts,
                    retry_backoff_seconds,
                    available_text,
                    now_text,
                    now_text,
                ),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            response = {"ok": True, "command": "enqueue", "created": True, "task": self._task_dict(row)}
            self._store_operation(
                connection,
                key=key,
                command="queue.enqueue",
                request_hash=request_hash,
                response=response,
                now=now_text,
            )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _recover_expired_in_tx(
        connection: sqlite3.Connection, now_text: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'leased' AND lease_expires_at <= ?
            ORDER BY lease_expires_at, id
            """,
            (now_text,),
        ).fetchall()
        recovered: list[dict[str, Any]] = []
        for row in rows:
            terminal = row["attempt_count"] >= row["max_attempts"]
            next_status = "dead" if terminal else "queued"
            error = {
                "code": "lease_expired",
                "message": "worker did not acknowledge the task before lease expiry",
                "attempt_no": row["attempt_count"],
            }
            connection.execute(
                """
                UPDATE task_attempts
                SET status = 'expired', finished_at = ?, error_json = ?
                WHERE task_id = ? AND lease_token = ? AND status = 'leased'
                """,
                (now_text, canonical_json(error), row["id"], row["lease_token"]),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, available_at = ?, lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_json = ?, updated_at = ?,
                    completed_at = ?
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (
                    next_status,
                    now_text,
                    canonical_json(error),
                    now_text,
                    now_text if terminal else None,
                    row["id"],
                    row["lease_token"],
                ),
            )
            if terminal:
                Dispatcher._record_dead_letter(
                    connection,
                    task_id=row["id"],
                    error=error,
                    now_text=now_text,
                )
            recovered.append(
                {"task_id": row["id"], "attempt_no": row["attempt_count"], "status": next_status}
            )
        return recovered

    @staticmethod
    def _propagate_dead_dependencies(connection: sqlite3.Connection, now_text: str) -> int:
        total = 0
        while True:
            rows = connection.execute(
                """
                SELECT child.id, child.dependency_task_id
                FROM tasks AS child
                JOIN tasks AS parent ON parent.id = child.dependency_task_id
                WHERE child.status = 'queued' AND parent.status = 'dead'
                ORDER BY child.id
                """
            ).fetchall()
            if not rows:
                return total
            for row in rows:
                error = {
                    "code": "dependency_dead",
                    "message": "dependency exhausted its attempts",
                    "dependency_task_id": row["dependency_task_id"],
                }
                connection.execute(
                    """
                    UPDATE tasks SET status = 'dead', last_error_json = ?,
                        updated_at = ?, completed_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (canonical_json(error), now_text, now_text, row["id"]),
                )
                Dispatcher._record_dead_letter(
                    connection,
                    task_id=row["id"],
                    error=error,
                    now_text=now_text,
                )
                total += 1

    @staticmethod
    def _wip_violation(
        connection: sqlite3.Connection, *, role: str, pod: str
    ) -> dict[str, Any] | None:
        limits = connection.execute(
            """
            SELECT role, pod, max_leased FROM queue_limits
            WHERE (role = ? AND pod IS NULL)
               OR (role IS NULL AND pod = ?)
               OR (role = ? AND pod = ?)
            ORDER BY CASE WHEN role IS NOT NULL AND pod IS NOT NULL THEN 0 ELSE 1 END, id
            """,
            (role, pod, role, pod),
        ).fetchall()
        for limit in limits:
            clauses = ["status = 'leased'"]
            params: list[Any] = []
            if limit["role"] is not None:
                clauses.append("role = ?")
                params.append(limit["role"])
            if limit["pod"] is not None:
                clauses.append("pod = ?")
                params.append(limit["pod"])
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE " + " AND ".join(clauses), params
            ).fetchone()["count"]
            if count >= limit["max_leased"]:
                return {
                    "role": limit["role"],
                    "pod": limit["pod"],
                    "max_leased": limit["max_leased"],
                    "current_leased": count,
                }
        return None

    def claim(
        self,
        *,
        worker_id: str,
        role: str,
        idempotency_key: str,
        pod: str | None = None,
        kind: str | None = None,
        lease_seconds: int = 900,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        worker_id = require_nonempty_string(worker_id, "worker_id")
        role = require_nonempty_string(role, "role")
        key = require_nonempty_string(idempotency_key, "idempotency_key")
        if pod is not None:
            pod = require_nonempty_string(pod, "pod")
        if kind is not None:
            kind = require_nonempty_string(kind, "kind")
        lease_seconds = _bounded_int(lease_seconds, "lease_seconds", 5, 86400)
        now_text = _timestamp(now)
        request = {
            "worker_id": worker_id,
            "role": role,
            "pod": pod,
            "kind": kind,
            "lease_seconds": lease_seconds,
        }
        connection = self._connection()
        try:
            replay, request_hash = self._operation_replay(
                connection, key=key, command="queue.claim", request=request
            )
            if replay is not None:
                connection.rollback()
                return replay
            recovered = self._recover_expired_in_tx(connection, now_text)
            dead_dependencies = self._propagate_dead_dependencies(connection, now_text)
            clauses = [
                "task.status = 'queued'",
                "task.role = ?",
                "task.available_at <= ?",
                "(task.dependency_task_id IS NULL OR dependency.status = 'succeeded')",
            ]
            params: list[Any] = [role, now_text]
            if pod is not None:
                clauses.append("task.pod = ?")
                params.append(pod)
            if kind is not None:
                clauses.append("task.kind = ?")
                params.append(kind)
            candidates = connection.execute(
                """
                SELECT task.* FROM tasks AS task
                LEFT JOIN tasks AS dependency ON dependency.id = task.dependency_task_id
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY task.priority DESC, task.available_at, task.created_at, task.id LIMIT 500",
                params,
            ).fetchall()
            selected = None
            blocked_by: list[dict[str, Any]] = []
            for candidate in candidates:
                violation = self._wip_violation(
                    connection, role=candidate["role"], pod=candidate["pod"]
                )
                if violation is None:
                    selected = candidate
                    break
                if violation not in blocked_by:
                    blocked_by.append(violation)
            task_payload = None
            if selected is not None:
                attempt_no = selected["attempt_count"] + 1
                # Prefix URL-safe randomness so the token can never begin with
                # '-' and be misparsed as a CLI option value by argparse.
                token = f"lt_{secrets.token_urlsafe(24)}"
                expires_at = _plus_seconds(now_text, lease_seconds)
                connection.execute(
                    """
                    UPDATE tasks SET status = 'leased', attempt_count = ?, lease_owner = ?,
                        lease_token = ?, lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (attempt_no, worker_id, token, expires_at, now_text, selected["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO task_attempts(
                        task_id, attempt_no, worker_id, lease_token, status,
                        claimed_at, lease_expires_at
                    ) VALUES (?, ?, ?, ?, 'leased', ?, ?)
                    """,
                    (selected["id"], attempt_no, worker_id, token, now_text, expires_at),
                )
                fresh = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (selected["id"],)
                ).fetchone()
                task_payload = self._task_dict(fresh)
            response = {
                "ok": True,
                "command": "claim",
                "task": task_payload,
                "recovered_expired": recovered,
                "dead_dependencies": dead_dependencies,
                "wip_blocked_by": blocked_by,
            }
            # Empty polls are read-like and may happen continuously for a
            # daemon.  Persist only a successful claim: that is the response
            # whose replay prevents an ambiguous network/client retry from
            # leasing a second task.  Skipping empty responses avoids unbounded
            # operations-table growth while retaining the safety property.
            if task_payload is not None:
                self._store_operation(
                    connection,
                    key=key,
                    command="queue.claim",
                    request_hash=request_hash,
                    response=response,
                    now=now_text,
                )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def execution_context(
        self,
        task_id: str,
        *,
        lease_token: str,
        worker_id: str,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Return a secret-free, root-first dependency result chain.

        This is a fenced read-only query for the currently leased task.  It does
        not expose the task payload, owner, fencing token, attempts, or any
        upstream payload.  Every transitive dependency must exist, belong to the
        same job/pod boundary, and have a JSON-object result in ``succeeded``
        state; otherwise execution is refused.
        """

        task_id = require_nonempty_string(task_id, "task_id")
        lease_token = require_nonempty_string(lease_token, "lease_token")
        worker_id = require_nonempty_string(worker_id, "worker_id")
        now_text = _timestamp(now)
        if not self.db.path.is_file():
            raise NotFoundError("queue database does not exist")
        with closing(self.db.connect()) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task {task_id!r} does not exist")
            if (
                task["status"] != "leased"
                or task["lease_token"] != lease_token
                or task["lease_owner"] != worker_id
            ):
                raise LeaseConflictError(
                    "execution context requires the current worker lease"
                )
            if task["lease_expires_at"] <= now_text:
                raise LeaseConflictError("lease expired before execution context read")

            upstream: list[dict[str, Any]] = []
            context_bytes = 0
            dependency_id = task["dependency_task_id"]
            seen: set[str] = set()
            while dependency_id is not None:
                if dependency_id in seen:
                    raise ValidationError("task dependency cycle detected")
                seen.add(dependency_id)
                if len(seen) > _MAX_UPSTREAM_TASKS:
                    raise ValidationError("task dependency chain exceeds the context limit")
                dependency = connection.execute(
                    "SELECT * FROM tasks WHERE id = ?", (dependency_id,)
                ).fetchone()
                if dependency is None:
                    raise ValidationError(
                        f"dependency task {dependency_id!r} does not exist"
                    )
                if dependency["status"] != "succeeded" or dependency["result_json"] is None:
                    raise ValidationError(
                        f"dependency task {dependency_id!r} has not succeeded"
                    )
                if dependency["job_id"] != task["job_id"]:
                    raise ValidationError(
                        f"dependency task {dependency_id!r} crosses the job boundary"
                    )
                if dependency["pod"] != task["pod"]:
                    raise ValidationError(
                        f"dependency task {dependency_id!r} crosses the pod boundary"
                    )
                try:
                    result_text = dependency["result_json"]
                    result_bytes = len(result_text.encode("utf-8"))
                    if result_bytes > _MAX_UPSTREAM_RESULT_BYTES:
                        raise ValidationError(
                            f"dependency task {dependency_id!r} result exceeds the context limit"
                        )
                    context_bytes += result_bytes
                    if context_bytes > _MAX_UPSTREAM_CONTEXT_BYTES:
                        raise ValidationError(
                            "transitive dependency results exceed the context limit"
                        )
                    raw_result = json.loads(result_text)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValidationError(
                        f"dependency task {dependency_id!r} has invalid result JSON"
                    ) from exc
                if not isinstance(raw_result, dict):
                    raise ValidationError(
                        f"dependency task {dependency_id!r} result is not an object"
                    )
                upstream.append(
                    {
                        "task_id": dependency["id"],
                        "job_id": dependency["job_id"],
                        "role": dependency["role"],
                        "pod": dependency["pod"],
                        "kind": dependency["kind"],
                        "result": _safe_upstream_value(
                            raw_result, f"upstream[{dependency['id']}].result"
                        ),
                        "completed_at": dependency["completed_at"],
                    }
                )
                dependency_id = dependency["dependency_task_id"]
            upstream.reverse()
            return {
                "ok": True,
                "command": "execution-context",
                "task_id": task_id,
                "upstream_results": upstream,
            }

    def complete(
        self,
        task_id: str,
        *,
        lease_token: str,
        result: dict[str, Any] | None,
        idempotency_key: str,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        return self._finish(
            task_id,
            lease_token=lease_token,
            body=_json_object(result, "result"),
            idempotency_key=idempotency_key,
            outcome="succeeded",
            terminal=True,
            now=now,
        )

    def renew_lease(
        self,
        task_id: str,
        *,
        lease_token: str,
        worker_id: str,
        lease_seconds: int = 900,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Extend the current fenced lease without changing its attempt number.

        Heartbeats deliberately do not use the operations table: a worker may emit
        thousands of them during a long render, while the lease token already makes
        each update conditional and safe to repeat.  A retry at the same timestamp
        produces the same expiry; an early retry can only keep or extend the lease,
        never shorten it.
        """

        task_id = require_nonempty_string(task_id, "task_id")
        lease_token = require_nonempty_string(lease_token, "lease_token")
        worker_id = require_nonempty_string(worker_id, "worker_id")
        lease_seconds = _bounded_int(lease_seconds, "lease_seconds", 5, 86400)
        now_text = _timestamp(now)
        requested_expiry = _plus_seconds(now_text, lease_seconds)

        connection = self._connection()
        try:
            task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"task {task_id!r} does not exist")
            if (
                task["status"] != "leased"
                or task["lease_token"] != lease_token
                or task["lease_owner"] != worker_id
            ):
                raise LeaseConflictError(
                    "task is not leased by this worker with this token"
                )
            if task["lease_expires_at"] <= now_text:
                raise LeaseConflictError("lease expired before heartbeat")

            expires_at = max(task["lease_expires_at"], requested_expiry)
            updated = connection.execute(
                """
                UPDATE tasks SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                    AND lease_owner = ? AND lease_expires_at > ?
                """,
                (
                    expires_at,
                    now_text,
                    task_id,
                    lease_token,
                    worker_id,
                    now_text,
                ),
            )
            if updated.rowcount != 1:
                raise LeaseConflictError("lease changed during heartbeat")
            attempt = connection.execute(
                """
                UPDATE task_attempts SET lease_expires_at = ?
                WHERE task_id = ? AND lease_token = ? AND status = 'leased'
                """,
                (expires_at, task_id, lease_token),
            )
            if attempt.rowcount != 1:
                raise LeaseConflictError("active attempt is missing during heartbeat")
            fresh = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            connection.commit()
            return {
                "ok": True,
                "command": "renew-lease",
                "task": self._task_dict(fresh),
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail(
        self,
        task_id: str,
        *,
        lease_token: str,
        error: dict[str, Any],
        idempotency_key: str,
        terminal: bool = False,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        return self._finish(
            task_id,
            lease_token=lease_token,
            body=_json_object(error, "error"),
            idempotency_key=idempotency_key,
            outcome="failed",
            terminal=bool(terminal),
            now=now,
        )

    def _finish(
        self,
        task_id: str,
        *,
        lease_token: str,
        body: dict[str, Any],
        idempotency_key: str,
        outcome: str,
        terminal: bool,
        now: datetime | str | None,
    ) -> dict[str, Any]:
        task_id = require_nonempty_string(task_id, "task_id")
        lease_token = require_nonempty_string(lease_token, "lease_token")
        key = require_nonempty_string(idempotency_key, "idempotency_key")
        now_text = _timestamp(now)
        request = {
            "task_id": task_id,
            "lease_token": lease_token,
            "body": body,
            "terminal": terminal,
        }
        command = f"queue.{outcome}"
        connection = self._connection()
        try:
            replay, request_hash = self._operation_replay(
                connection, key=key, command=command, request=request
            )
            if replay is not None:
                connection.rollback()
                return replay
            task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise NotFoundError(f"task {task_id!r} does not exist")
            if task["status"] != "leased" or task["lease_token"] != lease_token:
                raise LeaseConflictError(
                    "task is not leased with this token; the lease may have expired or been reassigned"
                )
            if task["lease_expires_at"] <= now_text:
                self._recover_expired_in_tx(connection, now_text)
                raise LeaseConflictError("lease expired before acknowledgement")

            if outcome == "succeeded":
                _validate_success_result(connection, task, body)
                task_status = "succeeded"
                available_at = task["available_at"]
                completed_at = now_text
                result_json = canonical_json(body)
                error_json = None
            else:
                exhausted = task["attempt_count"] >= task["max_attempts"]
                task_status = "dead" if terminal or exhausted else "queued"
                delay = min(
                    task["retry_backoff_seconds"] * (2 ** max(0, task["attempt_count"] - 1)),
                    86400,
                )
                available_at = now_text if task_status == "dead" else _plus_seconds(now_text, delay)
                completed_at = now_text if task_status == "dead" else None
                result_json = None
                error_json = canonical_json(body)
            connection.execute(
                """
                UPDATE task_attempts
                SET status = ?, finished_at = ?, result_json = ?, error_json = ?
                WHERE task_id = ? AND lease_token = ? AND status = 'leased'
                """,
                (outcome, now_text, result_json, error_json, task_id, lease_token),
            )
            connection.execute(
                """
                UPDATE tasks SET status = ?, available_at = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL, result_json = ?,
                    last_error_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (
                    task_status,
                    available_at,
                    result_json,
                    error_json,
                    now_text,
                    completed_at,
                    task_id,
                    lease_token,
                ),
            )
            dead_letter = None
            if task_status == "dead":
                dead_letter = self._record_dead_letter(
                    connection,
                    task_id=task_id,
                    error=body,
                    now_text=now_text,
                )
            fresh = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            response = {
                "ok": True,
                "command": "complete" if outcome == "succeeded" else "fail",
                "retried": outcome == "failed" and task_status == "queued",
                "task": self._task_dict(fresh),
                "dead_letter": self._dead_letter_dict(dead_letter)
                if dead_letter is not None
                else None,
            }
            self._store_operation(
                connection,
                key=key,
                command=command,
                request_hash=request_hash,
                response=response,
                now=now_text,
            )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_expired(self, *, now: datetime | str | None = None) -> dict[str, Any]:
        now_text = _timestamp(now)
        connection = self._connection()
        try:
            recovered = self._recover_expired_in_tx(connection, now_text)
            dead_dependencies = self._propagate_dead_dependencies(connection, now_text)
            connection.commit()
            return {
                "ok": True,
                "command": "recover-expired",
                "count": len(recovered),
                "items": recovered,
                "dead_dependencies": dead_dependencies,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def dead_letters(
        self,
        *,
        status: str | None = "open",
        task_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return the durable dead-letter ledger, newest first."""

        if status not in {None, "open", "resolved"}:
            raise ValidationError("status must be open, resolved, or all")
        if task_id is not None:
            task_id = require_nonempty_string(task_id, "task_id")
        limit = _bounded_int(limit, "limit", 1, 1000)
        self.db.initialize()
        clauses: list[str] = []
        parameters: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if task_id is not None:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        query = "SELECT * FROM dead_letters"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, task_id, cycle_no DESC LIMIT ?"
        parameters.append(limit)
        with closing(self.db.connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
            return {
                "ok": True,
                "command": "dead-list",
                "status": status or "all",
                "count": len(rows),
                "items": [self._dead_letter_dict(row) for row in rows],
            }

    def retry_dead(
        self,
        task_id: str,
        *,
        reason: str,
        actor: str,
        idempotency_key: str,
        additional_attempts: int = 1,
        available_at: datetime | str | None = None,
        cascade_dependents: bool = False,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Requeue a dead task without changing its immutable inputs.

        This is intentionally operator-controlled. It extends the attempt budget,
        preserves attempt history, resolves the current DLQ cycle, and optionally
        revives only descendants that died from ``dependency_dead``.
        """

        task_id = require_nonempty_string(task_id, "task_id")
        reason = require_nonempty_string(reason, "reason")
        actor = require_nonempty_string(actor, "actor")
        key = require_nonempty_string(idempotency_key, "idempotency_key")
        additional_attempts = _bounded_int(
            additional_attempts, "additional_attempts", 1, 100
        )
        if not isinstance(cascade_dependents, bool):
            raise ValidationError("cascade_dependents must be a boolean")
        now_text = _timestamp(now)
        requested_available_at = (
            _timestamp(available_at) if available_at is not None else None
        )
        available_text = requested_available_at or now_text
        request = {
            "task_id": task_id,
            "reason": reason,
            "actor": actor,
            "additional_attempts": additional_attempts,
            "available_at": requested_available_at,
            "cascade_dependents": cascade_dependents,
        }
        connection = self._connection()
        try:
            replay, request_hash = self._operation_replay(
                connection, key=key, command="queue.dead-retry", request=request
            )
            if replay is not None:
                connection.rollback()
                return replay
            task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise NotFoundError(f"task {task_id!r} does not exist")
            if task["status"] != "dead":
                raise ValidationError("dead-retry requires a task in dead state")
            if task["dependency_task_id"] is not None:
                dependency = connection.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task["dependency_task_id"],)
                ).fetchone()
                if dependency is None or dependency["status"] != "succeeded":
                    raise ValidationError(
                        "dead task dependency must succeed before it can be retried"
                    )
            new_max_attempts = max(
                task["max_attempts"], task["attempt_count"] + additional_attempts
            )
            if new_max_attempts > 100:
                raise ValidationError("retry would exceed the 100-attempt safety ceiling")
            open_letter = connection.execute(
                "SELECT id FROM dead_letters WHERE task_id = ? AND status = 'open'",
                (task_id,),
            ).fetchone()
            if open_letter is None:
                self._record_dead_letter(
                    connection,
                    task_id=task_id,
                    error=json.loads(task["last_error_json"])
                    if task["last_error_json"]
                    else {"code": "legacy_dead", "message": "dead task imported without DLQ"},
                    now_text=now_text,
                )
            connection.execute(
                """
                UPDATE tasks
                SET status = 'queued', max_attempts = ?, available_at = ?,
                    completed_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'dead'
                """,
                (new_max_attempts, available_text, now_text, task_id),
            )
            resolution = {
                "action": "retry",
                "actor": actor,
                "reason": reason,
                "additional_attempts": additional_attempts,
                "idempotency_key": key,
            }
            resolved = [
                self._resolve_open_dead_letter(
                    connection,
                    task_id=task_id,
                    resolution=resolution,
                    now_text=now_text,
                )
            ]
            revived: list[str] = []
            if cascade_dependents:
                descendants = connection.execute(
                    """
                    WITH RECURSIVE descendants(id, depth) AS (
                        SELECT id, 1 FROM tasks WHERE dependency_task_id = ?
                        UNION ALL
                        SELECT child.id, descendants.depth + 1
                        FROM tasks AS child
                        JOIN descendants ON child.dependency_task_id = descendants.id
                    )
                    SELECT task.*, descendants.depth
                    FROM descendants JOIN tasks AS task ON task.id = descendants.id
                    ORDER BY descendants.depth, task.id
                    """,
                    (task_id,),
                ).fetchall()
                for descendant in descendants:
                    error = (
                        json.loads(descendant["last_error_json"])
                        if descendant["last_error_json"]
                        else {}
                    )
                    if descendant["status"] != "dead" or error.get("code") != "dependency_dead":
                        continue
                    connection.execute(
                        """
                        UPDATE tasks SET status = 'queued', available_at = ?,
                            completed_at = NULL, updated_at = ?
                        WHERE id = ? AND status = 'dead'
                        """,
                        (available_text, now_text, descendant["id"]),
                    )
                    letter_id = self._resolve_open_dead_letter(
                        connection,
                        task_id=descendant["id"],
                        resolution={
                            "action": "cascade_retry",
                            "actor": actor,
                            "reason": reason,
                            "root_task_id": task_id,
                            "idempotency_key": key,
                        },
                        now_text=now_text,
                    )
                    if letter_id is not None:
                        resolved.append(letter_id)
                    revived.append(descendant["id"])
            fresh = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            response = {
                "ok": True,
                "command": "dead-retry",
                "task": self._task_dict(fresh),
                "revived_dependents": revived,
                "resolved_dead_letters": [item for item in resolved if item is not None],
            }
            self._store_operation(
                connection,
                key=key,
                command="queue.dead-retry",
                request_hash=request_hash,
                response=response,
                now=now_text,
            )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def rework_task(
        self,
        task_id: str,
        *,
        reason: str,
        actor: str,
        idempotency_key: str,
        payload_patch: Mapping[str, Any] | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Clone a task and its downstream DAG as a fresh, auditable rework run."""

        task_id = require_nonempty_string(task_id, "task_id")
        reason = require_nonempty_string(reason, "reason")
        actor = require_nonempty_string(actor, "actor")
        key = require_nonempty_string(idempotency_key, "idempotency_key")
        if payload_patch is None:
            patch: dict[str, Any] = {}
        elif isinstance(payload_patch, Mapping):
            patch = dict(payload_patch)
            # Validate JSON-serializability and normalize mappings before storage.
            patch = json.loads(canonical_json(patch))
        else:
            raise ValidationError("payload_patch must be a JSON object")
        now_text = _timestamp(now)
        request = {
            "task_id": task_id,
            "reason": reason,
            "actor": actor,
            "payload_patch": patch,
            "cascade": True,
        }
        connection = self._connection()
        try:
            replay, request_hash = self._operation_replay(
                connection, key=key, command="queue.task-rework", request=request
            )
            if replay is not None:
                connection.rollback()
                return replay
            root = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if root is None:
                raise NotFoundError(f"task {task_id!r} does not exist")
            if root["role"] == "publisher":
                raise ValidationError(
                    "publisher tasks cannot be reworked directly; reconcile the remote "
                    "outcome and retry the same immutable publication artifact"
                )
            rows = connection.execute(
                """
                WITH RECURSIVE subtree(id, depth) AS (
                    SELECT ?, 0
                    UNION ALL
                    SELECT child.id, subtree.depth + 1
                    FROM tasks AS child JOIN subtree
                      ON child.dependency_task_id = subtree.id
                )
                SELECT task.*, subtree.depth
                FROM subtree JOIN tasks AS task ON task.id = subtree.id
                ORDER BY subtree.depth, task.created_at, task.id
                """,
                (task_id,),
            ).fetchall()
            leased = [row["id"] for row in rows if row["status"] == "leased"]
            if leased:
                raise LeaseConflictError(
                    "cannot rework a subtree with active leases: " + ", ".join(leased)
                )
            mapping = {
                row["id"]: f"task_{digest_text(f'{key}:{request_hash}:{row["id"]}')[:24]}"
                for row in rows
            }
            created: list[dict[str, Any]] = []
            for row in rows:
                payload = json.loads(row["payload_json"])
                if row["id"] == task_id:
                    payload.update(patch)
                dependency_id = row["dependency_task_id"]
                if dependency_id in mapping:
                    dependency_id = mapping[dependency_id]
                replacement_id = mapping[row["id"]]
                replacement_key = f"rework:{digest_text(f'{key}:{row["id"]}')[:40]}"
                connection.execute(
                    """
                    INSERT INTO tasks(
                        id, job_id, dependency_task_id, role, pod, kind, payload_json,
                        priority, status, idempotency_key, max_attempts, attempt_count,
                        retry_backoff_seconds, available_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        replacement_id,
                        row["job_id"],
                        dependency_id,
                        row["role"],
                        row["pod"],
                        row["kind"],
                        canonical_json(payload),
                        row["priority"],
                        replacement_key,
                        row["max_attempts"],
                        row["retry_backoff_seconds"],
                        now_text,
                        now_text,
                        now_text,
                    ),
                )
                created.append(
                    self._task_dict(
                        connection.execute(
                            "SELECT * FROM tasks WHERE id = ?", (replacement_id,)
                        ).fetchone()
                    )
                )
            rework_id = f"rw_{digest_text(f'{key}:{request_hash}')[:24]}"
            superseded: list[str] = []
            resolved_letters: list[str] = []
            for row in rows:
                resolution = {
                    "action": "rework",
                    "actor": actor,
                    "reason": reason,
                    "rework_id": rework_id,
                    "replacement_task_id": mapping[row["id"]],
                    "idempotency_key": key,
                }
                if row["status"] == "queued":
                    error = {
                        "code": "rework_superseded",
                        "message": "task was replaced by a controlled rework",
                        "rework_id": rework_id,
                        "replacement_task_id": mapping[row["id"]],
                    }
                    connection.execute(
                        """
                        UPDATE tasks SET status = 'dead', last_error_json = ?,
                            completed_at = ?, updated_at = ?
                        WHERE id = ? AND status = 'queued'
                        """,
                        (canonical_json(error), now_text, now_text, row["id"]),
                    )
                    self._record_dead_letter(
                        connection,
                        task_id=row["id"],
                        error=error,
                        now_text=now_text,
                    )
                    superseded.append(row["id"])
                letter_id = self._resolve_open_dead_letter(
                    connection,
                    task_id=row["id"],
                    resolution=resolution,
                    now_text=now_text,
                )
                if letter_id is not None:
                    resolved_letters.append(letter_id)
            connection.execute(
                """
                INSERT INTO task_reworks(
                    id, root_task_id, replacement_root_task_id, actor, reason,
                    task_mapping_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rework_id,
                    task_id,
                    mapping[task_id],
                    actor,
                    reason,
                    canonical_json(mapping),
                    now_text,
                ),
            )
            response = {
                "ok": True,
                "command": "task-rework",
                "rework_id": rework_id,
                "root_task_id": task_id,
                "replacement_root_task_id": mapping[task_id],
                "task_mapping": mapping,
                "created_tasks": created,
                "superseded_queued_tasks": superseded,
                "resolved_dead_letters": resolved_letters,
            }
            self._store_operation(
                connection,
                key=key,
                command="queue.task-rework",
                request_hash=request_hash,
                response=response,
                now=now_text,
            )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def configure_limit(
        self,
        *,
        max_leased: int,
        role: str | None = None,
        pod: str | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        if role is not None:
            role = require_nonempty_string(role, "role")
        if pod is not None:
            pod = require_nonempty_string(pod, "pod")
        if role is None and pod is None:
            raise ValidationError("at least one of role or pod is required")
        max_leased = _bounded_int(max_leased, "max_leased", 1, 1000)
        now_text = _timestamp(now)
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT id FROM queue_limits WHERE role IS ? AND pod IS ?", (role, pod)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO queue_limits(role, pod, max_leased, updated_at) VALUES (?, ?, ?, ?)",
                    (role, pod, max_leased, now_text),
                )
            else:
                connection.execute(
                    "UPDATE queue_limits SET max_leased = ?, updated_at = ? WHERE id = ?",
                    (max_leased, now_text, row["id"]),
                )
            connection.commit()
            return {
                "ok": True,
                "command": "queue-limit",
                "role": role,
                "pod": pod,
                "max_leased": max_leased,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def status(
        self, task_id: str | None = None, *, now: datetime | str | None = None
    ) -> dict[str, Any]:
        self.db.initialize()
        now_text = _timestamp(now)
        with closing(self.db.connect()) as connection:
            if task_id is not None:
                task_id = require_nonempty_string(task_id, "task_id")
                row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"task {task_id!r} does not exist")
                attempts = connection.execute(
                    "SELECT * FROM task_attempts WHERE task_id = ? ORDER BY attempt_no", (task_id,)
                ).fetchall()
                dead_letters = connection.execute(
                    "SELECT * FROM dead_letters WHERE task_id = ? ORDER BY cycle_no", (task_id,)
                ).fetchall()
                reworks = [
                    {
                        **dict(item),
                        "task_mapping": json.loads(item["task_mapping_json"]),
                    }
                    for item in connection.execute(
                        "SELECT * FROM task_reworks WHERE root_task_id = ? ORDER BY created_at",
                        (task_id,),
                    ).fetchall()
                ]
                for item in reworks:
                    item.pop("task_mapping_json", None)
                return {
                    "ok": True,
                    "command": "queue-status",
                    "task": self._task_dict(row),
                    "attempts": [self._attempt_dict(item) for item in attempts],
                    "dead_letters": [self._dead_letter_dict(item) for item in dead_letters],
                    "reworks": reworks,
                }
            by_status = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
                )
            }
            by_role_pod = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT role, pod, status, COUNT(*) AS count FROM tasks
                    GROUP BY role, pod, status ORDER BY role, pod, status
                    """
                )
            ]
            limits = [
                dict(row)
                for row in connection.execute(
                    "SELECT role, pod, max_leased, updated_at FROM queue_limits ORDER BY role, pod"
                )
            ]
            due = connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE status = 'queued' AND available_at <= ?",
                (now_text,),
            ).fetchone()["count"]
            expired = connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE status = 'leased' AND lease_expires_at <= ?",
                (now_text,),
            ).fetchone()["count"]
            attempts = connection.execute("SELECT COUNT(*) AS count FROM task_attempts").fetchone()[
                "count"
            ]
            dead_letter_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM dead_letters GROUP BY status"
                )
            }
            return {
                "ok": True,
                "command": "queue-status",
                "database": str(self.db.path),
                "tasks": by_status,
                "due_queued": due,
                "expired_leases": expired,
                "attempts": attempts,
                "dead_letters": dead_letter_counts,
                "open_dead_letters": dead_letter_counts.get("open", 0),
                "by_role_pod": by_role_pod,
                "limits": limits,
            }

    def simulate_day(
        self,
        *,
        target: int,
        pods: Iterable[str],
        roles: Iterable[str],
        idempotency_key: str,
        lease_seconds: int = 60,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        target = _bounded_int(target, "target", 10, 15)
        pod_list = [require_nonempty_string(item, "pod") for item in pods]
        role_list = [require_nonempty_string(item, "role") for item in roles]
        if not pod_list or len(set(pod_list)) != len(pod_list):
            raise ValidationError("pods must be a non-empty list of unique values")
        if not role_list or len(set(role_list)) != len(role_list):
            raise ValidationError("roles must be a non-empty list of unique values")
        key = require_nonempty_string(idempotency_key, "idempotency_key")
        lease_seconds = _bounded_int(lease_seconds, "lease_seconds", 5, 86400)
        requested_now = _timestamp(now) if now is not None else None
        now_text = requested_now or _timestamp()
        request = {
            "target": target,
            "pods": pod_list,
            "roles": role_list,
            "lease_seconds": lease_seconds,
            "now": requested_now,
        }
        connection = self._connection()
        try:
            replay, request_hash = self._operation_replay(
                connection, key=key, command="queue.simulate-day", request=request
            )
            if replay is not None:
                connection.rollback()
                return replay
            connection.rollback()
        finally:
            connection.close()

        run_id = digest_text(key + canonical_json(request))[:16]
        task_chains: list[list[str]] = []
        kinds: dict[str, str] = {}
        for video_index in range(target):
            pod = pod_list[video_index % len(pod_list)]
            dependency = None
            chain: list[str] = []
            for role in role_list:
                kind = f"simulation.{run_id}.{role}"
                kinds[role] = kind
                enqueued = self.enqueue(
                    role=role,
                    pod=pod,
                    kind=kind,
                    payload={"simulation_run": run_id, "video_index": video_index},
                    dependency_task_id=dependency,
                    idempotency_key=f"sim-enqueue:{run_id}:{video_index}:{role}",
                    max_attempts=3,
                    retry_backoff_seconds=0,
                    now=now_text,
                )
                dependency = enqueued["task"]["id"]
                chain.append(dependency)
            task_chains.append(chain)

        max_role_wip = {role: 0 for role in role_list}
        max_role_pod_wip: dict[str, int] = {}
        claim_sequence = 0
        for role in role_list:
            pending = True
            while pending:
                claimed: list[dict[str, Any]] = []
                for pod in pod_list:
                    while True:
                        claim_sequence += 1
                        claim = self.claim(
                            worker_id=f"sim-worker-{role}-{pod}-{claim_sequence}",
                            role=role,
                            pod=pod,
                            kind=kinds[role],
                            lease_seconds=lease_seconds,
                            idempotency_key=f"sim-claim:{run_id}:{claim_sequence}",
                            now=now_text,
                        )
                        if claim["task"] is None:
                            break
                        claimed.append(claim["task"])
                        key_role_pod = f"{role}/{pod}"
                        pod_wip = sum(
                            1
                            for item in claimed
                            if item["role"] == role and item["pod"] == pod
                        )
                        max_role_pod_wip[key_role_pod] = max(
                            max_role_pod_wip.get(key_role_pod, 0), pod_wip
                        )
                        max_role_wip[role] = max(max_role_wip[role], len(claimed))
                if not claimed:
                    pending = False
                    continue
                for task in claimed:
                    self.complete(
                        task["id"],
                        lease_token=task["lease_token"],
                        result={"simulated": True},
                        idempotency_key=f"sim-complete:{task['lease_token']}",
                        now=now_text,
                    )

        self.db.initialize()
        final_ids = [chain[-1] for chain in task_chains]
        placeholders = ",".join("?" for _ in final_ids)
        with closing(self.db.connect()) as connection:
            completed_videos = connection.execute(
                f"SELECT COUNT(*) AS count FROM tasks WHERE id IN ({placeholders}) AND status = 'succeeded'",
                final_ids,
            ).fetchone()["count"]
            run_tasks = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks WHERE kind LIKE ? GROUP BY status",
                (f"simulation.{run_id}.%",),
            ).fetchall()
        response = {
            "ok": completed_videos == target,
            "command": "simulate-day",
            "simulation_run": run_id,
            "target_videos": target,
            "completed_videos": completed_videos,
            "workflow_roles": role_list,
            "pods": pod_list,
            "task_counts": {row["status"]: row["count"] for row in run_tasks},
            "max_observed_role_wip": max_role_wip,
            "max_observed_role_pod_wip": max_role_pod_wip,
            "background_processes_started": False,
        }
        connection = self._connection()
        try:
            replay, request_hash = self._operation_replay(
                connection, key=key, command="queue.simulate-day", request=request
            )
            if replay is not None:
                connection.rollback()
                return replay
            self._store_operation(
                connection,
                key=key,
                command="queue.simulate-day",
                request_hash=request_hash,
                response=response,
                now=now_text,
            )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
