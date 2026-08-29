from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    topic TEXT,
    summary TEXT,
    payload_json TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    source_index INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('candidate', 'in_review', 'approved', 'rejected', 'processing', 'ready')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    idea_id TEXT NOT NULL UNIQUE REFERENCES ideas(id) ON DELETE RESTRICT,
    batch_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'review_pending', 'approved', 'rejected', 'rights_pending',
            'rights_failed', 'production_pending', 'qc_pending', 'qc_failed', 'ready'
        )
    ),
    rights_status TEXT NOT NULL CHECK (rights_status IN ('pending', 'passed', 'failed')),
    qc_status TEXT NOT NULL CHECK (qc_status IN ('pending', 'passed', 'failed')),
    rights_json TEXT,
    qc_json TEXT,
    rejection_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('idea', 'job', 'system')),
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    metadata_json TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    idempotency_key TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id) ON DELETE RESTRICT,
    dependency_task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    pod TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('queued', 'leased', 'succeeded', 'dead')),
    idempotency_key TEXT NOT NULL UNIQUE,
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    retry_backoff_seconds INTEGER NOT NULL DEFAULT 60
        CHECK (retry_backoff_seconds BETWEEN 0 AND 86400),
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_token TEXT UNIQUE,
    lease_expires_at TEXT,
    result_json TEXT,
    last_error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (
        (status = 'leased' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL
             AND lease_expires_at IS NOT NULL)
        OR
        (status != 'leased' AND lease_owner IS NULL AND lease_token IS NULL
             AND lease_expires_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS task_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    worker_id TEXT NOT NULL,
    lease_token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('leased', 'succeeded', 'failed', 'expired')),
    claimed_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    finished_at TEXT,
    result_json TEXT,
    error_json TEXT,
    UNIQUE(task_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS dead_letters (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    cycle_no INTEGER NOT NULL CHECK (cycle_no >= 1),
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    cause_code TEXT NOT NULL,
    error_json TEXT NOT NULL,
    task_snapshot_json TEXT NOT NULL,
    resolution_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(task_id, cycle_no),
    CHECK (
        (status = 'open' AND resolution_json IS NULL AND resolved_at IS NULL)
        OR
        (status = 'resolved' AND resolution_json IS NOT NULL AND resolved_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS task_reworks (
    id TEXT PRIMARY KEY,
    root_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    replacement_root_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    task_mapping_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT,
    pod TEXT,
    max_leased INTEGER NOT NULL CHECK (max_leased BETWEEN 1 AND 1000),
    updated_at TEXT NOT NULL,
    CHECK (role IS NOT NULL OR pod IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS production_metrics (
    event_id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
    lane TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('succeeded', 'failed', 'cancelled')
    ),
    occurred_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL CHECK (duration_seconds >= 0),
    attempts INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 100),
    estimated_cost_usd REAL NOT NULL CHECK (estimated_cost_usd >= 0),
    cpu_seconds REAL CHECK (cpu_seconds IS NULL OR cpu_seconds >= 0),
    gpu_seconds REAL CHECK (gpu_seconds IS NULL OR gpu_seconds >= 0),
    input_bytes INTEGER CHECK (input_bytes IS NULL OR input_bytes >= 0),
    output_bytes INTEGER CHECK (output_bytes IS NULL OR output_bytes >= 0),
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_outbox (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    render_id TEXT NOT NULL,
    render_path TEXT NOT NULL,
    render_sha256 TEXT NOT NULL CHECK (
        length(render_sha256) = 64 AND render_sha256 NOT GLOB '*[^a-f0-9]*'
    ),
    metadata_sha256 TEXT NOT NULL CHECK (
        length(metadata_sha256) = 64 AND metadata_sha256 NOT GLOB '*[^a-f0-9]*'
    ),
    qc_report TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (
        platform IN ('youtube_shorts', 'instagram_reels', 'tiktok')
    ),
    account_id TEXT NOT NULL,
    destination_json TEXT NOT NULL,
    disclosures_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending_approval', 'approved', 'dispatching', 'published',
            'failed', 'unknown', 'cancelled'
        )
    ),
    approved_by TEXT,
    approved_at TEXT,
    approval_note TEXT,
    lease_owner TEXT,
    lease_token TEXT UNIQUE,
    lease_expires_at TEXT,
    remote_id TEXT,
    published_at TEXT,
    receipt_json TEXT,
    failure_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (status IN ('approved', 'dispatching', 'published', 'failed', 'unknown')
            AND approved_by IS NOT NULL AND approved_at IS NOT NULL)
        OR
        (status IN ('pending_approval', 'cancelled'))
    ),
    CHECK (
        (status = 'dispatching' AND lease_owner IS NOT NULL
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status != 'dispatching' AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    UNIQUE(job_id, render_sha256, metadata_sha256, platform, account_id)
);

CREATE TABLE IF NOT EXISTS performance_feedback (
    id TEXT PRIMARY KEY,
    outbox_id TEXT NOT NULL REFERENCES publish_outbox(id) ON DELETE RESTRICT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
    render_sha256 TEXT NOT NULL CHECK (
        length(render_sha256) = 64 AND render_sha256 NOT GLOB '*[^a-f0-9]*'
    ),
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    age_hours REAL NOT NULL CHECK (age_hours >= 0),
    metrics_json TEXT NOT NULL,
    policy_events_json TEXT NOT NULL,
    production_json TEXT,
    source_file TEXT NOT NULL,
    source_digest TEXT NOT NULL CHECK (
        length(source_digest) = 64 AND source_digest NOT GLOB '*[^a-f0-9]*'
    ),
    imported_at TEXT NOT NULL,
    UNIQUE(outbox_id, captured_at)
);

CREATE TABLE IF NOT EXISTS performance_feedback_dimensions (
    feedback_id TEXT PRIMARY KEY REFERENCES performance_feedback(id) ON DELETE RESTRICT,
    lane TEXT NOT NULL,
    duration_seconds REAL NOT NULL CHECK (duration_seconds > 0),
    duration_band TEXT NOT NULL CHECK (
        duration_band IN ('under_20', '20_34', '35_59', '60_plus')
    ),
    canonical_snapshot_hour INTEGER NOT NULL CHECK (
        canonical_snapshot_hour IN (1, 6, 24, 72, 168)
    )
);

CREATE TABLE IF NOT EXISTS editorial_recommendations (
    id TEXT PRIMARY KEY,
    outbox_id TEXT NOT NULL REFERENCES publish_outbox(id) ON DELETE RESTRICT,
    candidate_feedback_id TEXT NOT NULL
        REFERENCES performance_feedback(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL,
    evaluation_key_sha256 TEXT NOT NULL UNIQUE CHECK (
        length(evaluation_key_sha256) = 64
        AND evaluation_key_sha256 NOT GLOB '*[^a-f0-9]*'
    ),
    lane TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    duration_band TEXT NOT NULL,
    canonical_snapshot_hour INTEGER NOT NULL,
    cohort_feedback_ids_json TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    recommendations_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ideas_status ON ideas(status);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit(entity_type, entity_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_dispatch
    ON tasks(status, role, pod, available_at, priority DESC, created_at, id);
CREATE INDEX IF NOT EXISTS idx_tasks_lease_expiry ON tasks(status, lease_expires_at, id);
CREATE INDEX IF NOT EXISTS idx_tasks_dependency ON tasks(dependency_task_id, status);
CREATE INDEX IF NOT EXISTS idx_task_attempts_task ON task_attempts(task_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_dead_letters_status
    ON dead_letters(status, created_at, task_id);
CREATE INDEX IF NOT EXISTS idx_dead_letters_task
    ON dead_letters(task_id, cycle_no);
CREATE INDEX IF NOT EXISTS idx_task_reworks_root
    ON task_reworks(root_task_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_limits_scope
    ON queue_limits(IFNULL(role, ''), IFNULL(pod, ''));
CREATE INDEX IF NOT EXISTS idx_production_metrics_time
    ON production_metrics(occurred_at, lane, stage, status);
CREATE INDEX IF NOT EXISTS idx_production_metrics_job
    ON production_metrics(job_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_publish_outbox_dispatch
    ON publish_outbox(status, platform, created_at, id);
CREATE INDEX IF NOT EXISTS idx_publish_outbox_job
    ON publish_outbox(job_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_publish_outbox_remote
    ON publish_outbox(platform, account_id, remote_id)
    WHERE remote_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_performance_feedback_cohort
    ON performance_feedback(platform, account_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_performance_feedback_job
    ON performance_feedback(job_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_feedback_dimensions_cohort
    ON performance_feedback_dimensions(
        lane, duration_band, canonical_snapshot_hour, feedback_id
    );
CREATE INDEX IF NOT EXISTS idx_editorial_recommendations_outbox
    ON editorial_recommendations(outbox_id, created_at);

INSERT OR IGNORE INTO queue_limits(role, pod, max_leased, updated_at) VALUES
    ('scout', NULL, 4, '1970-01-01T00:00:00.000Z'),
    ('research', NULL, 4, '1970-01-01T00:00:00.000Z'),
    ('sensitivity_review', NULL, 2, '1970-01-01T00:00:00.000Z'),
    ('privacy_review', NULL, 2, '1970-01-01T00:00:00.000Z'),
    ('medical_review', NULL, 2, '1970-01-01T00:00:00.000Z'),
    ('rights', NULL, 2, '1970-01-01T00:00:00.000Z'),
    ('script', NULL, 3, '1970-01-01T00:00:00.000Z'),
    ('voice', NULL, 2, '1970-01-01T00:00:00.000Z'),
    ('source_audio', NULL, 2, '1970-01-01T00:00:00.000Z'),
    ('editor', NULL, 2, '1970-01-01T00:00:00.000Z'),
    ('render', NULL, 1, '1970-01-01T00:00:00.000Z'),
    ('qc', NULL, 1, '1970-01-01T00:00:00.000Z'),
    ('final_review', NULL, 1, '1970-01-01T00:00:00.000Z'),
    ('publisher', NULL, 1, '1970-01-01T00:00:00.000Z'),
    (NULL, 'space_technology', 5, '1970-01-01T00:00:00.000Z'),
    (NULL, 'nature_animals', 5, '1970-01-01T00:00:00.000Z'),
    (NULL, 'people_culture', 5, '1970-01-01T00:00:00.000Z'),
    (NULL, 'war_history', 3, '1970-01-01T00:00:00.000Z'),
    (NULL, 'celebrity_news', 3, '1970-01-01T00:00:00.000Z'),
    (NULL, 'motivation', 3, '1970-01-01T00:00:00.000Z'),
    (NULL, 'chinese_medicine', 3, '1970-01-01T00:00:00.000Z'),
    (NULL, 'health', 3, '1970-01-01T00:00:00.000Z');
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            self._backfill_dead_letters(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()

    @staticmethod
    def _backfill_dead_letters(connection: sqlite3.Connection) -> None:
        """Expose dead tasks created before the DLQ schema in the durable ledger.

        Legacy rows cannot carry a trustworthy operator resolution, so migration
        fails closed by opening one ``legacy_dead`` cycle for every unrepresented
        dead task. This is idempotent and runs inside schema initialization.
        """

        rows = connection.execute(
            """
            SELECT task.* FROM tasks AS task
            WHERE task.status = 'dead'
              AND NOT EXISTS (
                  SELECT 1 FROM dead_letters AS letter
                  WHERE letter.task_id = task.id
              )
            ORDER BY task.created_at, task.id
            """
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {"legacy_payload_json": row["payload_json"]}
            try:
                result = json.loads(row["result_json"]) if row["result_json"] else None
            except (TypeError, json.JSONDecodeError):
                result = {"legacy_result_json": row["result_json"]}
            try:
                error = (
                    json.loads(row["last_error_json"])
                    if row["last_error_json"]
                    else {"code": "legacy_dead", "message": "legacy dead task"}
                )
            except (TypeError, json.JSONDecodeError):
                error = {
                    "code": "legacy_dead",
                    "message": "legacy dead task had malformed error JSON",
                }
            if not isinstance(error, dict):
                error = {"code": "legacy_dead", "message": str(error)}
            cause_code = error.get("code")
            if not isinstance(cause_code, str) or not cause_code.strip():
                cause_code = "legacy_dead"
            snapshot = {
                "id": row["id"],
                "job_id": row["job_id"],
                "dependency_task_id": row["dependency_task_id"],
                "role": row["role"],
                "pod": row["pod"],
                "kind": row["kind"],
                "payload": payload,
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
                "result": result,
                "last_error": error,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "completed_at": row["completed_at"],
            }
            compact = lambda value: json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            letter_id = "dlq_" + hashlib.sha256(
                f"{row['id']}:1".encode("utf-8")
            ).hexdigest()[:24]
            connection.execute(
                """
                INSERT INTO dead_letters(
                    id, task_id, cycle_no, status, cause_code, error_json,
                    task_snapshot_json, created_at
                ) VALUES (?, ?, 1, 'open', ?, ?, ?, ?)
                """,
                (
                    letter_id,
                    row["id"],
                    cause_code.strip(),
                    compact(error),
                    compact(snapshot),
                    row["completed_at"] or row["updated_at"],
                ),
            )
