from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_artifact
from .db import Database
from .errors import IdempotencyConflictError, NotFoundError, ValidationError
from .performance import evaluate_performance
from .validators import canonical_json, digest_text, load_json_file, require_nonempty_string


PRODUCTION_STAGES = frozenset(
    {
        "scout",
        "research",
        "sensitivity_review",
        "privacy_review",
        "medical_review",
        "rights",
        "script",
        "voice",
        "source_audio",
        "editor",
        "render",
        "qc",
        "final_review",
        "publisher",
        "system",
    }
)
PRODUCTION_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|password|secret|token)", re.I)
CANONICAL_SNAPSHOT_HOURS = (1, 6, 24, 72, 168)
SNAPSHOT_TOLERANCE_HOURS = {1: 0.5, 6: 1.0, 24: 3.0, 72: 6.0, 168: 12.0}
EDITORIAL_POLICY_VERSION = "bounded-editorial-v1"
MAXIMUM_FOLLOWUPS = 2


def _utc_now(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValidationError("now must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO 8601 date-time") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_id(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    if not SAFE_ID.fullmatch(text):
        raise ValidationError(
            f"{field} must contain only letters, digits, dot, colon, underscore, or hyphen"
        )
    return text


def _number(
    value: Any,
    field: str,
    *,
    minimum: float = 0,
    optional: bool = False,
) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValidationError(f"{field} must be finite and at least {minimum}")
    return result


def _integer(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
    optional: bool = False,
) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f" through {maximum}" if maximum is not None else " or greater"
        raise ValidationError(f"{field} must be {minimum}{suffix}")
    return value


def _sha256(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    if not SHA256_RE.fullmatch(text):
        raise ValidationError(f"{field} must be a lowercase sha256")
    return text


def _reject_secrets(value: Any, path: str = "metadata") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                raise ValidationError(f"{path}.{key} looks like a secret and cannot be persisted")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")


def _duration_band(duration_seconds: float) -> str:
    if duration_seconds < 20:
        return "under_20"
    if duration_seconds < 35:
        return "20_34"
    if duration_seconds < 60:
        return "35_59"
    return "60_plus"


def _canonical_snapshot_hour(age_hours: float) -> int:
    hour = min(CANONICAL_SNAPSHOT_HOURS, key=lambda item: (abs(item - age_hours), item))
    if abs(hour - age_hours) > SNAPSHOT_TOLERANCE_HOURS[hour]:
        raise ValidationError(
            "age_hours is outside the tolerance for canonical 1/6/24/72/168-hour snapshots"
        )
    return hour


_EDITORIAL_ACTIONS = {
    "hook": "Test one clearer, truthful opening formulation in the first two seconds.",
    "hold": "Test tighter pacing and move the promised payoff earlier without removing context.",
    "value": "Make the practical takeaway easier to save or share without adding unsupported claims.",
    "conversion": "Test one restrained, topic-relevant call to follow at the ending.",
}


def _bounded_recommendations(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    base = {
        "policy_version": EDITORIAL_POLICY_VERSION,
        "scope": "editorial_only",
        "maximum_followups": 0,
        "actions": [],
        "immutable_boundaries": [
            "factual_confidence",
            "rights_confidence",
            "medical_safety",
            "human_publish_approval",
        ],
        "automatic_job_changes": False,
        "automatic_publish": False,
        "reach_guarantee": False,
    }
    if not evaluation.get("ok"):
        return {
            **base,
            "status": "insufficient_cohort",
            "note": "Collect more comparable snapshots before changing the editorial mix.",
        }
    if not evaluation.get("safety_clear"):
        return {
            **base,
            "status": "safety_blocked",
            "note": "A policy event blocks winner promotion and follow-up recommendations.",
        }
    percentiles = evaluation["percentiles"]
    ranked_low = sorted(percentiles, key=lambda key: (percentiles[key], key))
    actions: list[dict[str, Any]] = []
    if evaluation.get("winner"):
        strongest = max(percentiles, key=lambda key: (percentiles[key], key))
        actions.append(
            {
                "signal": strongest,
                "priority": 1,
                "change": (
                    "Preserve this editorial strength in at most two new angles; "
                    "use new facts, wording, and footage."
                ),
                "constraint": "Change only one major creative dimension per follow-up.",
            }
        )
        if ranked_low[0] != strongest:
            actions.append(
                {
                    "signal": ranked_low[0],
                    "priority": 2,
                    "change": _EDITORIAL_ACTIONS[ranked_low[0]],
                    "constraint": "Do not weaken the winning signal or copy the source edit.",
                }
            )
        return {
            **base,
            "status": "winner",
            "maximum_followups": min(
                MAXIMUM_FOLLOWUPS, int(evaluation.get("maximum_followups", 0))
            ),
            "actions": actions[:MAXIMUM_FOLLOWUPS],
            "note": "A winner is a bounded experiment signal, not a reach guarantee.",
        }
    for priority, signal in enumerate(ranked_low[:MAXIMUM_FOLLOWUPS], start=1):
        actions.append(
            {
                "signal": signal,
                "priority": priority,
                "change": _EDITORIAL_ACTIONS[signal],
                "constraint": "Run as a new controlled test; do not clone the evaluated video.",
            }
        )
    return {
        **base,
        "status": "nonwinner",
        "actions": actions,
        "note": "Recommendations are test hypotheses only; no follow-up is auto-created.",
    }


class AnalyticsStore:
    """Transactional production telemetry and checksum-attributed feedback store."""

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
    def _metric_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "event_id": row["event_id"],
            "job_id": row["job_id"],
            "task_id": row["task_id"],
            "lane": row["lane"],
            "stage": row["stage"],
            "status": row["status"],
            "occurred_at": row["occurred_at"],
            "duration_seconds": row["duration_seconds"],
            "attempts": row["attempts"],
            "estimated_cost_usd": row["estimated_cost_usd"],
            "cpu_seconds": row["cpu_seconds"],
            "gpu_seconds": row["gpu_seconds"],
            "input_bytes": row["input_bytes"],
            "output_bytes": row["output_bytes"],
            "metadata": json.loads(row["metadata_json"]),
            "recorded_at": row["created_at"],
        }

    @staticmethod
    def _normalize_metric(event: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "schema_version",
            "event_id",
            "job_id",
            "task_id",
            "lane",
            "stage",
            "status",
            "occurred_at",
            "duration_seconds",
            "attempts",
            "estimated_cost_usd",
            "cpu_seconds",
            "gpu_seconds",
            "input_bytes",
            "output_bytes",
            "metadata",
        }
        unknown = sorted(set(event) - allowed)
        if unknown:
            raise ValidationError(f"production metric has unknown fields: {', '.join(unknown)}")
        if event.get("schema_version") != "1.0.0":
            raise ValidationError("production metric schema_version must equal '1.0.0'")
        job_id = event.get("job_id")
        task_id = event.get("task_id")
        if job_id is not None:
            job_id = _safe_id(job_id, "job_id")
        if task_id is not None:
            task_id = _safe_id(task_id, "task_id")
        lane = _safe_id(event.get("lane"), "lane")
        stage = require_nonempty_string(event.get("stage"), "stage")
        if stage not in PRODUCTION_STAGES:
            raise ValidationError(f"stage must be one of: {', '.join(sorted(PRODUCTION_STAGES))}")
        status = require_nonempty_string(event.get("status"), "status")
        if status not in PRODUCTION_STATUSES:
            raise ValidationError(
                f"status must be one of: {', '.join(sorted(PRODUCTION_STATUSES))}"
            )
        metadata = event.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValidationError("metadata must be a JSON object")
        metadata = dict(metadata)
        _reject_secrets(metadata)
        return {
            "schema_version": "1.0.0",
            "event_id": _safe_id(event.get("event_id"), "event_id"),
            "job_id": job_id,
            "task_id": task_id,
            "lane": lane,
            "stage": stage,
            "status": status,
            "occurred_at": _datetime(event.get("occurred_at"), "occurred_at"),
            "duration_seconds": _number(
                event.get("duration_seconds"), "duration_seconds"
            ),
            "attempts": _integer(event.get("attempts", 1), "attempts", minimum=1, maximum=100),
            "estimated_cost_usd": _number(
                event.get("estimated_cost_usd", 0), "estimated_cost_usd"
            ),
            "cpu_seconds": _number(
                event.get("cpu_seconds"), "cpu_seconds", optional=True
            ),
            "gpu_seconds": _number(
                event.get("gpu_seconds"), "gpu_seconds", optional=True
            ),
            "input_bytes": _integer(
                event.get("input_bytes"), "input_bytes", optional=True
            ),
            "output_bytes": _integer(
                event.get("output_bytes"), "output_bytes", optional=True
            ),
            "metadata": metadata,
        }

    def record_metric(
        self,
        event: Mapping[str, Any],
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(event, Mapping):
            raise ValidationError("production metric must be a JSON object")
        normalized = self._normalize_metric(event)
        timestamp = _utc_now(now)
        with closing(self._connection()) as connection:
            replay, request_hash = self._operation_replay(
                connection,
                key=idempotency_key,
                command="metrics_record",
                request=normalized,
            )
            if replay is not None:
                connection.rollback()
                return replay
            if normalized["job_id"] is not None:
                if connection.execute(
                    "SELECT 1 FROM jobs WHERE id = ?", (normalized["job_id"],)
                ).fetchone() is None:
                    raise NotFoundError(f"job {normalized['job_id']!r} not found")
            if normalized["task_id"] is not None:
                task = connection.execute(
                    "SELECT job_id FROM tasks WHERE id = ?", (normalized["task_id"],)
                ).fetchone()
                if task is None:
                    raise NotFoundError(f"task {normalized['task_id']!r} not found")
                if (
                    normalized["job_id"] is not None
                    and task["job_id"] is not None
                    and task["job_id"] != normalized["job_id"]
                ):
                    raise ValidationError("task_id belongs to a different job_id")
            existing = connection.execute(
                "SELECT * FROM production_metrics WHERE event_id = ?",
                (normalized["event_id"],),
            ).fetchone()
            created = existing is None
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO production_metrics (
                        event_id, job_id, task_id, lane, stage, status, occurred_at,
                        duration_seconds, attempts, estimated_cost_usd, cpu_seconds,
                        gpu_seconds, input_bytes, output_bytes, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized["event_id"],
                        normalized["job_id"],
                        normalized["task_id"],
                        normalized["lane"],
                        normalized["stage"],
                        normalized["status"],
                        normalized["occurred_at"],
                        normalized["duration_seconds"],
                        normalized["attempts"],
                        normalized["estimated_cost_usd"],
                        normalized["cpu_seconds"],
                        normalized["gpu_seconds"],
                        normalized["input_bytes"],
                        normalized["output_bytes"],
                        canonical_json(normalized["metadata"]),
                        timestamp,
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM production_metrics WHERE event_id = ?",
                    (normalized["event_id"],),
                ).fetchone()
            else:
                stored = self._metric_row(existing)
                comparable = {key: stored[key] for key in normalized}
                if comparable != normalized:
                    raise ValidationError(
                        f"event_id {normalized['event_id']!r} already has different metrics"
                    )
            response = {
                "ok": True,
                "command": "metrics-record",
                "created": created,
                "metric": self._metric_row(existing),
            }
            self._store_operation(
                connection,
                key=idempotency_key,
                command="metrics_record",
                request_hash=request_hash,
                response=response,
                now=timestamp,
            )
            connection.commit()
            return response

    def collect_queue_metrics(
        self,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Materialize finished queue attempts into immutable production metrics.

        This is safe to run from a timer. Every attempt has a deterministic event
        identity, and unsupported custom roles/pods are reported instead of being
        silently coerced into the analytics model.
        """

        timestamp = _utc_now(now)
        request = {"finished_at_or_before": timestamp}
        with closing(self._connection()) as connection:
            replay, request_hash = self._operation_replay(
                connection,
                key=idempotency_key,
                command="metrics_collect_queue",
                request=request,
            )
            if replay is not None:
                connection.rollback()
                return replay
            rows = connection.execute(
                """
                SELECT a.task_id, a.attempt_no, a.status AS attempt_status,
                       a.claimed_at, a.finished_at, a.error_json,
                       t.job_id, t.role, t.pod
                FROM task_attempts AS a
                JOIN tasks AS t ON t.id = a.task_id
                WHERE a.status IN ('succeeded', 'failed', 'expired')
                  AND a.finished_at IS NOT NULL
                  AND a.finished_at <= ?
                ORDER BY a.finished_at, a.task_id, a.attempt_no
                """,
                (timestamp,),
            ).fetchall()
            created_ids: list[str] = []
            existing_ids: list[str] = []
            skipped: list[dict[str, Any]] = []
            for row in rows:
                if row["role"] not in PRODUCTION_STAGES or not SAFE_ID.fullmatch(row["pod"]):
                    skipped.append(
                        {
                            "task_id": row["task_id"],
                            "attempt_no": row["attempt_no"],
                            "reason": "unsupported_role_or_lane",
                        }
                    )
                    continue
                claimed = datetime.fromisoformat(row["claimed_at"].replace("Z", "+00:00"))
                finished = datetime.fromisoformat(row["finished_at"].replace("Z", "+00:00"))
                duration = max(0.0, (finished - claimed).total_seconds())
                identity = canonical_json(
                    {"task_id": row["task_id"], "attempt_no": row["attempt_no"]}
                )
                event_id = f"evt_queue_{digest_text(identity)[:20]}"
                error_code = None
                if row["error_json"]:
                    try:
                        error = json.loads(row["error_json"])
                    except json.JSONDecodeError:
                        error = None
                    if isinstance(error, Mapping) and isinstance(error.get("code"), str):
                        error_code = error["code"][:128]
                metadata = {
                    "collector": "queue_attempts_v1",
                    "attempt_status": row["attempt_status"],
                    "error_code": error_code,
                }
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO production_metrics (
                        event_id, job_id, task_id, lane, stage, status, occurred_at,
                        duration_seconds, attempts, estimated_cost_usd, cpu_seconds,
                        gpu_seconds, input_bytes, output_bytes, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        event_id,
                        row["job_id"],
                        row["task_id"],
                        row["pod"],
                        row["role"],
                        "succeeded" if row["attempt_status"] == "succeeded" else "failed",
                        _datetime(row["finished_at"], "finished_at"),
                        duration,
                        row["attempt_no"],
                        canonical_json(metadata),
                        timestamp,
                    ),
                )
                if cursor.rowcount == 1:
                    created_ids.append(event_id)
                else:
                    existing_ids.append(event_id)
            response = {
                "ok": not skipped,
                "command": "metrics-collect-queue",
                "as_of": timestamp,
                "created": len(created_ids),
                "existing": len(existing_ids),
                "metric_event_ids": created_ids + existing_ids,
                "skipped": skipped,
            }
            self._store_operation(
                connection,
                key=idempotency_key,
                command="metrics_collect_queue",
                request_hash=request_hash,
                response=response,
                now=timestamp,
            )
            connection.commit()
            return response

    def summary(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        lane: str | None = None,
        stage: str | None = None,
    ) -> dict[str, Any]:
        where = ["1 = 1"]
        params: list[Any] = []
        if since is not None:
            where.append("occurred_at >= ?")
            params.append(_datetime(since, "since"))
        if until is not None:
            where.append("occurred_at <= ?")
            params.append(_datetime(until, "until"))
        if lane is not None:
            where.append("lane = ?")
            params.append(_safe_id(lane, "lane"))
        if stage is not None:
            if stage not in PRODUCTION_STAGES:
                raise ValidationError(f"stage must be one of: {', '.join(sorted(PRODUCTION_STAGES))}")
            where.append("stage = ?")
            params.append(stage)
        condition = " AND ".join(where)
        self.db.initialize()
        with closing(self.db.connect()) as connection:
            groups = connection.execute(
                f"""
                SELECT lane, stage, status, COUNT(*) AS events,
                       ROUND(SUM(duration_seconds), 3) AS duration_seconds,
                       ROUND(SUM(estimated_cost_usd), 6) AS estimated_cost_usd,
                       SUM(COALESCE(output_bytes, 0)) AS output_bytes,
                       ROUND(AVG(duration_seconds), 3) AS average_duration_seconds
                FROM production_metrics
                WHERE {condition}
                GROUP BY lane, stage, status
                ORDER BY lane, stage, status
                """,
                params,
            ).fetchall()
            totals = connection.execute(
                f"""
                SELECT COUNT(*) AS events,
                       COALESCE(ROUND(SUM(duration_seconds), 3), 0) AS duration_seconds,
                       COALESCE(ROUND(SUM(estimated_cost_usd), 6), 0) AS estimated_cost_usd,
                       COALESCE(SUM(output_bytes), 0) AS output_bytes,
                       COUNT(DISTINCT job_id) AS jobs
                FROM production_metrics WHERE {condition}
                """,
                params,
            ).fetchone()
            outbox_counts = connection.execute(
                "SELECT status, COUNT(*) AS count FROM publish_outbox GROUP BY status ORDER BY status"
            ).fetchall()
            feedback_count = connection.execute(
                "SELECT COUNT(*) AS count FROM performance_feedback"
            ).fetchone()["count"]
        return {
            "ok": True,
            "command": "analytics-summary",
            "filters": {"since": since, "until": until, "lane": lane, "stage": stage},
            "totals": dict(totals),
            "groups": [dict(row) for row in groups],
            "outbox": {row["status"]: row["count"] for row in outbox_counts},
            "performance_feedback_snapshots": feedback_count,
        }

    @staticmethod
    def _feedback_row(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "outbox_id": row["outbox_id"],
            "job_id": row["job_id"],
            "render_sha256": row["render_sha256"],
            "platform": row["platform"],
            "account_id": row["account_id"],
            "remote_id": row["remote_id"],
            "captured_at": row["captured_at"],
            "age_hours": row["age_hours"],
            "metrics": json.loads(row["metrics_json"]),
            "policy_events": json.loads(row["policy_events_json"]),
            "production": json.loads(row["production_json"])
            if row["production_json"]
            else None,
            "source_file": row["source_file"],
            "source_digest": row["source_digest"],
            "imported_at": row["imported_at"],
        }
        if "lane" in row.keys():
            result["cohort"] = {
                "lane": row["lane"],
                "duration_seconds": row["duration_seconds"],
                "duration_band": row["duration_band"],
                "canonical_snapshot_hour": row["canonical_snapshot_hour"],
            }
        return result

    def import_feedback(
        self,
        source: str | Path,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        path = Path(source).expanduser().resolve()
        try:
            source_bytes = path.read_bytes()
        except OSError as exc:
            raise ValidationError(f"cannot read feedback file {path}: {exc}") from exc
        payload = load_json_file(path)
        entries = payload.get("snapshots") if isinstance(payload, Mapping) else payload
        if not isinstance(entries, list) or not entries:
            raise ValidationError("feedback JSON must be an array or contain a non-empty snapshots array")
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        normalized: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise ValidationError(f"snapshots[{index}] must be a JSON object")
            if set(entry) != {"outbox_id", "render_sha256", "cohort", "snapshot"}:
                raise ValidationError(
                    f"snapshots[{index}] must contain only outbox_id, render_sha256, cohort, and snapshot"
                )
            outbox_id = _safe_id(entry.get("outbox_id"), f"snapshots[{index}].outbox_id")
            render_sha = _sha256(
                entry.get("render_sha256"), f"snapshots[{index}].render_sha256"
            )
            cohort = entry.get("cohort")
            if not isinstance(cohort, Mapping) or set(cohort) != {
                "lane",
                "duration_seconds",
            }:
                raise ValidationError(
                    f"snapshots[{index}].cohort must contain only lane and duration_seconds"
                )
            lane = _safe_id(cohort.get("lane"), f"snapshots[{index}].cohort.lane")
            duration_seconds = _number(
                cohort.get("duration_seconds"),
                f"snapshots[{index}].cohort.duration_seconds",
                minimum=0.001,
            )
            snapshot = entry.get("snapshot")
            if not isinstance(snapshot, Mapping):
                raise ValidationError(f"snapshots[{index}].snapshot must be a JSON object")
            snapshot = dict(snapshot)
            validate_artifact("metrics_snapshot", snapshot)
            snapshot["captured_at"] = _datetime(
                snapshot["captured_at"], f"snapshots[{index}].snapshot.captured_at"
            )
            _reject_secrets(snapshot, f"snapshots[{index}].snapshot")
            canonical_hour = _canonical_snapshot_hour(float(snapshot["age_hours"]))
            normalized.append(
                {
                    "outbox_id": outbox_id,
                    "render_sha256": render_sha,
                    "cohort": {
                        "lane": lane,
                        "duration_seconds": duration_seconds,
                        "duration_band": _duration_band(duration_seconds),
                        "canonical_snapshot_hour": canonical_hour,
                    },
                    "snapshot": snapshot,
                }
            )
        request = {"source_digest": source_digest, "snapshots": normalized}
        timestamp = _utc_now(now)
        with closing(self._connection()) as connection:
            replay, request_hash = self._operation_replay(
                connection,
                key=idempotency_key,
                command="feedback_import",
                request=request,
            )
            if replay is not None:
                connection.rollback()
                return replay
            created_ids: list[str] = []
            existing_ids: list[str] = []
            for index, entry in enumerate(normalized):
                snapshot = entry["snapshot"]
                outbox = connection.execute(
                    "SELECT * FROM publish_outbox WHERE id = ?", (entry["outbox_id"],)
                ).fetchone()
                if outbox is None:
                    raise NotFoundError(f"outbox item {entry['outbox_id']!r} not found")
                if outbox["status"] != "published":
                    raise ValidationError(
                        f"snapshots[{index}] requires a published outbox item"
                    )
                expected = {
                    "job_id": outbox["job_id"],
                    "platform": outbox["platform"],
                    "remote_id": outbox["remote_id"],
                }
                for field, value in expected.items():
                    if snapshot[field] != value:
                        raise ValidationError(
                            f"snapshots[{index}].snapshot.{field} does not match outbox"
                        )
                if entry["render_sha256"] != outbox["render_sha256"]:
                    raise ValidationError(
                        f"snapshots[{index}].render_sha256 does not match outbox"
                    )
                identity = canonical_json(
                    {"outbox_id": outbox["id"], "captured_at": snapshot["captured_at"]}
                )
                feedback_id = f"fb_{digest_text(identity)[:24]}"
                existing = connection.execute(
                    "SELECT * FROM performance_feedback WHERE id = ?", (feedback_id,)
                ).fetchone()
                expected_payload = {
                    "outbox_id": outbox["id"],
                    "job_id": outbox["job_id"],
                    "render_sha256": outbox["render_sha256"],
                    "platform": outbox["platform"],
                    "account_id": outbox["account_id"],
                    "remote_id": outbox["remote_id"],
                    "captured_at": snapshot["captured_at"],
                    "age_hours": float(snapshot["age_hours"]),
                    "metrics_json": canonical_json(snapshot["metrics"]),
                    "policy_events_json": canonical_json(snapshot.get("policy_events", [])),
                    "production_json": canonical_json(snapshot["production"])
                    if snapshot.get("production") is not None
                    else None,
                }
                expected_dimensions = entry["cohort"]
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO performance_feedback (
                            id, outbox_id, job_id, render_sha256, platform, account_id,
                            remote_id, captured_at, age_hours, metrics_json,
                            policy_events_json, production_json, source_file,
                            source_digest, imported_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            feedback_id,
                            expected_payload["outbox_id"],
                            expected_payload["job_id"],
                            expected_payload["render_sha256"],
                            expected_payload["platform"],
                            expected_payload["account_id"],
                            expected_payload["remote_id"],
                            expected_payload["captured_at"],
                            expected_payload["age_hours"],
                            expected_payload["metrics_json"],
                            expected_payload["policy_events_json"],
                            expected_payload["production_json"],
                            str(path),
                            source_digest,
                            timestamp,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO performance_feedback_dimensions (
                            feedback_id, lane, duration_seconds, duration_band,
                            canonical_snapshot_hour
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            feedback_id,
                            expected_dimensions["lane"],
                            expected_dimensions["duration_seconds"],
                            expected_dimensions["duration_band"],
                            expected_dimensions["canonical_snapshot_hour"],
                        ),
                    )
                    created_ids.append(feedback_id)
                else:
                    for field, expected_value in expected_payload.items():
                        if existing[field] != expected_value:
                            raise ValidationError(
                                f"feedback for outbox {outbox['id']!r} at "
                                f"{snapshot['captured_at']} already differs in {field}"
                            )
                    dimensions = connection.execute(
                        """
                        SELECT * FROM performance_feedback_dimensions
                        WHERE feedback_id = ?
                        """,
                        (feedback_id,),
                    ).fetchone()
                    if dimensions is None:
                        connection.execute(
                            """
                            INSERT INTO performance_feedback_dimensions (
                                feedback_id, lane, duration_seconds, duration_band,
                                canonical_snapshot_hour
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                feedback_id,
                                expected_dimensions["lane"],
                                expected_dimensions["duration_seconds"],
                                expected_dimensions["duration_band"],
                                expected_dimensions["canonical_snapshot_hour"],
                            ),
                        )
                    else:
                        for field, expected_value in expected_dimensions.items():
                            if dimensions[field] != expected_value:
                                raise ValidationError(
                                    f"feedback {feedback_id!r} already differs in cohort.{field}"
                                )
                    existing_ids.append(feedback_id)
            response = {
                "ok": True,
                "command": "feedback-import",
                "created": len(created_ids),
                "existing": len(existing_ids),
                "feedback_ids": created_ids + existing_ids,
                "source_digest": source_digest,
                "checksum_attribution_verified": True,
            }
            self._store_operation(
                connection,
                key=idempotency_key,
                command="feedback_import",
                request_hash=request_hash,
                response=response,
                now=timestamp,
            )
            connection.commit()
            return response

    def list_feedback(
        self,
        *,
        job_id: str | None = None,
        platform: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if job_id is not None:
            job_id = _safe_id(job_id, "job_id")
        if platform is not None:
            platform = _safe_id(platform, "platform")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationError("limit must be an integer from 1 to 1000")
        self.db.initialize()
        query = """
            SELECT feedback.*, dimensions.lane, dimensions.duration_seconds,
                   dimensions.duration_band, dimensions.canonical_snapshot_hour
            FROM performance_feedback AS feedback
            JOIN performance_feedback_dimensions AS dimensions
              ON dimensions.feedback_id = feedback.id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if job_id is not None:
            query += " AND feedback.job_id = ?"
            params.append(job_id)
        if platform is not None:
            query += " AND feedback.platform = ?"
            params.append(platform)
        query += " ORDER BY feedback.captured_at DESC, feedback.id DESC LIMIT ?"
        params.append(limit)
        with closing(self.db.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return {
            "ok": True,
            "command": "feedback-list",
            "items": [self._feedback_row(row) for row in rows],
        }

    @staticmethod
    def _performance_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        metrics = json.loads(row["metrics_json"])
        snapshot = dict(metrics)
        snapshot["policy_events"] = json.loads(row["policy_events_json"])
        return snapshot

    @staticmethod
    def _snapshot_usable(snapshot: Mapping[str, Any]) -> bool:
        engaged = snapshot.get("engaged_views")
        hook = snapshot.get("stayed_to_watch_rate")
        hold = snapshot.get("average_percentage_viewed")
        completion = snapshot.get("completion_rate")
        return (
            isinstance(engaged, int)
            and not isinstance(engaged, bool)
            and engaged >= 1
            and isinstance(hook, (int, float))
            and not isinstance(hook, bool)
            and (hold is not None or completion is not None)
        )

    @staticmethod
    def _recommendation_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "outbox_id": row["outbox_id"],
            "candidate_feedback_id": row["candidate_feedback_id"],
            "policy_version": row["policy_version"],
            "lane": row["lane"],
            "platform": row["platform"],
            "account_id": row["account_id"],
            "duration_band": row["duration_band"],
            "canonical_snapshot_hour": row["canonical_snapshot_hour"],
            "cohort_feedback_ids": json.loads(row["cohort_feedback_ids_json"]),
            "evaluation": json.loads(row["evaluation_json"]),
            "recommendations": json.loads(row["recommendations_json"]),
            "created_at": row["created_at"],
        }

    def evaluate_editorial_feedback(
        self,
        outbox_id: str,
        *,
        minimum_cohort: int = 5,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Build a comparable cohort and persist bounded editorial-only guidance."""

        outbox_id = _safe_id(outbox_id, "outbox_id")
        if (
            isinstance(minimum_cohort, bool)
            or not isinstance(minimum_cohort, int)
            or not 5 <= minimum_cohort <= 50
        ):
            raise ValidationError("minimum_cohort must be an integer from 5 to 50")
        request = {
            "outbox_id": outbox_id,
            "minimum_cohort": minimum_cohort,
            "policy_version": EDITORIAL_POLICY_VERSION,
        }
        timestamp = _utc_now(now)
        with closing(self._connection()) as connection:
            replay, request_hash = self._operation_replay(
                connection,
                key=idempotency_key,
                command="feedback_evaluate",
                request=request,
            )
            if replay is not None:
                connection.rollback()
                return replay
            outbox = connection.execute(
                "SELECT status FROM publish_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            if outbox is None:
                raise NotFoundError(f"outbox item {outbox_id!r} not found")
            if outbox["status"] != "published":
                raise ValidationError("editorial feedback requires a published outbox item")
            candidate_rows = connection.execute(
                """
                SELECT feedback.*, dimensions.lane, dimensions.duration_seconds,
                       dimensions.duration_band, dimensions.canonical_snapshot_hour
                FROM performance_feedback AS feedback
                JOIN performance_feedback_dimensions AS dimensions
                  ON dimensions.feedback_id = feedback.id
                WHERE feedback.outbox_id = ?
                ORDER BY feedback.captured_at DESC, feedback.id DESC
                """,
                (outbox_id,),
            ).fetchall()
            if not candidate_rows:
                raise NotFoundError(
                    f"no cohort-attributed feedback exists for outbox {outbox_id!r}"
                )
            available_hours = {row["canonical_snapshot_hour"] for row in candidate_rows}
            selected_hour = min(
                available_hours,
                key=lambda hour: (abs(hour - 72), hour),
            )
            candidate = next(
                row
                for row in candidate_rows
                if row["canonical_snapshot_hour"] == selected_hour
            )
            candidate_snapshot = self._performance_snapshot(candidate)
            if not self._snapshot_usable(candidate_snapshot):
                raise ValidationError(
                    "candidate snapshot lacks engaged_views, hook, or hold metrics"
                )
            candidate_time = datetime.fromisoformat(
                candidate["captured_at"].replace("Z", "+00:00")
            )
            earliest = (candidate_time - timedelta(days=90)).astimezone(UTC).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            cohort_rows = connection.execute(
                """
                SELECT feedback.*, dimensions.lane, dimensions.duration_seconds,
                       dimensions.duration_band, dimensions.canonical_snapshot_hour
                FROM performance_feedback AS feedback
                JOIN performance_feedback_dimensions AS dimensions
                  ON dimensions.feedback_id = feedback.id
                WHERE feedback.outbox_id != ?
                  AND feedback.platform = ?
                  AND feedback.account_id = ?
                  AND dimensions.lane = ?
                  AND dimensions.duration_band = ?
                  AND dimensions.canonical_snapshot_hour = ?
                  AND feedback.captured_at >= ?
                  AND feedback.captured_at <= ?
                ORDER BY feedback.outbox_id, feedback.captured_at DESC, feedback.id DESC
                """,
                (
                    outbox_id,
                    candidate["platform"],
                    candidate["account_id"],
                    candidate["lane"],
                    candidate["duration_band"],
                    selected_hour,
                    earliest,
                    candidate["captured_at"],
                ),
            ).fetchall()
            latest_by_outbox: dict[str, sqlite3.Row] = {}
            for row in cohort_rows:
                latest_by_outbox.setdefault(row["outbox_id"], row)
            usable_rows: list[sqlite3.Row] = []
            excluded_ids: list[str] = []
            for row in latest_by_outbox.values():
                if self._snapshot_usable(self._performance_snapshot(row)):
                    usable_rows.append(row)
                else:
                    excluded_ids.append(row["id"])
            usable_rows.sort(key=lambda row: (row["outbox_id"], row["id"]))
            cohort_feedback_ids = [row["id"] for row in usable_rows]
            evaluation = evaluate_performance(
                candidate_snapshot,
                [self._performance_snapshot(row) for row in usable_rows],
                minimum_cohort=minimum_cohort,
            )
            evaluation = {
                **evaluation,
                "candidate_feedback_id": candidate["id"],
                "requested_snapshot_hour": 72,
                "selected_canonical_snapshot_hour": selected_hour,
                "snapshot_resolution": "exact_72h" if selected_hour == 72 else "nearest_canonical",
                "cohort_count": len(usable_rows),
                "excluded_unusable_feedback_ids": sorted(excluded_ids),
                "cohort_dimensions": {
                    "lane": candidate["lane"],
                    "platform": candidate["platform"],
                    "account_id": candidate["account_id"],
                    "duration_band": candidate["duration_band"],
                    "maximum_age_days": 90,
                },
            }
            recommendations = _bounded_recommendations(evaluation)
            evaluation_key = digest_text(
                canonical_json(
                    {
                        "candidate_feedback_id": candidate["id"],
                        "cohort_feedback_ids": cohort_feedback_ids,
                        "minimum_cohort": minimum_cohort,
                        "policy_version": EDITORIAL_POLICY_VERSION,
                    }
                )
            )
            recommendation_id = f"rec_{evaluation_key[:24]}"
            row = connection.execute(
                "SELECT * FROM editorial_recommendations WHERE id = ?",
                (recommendation_id,),
            ).fetchone()
            created = row is None
            if row is None:
                connection.execute(
                    """
                    INSERT INTO editorial_recommendations (
                        id, outbox_id, candidate_feedback_id, policy_version,
                        evaluation_key_sha256, lane, platform, account_id,
                        duration_band, canonical_snapshot_hour,
                        cohort_feedback_ids_json, evaluation_json,
                        recommendations_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recommendation_id,
                        outbox_id,
                        candidate["id"],
                        EDITORIAL_POLICY_VERSION,
                        evaluation_key,
                        candidate["lane"],
                        candidate["platform"],
                        candidate["account_id"],
                        candidate["duration_band"],
                        selected_hour,
                        canonical_json(cohort_feedback_ids),
                        canonical_json(evaluation),
                        canonical_json(recommendations),
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM editorial_recommendations WHERE id = ?",
                    (recommendation_id,),
                ).fetchone()
            response = {
                "ok": True,
                "command": "feedback-evaluate",
                "created": created,
                "external_send_performed": False,
                "automatic_mutation_performed": False,
                "recommendation": self._recommendation_row(row),
            }
            self._store_operation(
                connection,
                key=idempotency_key,
                command="feedback_evaluate",
                request_hash=request_hash,
                response=response,
                now=timestamp,
            )
            connection.commit()
            return response

    def list_recommendations(
        self,
        *,
        outbox_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if outbox_id is not None:
            outbox_id = _safe_id(outbox_id, "outbox_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationError("limit must be an integer from 1 to 1000")
        self.db.initialize()
        query = "SELECT * FROM editorial_recommendations WHERE 1 = 1"
        params: list[Any] = []
        if outbox_id is not None:
            query += " AND outbox_id = ?"
            params.append(outbox_id)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with closing(self.db.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return {
            "ok": True,
            "command": "recommendations-list",
            "items": [self._recommendation_row(row) for row in rows],
        }
