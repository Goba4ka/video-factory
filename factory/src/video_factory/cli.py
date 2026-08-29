from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Sequence, TextIO

from .analytics import AnalyticsStore, PRODUCTION_STAGES
from .capacity import plan_daily_batch
from .artifact_store import ArtifactStore
from .chat_audit import audit_chat_topology
from .contracts import CONTRACT_FILES, load_and_validate_chain, validate_artifact
from .daily import DEFAULT_ROLES, launch_approved, prepare_day
from .dedup import DedupThresholds, evaluate_candidate
from .dedup_corpus import (
    CORPUS_APPROVAL_CONFIRMATION,
    create_corpus_approval,
    update_dedup_corpus,
)
from .derived_cache import DerivedCache
from .errors import FactoryError, ValidationError
from .fish_audio import (
    ALLOWED_MODELS,
    DEFAULT_MODEL as DEFAULT_FISH_MODEL,
    DEFAULT_USAGE_DB as DEFAULT_FISH_USAGE_DB,
    FishTTSRequest,
    RETRY_REASONS,
    VOICE_RIGHTS_STATUSES,
    generate_tts,
    list_owned_voices,
    store_api_key,
    usage_status as fish_usage_status,
)
from .freshness import DEFAULT_TTL_HOURS, evaluate_freshness
from .media_freeze import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    freeze_approved_media,
)
from .media_qc import QC_LEVELS, QC_PROFILES, run_media_qc
from .media_tools import MEDIA_MODES, transcode_cached
from .lanes import enabled_lane_ids, load_lane_registry, validate_lane_packages
from .performance import evaluate_performance
from .outbox import HUMAN_CONFIRMATION, OUTBOX_STATUSES, PLATFORMS, PublishOutbox
from .preflight import run_preflight
from .quality_score import evaluate_quality
from .queue import Dispatcher
from .throughput_acceptance import evaluate_throughput_acceptance
from .runtime import (
    PROFILE_NAMES,
    apply_runtime_plan,
    build_runtime_plan,
    database_status,
    write_runtime_plan,
)
from .scout import run_scout
from .service import Factory
from .validators import load_json_file
from .worker import (
    ExecutorRegistry,
    HeadlessWorker,
    NullResourceLock,
    ResourceLock,
    SubprocessExecutor,
    WorkerConfig,
    default_resource_lock_path,
    install_shutdown_handlers,
)


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=os.environ.get("VIDEO_FACTORY_DB", "factory.sqlite3"),
        help="SQLite database path (default: factory.sqlite3)",
    )
    parser.add_argument("--export", help="Atomically export the command result to JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-factory",
        description="Review-first SQLite control plane for a short-video factory",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize or migrate the database")
    _add_common_options(init_parser)

    start_parser = subparsers.add_parser(
        "start", aliases=["начинаем"], help="Import ideas JSON and create a review batch"
    )
    start_parser.add_argument("ideas_file")
    start_parser.add_argument("--batch-size", type=int, default=5)
    start_parser.add_argument("--idempotency-key")
    _add_common_options(start_parser)

    list_parser = subparsers.add_parser("list", help="List ideas, jobs, or audit events")
    list_parser.add_argument("--entity", choices=["ideas", "jobs", "audit"], default="jobs")
    list_parser.add_argument("--state")
    list_parser.add_argument("--batch-id")
    list_parser.add_argument("--limit", type=int, default=100)
    _add_common_options(list_parser)

    approve_parser = subparsers.add_parser(
        "approve", aliases=["одобрить"], help="Approve a review-pending job"
    )
    approve_parser.add_argument("target", help="Job ID or idea ID")
    approve_parser.add_argument("--idempotency-key")
    _add_common_options(approve_parser)

    reject_parser = subparsers.add_parser(
        "reject", aliases=["отклонить"], help="Reject a review-pending job"
    )
    reject_parser.add_argument("target", help="Job ID or idea ID")
    reject_parser.add_argument("--reason", required=True)
    reject_parser.add_argument("--idempotency-key")
    _add_common_options(reject_parser)

    status_parser = subparsers.add_parser("status", help="Show aggregate or target status")
    status_parser.add_argument("target", nargs="?", help="Optional job ID or idea ID")
    status_parser.add_argument(
        "--queue", action="store_true", help="Show task queue status instead of job status"
    )
    _add_common_options(status_parser)

    preflight_parser = subparsers.add_parser(
        "preflight", help="Validate a preproduction dossier against a reference profile"
    )
    preflight_parser.add_argument("project", help="Preproduction directory")
    preflight_parser.add_argument(
        "--profiles",
        default="factory/quality/reference_profiles.json",
        help="Reference profiles JSON",
    )
    preflight_parser.add_argument("--export", help="Atomically export the result to JSON")

    quality_parser = subparsers.add_parser(
        "quality-score",
        aliases=["качество"],
        help="Score reference-quality readiness without bypassing hard gates",
    )
    quality_parser.add_argument("--preflight", required=True, help="Preflight result JSON")
    quality_parser.add_argument("--editorial", required=True, help="Editorial review JSON")
    quality_parser.add_argument("--originality", required=True, help="Dedup decision JSON")
    quality_parser.add_argument("--export", help="Atomically export the result to JSON")

    freshness_parser = subparsers.add_parser(
        "freshness-gate",
        aliases=["свежесть"],
        help="Fail closed when a lane's fact check is older than its publication TTL",
    )
    freshness_parser.add_argument("--lane", required=True, choices=sorted(DEFAULT_TTL_HOURS))
    freshness_parser.add_argument("--checked-at", required=True, help="ISO-8601 datetime with timezone")
    freshness_parser.add_argument("--now", help="Deterministic evaluation time for tests/replays")
    freshness_parser.add_argument("--ttl-hours", type=float)
    freshness_parser.add_argument("--export", help="Atomically export the result to JSON")

    capacity_parser = subparsers.add_parser(
        "plan-day",
        aliases=["план-дня"],
        help="Plan a 10-15 output day with explicit attrition and capacity",
    )
    capacity_parser.add_argument("input", help="Capacity planner input JSON")
    capacity_parser.add_argument("--export", help="Atomically export the result to JSON")

    throughput_parser = subparsers.add_parser(
        "throughput-acceptance",
        help="Verify a real 10-15 master batch from read-only production evidence",
    )
    throughput_parser.add_argument("--target", type=int, required=True)
    throughput_parser.add_argument("--deadline-hours", type=float, required=True)
    throughput_parser.add_argument("--batch-id")
    throughput_parser.add_argument(
        "--registry",
        default="factory/lanes/registry.json",
        help="Lane registry JSON",
    )
    throughput_parser.add_argument(
        "--safety-margin",
        type=float,
        default=0.20,
        help="Reserved deadline fraction (default: 0.20)",
    )
    throughput_parser.add_argument("--gpu-heavy-slots", type=int, default=1)
    throughput_parser.add_argument(
        "--evidence-root",
        action="append",
        dest="evidence_roots",
        help=(
            "Allowed root for master/evidence files; repeatable "
            "(default: database directory)"
        ),
    )
    throughput_parser.add_argument("--as-of", help="Deterministic ISO-8601 audit time")
    _add_common_options(throughput_parser)

    performance_parser = subparsers.add_parser(
        "evaluate-performance",
        aliases=["метрики"],
        help="Evaluate a 72-hour snapshot against a comparable cohort",
    )
    performance_parser.add_argument("--candidate", required=True)
    performance_parser.add_argument("--cohort", required=True)
    performance_parser.add_argument("--minimum-cohort", type=int, default=5)
    performance_parser.add_argument("--export", help="Atomically export the result to JSON")

    metrics_record_parser = subparsers.add_parser(
        "metrics-record",
        help="Persist one validated production telemetry event idempotently",
    )
    metrics_record_parser.add_argument("input", help="Production metric JSON")
    metrics_record_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(metrics_record_parser)

    metrics_collect_parser = subparsers.add_parser(
        "metrics-collect-queue",
        help="Materialize completed queue attempts into production metrics",
    )
    metrics_collect_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(metrics_collect_parser)

    analytics_summary_parser = subparsers.add_parser(
        "analytics-summary",
        help="Aggregate production time, cost, output, outbox, and feedback counts",
    )
    analytics_summary_parser.add_argument("--since", help="ISO-8601 lower bound")
    analytics_summary_parser.add_argument("--until", help="ISO-8601 upper bound")
    analytics_summary_parser.add_argument("--lane")
    analytics_summary_parser.add_argument("--stage", choices=sorted(PRODUCTION_STAGES))
    _add_common_options(analytics_summary_parser)

    feedback_import_parser = subparsers.add_parser(
        "feedback-import",
        help="Import checksum-attributed platform performance snapshots atomically",
    )
    feedback_import_parser.add_argument("input", help="Feedback bundle JSON")
    feedback_import_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(feedback_import_parser)

    feedback_list_parser = subparsers.add_parser(
        "feedback-list", help="List imported performance feedback snapshots"
    )
    feedback_list_parser.add_argument("--job-id")
    feedback_list_parser.add_argument("--platform", choices=sorted(PLATFORMS))
    feedback_list_parser.add_argument("--limit", type=int, default=100)
    _add_common_options(feedback_list_parser)

    feedback_evaluate_parser = subparsers.add_parser(
        "feedback-evaluate",
        help="Persist bounded editorial guidance from a comparable 72-hour cohort",
    )
    feedback_evaluate_parser.add_argument("outbox_id")
    feedback_evaluate_parser.add_argument("--minimum-cohort", type=int, default=5)
    feedback_evaluate_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(feedback_evaluate_parser)

    recommendations_list_parser = subparsers.add_parser(
        "recommendations-list",
        help="List immutable editorial-only performance recommendations",
    )
    recommendations_list_parser.add_argument("--outbox-id")
    recommendations_list_parser.add_argument("--limit", type=int, default=100)
    _add_common_options(recommendations_list_parser)

    outbox_create_parser = subparsers.add_parser(
        "outbox-create",
        help="Create a checksum-verified publish request in pending approval state",
    )
    outbox_create_parser.add_argument("request", help="Publish request JSON")
    outbox_create_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(outbox_create_parser)

    outbox_approve_parser = subparsers.add_parser(
        "outbox-approve",
        help="Record explicit human approval bound to render and metadata checksums",
    )
    outbox_approve_parser.add_argument("outbox_id")
    outbox_approve_parser.add_argument("--render-sha256", required=True)
    outbox_approve_parser.add_argument("--metadata-sha256", required=True)
    outbox_approve_parser.add_argument("--approved-by", required=True)
    outbox_approve_parser.add_argument("--approval-note", required=True)
    outbox_approve_parser.add_argument(
        "--human-confirm",
        required=True,
        help=f"Must equal {HUMAN_CONFIRMATION}",
    )
    outbox_approve_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(outbox_approve_parser)

    outbox_claim_parser = subparsers.add_parser(
        "outbox-claim",
        help="Lease one approved item to a connector without sending it",
    )
    outbox_claim_parser.add_argument("--worker", required=True)
    outbox_claim_parser.add_argument("--platform", choices=sorted(PLATFORMS))
    outbox_claim_parser.add_argument("--lease-seconds", type=int, default=900)
    outbox_claim_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(outbox_claim_parser)

    outbox_complete_parser = subparsers.add_parser(
        "outbox-complete",
        help="Persist a connector success receipt; this command performs no send",
    )
    outbox_complete_parser.add_argument("outbox_id")
    outbox_complete_parser.add_argument("--lease-token", required=True)
    outbox_complete_parser.add_argument("--remote-id", required=True)
    outbox_complete_parser.add_argument("--receipt", required=True, help="Receipt JSON")
    outbox_complete_parser.add_argument("--published-at")
    outbox_complete_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(outbox_complete_parser)

    outbox_fail_parser = subparsers.add_parser(
        "outbox-fail",
        help="Persist a connector failure or ambiguous outcome without automatic retry",
    )
    outbox_fail_parser.add_argument("outbox_id")
    outbox_fail_parser.add_argument("--lease-token", required=True)
    outbox_fail_parser.add_argument("--outcome", choices=["failed", "unknown"], required=True)
    outbox_fail_parser.add_argument("--error", required=True, help="Error JSON")
    outbox_fail_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(outbox_fail_parser)

    outbox_recover_parser = subparsers.add_parser(
        "outbox-recover-expired",
        help="Move expired delivery leases to unknown for manual reconciliation",
    )
    outbox_recover_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(outbox_recover_parser)

    outbox_list_parser = subparsers.add_parser(
        "outbox-list", help="List publish handoffs and verify current render checksums"
    )
    outbox_list_parser.add_argument("--status", choices=sorted(OUTBOX_STATUSES))
    outbox_list_parser.add_argument("--platform", choices=sorted(PLATFORMS))
    outbox_list_parser.add_argument("--limit", type=int, default=100)
    _add_common_options(outbox_list_parser)

    dedup_parser = subparsers.add_parser(
        "dedup",
        aliases=["дубли"],
        help="Compare one IdeaCard to recent ideas using deterministic lexical signals",
    )
    dedup_parser.add_argument("--candidate", required=True)
    dedup_parser.add_argument("--existing", required=True)
    dedup_parser.add_argument("--review-threshold", type=float, default=0.20)
    dedup_parser.add_argument("--block-threshold", type=float, default=0.78)
    dedup_parser.add_argument("--export", help="Atomically export the result to JSON")

    scout_parser = subparsers.add_parser(
        "scout",
        aliases=["разведка"],
        help="Discover current ideas from keyless official feeds with offline fallback",
    )
    scout_parser.add_argument("--date", help="Production date in YYYY-MM-DD (default: today)")
    scout_parser.add_argument("--limit", type=int, default=12)
    scout_parser.add_argument(
        "--cache-dir",
        default=".video-factory-cache/scout",
        help="Local response cache directory",
    )
    scout_parser.add_argument("--timeout", type=float, default=8.0)
    scout_parser.add_argument("--retries", type=int, default=1)
    scout_parser.add_argument(
        "--lanes",
        help=(
            "Comma-separated production lanes: war_history, celebrity_news, "
            "motivation, chinese_medicine, health"
        ),
    )
    scout_parser.add_argument(
        "--offline",
        action="store_true",
        help="Disable network access and use cache or bundled evergreen fallbacks",
    )
    scout_parser.add_argument("--export", help="Atomically export the result to JSON")

    prepare_parser = subparsers.add_parser(
        "prepare-day",
        aliases=["готовим-день"],
        help="Discover, deduplicate, persist, and open a 10-15 output review batch",
    )
    prepare_parser.add_argument("--date", help="Production date in YYYY-MM-DD")
    prepare_parser.add_argument("--target", type=int, default=15)
    prepare_parser.add_argument("--expected-yield", type=float, default=0.55)
    prepare_parser.add_argument("--output-root", default="factory/runs")
    prepare_parser.add_argument("--artifact-root", default="factory/artifacts")
    prepare_parser.add_argument("--cache-dir", default=".video-factory-cache/scout")
    prepare_parser.add_argument("--timeout", type=float, default=8.0)
    prepare_parser.add_argument("--retries", type=int, default=1)
    prepare_parser.add_argument("--offline", action="store_true")
    prepare_parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Run discovery again and create/replay a content-addressed source batch",
    )
    _add_common_options(prepare_parser)

    launch_parser = subparsers.add_parser(
        "launch-approved",
        aliases=["запустить-одобренные"],
        help="Create an idempotent specialist-agent chain for every approved topic",
    )
    launch_parser.add_argument("--batch-id")
    launch_parser.add_argument(
        "--roles",
        default="auto",
        help="Comma-separated global chain or 'auto' for per-lane safety chains",
    )
    _add_common_options(launch_parser)

    contract_parser = subparsers.add_parser(
        "validate-artifact", help="Validate one JSON artifact against its canonical contract"
    )
    contract_parser.add_argument("contract", choices=sorted(CONTRACT_FILES))
    contract_parser.add_argument("file")
    contract_parser.add_argument("--export", help="Atomically export the result to JSON")

    chain_parser = subparsers.add_parser(
        "validate-chain", help="Validate IdeaCard -> claims -> rights -> shotlist integrity"
    )
    chain_parser.add_argument("--idea-card", required=True)
    chain_parser.add_argument("--claim-ledger", required=True)
    chain_parser.add_argument("--rights-manifest", required=True)
    chain_parser.add_argument("--shotlist", required=True)
    chain_parser.add_argument("--safety-gate-report")
    chain_parser.add_argument("--export", help="Atomically export the result to JSON")

    freeze_parser = subparsers.add_parser(
        "freeze-media",
        aliases=["заморозить-медиа"],
        help="Freeze explicit approved HTTP(S) media into a hashed local ledger",
    )
    freeze_parser.add_argument("manifest", help="Passed RightsManifest JSON")
    freeze_parser.add_argument("--output-dir", required=True)
    freeze_parser.add_argument("--ledger", help="Frozen ledger JSON path")
    freeze_parser.add_argument(
        "--asset-id",
        action="append",
        dest="asset_ids",
        help="Freeze only this approved asset ID; may be repeated",
    )
    freeze_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    freeze_parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    freeze_parser.add_argument(
        "--probe", action="store_true", help="Run a strict HEAD probe before GET"
    )
    freeze_parser.add_argument(
        "--allow-private-hosts",
        action="store_true",
        help="Explicitly allow private/loopback hosts for controlled local infrastructure",
    )
    freeze_parser.add_argument("--export", help="Atomically export the result to JSON")

    corpus_approve_parser = subparsers.add_parser(
        "dedup-corpus-approve",
        help="Bind explicit human corpus approval to one exact RenderManifest and master",
    )
    corpus_approve_parser.add_argument("--render-manifest", required=True)
    corpus_approve_parser.add_argument("--master", required=True)
    corpus_approve_parser.add_argument("--output", required=True)
    corpus_approve_parser.add_argument("--approved-by", required=True)
    corpus_approve_parser.add_argument("--approval-note", required=True)
    corpus_approve_parser.add_argument(
        "--human-confirm",
        required=True,
        help=f"Must equal {CORPUS_APPROVAL_CONFIRMATION}",
    )
    corpus_approve_parser.add_argument(
        "--export", help="Atomically export the result to JSON"
    )

    corpus_update_parser = subparsers.add_parser(
        "dedup-corpus-update",
        help="Atomically fingerprint approved masters and update the dedup corpus",
    )
    corpus_update_parser.add_argument("--snapshot", required=True)
    corpus_update_parser.add_argument(
        "--approval",
        action="append",
        required=True,
        dest="approvals",
        help="Checksum-bound DedupCorpusApproval JSON; may be repeated",
    )
    corpus_update_parser.add_argument(
        "--sample-interval-seconds", type=float, default=1.0
    )
    corpus_update_parser.add_argument(
        "--lock-timeout-seconds", type=float, default=30.0
    )
    corpus_update_parser.add_argument(
        "--export", help="Atomically export the result to JSON"
    )

    next_parser = subparsers.add_parser("next", help="Advance a job by exactly one transition")
    next_parser.add_argument("target", help="Job ID or idea ID")
    next_parser.add_argument("--idempotency-key", required=True)
    next_parser.add_argument("--gate-result", choices=["pass", "fail"])
    next_parser.add_argument("--evidence", help="Rights or QC evidence JSON file")
    _add_common_options(next_parser)

    enqueue_parser = subparsers.add_parser("enqueue", help="Durably enqueue one worker task")
    enqueue_parser.add_argument("--role", required=True)
    enqueue_parser.add_argument("--pod", required=True)
    enqueue_parser.add_argument("--kind", required=True)
    enqueue_parser.add_argument("--payload", help="Task payload JSON object")
    enqueue_parser.add_argument("--job-id")
    enqueue_parser.add_argument("--depends-on", dest="dependency_task_id")
    enqueue_parser.add_argument("--priority", type=int, default=0)
    enqueue_parser.add_argument("--max-attempts", type=int, default=3)
    enqueue_parser.add_argument("--retry-backoff-seconds", type=int, default=60)
    enqueue_parser.add_argument("--available-at", help="ISO-8601 timestamp")
    enqueue_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(enqueue_parser)

    claim_parser = subparsers.add_parser(
        "claim", help="Atomically claim one task and receive a fencing token"
    )
    claim_parser.add_argument("--worker", required=True)
    claim_parser.add_argument("--role", required=True)
    claim_parser.add_argument("--pod")
    claim_parser.add_argument("--kind")
    claim_parser.add_argument("--lease-seconds", type=int, default=900)
    claim_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(claim_parser)

    renew_parser = subparsers.add_parser(
        "renew-lease", help="Heartbeat a fenced lease without changing its attempt"
    )
    renew_parser.add_argument("task_id")
    renew_parser.add_argument("--lease-token", required=True)
    renew_parser.add_argument("--worker", required=True)
    renew_parser.add_argument("--lease-seconds", type=int, default=900)
    _add_common_options(renew_parser)

    complete_parser = subparsers.add_parser(
        "complete", help="Acknowledge a leased task with its fencing token"
    )
    complete_parser.add_argument("task_id")
    complete_parser.add_argument("--lease-token", required=True)
    complete_parser.add_argument("--result", help="Result JSON object")
    complete_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(complete_parser)

    fail_parser = subparsers.add_parser(
        "fail", help="Fail an attempt and retry with deterministic backoff"
    )
    fail_parser.add_argument("task_id")
    fail_parser.add_argument("--lease-token", required=True)
    error_group = fail_parser.add_mutually_exclusive_group(required=True)
    error_group.add_argument("--error", help="Error JSON object")
    error_group.add_argument("--reason", help="Plain-text error reason")
    fail_parser.add_argument("--terminal", action="store_true")
    fail_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(fail_parser)

    queue_status_parser = subparsers.add_parser(
        "queue-status", help="Show queue, WIP, leases, or one task's attempts"
    )
    queue_status_parser.add_argument("task_id", nargs="?")
    _add_common_options(queue_status_parser)

    recover_parser = subparsers.add_parser(
        "recover-expired", help="Recover expired leases in deterministic order"
    )
    _add_common_options(recover_parser)

    dead_list_parser = subparsers.add_parser(
        "dead-list", help="List durable open or resolved dead-letter records"
    )
    dead_list_parser.add_argument("--status", choices=["open", "resolved", "all"], default="open")
    dead_list_parser.add_argument("--task-id")
    dead_list_parser.add_argument("--limit", type=int, default=100)
    _add_common_options(dead_list_parser)

    dead_retry_parser = subparsers.add_parser(
        "dead-retry", help="Operator-controlled retry of one dead task with unchanged inputs"
    )
    dead_retry_parser.add_argument("task_id")
    dead_retry_parser.add_argument("--reason", required=True)
    dead_retry_parser.add_argument("--actor", required=True)
    dead_retry_parser.add_argument("--additional-attempts", type=int, default=1)
    dead_retry_parser.add_argument("--available-at", help="ISO-8601 timestamp")
    dead_retry_parser.add_argument(
        "--cascade-dependents",
        action="store_true",
        help="Revive descendants that died only because this dependency was dead",
    )
    dead_retry_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(dead_retry_parser)

    rework_parser = subparsers.add_parser(
        "task-rework",
        help="Version a task and its downstream chain after a controlled input correction",
    )
    rework_parser.add_argument("task_id")
    rework_parser.add_argument("--reason", required=True)
    rework_parser.add_argument("--actor", required=True)
    rework_parser.add_argument("--payload-patch", help="JSON object shallow-merged into root payload")
    rework_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(rework_parser)

    artifact_put_parser = subparsers.add_parser(
        "artifact-put",
        help="Register an immutable artifact and invalidate stale downstream versions",
    )
    artifact_put_parser.add_argument("--root", required=True, help="Artifact store root")
    artifact_put_parser.add_argument("--job-id", required=True)
    artifact_put_parser.add_argument("--kind", required=True)
    artifact_put_parser.add_argument("--file", required=True, help="Artifact JSON object")
    artifact_put_parser.add_argument("--producer", required=True)
    artifact_put_parser.add_argument("--producer-version", required=True)
    artifact_put_parser.add_argument("--dependencies", help="JSON array of dependency records")
    artifact_put_parser.add_argument("--metadata", help="JSON object of identity-bearing metadata")
    artifact_put_parser.add_argument("--prompt-version")
    artifact_put_parser.add_argument("--model")
    artifact_put_parser.add_argument("--skip-contract-validation", action="store_true")
    artifact_put_parser.add_argument("--export", help="Atomically export the result to JSON")

    artifact_list_parser = subparsers.add_parser(
        "artifact-list", help="List active, superseded, or invalidated artifact versions"
    )
    artifact_list_parser.add_argument("--root", required=True, help="Artifact store root")
    artifact_list_parser.add_argument("--job-id")
    artifact_list_parser.add_argument("--kind")
    artifact_list_parser.add_argument(
        "--status", choices=["active", "superseded", "invalidated"]
    )
    artifact_list_parser.add_argument("--export", help="Atomically export the result to JSON")

    artifact_current_parser = subparsers.add_parser(
        "artifact-current", help="Resolve the single active artifact for a job and kind"
    )
    artifact_current_parser.add_argument("--root", required=True, help="Artifact store root")
    artifact_current_parser.add_argument("--job-id", required=True)
    artifact_current_parser.add_argument("--kind", required=True)
    artifact_current_parser.add_argument("--export", help="Atomically export the result to JSON")

    limit_parser = subparsers.add_parser(
        "queue-limit", help="Set a role, pod, or role/pod WIP ceiling"
    )
    limit_parser.add_argument("--role")
    limit_parser.add_argument("--pod")
    limit_parser.add_argument("--max-leased", type=int, required=True)
    _add_common_options(limit_parser)

    simulate_parser = subparsers.add_parser(
        "simulate-day",
        help="Simulate 10-15 queue chains without providers, renders, human approvals, or publishing",
    )
    simulate_parser.add_argument("--target", type=int, default=15)
    simulate_parser.add_argument(
        "--pods", default=",".join(enabled_lane_ids())
    )
    simulate_parser.add_argument(
        "--roles",
        default=",".join(DEFAULT_ROLES),
        help="Generic queue chain; all-lane safety topology is tested by launch-approved auto",
    )
    simulate_parser.add_argument("--lease-seconds", type=int, default=60)
    simulate_parser.add_argument("--idempotency-key", required=True)
    _add_common_options(simulate_parser)

    worker_parser = subparsers.add_parser(
        "worker", help="Run a heartbeat-enabled headless queue worker"
    )
    worker_parser.add_argument("--worker", required=True, dest="worker_id")
    worker_parser.add_argument("--role", required=True)
    worker_parser.add_argument("--pod")
    worker_parser.add_argument("--kind")
    worker_parser.add_argument("--handler-executable", required=True)
    worker_parser.add_argument(
        "--handler-arg",
        action="append",
        default=[],
        help="One argv item for the handler; repeat and use --handler-arg=-x for flags",
    )
    worker_parser.add_argument("--handler-cwd")
    worker_parser.add_argument("--handler-timeout", type=float, default=7200)
    worker_parser.add_argument("--shutdown-grace", type=float, default=30)
    worker_parser.add_argument("--max-handler-input-bytes", type=int, default=4 * 1024 * 1024)
    worker_parser.add_argument("--max-handler-output-bytes", type=int, default=4 * 1024 * 1024)
    worker_parser.add_argument("--max-handler-stderr-bytes", type=int, default=1024 * 1024)
    worker_parser.add_argument("--lease-seconds", type=int, default=900)
    worker_parser.add_argument("--heartbeat-seconds", type=float, default=300)
    worker_parser.add_argument("--poll-seconds", type=float, default=2)
    worker_parser.add_argument("--lock-timeout-seconds", type=float, default=0)
    worker_parser.add_argument(
        "--resource-lock",
        default="auto",
        help="'auto', 'none', or an advisory lock-file path acquired before claim",
    )
    worker_parser.add_argument(
        "--max-tasks", type=int, default=0, help="0 keeps consuming until shutdown"
    )
    worker_parser.add_argument(
        "--max-idle-polls", type=int, default=0, help="0 keeps polling while idle"
    )
    worker_parser.add_argument(
        "--max-runtime-seconds", type=float, default=0, help="0 disables the runtime bound"
    )
    worker_parser.add_argument("--acknowledgement-attempts", type=int, default=3)
    worker_parser.add_argument("--acknowledgement-retry-seconds", type=float, default=0.25)
    worker_parser.add_argument("--terminal-on-handler-error", action="store_true")
    worker_parser.add_argument("--quiet-events", action="store_true")
    _add_common_options(worker_parser)

    lanes_parser = subparsers.add_parser(
        "lanes", help="Validate the five lane packages and show their agent chains"
    )
    lanes_parser.add_argument(
        "--registry", default="factory/lanes/registry.json", help="Lane registry JSON"
    )
    lanes_parser.add_argument("--minimum-candidates", type=int, default=20)
    lanes_parser.add_argument("--export", help="Atomically export the result to JSON")

    chat_audit_parser = subparsers.add_parser(
        "chat-audit",
        help="Read-only verification of the five registered Codex chat rollouts",
    )
    chat_audit_parser.add_argument(
        "--registry",
        default="factory/lanes/registry.json",
        help="Lane registry JSON",
    )
    chat_audit_parser.add_argument(
        "--session-index",
        required=True,
        help="Explicit Codex session_index.jsonl evidence path",
    )
    chat_audit_parser.add_argument(
        "--sessions-root",
        required=True,
        help="Explicit root containing rollout-*.jsonl evidence",
    )

    runtime_parser = subparsers.add_parser(
        "optimize-runtime",
        help="Inspect this computer and create a resource-safe five-lane runtime plan",
    )
    runtime_parser.add_argument("--profile", choices=PROFILE_NAMES, default="auto")
    runtime_parser.add_argument("--target", type=int, default=15)
    runtime_parser.add_argument("--runtime-root")
    runtime_parser.add_argument("--registry", default="factory/lanes/registry.json")
    runtime_parser.add_argument(
        "--db", help="Clean current-schema runtime DB; defaults outside synchronized folders"
    )
    runtime_parser.add_argument(
        "--legacy-db",
        default="factory/runtime/factory.sqlite3",
        help="Optional old DB to inspect without modifying",
    )
    runtime_parser.add_argument("--plan-output")
    runtime_parser.add_argument(
        "--apply", action="store_true", help="Create the clean DB and apply WIP limits"
    )
    runtime_parser.add_argument("--export", help="Atomically export the result to JSON")

    cache_status_parser = subparsers.add_parser(
        "cache-status", help="Show reusable derived-media/QC cache usage"
    )
    cache_status_parser.add_argument("--cache-root")
    cache_status_parser.add_argument("--export", help="Atomically export the result to JSON")

    cache_prune_parser = subparsers.add_parser(
        "cache-prune", help="Dry-run an LRU cache cleanup unless --execute is explicit"
    )
    cache_prune_parser.add_argument("--cache-root")
    cache_prune_parser.add_argument("--max-bytes", type=int)
    cache_prune_parser.add_argument("--older-than-days", type=int)
    cache_prune_parser.add_argument("--execute", action="store_true")
    cache_prune_parser.add_argument("--export", help="Atomically export the result to JSON")

    cache_media_parser = subparsers.add_parser(
        "cache-media", help="Create or reuse an edit proxy, draft, or Telegram MP4"
    )
    cache_media_parser.add_argument("input")
    cache_media_parser.add_argument("--mode", choices=MEDIA_MODES, required=True)
    cache_media_parser.add_argument("--cache-root")
    cache_media_parser.add_argument("--proxy-max-height", type=int, default=960)
    cache_media_parser.add_argument("--ffmpeg-threads", type=int)
    cache_media_parser.add_argument("--cpu", action="store_true", help="Disable NVENC")
    cache_media_parser.add_argument("--export", help="Atomically export the result to JSON")

    media_qc_parser = subparsers.add_parser(
        "media-qc", help="Run cached FAST draft QC or FULL final technical QC"
    )
    media_qc_parser.add_argument("input")
    media_qc_parser.add_argument("--level", choices=QC_LEVELS, default="fast")
    media_qc_parser.add_argument(
        "--profile", choices=sorted(QC_PROFILES), default="portrait_draft"
    )
    media_qc_parser.add_argument("--cache-root")
    media_qc_parser.add_argument("--ffmpeg-threads", type=int)
    media_qc_parser.add_argument("--export", help="Atomically export the result to JSON")

    fish_tts_parser = subparsers.add_parser(
        "fish-tts",
        help="Generate one full WAV voiceover with a hard two-call limit per video",
    )
    fish_tts_parser.add_argument("--video-id", required=True)
    fish_tts_parser.add_argument("--text-file", required=True, help="UTF-8 narration text")
    fish_tts_parser.add_argument("--output", required=True, help="Output .wav path")
    fish_tts_parser.add_argument("--reference-id")
    fish_tts_parser.add_argument(
        "--model", choices=sorted(ALLOWED_MODELS), default=DEFAULT_FISH_MODEL
    )
    fish_tts_parser.add_argument(
        "--retry-reason",
        choices=sorted(RETRY_REASONS),
        help="Required for a different second generation",
    )
    fish_tts_parser.add_argument(
        "--defect-reference",
        help="Required path to a validated VoiceDefect JSON artifact for generation 2",
    )
    fish_tts_parser.add_argument("--speed", type=float, default=1.0)
    fish_tts_parser.add_argument("--temperature", type=float, default=0.7)
    fish_tts_parser.add_argument("--top-p", type=float, default=0.7)
    fish_tts_parser.add_argument("--timeout", type=float, default=180.0)
    fish_tts_parser.add_argument(
        "--voice-rights-status",
        choices=sorted(VOICE_RIGHTS_STATUSES),
        default="user_confirmation_required",
        help="Keep confirmation-required unless ownership or license was explicitly verified",
    )
    fish_tts_parser.add_argument("--export", help="Atomically export the result to JSON")

    fish_status_parser = subparsers.add_parser(
        "fish-tts-status", help="Show durable Fish Audio generation usage"
    )
    fish_status_parser.add_argument("video_id", nargs="?")
    fish_status_parser.add_argument("--export", help="Atomically export the result to JSON")

    fish_voices_parser = subparsers.add_parser(
        "fish-voices", help="Verify Fish Audio auth and list voices owned by this workspace"
    )
    fish_voices_parser.add_argument("--page-size", type=int, default=100)
    fish_voices_parser.add_argument("--timeout", type=float, default=30.0)
    fish_voices_parser.add_argument("--export", help="Atomically export the result to JSON")

    fish_auth_parser = subparsers.add_parser(
        "fish-auth", help="Securely replace the Fish Audio key in Windows DPAPI"
    )
    fish_auth_parser.add_argument("--export", help="Atomically export the result to JSON")
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    command = args.command
    if command == "preflight":
        return run_preflight(args.project, args.profiles)
    if command in {"quality-score", "качество"}:
        return evaluate_quality(
            preflight=load_json_file(args.preflight),
            editorial=load_json_file(args.editorial),
            originality=load_json_file(args.originality),
        )
    if command in {"freshness-gate", "свежесть"}:
        return evaluate_freshness(
            lane=args.lane,
            checked_at=args.checked_at,
            now=args.now,
            ttl_hours=args.ttl_hours,
        )
    if command in {"plan-day", "план-дня"}:
        payload = load_json_file(args.input)
        return plan_daily_batch(**payload)
    if command == "throughput-acceptance":
        return evaluate_throughput_acceptance(
            db_path=args.db,
            target=args.target,
            deadline_hours=args.deadline_hours,
            batch_id=args.batch_id,
            registry_path=args.registry,
            safety_margin=args.safety_margin,
            gpu_heavy_slots=args.gpu_heavy_slots,
            allowed_evidence_roots=args.evidence_roots,
            as_of=args.as_of,
        )
    if command in {"evaluate-performance", "метрики"}:
        cohort_payload = load_json_file(args.cohort)
        cohort = cohort_payload.get("snapshots") if isinstance(cohort_payload, dict) else None
        if not isinstance(cohort, list):
            raise ValidationError("cohort JSON must contain a 'snapshots' array")
        return evaluate_performance(
            load_json_file(args.candidate),
            cohort,
            minimum_cohort=args.minimum_cohort,
        )
    if command == "metrics-record":
        event = load_json_file(args.input)
        if not isinstance(event, dict):
            raise ValidationError("production metric JSON must be an object")
        return AnalyticsStore(args.db).record_metric(
            event, idempotency_key=args.idempotency_key
        )
    if command == "metrics-collect-queue":
        return AnalyticsStore(args.db).collect_queue_metrics(
            idempotency_key=args.idempotency_key
        )
    if command == "analytics-summary":
        return AnalyticsStore(args.db).summary(
            since=args.since,
            until=args.until,
            lane=args.lane,
            stage=args.stage,
        )
    if command == "feedback-import":
        return AnalyticsStore(args.db).import_feedback(
            args.input, idempotency_key=args.idempotency_key
        )
    if command == "feedback-list":
        return AnalyticsStore(args.db).list_feedback(
            job_id=args.job_id,
            platform=args.platform,
            limit=args.limit,
        )
    if command == "feedback-evaluate":
        return AnalyticsStore(args.db).evaluate_editorial_feedback(
            args.outbox_id,
            minimum_cohort=args.minimum_cohort,
            idempotency_key=args.idempotency_key,
        )
    if command == "recommendations-list":
        return AnalyticsStore(args.db).list_recommendations(
            outbox_id=args.outbox_id,
            limit=args.limit,
        )
    if command == "outbox-create":
        request = load_json_file(args.request)
        if not isinstance(request, dict):
            raise ValidationError("publish request JSON must be an object")
        return PublishOutbox(args.db).create(
            request, idempotency_key=args.idempotency_key
        )
    if command == "outbox-approve":
        return PublishOutbox(args.db).approve(
            args.outbox_id,
            render_sha256=args.render_sha256,
            metadata_sha256=args.metadata_sha256,
            approved_by=args.approved_by,
            approval_note=args.approval_note,
            human_confirmation=args.human_confirm,
            idempotency_key=args.idempotency_key,
        )
    if command == "outbox-claim":
        return PublishOutbox(args.db).claim(
            worker_id=args.worker,
            platform=args.platform,
            lease_seconds=args.lease_seconds,
            idempotency_key=args.idempotency_key,
        )
    if command == "outbox-complete":
        receipt = load_json_file(args.receipt)
        if not isinstance(receipt, dict):
            raise ValidationError("receipt JSON must be an object")
        return PublishOutbox(args.db).complete(
            args.outbox_id,
            lease_token=args.lease_token,
            remote_id=args.remote_id,
            receipt=receipt,
            published_at=args.published_at,
            idempotency_key=args.idempotency_key,
        )
    if command == "outbox-fail":
        error = load_json_file(args.error)
        if not isinstance(error, dict):
            raise ValidationError("error JSON must be an object")
        return PublishOutbox(args.db).fail(
            args.outbox_id,
            lease_token=args.lease_token,
            outcome=args.outcome,
            error=error,
            idempotency_key=args.idempotency_key,
        )
    if command == "outbox-recover-expired":
        return PublishOutbox(args.db).recover_expired(
            idempotency_key=args.idempotency_key
        )
    if command == "outbox-list":
        return PublishOutbox(args.db).list(
            status=args.status, platform=args.platform, limit=args.limit
        )
    if command in {"dedup", "дубли"}:
        candidate = load_json_file(args.candidate)
        existing_payload = load_json_file(args.existing)
        if isinstance(existing_payload, dict):
            existing = existing_payload.get("ideas")
        else:
            existing = existing_payload
        if not isinstance(candidate, dict):
            raise ValidationError("candidate JSON must be an object")
        if not isinstance(existing, list) or not all(
            isinstance(item, dict) for item in existing
        ):
            raise ValidationError("existing JSON must be an array or {'ideas': [...]} object")
        try:
            thresholds = DedupThresholds(
                review=args.review_threshold,
                block=args.block_threshold,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return evaluate_candidate(candidate, existing, thresholds=thresholds).as_dict()
    if command in {"scout", "разведка"}:
        return run_scout(
            production_date=args.date,
            limit=args.limit,
            cache_dir=args.cache_dir,
            timeout=args.timeout,
            retries=args.retries,
            offline=args.offline,
            lanes=(
                [lane.strip() for lane in args.lanes.split(",") if lane.strip()]
                if args.lanes
                else None
            ),
        )
    if command in {"prepare-day", "готовим-день"}:
        return prepare_day(
            db_path=args.db,
            output_root=args.output_root,
            artifact_root=args.artifact_root,
            cache_dir=args.cache_dir,
            target=args.target,
            expected_yield=args.expected_yield,
            production_date=args.date,
            offline=args.offline,
            timeout=args.timeout,
            retries=args.retries,
            force_refresh=args.force_refresh,
        )
    if command in {"launch-approved", "запустить-одобренные"}:
        roles = None
        if args.roles.strip().lower() != "auto":
            roles = [item.strip() for item in args.roles.split(",") if item.strip()]
        return launch_approved(db_path=args.db, batch_id=args.batch_id, roles=roles)
    if command == "dedup-corpus-approve":
        return create_corpus_approval(
            args.render_manifest,
            args.master,
            args.output,
            approved_by=args.approved_by,
            approval_note=args.approval_note,
            human_confirm=args.human_confirm,
        )
    if command == "dedup-corpus-update":
        return update_dedup_corpus(
            args.snapshot,
            args.approvals,
            sample_interval_seconds=args.sample_interval_seconds,
            lock_timeout_seconds=args.lock_timeout_seconds,
        )
    if command == "lanes":
        # Loading first gives malformed registries a precise fail-closed error.
        load_lane_registry(args.registry)
        return validate_lane_packages(
            registry_path=args.registry, minimum_candidates=args.minimum_candidates
        )
    if command == "chat-audit":
        return audit_chat_topology(
            registry_path=args.registry,
            session_index=args.session_index,
            sessions_root=args.sessions_root,
        )
    if command == "optimize-runtime":
        plan = build_runtime_plan(
            profile=args.profile,
            target=args.target,
            runtime_root=args.runtime_root,
            registry_path=args.registry,
        )
        plan["legacy_database"] = database_status(args.legacy_db)
        output = args.plan_output
        if output is None and args.apply:
            output = str(Path(plan["paths"]["runtime_root"]) / "active-plan.json")
        if output is not None:
            plan["plan_path"] = str(write_runtime_plan(plan, output))
        plan["applied"] = (
            apply_runtime_plan(plan, db_path=args.db) if args.apply else None
        )
        return plan
    if command == "cache-status":
        return DerivedCache(args.cache_root).stats()
    if command == "cache-prune":
        return DerivedCache(args.cache_root).prune(
            max_bytes=args.max_bytes,
            older_than_days=args.older_than_days,
            dry_run=not args.execute,
        )
    if command == "cache-media":
        return transcode_cached(
            args.input,
            mode=args.mode,
            cache_root=args.cache_root,
            proxy_max_height=args.proxy_max_height,
            prefer_gpu=not args.cpu,
            ffmpeg_threads=args.ffmpeg_threads,
        )
    if command == "media-qc":
        return run_media_qc(
            args.input,
            level=args.level,
            profile_name=args.profile,
            cache_root=args.cache_root,
            ffmpeg_threads=args.ffmpeg_threads,
        )
    if command == "fish-tts":
        text_path = Path(args.text_file)
        try:
            text = text_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("Fish Audio text file must be valid UTF-8") from exc
        return generate_tts(
            FishTTSRequest(
                video_id=args.video_id,
                text=text,
                output_path=Path(args.output),
                reference_id=args.reference_id,
                model=args.model,
                speed=args.speed,
                temperature=args.temperature,
                top_p=args.top_p,
                timeout_seconds=args.timeout,
                voice_rights_status=args.voice_rights_status,
                retry_reason=args.retry_reason,
                defect_reference=args.defect_reference,
            ),
            usage_db=DEFAULT_FISH_USAGE_DB,
        )
    if command == "fish-tts-status":
        return fish_usage_status(args.video_id, usage_db=DEFAULT_FISH_USAGE_DB)
    if command == "fish-voices":
        return list_owned_voices(page_size=args.page_size, timeout_seconds=args.timeout)
    if command == "fish-auth":
        key = getpass.getpass("Fish Audio API key: ")
        path = store_api_key(key)
        return {
            "ok": True,
            "provider": "fish_audio",
            "protected": True,
            "key_store": str(path),
            "restart_required": False,
        }
    if command == "validate-artifact":
        payload = load_json_file(args.file)
        validate_artifact(args.contract, payload)
        return {"ok": True, "command": command, "contract": args.contract, "valid": True}
    if command == "validate-chain":
        paths = {
                "idea_card": args.idea_card,
                "claim_ledger": args.claim_ledger,
                "rights_manifest": args.rights_manifest,
                "shotlist": args.shotlist,
        }
        if args.safety_gate_report:
            paths["safety_gate_report"] = args.safety_gate_report
        return load_and_validate_chain(paths)
    if command == "artifact-put":
        payload = load_json_file(args.file)
        if not isinstance(payload, dict):
            raise ValidationError("artifact file must contain a JSON object")
        dependencies: list[dict[str, Any]] = []
        if args.dependencies:
            dependency_payload = load_json_file(args.dependencies)
            if isinstance(dependency_payload, dict):
                dependency_payload = dependency_payload.get("dependencies")
            if not isinstance(dependency_payload, list) or not all(
                isinstance(item, dict) for item in dependency_payload
            ):
                raise ValidationError("dependencies must be a JSON array of objects")
            dependencies = dependency_payload
        metadata: dict[str, Any] = {}
        if args.metadata:
            metadata_payload = load_json_file(args.metadata)
            if not isinstance(metadata_payload, dict):
                raise ValidationError("metadata must be a JSON object")
            metadata = metadata_payload
        store = ArtifactStore(args.root)
        before = {
            item["artifact_id"]: item["status"]
            for item in store.list(job_id=args.job_id)
        }
        artifact = store.put(
            job_id=args.job_id,
            kind=args.kind,
            payload=payload,
            producer=args.producer,
            producer_version=args.producer_version,
            dependencies=dependencies,
            prompt_version=args.prompt_version,
            model=args.model,
            metadata=metadata,
            validate_contract=not args.skip_contract_validation,
        )
        after = store.list(job_id=args.job_id)
        invalidated = [
            item
            for item in after
            if item["status"] in {"superseded", "invalidated"}
            and before.get(item["artifact_id"]) != item["status"]
        ]
        return {
            "ok": True,
            "command": "artifact-put",
            "artifact": artifact,
            "invalidated": invalidated,
        }
    if command == "artifact-list":
        records = ArtifactStore(args.root).list(
            job_id=args.job_id, kind=args.kind, status=args.status
        )
        return {
            "ok": True,
            "command": "artifact-list",
            "count": len(records),
            "items": records,
        }
    if command == "artifact-current":
        return {
            "ok": True,
            "command": "artifact-current",
            "artifact": ArtifactStore(args.root).current(
                job_id=args.job_id, kind=args.kind
            ),
        }
    if command in {"freeze-media", "заморозить-медиа"}:
        return freeze_approved_media(
            args.manifest,
            args.output_dir,
            ledger_path=args.ledger,
            asset_ids=args.asset_ids,
            max_bytes=args.max_bytes,
            timeout_seconds=args.timeout,
            probe=args.probe,
            allow_private_hosts=args.allow_private_hosts,
        )
    if command == "status" and args.queue:
        return Dispatcher(args.db).status(args.target)
    if command in {
        "enqueue",
        "claim",
        "complete",
        "renew-lease",
        "fail",
        "queue-status",
        "recover-expired",
        "dead-list",
        "dead-retry",
        "task-rework",
        "queue-limit",
        "simulate-day",
    }:
        dispatcher = Dispatcher(args.db)
        if command == "enqueue":
            payload = load_json_file(args.payload) if args.payload else {}
            if not isinstance(payload, dict):
                raise ValidationError("payload JSON must be an object")
            return dispatcher.enqueue(
                role=args.role,
                pod=args.pod,
                kind=args.kind,
                payload=payload,
                job_id=args.job_id,
                dependency_task_id=args.dependency_task_id,
                priority=args.priority,
                max_attempts=args.max_attempts,
                retry_backoff_seconds=args.retry_backoff_seconds,
                available_at=args.available_at,
                idempotency_key=args.idempotency_key,
            )
        if command == "claim":
            return dispatcher.claim(
                worker_id=args.worker,
                role=args.role,
                pod=args.pod,
                kind=args.kind,
                lease_seconds=args.lease_seconds,
                idempotency_key=args.idempotency_key,
            )
        if command == "renew-lease":
            return dispatcher.renew_lease(
                args.task_id,
                lease_token=args.lease_token,
                worker_id=args.worker,
                lease_seconds=args.lease_seconds,
            )
        if command == "complete":
            result = load_json_file(args.result) if args.result else {}
            if not isinstance(result, dict):
                raise ValidationError("result JSON must be an object")
            return dispatcher.complete(
                args.task_id,
                lease_token=args.lease_token,
                result=result,
                idempotency_key=args.idempotency_key,
            )
        if command == "fail":
            error = load_json_file(args.error) if args.error else {"message": args.reason}
            if not isinstance(error, dict):
                raise ValidationError("error JSON must be an object")
            return dispatcher.fail(
                args.task_id,
                lease_token=args.lease_token,
                error=error,
                terminal=args.terminal,
                idempotency_key=args.idempotency_key,
            )
        if command == "queue-status":
            return dispatcher.status(args.task_id)
        if command == "recover-expired":
            return dispatcher.recover_expired()
        if command == "dead-list":
            return dispatcher.dead_letters(
                status=None if args.status == "all" else args.status,
                task_id=args.task_id,
                limit=args.limit,
            )
        if command == "dead-retry":
            return dispatcher.retry_dead(
                args.task_id,
                reason=args.reason,
                actor=args.actor,
                additional_attempts=args.additional_attempts,
                available_at=args.available_at,
                cascade_dependents=args.cascade_dependents,
                idempotency_key=args.idempotency_key,
            )
        if command == "task-rework":
            payload_patch = load_json_file(args.payload_patch) if args.payload_patch else {}
            if not isinstance(payload_patch, dict):
                raise ValidationError("payload patch must be a JSON object")
            return dispatcher.rework_task(
                args.task_id,
                reason=args.reason,
                actor=args.actor,
                payload_patch=payload_patch,
                idempotency_key=args.idempotency_key,
            )
        if command == "queue-limit":
            return dispatcher.configure_limit(
                role=args.role, pod=args.pod, max_leased=args.max_leased
            )
        if command == "simulate-day":
            pods = [item.strip() for item in args.pods.split(",") if item.strip()]
            roles = [item.strip() for item in args.roles.split(",") if item.strip()]
            return dispatcher.simulate_day(
                target=args.target,
                pods=pods,
                roles=roles,
                lease_seconds=args.lease_seconds,
                idempotency_key=args.idempotency_key,
            )
        raise AssertionError(f"unhandled queue command: {command}")
    if command == "worker":
        registry = ExecutorRegistry()
        registry.register(
            "*",
            SubprocessExecutor(
                [args.handler_executable, *args.handler_arg],
                cwd=args.handler_cwd,
                timeout_seconds=args.handler_timeout,
                shutdown_grace_seconds=args.shutdown_grace,
                max_input_bytes=args.max_handler_input_bytes,
                max_output_bytes=args.max_handler_output_bytes,
                max_stderr_bytes=args.max_handler_stderr_bytes,
            ),
        )
        if args.resource_lock == "none":
            resource_lock = NullResourceLock()
        else:
            lock_path = (
                default_resource_lock_path(args.role)
                if args.resource_lock == "auto"
                else Path(args.resource_lock)
            )
            resource_lock = ResourceLock(lock_path) if lock_path else NullResourceLock()
        event_stream = getattr(args, "_event_stream", sys.stderr)

        def log_event(event: dict[str, Any]) -> None:
            if not args.quiet_events:
                print(json.dumps(event, ensure_ascii=False, sort_keys=True), file=event_stream)
                event_stream.flush()

        worker = HeadlessWorker(
            Dispatcher(args.db),
            registry,
            WorkerConfig(
                worker_id=args.worker_id,
                role=args.role,
                pod=args.pod,
                kind=args.kind,
                lease_seconds=args.lease_seconds,
                heartbeat_seconds=args.heartbeat_seconds,
                poll_seconds=args.poll_seconds,
                lock_timeout_seconds=args.lock_timeout_seconds,
                max_tasks=args.max_tasks,
                max_idle_polls=args.max_idle_polls,
                max_runtime_seconds=args.max_runtime_seconds,
                acknowledgement_attempts=args.acknowledgement_attempts,
                acknowledgement_retry_seconds=args.acknowledgement_retry_seconds,
                terminal_on_executor_error=args.terminal_on_handler_error,
            ),
            resource_lock=resource_lock,
            event_callback=log_event,
        )
        stop_event = threading.Event()
        restore = install_shutdown_handlers(stop_event)
        try:
            return worker.run(stop_event)
        finally:
            restore()
    factory = Factory(args.db)
    if command == "init":
        return factory.init()
    if command in {"start", "начинаем"}:
        return factory.start(
            args.ideas_file,
            batch_size=args.batch_size,
            idempotency_key=args.idempotency_key,
        )
    if command == "list":
        return factory.list(
            entity=args.entity,
            state=args.state,
            batch_id=args.batch_id,
            limit=args.limit,
        )
    if command in {"approve", "одобрить"}:
        return factory.approve(args.target, idempotency_key=args.idempotency_key)
    if command in {"reject", "отклонить"}:
        return factory.reject(
            args.target,
            reason=args.reason,
            idempotency_key=args.idempotency_key,
        )
    if command == "status":
        return factory.status(args.target)
    if command == "next":
        evidence = load_json_file(args.evidence) if args.evidence else None
        return factory.next(
            args.target,
            idempotency_key=args.idempotency_key,
            gate_result=args.gate_result,
            evidence=evidence,
        )
    raise AssertionError(f"unhandled command: {command}")


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    args._event_stream = err
    try:
        result = _run(args)
        export_path = getattr(args, "export", None)
        if export_path:
            Factory.export_json(result, Path(export_path))
        if (
            args.command in {"worker", "throughput-acceptance", "chat-audit"}
            and result.get("ok") is not True
        ):
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=err)
            return 3
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=out)
        return 0
    except FactoryError as exc:
        payload = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=err)
        return 2
    except OSError as exc:
        payload = {"ok": False, "error": {"code": "os_error", "message": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=err)
        return 2
