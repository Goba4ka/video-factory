"""Materialize ready final-review handoffs without approving or publishing.

This process is safe to run from a timer.  It discovers only queued
``final_review`` tasks whose direct QC dependency succeeded and delegates every
checksum decision to :class:`ReviewReleaseBridge`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .errors import FactoryError, NotFoundError, ValidationError
from .review_release_bridge import ReviewReleaseBridge
from .validators import canonical_json


BridgeFactory = Callable[[str | Path, str | Path], ReviewReleaseBridge]


def materialize_pending(
    db_path: str | Path,
    outbox_root: str | Path,
    *,
    limit: int = 100,
    bridge_factory: BridgeFactory = ReviewReleaseBridge,
) -> dict[str, Any]:
    database = Path(db_path).expanduser().resolve()
    output = Path(outbox_root).expanduser().resolve()
    if not database.is_file():
        raise NotFoundError("queue database does not exist")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValidationError("review reconciler limit must be from 1 to 1000")
    database_uri = database.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            """
            SELECT review.id
            FROM tasks AS review
            JOIN tasks AS dependency ON dependency.id = review.dependency_task_id
            WHERE review.role = 'final_review'
              AND review.kind = 'final_review_job'
              AND review.status = 'queued'
              AND review.result_json IS NULL
              AND dependency.role = 'qc'
              AND dependency.status = 'succeeded'
              AND dependency.result_json IS NOT NULL
            ORDER BY review.created_at ASC, review.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    bridge = bridge_factory(database, output)
    materialized: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for row in rows:
        task_id = str(row["id"])
        try:
            result = bridge.materialize(task_id)
        except (FactoryError, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            failures.append(
                {
                    "final_review_task_id": task_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        else:
            materialized.append(
                {
                    "final_review_task_id": task_id,
                    "bundle_id": result["bundle_id"],
                    "event_id": result["event_id"],
                    "created": bool(result["created"]),
                    "status": result["status"],
                }
            )
    return {
        "ok": not failures,
        "command": "reconcile-final-review",
        "candidate_count": len(rows),
        "materialized_count": len(materialized),
        "failure_count": len(failures),
        "materialized": materialized,
        "failures": failures,
        "automatic_approval": False,
        "publish_outbox_created": False,
        "external_send_performed": False,
    }


def main() -> int:
    try:
        database = os.environ.get("VIDEO_FACTORY_DB")
        outbox = os.environ.get("VIDEO_FACTORY_REVIEW_OUTBOX_ROOT")
        if not database:
            raise ValidationError("VIDEO_FACTORY_DB must be configured")
        if not outbox:
            raise ValidationError("VIDEO_FACTORY_REVIEW_OUTBOX_ROOT must be configured")
        raw_limit = os.environ.get("VIDEO_FACTORY_REVIEW_RECONCILE_LIMIT", "100")
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise ValidationError(
                "VIDEO_FACTORY_REVIEW_RECONCILE_LIMIT must be an integer"
            ) from exc
        result = materialize_pending(database, outbox, limit=limit)
    except (
        FactoryError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
    ) as exc:
        sys.stderr.write(
            f"review_release_reconciler_error:{type(exc).__name__}:{exc}\n"
        )
        return 2
    sys.stdout.write(canonical_json(result) + "\n")
    sys.stdout.flush()
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
