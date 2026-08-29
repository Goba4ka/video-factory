from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import SCHEMA_VERSION, Database
from .errors import (
    IdeaConflictError,
    IdempotencyConflictError,
    NotFoundError,
    StateTransitionError,
    ValidationError,
)
from .state import APPROVED_OR_LATER, IdeaState, JobState, ensure_job_transition
from .validators import (
    canonical_json,
    digest_text,
    load_ideas,
    normalize_idea,
    require_nonempty_string,
    validate_batch_size,
    validate_qc_evidence,
    validate_rights_evidence,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_or_none(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


class Factory:
    def __init__(self, db_path: str | Path):
        self.db = Database(db_path)

    def init(self) -> dict[str, Any]:
        existed = self.db.path.exists()
        self.db.initialize()
        return {
            "ok": True,
            "command": "init",
            "database": str(self.db.path),
            "schema_version": SCHEMA_VERSION,
            "created": not existed,
        }

    def _connection(self) -> sqlite3.Connection:
        self.db.initialize()
        connection = self.db.connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        entity_type: str,
        entity_id: str,
        action: str,
        from_state: str | None,
        to_state: str | None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit (
                entity_type, entity_id, action, from_state, to_state,
                metadata_json, idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                entity_id,
                action,
                from_state,
                to_state,
                canonical_json(metadata or {}),
                idempotency_key,
                utc_now(),
            ),
        )

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
    ) -> None:
        connection.execute(
            """
            INSERT INTO operations (
                idempotency_key, command, request_hash, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (key, command, request_hash, canonical_json(response), utc_now()),
        )

    @staticmethod
    def _idea_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "topic": row["topic"],
            "summary": row["summary"],
            "payload": json.loads(row["payload_json"]),
            "source_file": row["source_file"],
            "source_digest": row["source_digest"],
            "source_index": row["source_index"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "idea_id": row["idea_id"],
            "batch_id": row["batch_id"],
            "state": row["state"],
            "rights_status": row["rights_status"],
            "qc_status": row["qc_status"],
            "rights_evidence": _json_or_none(row["rights_json"]),
            "qc_evidence": _json_or_none(row["qc_json"]),
            "rejection_reason": row["rejection_reason"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _audit_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "action": row["action"],
            "from_state": row["from_state"],
            "to_state": row["to_state"],
            "metadata": json.loads(row["metadata_json"]),
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _resolve_job(connection: sqlite3.Connection, target: str) -> sqlite3.Row:
        target = require_nonempty_string(target, "target")
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (target,)).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT * FROM jobs WHERE idea_id = ?", (target,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"no job found for target {target!r}")
        return row

    def start(
        self,
        ideas_file: str | Path,
        *,
        batch_size: int = 5,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        batch_size = validate_batch_size(batch_size)
        source = Path(ideas_file).expanduser().resolve()
        ideas = load_ideas(source)
        source_bytes = source.read_bytes()
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        key = idempotency_key or f"start:{source_digest}:{batch_size}"
        request = {"source_digest": source_digest, "batch_size": batch_size}

        connection = self._connection()
        try:
            replay, request_hash = self._operation_replay(
                connection, key=key, command="start", request=request
            )
            if replay is not None:
                connection.rollback()
                return replay

            imported = 0
            existing = 0
            now = utc_now()
            for index, raw_idea in enumerate(ideas):
                idea = normalize_idea(raw_idea)
                row = connection.execute(
                    "SELECT payload_json FROM ideas WHERE id = ?", (idea["id"],)
                ).fetchone()
                if row is not None:
                    if row["payload_json"] != idea["payload_json"]:
                        raise IdeaConflictError(
                            f"idea id {idea['id']!r} already exists with a different payload"
                        )
                    existing += 1
                    continue
                connection.execute(
                    """
                    INSERT INTO ideas (
                        id, title, topic, summary, payload_json, source_file,
                        source_digest, source_index, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idea["id"],
                        idea["title"],
                        idea["topic"],
                        idea["summary"],
                        idea["payload_json"],
                        str(source),
                        source_digest,
                        index,
                        IdeaState.CANDIDATE.value,
                        now,
                        now,
                    ),
                )
                self._audit(
                    connection,
                    entity_type="idea",
                    entity_id=idea["id"],
                    action="import",
                    from_state=None,
                    to_state=IdeaState.CANDIDATE.value,
                    metadata={"source_digest": source_digest, "source_index": index},
                    idempotency_key=key,
                )
                imported += 1

            candidate_rows = connection.execute(
                """
                SELECT * FROM ideas
                WHERE status = ? AND source_digest = ?
                ORDER BY source_index, rowid
                LIMIT ?
                """,
                (IdeaState.CANDIDATE.value, source_digest, batch_size),
            ).fetchall()
            batch_id = f"batch_{digest_text(key + ':' + request_hash)[:20]}"
            jobs: list[dict[str, Any]] = []
            for idea_row in candidate_rows:
                job_id = f"job_{digest_text(batch_id + ':' + idea_row['id'])[:20]}"
                connection.execute(
                    """
                    INSERT INTO jobs (
                        id, idea_id, batch_id, state, rights_status, qc_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 'pending', ?, ?)
                    """,
                    (
                        job_id,
                        idea_row["id"],
                        batch_id,
                        JobState.REVIEW_PENDING.value,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE ideas SET status = ?, updated_at = ? WHERE id = ?",
                    (IdeaState.IN_REVIEW.value, now, idea_row["id"]),
                )
                self._audit(
                    connection,
                    entity_type="idea",
                    entity_id=idea_row["id"],
                    action="enqueue_review",
                    from_state=IdeaState.CANDIDATE.value,
                    to_state=IdeaState.IN_REVIEW.value,
                    metadata={"batch_id": batch_id, "job_id": job_id},
                    idempotency_key=key,
                )
                self._audit(
                    connection,
                    entity_type="job",
                    entity_id=job_id,
                    action="create",
                    from_state=None,
                    to_state=JobState.REVIEW_PENDING.value,
                    metadata={"batch_id": batch_id, "idea_id": idea_row["id"]},
                    idempotency_key=key,
                )
                jobs.append(
                    {
                        "id": job_id,
                        "idea_id": idea_row["id"],
                        "title": idea_row["title"],
                        "topic": idea_row["topic"],
                        "state": JobState.REVIEW_PENDING.value,
                    }
                )

            response = {
                "ok": True,
                "command": "start",
                "database": str(self.db.path),
                "source_file": str(source),
                "source_digest": source_digest,
                "batch_id": batch_id,
                "batch_size": len(jobs),
                "imported_ideas": imported,
                "existing_ideas": existing,
                "jobs": jobs,
            }
            self._store_operation(
                connection,
                key=key,
                command="start",
                request_hash=request_hash,
                response=response,
            )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def approve(
        self, target: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        connection = self._connection()
        try:
            job = self._resolve_job(connection, target)
            key = idempotency_key or f"approve:{job['id']}"
            request = {"job_id": job["id"]}
            replay, request_hash = self._operation_replay(
                connection, key=key, command="approve", request=request
            )
            if replay is not None:
                connection.rollback()
                return replay

            current = JobState(job["state"])
            changed = False
            if current is JobState.REVIEW_PENDING:
                ensure_job_transition(current.value, JobState.APPROVED)
                now = utc_now()
                connection.execute(
                    "UPDATE jobs SET state = ?, version = version + 1, updated_at = ? WHERE id = ?",
                    (JobState.APPROVED.value, now, job["id"]),
                )
                connection.execute(
                    "UPDATE ideas SET status = ?, updated_at = ? WHERE id = ?",
                    (IdeaState.APPROVED.value, now, job["idea_id"]),
                )
                self._audit(
                    connection,
                    entity_type="job",
                    entity_id=job["id"],
                    action="approve",
                    from_state=current.value,
                    to_state=JobState.APPROVED.value,
                    idempotency_key=key,
                )
                self._audit(
                    connection,
                    entity_type="idea",
                    entity_id=job["idea_id"],
                    action="approve",
                    from_state=IdeaState.IN_REVIEW.value,
                    to_state=IdeaState.APPROVED.value,
                    metadata={"job_id": job["id"]},
                    idempotency_key=key,
                )
                changed = True
            elif current not in APPROVED_OR_LATER:
                raise StateTransitionError(f"cannot approve job in state {current.value!r}")

            fresh = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job["id"],)
            ).fetchone()
            response = {
                "ok": True,
                "command": "approve",
                "changed": changed,
                "job": self._job_dict(fresh),
            }
            self._store_operation(
                connection,
                key=key,
                command="approve",
                request_hash=request_hash,
                response=response,
            )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reject(
        self,
        target: str,
        *,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        reason = require_nonempty_string(reason, "reason")
        connection = self._connection()
        try:
            job = self._resolve_job(connection, target)
            key = idempotency_key or f"reject:{job['id']}"
            request = {"job_id": job["id"], "reason": reason}
            replay, request_hash = self._operation_replay(
                connection, key=key, command="reject", request=request
            )
            if replay is not None:
                connection.rollback()
                return replay

            current = JobState(job["state"])
            changed = False
            if current is JobState.REVIEW_PENDING:
                ensure_job_transition(current.value, JobState.REJECTED)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = ?, rejection_reason = ?, version = version + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (JobState.REJECTED.value, reason, now, job["id"]),
                )
                connection.execute(
                    "UPDATE ideas SET status = ?, updated_at = ? WHERE id = ?",
                    (IdeaState.REJECTED.value, now, job["idea_id"]),
                )
                self._audit(
                    connection,
                    entity_type="job",
                    entity_id=job["id"],
                    action="reject",
                    from_state=current.value,
                    to_state=JobState.REJECTED.value,
                    metadata={"reason": reason},
                    idempotency_key=key,
                )
                self._audit(
                    connection,
                    entity_type="idea",
                    entity_id=job["idea_id"],
                    action="reject",
                    from_state=IdeaState.IN_REVIEW.value,
                    to_state=IdeaState.REJECTED.value,
                    metadata={"job_id": job["id"], "reason": reason},
                    idempotency_key=key,
                )
                changed = True
            elif current is not JobState.REJECTED:
                raise StateTransitionError(f"cannot reject job in state {current.value!r}")

            fresh = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job["id"],)
            ).fetchone()
            response = {
                "ok": True,
                "command": "reject",
                "changed": changed,
                "job": self._job_dict(fresh),
            }
            self._store_operation(
                connection,
                key=key,
                command="reject",
                request_hash=request_hash,
                response=response,
            )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def next(
        self,
        target: str,
        *,
        idempotency_key: str,
        gate_result: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = require_nonempty_string(idempotency_key, "idempotency_key")
        connection = self._connection()
        try:
            job = self._resolve_job(connection, target)
            request = {
                "job_id": job["id"],
                "gate_result": gate_result,
                "evidence": evidence,
            }
            replay, request_hash = self._operation_replay(
                connection, key=key, command="next", request=request
            )
            if replay is not None:
                connection.rollback()
                return replay

            current = JobState(job["state"])
            rights_status = job["rights_status"]
            qc_status = job["qc_status"]
            rights_json = job["rights_json"]
            qc_json = job["qc_json"]

            if current is JobState.APPROVED:
                self._require_no_gate_payload(gate_result, evidence, current)
                target_state = JobState.RIGHTS_PENDING
            elif current is JobState.RIGHTS_PENDING:
                validated = validate_rights_evidence(gate_result, evidence)
                rights_json = canonical_json(validated)
                if gate_result == "pass":
                    target_state = JobState.PRODUCTION_PENDING
                    rights_status = "passed"
                else:
                    target_state = JobState.RIGHTS_FAILED
                    rights_status = "failed"
            elif current is JobState.RIGHTS_FAILED:
                self._require_no_gate_payload(gate_result, evidence, current)
                target_state = JobState.RIGHTS_PENDING
                rights_status = "pending"
                rights_json = None
            elif current is JobState.PRODUCTION_PENDING:
                self._require_no_gate_payload(gate_result, evidence, current)
                target_state = JobState.QC_PENDING
            elif current is JobState.QC_PENDING:
                validated = validate_qc_evidence(gate_result, evidence)
                qc_json = canonical_json(validated)
                if gate_result == "pass":
                    target_state = JobState.READY
                    qc_status = "passed"
                else:
                    target_state = JobState.QC_FAILED
                    qc_status = "failed"
            elif current is JobState.QC_FAILED:
                self._require_no_gate_payload(gate_result, evidence, current)
                target_state = JobState.QC_PENDING
                qc_status = "pending"
                qc_json = None
            else:
                raise StateTransitionError(
                    f"next is not allowed for job in state {current.value!r}"
                )

            ensure_job_transition(current.value, target_state)
            now = utc_now()
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, rights_status = ?, qc_status = ?, rights_json = ?,
                    qc_json = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    target_state.value,
                    rights_status,
                    qc_status,
                    rights_json,
                    qc_json,
                    now,
                    job["id"],
                ),
            )
            if target_state is JobState.RIGHTS_PENDING:
                idea_state = IdeaState.APPROVED
            elif target_state is JobState.READY:
                idea_state = IdeaState.READY
            else:
                idea_state = IdeaState.PROCESSING
            connection.execute(
                "UPDATE ideas SET status = ?, updated_at = ? WHERE id = ?",
                (idea_state.value, now, job["idea_id"]),
            )
            audit_metadata: dict[str, Any] = {}
            if gate_result is not None:
                audit_metadata = {"gate_result": gate_result, "evidence": evidence}
            self._audit(
                connection,
                entity_type="job",
                entity_id=job["id"],
                action="next",
                from_state=current.value,
                to_state=target_state.value,
                metadata=audit_metadata,
                idempotency_key=key,
            )

            fresh = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job["id"],)
            ).fetchone()
            response = {
                "ok": True,
                "command": "next",
                "from_state": current.value,
                "to_state": target_state.value,
                "job": self._job_dict(fresh),
            }
            self._store_operation(
                connection,
                key=key,
                command="next",
                request_hash=request_hash,
                response=response,
            )
            connection.commit()
            return response
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _require_no_gate_payload(
        gate_result: str | None,
        evidence: dict[str, Any] | None,
        current: JobState,
    ) -> None:
        if gate_result is not None or evidence is not None:
            raise ValidationError(
                f"state {current.value!r} does not accept gate_result or evidence"
            )

    def list(
        self,
        *,
        entity: str = "jobs",
        state: str | None = None,
        batch_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        self.db.initialize()
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValidationError("limit must be an integer from 1 to 1000")
        if entity not in {"ideas", "jobs", "audit"}:
            raise ValidationError("entity must be one of: ideas, jobs, audit")
        with closing(self.db.connect()) as connection:
            if entity == "ideas":
                sql = "SELECT * FROM ideas"
                params: list[Any] = []
                if state:
                    sql += " WHERE status = ?"
                    params.append(state)
                sql += " ORDER BY rowid DESC LIMIT ?"
                params.append(limit)
                rows = [self._idea_dict(row) for row in connection.execute(sql, params)]
            elif entity == "jobs":
                clauses: list[str] = []
                params = []
                if state:
                    clauses.append("state = ?")
                    params.append(state)
                if batch_id:
                    clauses.append("batch_id = ?")
                    params.append(batch_id)
                sql = "SELECT * FROM jobs"
                if clauses:
                    sql += " WHERE " + " AND ".join(clauses)
                sql += " ORDER BY rowid DESC LIMIT ?"
                params.append(limit)
                rows = [self._job_dict(row) for row in connection.execute(sql, params)]
            else:
                sql = "SELECT * FROM audit"
                params = []
                if state:
                    sql += " WHERE to_state = ?"
                    params.append(state)
                sql += " ORDER BY id DESC LIMIT ?"
                params.append(limit)
                rows = [self._audit_dict(row) for row in connection.execute(sql, params)]
        return {
            "ok": True,
            "command": "list",
            "entity": entity,
            "count": len(rows),
            "items": rows,
        }

    def status(self, target: str | None = None) -> dict[str, Any]:
        self.db.initialize()
        with closing(self.db.connect()) as connection:
            if target is None:
                idea_counts = {
                    row["status"]: row["count"]
                    for row in connection.execute(
                        "SELECT status, COUNT(*) AS count FROM ideas GROUP BY status"
                    )
                }
                job_counts = {
                    row["state"]: row["count"]
                    for row in connection.execute(
                        "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
                    )
                }
                batches = [
                    {"batch_id": row["batch_id"], "count": row["count"]}
                    for row in connection.execute(
                        """
                        SELECT batch_id, COUNT(*) AS count
                        FROM jobs GROUP BY batch_id ORDER BY MIN(rowid) DESC
                        """
                    )
                ]
                return {
                    "ok": True,
                    "command": "status",
                    "database": str(self.db.path),
                    "ideas": idea_counts,
                    "jobs": job_counts,
                    "batches": batches,
                }

            target = require_nonempty_string(target, "target")
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (target,)).fetchone()
            if job is None:
                job = connection.execute(
                    "SELECT * FROM jobs WHERE idea_id = ?", (target,)
                ).fetchone()
            if job is not None:
                idea = connection.execute(
                    "SELECT * FROM ideas WHERE id = ?", (job["idea_id"],)
                ).fetchone()
                audit_rows = connection.execute(
                    """
                    SELECT * FROM audit
                    WHERE (entity_type = 'job' AND entity_id = ?)
                       OR (entity_type = 'idea' AND entity_id = ?)
                    ORDER BY id
                    """,
                    (job["id"], job["idea_id"]),
                ).fetchall()
                return {
                    "ok": True,
                    "command": "status",
                    "job": self._job_dict(job),
                    "idea": self._idea_dict(idea),
                    "audit": [self._audit_dict(row) for row in audit_rows],
                }

            idea = connection.execute("SELECT * FROM ideas WHERE id = ?", (target,)).fetchone()
            if idea is None:
                raise NotFoundError(f"no idea or job found for target {target!r}")
            audit_rows = connection.execute(
                "SELECT * FROM audit WHERE entity_type = 'idea' AND entity_id = ? ORDER BY id",
                (idea["id"],),
            ).fetchall()
            return {
                "ok": True,
                "command": "status",
                "job": None,
                "idea": self._idea_dict(idea),
                "audit": [self._audit_dict(row) for row in audit_rows],
            }

    @staticmethod
    def export_json(result: dict[str, Any], path: str | Path) -> Path:
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return destination
