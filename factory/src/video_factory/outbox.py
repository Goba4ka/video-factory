from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .db import Database
from .errors import (
    IdempotencyConflictError,
    LeaseConflictError,
    NotFoundError,
    StateTransitionError,
    ValidationError,
)
from .validators import canonical_json, digest_text, require_nonempty_string


PLATFORMS = frozenset({"youtube_shorts", "instagram_reels", "tiktok"})
VISIBILITIES = frozenset({"private", "draft", "scheduled", "public"})
OUTBOX_STATUSES = frozenset(
    {
        "pending_approval",
        "approved",
        "dispatching",
        "published",
        "failed",
        "unknown",
        "cancelled",
    }
)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
HUMAN_CONFIRMATION = "I_REVIEWED_THIS_RENDER"
_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|password|secret|token)", re.I)


def _utc_now(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValidationError("now must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO 8601 date-time") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_file(value: Any, field: str) -> Path:
    raw = require_nonempty_string(value, field)
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"{field} does not identify a readable file: {raw}") from exc
    if not path.is_file():
        raise ValidationError(f"{field} must identify a file")
    return path


def _sha256(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    if not SHA256_RE.fullmatch(text):
        raise ValidationError(f"{field} must be a lowercase sha256")
    return text


def _reject_secrets(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                raise ValidationError(f"{path}.{key} looks like a secret and cannot be persisted")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")


def _destination(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("destination must be a JSON object")
    allowed = {"platform", "account_id", "caption", "visibility", "scheduled_at"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(f"destination has unknown fields: {', '.join(unknown)}")
    platform = require_nonempty_string(value.get("platform"), "destination.platform")
    if platform not in PLATFORMS:
        raise ValidationError(f"destination.platform must be one of: {', '.join(sorted(PLATFORMS))}")
    account_id = require_nonempty_string(value.get("account_id"), "destination.account_id")
    caption = value.get("caption", "")
    if not isinstance(caption, str) or len(caption) > 2200:
        raise ValidationError("destination.caption must be a string of at most 2200 characters")
    visibility = require_nonempty_string(value.get("visibility"), "destination.visibility")
    if visibility not in VISIBILITIES:
        raise ValidationError(
            f"destination.visibility must be one of: {', '.join(sorted(VISIBILITIES))}"
        )
    scheduled_at = value.get("scheduled_at")
    if visibility == "scheduled" and scheduled_at is None:
        raise ValidationError("destination.scheduled_at is required for scheduled visibility")
    if scheduled_at is not None:
        scheduled_at = _parse_datetime(scheduled_at, "destination.scheduled_at")
    return {
        "platform": platform,
        "account_id": account_id,
        "caption": caption,
        "visibility": visibility,
        "scheduled_at": scheduled_at,
        "status": "pending",
        "remote_id": None,
    }


def _disclosures(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("disclosures must be a JSON object")
    required = {"ai_generated", "altered_or_synthetic", "paid_promotion", "notes"}
    if set(value) != required:
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - required)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown: {', '.join(unknown)}")
        raise ValidationError(f"disclosures fields are invalid ({'; '.join(details)})")
    for field in ("ai_generated", "altered_or_synthetic", "paid_promotion"):
        if not isinstance(value[field], bool):
            raise ValidationError(f"disclosures.{field} must be boolean")
    notes = value["notes"]
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        raise ValidationError("disclosures.notes must be an array of strings")
    return {
        "ai_generated": value["ai_generated"],
        "altered_or_synthetic": value["altered_or_synthetic"],
        "paid_promotion": value["paid_promotion"],
        "notes": notes,
    }


class PublishOutbox:
    """Durable checksum-bound handoff; this class never calls a platform API."""

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
    def _row(row: sqlite3.Row, *, expose_lease_token: bool = False) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "render_id": row["render_id"],
            "render_path": row["render_path"],
            "render_sha256": row["render_sha256"],
            "metadata_sha256": row["metadata_sha256"],
            "qc_report": row["qc_report"],
            "platform": row["platform"],
            "account_id": row["account_id"],
            "destination": json.loads(row["destination_json"]),
            "disclosures": json.loads(row["disclosures_json"]),
            "status": row["status"],
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
            "approval_note": row["approval_note"],
            "lease_owner": row["lease_owner"],
            "lease_token": row["lease_token"] if expose_lease_token else None,
            "lease_expires_at": row["lease_expires_at"],
            "delivery_idempotency_key": f"publish:{row['id']}:{row['render_sha256']}",
            "remote_id": row["remote_id"],
            "published_at": row["published_at"],
            "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None,
            "failure": json.loads(row["failure_json"]) if row["failure_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _verify_render(row: sqlite3.Row) -> None:
        path = _resolve_file(row["render_path"], "stored render_path")
        actual = _sha256_file(path)
        if actual != row["render_sha256"]:
            raise ValidationError(
                f"render checksum mismatch for outbox {row['id']!r}; publication remains blocked"
            )

    def create(
        self,
        request: Mapping[str, Any],
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise ValidationError("publish request must be a JSON object")
        allowed = {
            "schema_version",
            "job_id",
            "render_id",
            "render_path",
            "render_sha256",
            "qc_report",
            "destination",
            "disclosures",
        }
        unknown = sorted(set(request) - allowed)
        if unknown:
            raise ValidationError(f"publish request has unknown fields: {', '.join(unknown)}")
        if request.get("schema_version") != "1.0.0":
            raise ValidationError("publish request schema_version must equal '1.0.0'")
        job_id = require_nonempty_string(request.get("job_id"), "job_id")
        render_id = require_nonempty_string(request.get("render_id"), "render_id")
        qc_report = require_nonempty_string(request.get("qc_report"), "qc_report")
        render_path = _resolve_file(request.get("render_path"), "render_path")
        render_sha = _sha256(request.get("render_sha256"), "render_sha256")
        actual_sha = _sha256_file(render_path)
        if actual_sha != render_sha:
            raise ValidationError("render_sha256 does not match the actual render bytes")
        destination = _destination(request.get("destination"))
        disclosures = _disclosures(request.get("disclosures"))
        _reject_secrets(destination, "destination")
        _reject_secrets(disclosures, "disclosures")
        metadata_sha = digest_text(
            canonical_json({"destinations": [destination], "disclosures": disclosures})
        )
        normalized = {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "render_id": render_id,
            "render_path": str(render_path),
            "render_sha256": render_sha,
            "qc_report": qc_report,
            "destination": destination,
            "disclosures": disclosures,
        }
        timestamp = _utc_now(now)
        with closing(self._connection()) as connection:
            replay, request_hash = self._operation_replay(
                connection,
                key=idempotency_key,
                command="outbox_create",
                request=normalized,
            )
            if replay is not None:
                connection.rollback()
                return replay
            job = connection.execute(
                "SELECT state, rights_status, qc_status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise NotFoundError(f"job {job_id!r} not found")
            if not (
                job["state"] == "ready"
                and job["rights_status"] == "passed"
                and job["qc_status"] == "passed"
            ):
                raise StateTransitionError(
                    "publish outbox requires a ready job with passed rights and QC"
                )
            identity = canonical_json(
                {
                    "job_id": job_id,
                    "render_sha256": render_sha,
                    "metadata_sha256": metadata_sha,
                    "platform": destination["platform"],
                    "account_id": destination["account_id"],
                }
            )
            outbox_id = f"out_{digest_text(identity)[:24]}"
            existing = connection.execute(
                "SELECT * FROM publish_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            created = existing is None
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO publish_outbox (
                        id, job_id, render_id, render_path, render_sha256,
                        metadata_sha256, qc_report, platform, account_id,
                        destination_json, disclosures_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_approval', ?, ?)
                    """,
                    (
                        outbox_id,
                        job_id,
                        render_id,
                        str(render_path),
                        render_sha,
                        metadata_sha,
                        qc_report,
                        destination["platform"],
                        destination["account_id"],
                        canonical_json(destination),
                        canonical_json(disclosures),
                        timestamp,
                        timestamp,
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM publish_outbox WHERE id = ?", (outbox_id,)
                ).fetchone()
            response = {
                "ok": True,
                "command": "outbox-create",
                "created": created,
                "external_send_performed": False,
                "outbox": self._row(existing),
            }
            self._store_operation(
                connection,
                key=idempotency_key,
                command="outbox_create",
                request_hash=request_hash,
                response=response,
                now=timestamp,
            )
            connection.commit()
            return response

    def approve(
        self,
        outbox_id: str,
        *,
        render_sha256: str,
        metadata_sha256: str,
        approved_by: str,
        approval_note: str,
        human_confirmation: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        outbox_id = require_nonempty_string(outbox_id, "outbox_id")
        render_sha256 = _sha256(render_sha256, "render_sha256")
        metadata_sha256 = _sha256(metadata_sha256, "metadata_sha256")
        approved_by = require_nonempty_string(approved_by, "approved_by")
        approval_note = require_nonempty_string(approval_note, "approval_note")
        if human_confirmation != HUMAN_CONFIRMATION:
            raise ValidationError(
                f"human_confirmation must equal {HUMAN_CONFIRMATION!r}"
            )
        request = {
            "outbox_id": outbox_id,
            "render_sha256": render_sha256,
            "metadata_sha256": metadata_sha256,
            "approved_by": approved_by,
            "approval_note": approval_note,
            "human_confirmation": human_confirmation,
        }
        timestamp = _utc_now(now)
        with closing(self._connection()) as connection:
            replay, request_hash = self._operation_replay(
                connection,
                key=idempotency_key,
                command="outbox_approve",
                request=request,
            )
            if replay is not None:
                connection.rollback()
                return replay
            row = connection.execute(
                "SELECT * FROM publish_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"outbox item {outbox_id!r} not found")
            if row["status"] != "pending_approval":
                raise StateTransitionError(
                    f"outbox item cannot move from {row['status']!r} to 'approved'"
                )
            if render_sha256 != row["render_sha256"]:
                raise ValidationError("human approval render_sha256 does not match outbox")
            if metadata_sha256 != row["metadata_sha256"]:
                raise ValidationError("human approval metadata_sha256 does not match outbox")
            self._verify_render(row)
            connection.execute(
                """
                UPDATE publish_outbox
                SET status = 'approved', approved_by = ?, approved_at = ?,
                    approval_note = ?, updated_at = ?
                WHERE id = ?
                """,
                (approved_by, timestamp, approval_note, timestamp, outbox_id),
            )
            row = connection.execute(
                "SELECT * FROM publish_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            response = {
                "ok": True,
                "command": "outbox-approve",
                "external_send_performed": False,
                "outbox": self._row(row),
            }
            self._store_operation(
                connection,
                key=idempotency_key,
                command="outbox_approve",
                request_hash=request_hash,
                response=response,
                now=timestamp,
            )
            connection.commit()
            return response

    def claim(
        self,
        *,
        worker_id: str,
        platform: str | None,
        lease_seconds: int,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        worker_id = require_nonempty_string(worker_id, "worker_id")
        if platform is not None and platform not in PLATFORMS:
            raise ValidationError(f"platform must be one of: {', '.join(sorted(PLATFORMS))}")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise ValidationError("lease_seconds must be an integer")
        if not 30 <= lease_seconds <= 86400:
            raise ValidationError("lease_seconds must be between 30 and 86400")
        request = {
            "worker_id": worker_id,
            "platform": platform,
            "lease_seconds": lease_seconds,
        }
        instant = now or datetime.now(UTC)
        timestamp = _utc_now(instant)
        expiry = _utc_now(instant + timedelta(seconds=lease_seconds))
        with closing(self._connection()) as connection:
            replay, request_hash = self._operation_replay(
                connection,
                key=idempotency_key,
                command="outbox_claim",
                request=request,
            )
            if replay is not None:
                connection.rollback()
                return replay
            where = "status = 'approved'"
            params: list[Any] = []
            if platform is not None:
                where += " AND platform = ?"
                params.append(platform)
            row = connection.execute(
                f"SELECT * FROM publish_outbox WHERE {where} ORDER BY created_at, id LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                response = {
                    "ok": True,
                    "command": "outbox-claim",
                    "claimed": False,
                    "external_send_performed": False,
                    "outbox": None,
                }
            else:
                self._verify_render(row)
                token = secrets.token_urlsafe(32)
                connection.execute(
                    """
                    UPDATE publish_outbox
                    SET status = 'dispatching', lease_owner = ?, lease_token = ?,
                        lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'approved'
                    """,
                    (worker_id, token, expiry, timestamp, row["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM publish_outbox WHERE id = ?", (row["id"],)
                ).fetchone()
                response = {
                    "ok": True,
                    "command": "outbox-claim",
                    "claimed": True,
                    "external_send_performed": False,
                    "outbox": self._row(row, expose_lease_token=True),
                }
            self._store_operation(
                connection,
                key=idempotency_key,
                command="outbox_claim",
                request_hash=request_hash,
                response=response,
                now=timestamp,
            )
            connection.commit()
            return response

    def complete(
        self,
        outbox_id: str,
        *,
        lease_token: str,
        remote_id: str,
        receipt: Mapping[str, Any],
        idempotency_key: str,
        published_at: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        outbox_id = require_nonempty_string(outbox_id, "outbox_id")
        lease_token = require_nonempty_string(lease_token, "lease_token")
        remote_id = require_nonempty_string(remote_id, "remote_id")
        if not isinstance(receipt, Mapping):
            raise ValidationError("receipt must be a JSON object")
        receipt = dict(receipt)
        _reject_secrets(receipt, "receipt")
        published_at = _parse_datetime(
            published_at or _utc_now(now), "published_at"
        )
        request = {
            "outbox_id": outbox_id,
            "lease_token": lease_token,
            "remote_id": remote_id,
            "receipt": receipt,
            "published_at": published_at,
        }
        timestamp = _utc_now(now)
        with closing(self._connection()) as connection:
            replay, request_hash = self._operation_replay(
                connection,
                key=idempotency_key,
                command="outbox_complete",
                request=request,
            )
            if replay is not None:
                connection.rollback()
                return replay
            row = connection.execute(
                "SELECT * FROM publish_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"outbox item {outbox_id!r} not found")
            if row["status"] != "dispatching" or row["lease_token"] != lease_token:
                raise LeaseConflictError("outbox completion requires the current delivery lease token")
            if row["lease_expires_at"] <= timestamp:
                raise LeaseConflictError(
                    "outbox delivery lease expired; reconcile it instead of completing"
                )
            self._verify_render(row)
            duplicate_remote = connection.execute(
                """
                SELECT id FROM publish_outbox
                WHERE platform = ? AND account_id = ? AND remote_id = ? AND id != ?
                """,
                (row["platform"], row["account_id"], remote_id, outbox_id),
            ).fetchone()
            if duplicate_remote is not None:
                raise ValidationError(
                    f"remote_id is already attributed to outbox {duplicate_remote['id']!r}"
                )
            connection.execute(
                """
                UPDATE publish_outbox
                SET status = 'published', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, remote_id = ?, published_at = ?,
                    receipt_json = ?, failure_json = NULL, updated_at = ?
                WHERE id = ?
                """,
                (remote_id, published_at, canonical_json(receipt), timestamp, outbox_id),
            )
            row = connection.execute(
                "SELECT * FROM publish_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            response = {
                "ok": True,
                "command": "outbox-complete",
                "external_send_performed": False,
                "outbox": self._row(row),
            }
            self._store_operation(
                connection,
                key=idempotency_key,
                command="outbox_complete",
                request_hash=request_hash,
                response=response,
                now=timestamp,
            )
            connection.commit()
            return response

    def fail(
        self,
        outbox_id: str,
        *,
        lease_token: str,
        outcome: str,
        error: Mapping[str, Any],
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        outbox_id = require_nonempty_string(outbox_id, "outbox_id")
        lease_token = require_nonempty_string(lease_token, "lease_token")
        if outcome not in {"failed", "unknown"}:
            raise ValidationError("outcome must be 'failed' or 'unknown'")
        if not isinstance(error, Mapping):
            raise ValidationError("error must be a JSON object")
        error = dict(error)
        _reject_secrets(error, "error")
        request = {
            "outbox_id": outbox_id,
            "lease_token": lease_token,
            "outcome": outcome,
            "error": error,
        }
        timestamp = _utc_now(now)
        with closing(self._connection()) as connection:
            replay, request_hash = self._operation_replay(
                connection,
                key=idempotency_key,
                command="outbox_fail",
                request=request,
            )
            if replay is not None:
                connection.rollback()
                return replay
            row = connection.execute(
                "SELECT * FROM publish_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"outbox item {outbox_id!r} not found")
            if row["status"] != "dispatching" or row["lease_token"] != lease_token:
                raise LeaseConflictError("outbox failure requires the current delivery lease token")
            if row["lease_expires_at"] <= timestamp:
                raise LeaseConflictError(
                    "outbox delivery lease expired; reconcile it instead of failing"
                )
            connection.execute(
                """
                UPDATE publish_outbox
                SET status = ?, lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, failure_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (outcome, canonical_json(error), timestamp, outbox_id),
            )
            row = connection.execute(
                "SELECT * FROM publish_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            response = {
                "ok": True,
                "command": "outbox-fail",
                "automatic_retry": False,
                "external_send_performed": False,
                "outbox": self._row(row),
            }
            self._store_operation(
                connection,
                key=idempotency_key,
                command="outbox_fail",
                request_hash=request_hash,
                response=response,
                now=timestamp,
            )
            connection.commit()
            return response

    def recover_expired(
        self,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _utc_now(now)
        request = {"as_of": timestamp}
        with closing(self._connection()) as connection:
            replay, request_hash = self._operation_replay(
                connection,
                key=idempotency_key,
                command="outbox_recover_expired",
                request=request,
            )
            if replay is not None:
                connection.rollback()
                return replay
            rows = connection.execute(
                """
                SELECT id FROM publish_outbox
                WHERE status = 'dispatching' AND lease_expires_at <= ?
                ORDER BY id
                """,
                (timestamp,),
            ).fetchall()
            failure = canonical_json(
                {
                    "code": "delivery_lease_expired",
                    "message": "delivery outcome is unknown; manual reconciliation is required",
                }
            )
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"""
                    UPDATE publish_outbox
                    SET status = 'unknown', lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, failure_json = ?, updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (failure, timestamp, *ids),
                )
            response = {
                "ok": True,
                "command": "outbox-recover-expired",
                "recovered": len(ids),
                "outbox_ids": ids,
                "new_state": "unknown",
                "automatic_retry": False,
                "external_send_performed": False,
            }
            self._store_operation(
                connection,
                key=idempotency_key,
                command="outbox_recover_expired",
                request_hash=request_hash,
                response=response,
                now=timestamp,
            )
            connection.commit()
            return response

    def list(
        self,
        *,
        status: str | None = None,
        platform: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if status is not None and status not in OUTBOX_STATUSES:
            raise ValidationError(f"status must be one of: {', '.join(sorted(OUTBOX_STATUSES))}")
        if platform is not None and platform not in PLATFORMS:
            raise ValidationError(f"platform must be one of: {', '.join(sorted(PLATFORMS))}")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationError("limit must be an integer from 1 to 1000")
        self.db.initialize()
        query = "SELECT * FROM publish_outbox WHERE 1 = 1"
        params: list[Any] = []
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if platform is not None:
            query += " AND platform = ?"
            params.append(platform)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with closing(self.db.connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        items = []
        for row in rows:
            item = self._row(row)
            try:
                item["checksum_valid"] = (
                    _sha256_file(_resolve_file(row["render_path"], "stored render_path"))
                    == row["render_sha256"]
                )
            except ValidationError:
                item["checksum_valid"] = False
            items.append(item)
        return {
            "ok": True,
            "command": "outbox-list",
            "external_send_performed": False,
            "items": items,
        }
