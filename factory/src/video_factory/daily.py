from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact_store import ArtifactStore
from .contracts import validate_artifact
from .dedup import evaluate_candidate
from .errors import ValidationError
from .lanes import SPECIALIZED_REVIEW_ROLES, lane_index, load_lane_registry, roles_for_lane
from .queue import Dispatcher
from .scout import run_scout
from .service import Factory
from .validators import canonical_json, digest_text


DEFAULT_ROLES = (
    "research",
    "rights",
    "script",
    "voice",
    "editor",
    "render",
    "qc",
    "final_review",
    "publisher",
)

ROLE_RESULT_CONTRACTS = {
    "research": "claim_ledger",
    "sensitivity_review": "safety_gate_report",
    "privacy_review": "safety_gate_report",
    "medical_review": "safety_gate_report",
    "rights": "rights_manifest",
    "script": "script_package",
    "voice": "voice_manifest",
    "source_audio": "source_audio_manifest",
    "editor": "shotlist",
    "render": "render_manifest",
    "qc": "qc_report",
    "publisher": "publish_manifest",
}

IDEA_CARD_FIELDS = {
    "schema_version",
    "idea_id",
    "pod",
    "title",
    "hook",
    "message",
    "why_now",
    "audience",
    "destination",
    "source_candidates",
    "visual_plan",
    "score",
    "risk",
    "status",
    "created_at",
}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(dict(payload)))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _production_date(value: date | str | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError("production_date must use YYYY-MM-DD") from exc
    raise ValidationError("production_date must be a date or YYYY-MM-DD")


def _target(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 10 <= value <= 15:
        raise ValidationError("target must be an integer from 10 to 15")
    return value


def _candidate_goal(target: int, expected_yield: float) -> int:
    if (
        isinstance(expected_yield, bool)
        or not isinstance(expected_yield, (int, float))
        or not 0.1 <= expected_yield <= 1.0
    ):
        raise ValidationError("expected_yield must be between 0.1 and 1.0")
    return min(50, math.ceil(target / float(expected_yield)))


def _legacy_idea(card: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(card)
    payload.update(
        {
            "id": card["idea_id"],
            "topic": card["pod"],
            "summary": card["message"],
        }
    )
    return payload


def prepare_day(
    *,
    db_path: str | Path,
    output_root: str | Path,
    artifact_root: str | Path,
    cache_dir: str | Path,
    target: int = 15,
    expected_yield: float = 0.55,
    production_date: date | str | None = None,
    offline: bool = False,
    timeout: float = 8.0,
    retries: int = 1,
    scout_result: Mapping[str, Any] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Discover, validate, deduplicate, persist, and open a human review batch."""

    target = _target(target)
    day = _production_date(production_date)
    candidate_goal = _candidate_goal(target, expected_yield)
    day_dir = Path(output_root).expanduser().resolve() / day.isoformat()
    result_path = day_dir / "prepare_day_result.json"
    request_signature = {
        "production_date": day.isoformat(),
        "target": target,
        "expected_yield": float(expected_yield),
        "offline": offline,
    }
    factory = Factory(db_path)
    factory.init()

    if scout_result is None and not force_refresh and result_path.exists():
        try:
            replay = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            replay = None
        if isinstance(replay, dict) and replay.get("request") == request_signature:
            batch_id = replay.get("batch_id")
            replay_jobs = replay.get("jobs")
            if isinstance(replay_jobs, list) and all(
                isinstance(item, dict) and isinstance(item.get("id"), str)
                for item in replay_jobs
            ):
                current_jobs = (
                    factory.list(entity="jobs", batch_id=batch_id, limit=1000)["items"]
                    if batch_id
                    else []
                )
                expected_ids = {item["id"] for item in replay_jobs}
                if expected_ids == {item["id"] for item in current_jobs}:
                    replay["replayed"] = True
                    return replay

    discovery = dict(scout_result) if scout_result is not None else run_scout(
        production_date=day,
        limit=candidate_goal,
        cache_dir=cache_dir,
        timeout=timeout,
        retries=retries,
        offline=offline,
    )
    ideas = discovery.get("ideas")
    ledgers = discovery.get("claim_ledgers")
    if not isinstance(ideas, list) or not isinstance(ledgers, list):
        raise ValidationError("scout result must contain ideas and claim_ledgers arrays")
    if len(ideas) != len(ledgers):
        raise ValidationError("scout ideas and claim_ledgers must have equal length")

    existing_rows = factory.list(entity="ideas", limit=1000)["items"]
    comparison_pool = [row["payload"] for row in existing_rows]
    accepted: list[dict[str, Any]] = []
    accepted_ledgers: dict[str, dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []
    for index, (raw_idea, raw_ledger) in enumerate(zip(ideas, ledgers, strict=True)):
        idea = validate_artifact("idea_card", raw_idea)
        ledger = validate_artifact("claim_ledger", raw_ledger)
        if ledger["idea_id"] != idea["idea_id"]:
            raise ValidationError(f"claim_ledgers[{index}].idea_id does not match idea")
        decision = evaluate_candidate(idea, comparison_pool).as_dict()
        decisions.append({"idea_id": idea["idea_id"], **decision})
        if decision["decision"] == "block":
            continue
        accepted.append(idea)
        accepted_ledgers[idea["idea_id"]] = ledger
        comparison_pool.append(idea)

    source_payload = {
        "ideas": [_legacy_idea(card) for card in accepted],
        "production_date": day.isoformat(),
        "target": target,
    }
    source_digest = digest_text(canonical_json(source_payload) + "\n")
    # Content-addressing prevents concurrent refreshes from overwriting the file
    # between the atomic write and Factory.start reading it.
    source_path = day_dir / f"review_candidates-{source_digest[:16]}.json"
    _atomic_json(source_path, source_payload)
    batch = factory.start(
        source_path,
        batch_size=max(1, len(accepted)),
        idempotency_key=f"prepare-day:{day.isoformat()}:{target}:{source_digest}",
    ) if accepted else {
        "ok": True,
        "command": "start",
        "batch_id": None,
        "jobs": [],
        "imported_ideas": 0,
        "existing_ideas": 0,
    }

    cards_by_id = {item["idea_id"]: item for item in accepted}
    store = ArtifactStore(artifact_root)
    artifact_records: list[dict[str, Any]] = []
    for job in batch["jobs"]:
        idea_id = job["idea_id"]
        if idea_id not in cards_by_id:
            continue
        card_record = store.put(
            job_id=job["id"],
            kind="idea_card",
            payload=cards_by_id[idea_id],
            producer="scout_agent",
            producer_version="1.0.0",
            prompt_version="scout-runtime-1.0.0",
        )
        ledger_record = store.put(
            job_id=job["id"],
            kind="claim_ledger",
            payload=accepted_ledgers[idea_id],
            producer="scout_agent",
            producer_version="1.0.0",
            prompt_version="scout-runtime-1.0.0",
            dependencies=[card_record],
        )
        artifact_records.extend((card_record, ledger_record))

    result = {
        "schema_version": "1.0.0",
        "ok": True,
        "command": "prepare-day",
        "request": request_signature,
        "replayed": False,
        "force_refreshed": bool(force_refresh),
        "source_digest": source_digest,
        "production_date": day.isoformat(),
        "target_outputs": target,
        "candidate_goal": candidate_goal,
        "discovered": len(ideas),
        "accepted_for_review": len(accepted),
        "blocked_as_duplicates": sum(
            item["decision"] == "block" for item in decisions
        ),
        "needs_more_candidates": len(accepted) < candidate_goal,
        "human_topic_gate_required": True,
        "batch_id": batch.get("batch_id"),
        "jobs": batch["jobs"],
        "proposals": [
            {
                "idea_id": item["idea_id"],
                "pod": item["pod"],
                "title": item["title"],
                "hook": item["hook"],
                "risk": item["risk"],
            }
            for item in accepted
        ],
        "dedup": decisions,
        "artifact_count": len(artifact_records),
        "paths": {
            "day_dir": str(day_dir),
            "candidate_file": str(source_path),
            "artifact_root": str(Path(artifact_root).expanduser().resolve()),
        },
        "scout": {
            "mode": discovery.get("mode"),
            "warnings": discovery.get("warnings", []),
        },
    }
    _atomic_json(result_path, result)
    return result


def launch_approved(
    *,
    db_path: str | Path,
    batch_id: str | None = None,
    roles: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Create one dependency chain per topic-approved job, idempotently.

    With ``roles=None`` the canonical lane registry selects the chain. Legacy or
    unregistered lanes retain ``DEFAULT_ROLES`` for backward compatibility.
    """

    explicit_roles: list[str] | None = None
    if roles is not None:
        if not roles:
            raise ValidationError("roles must not be empty")
        explicit_roles = []
        for role in roles:
            if not isinstance(role, str) or not role.strip():
                raise ValidationError("every role must be a non-empty string")
            explicit_roles.append(role.strip())
        if explicit_roles[-2:] != ["final_review", "publisher"]:
            raise ValidationError("the role chain must end with final_review,publisher")

    registry = load_lane_registry()
    lanes = lane_index(registry)

    factory = Factory(db_path)
    approved = factory.list(entity="jobs", state="approved", batch_id=batch_id, limit=1000)[
        "items"
    ]
    dispatcher = Dispatcher(db_path)
    chains: list[dict[str, Any]] = []
    approved_ideas = factory.list(entity="ideas", state="approved", limit=1000)["items"]
    idea_by_id = {item["id"]: item for item in approved_ideas}
    roles_by_lane: dict[str, list[str]] = {}
    for job in sorted(approved, key=lambda item: item["id"]):
        pod = (idea_by_id.get(job["idea_id"], {}).get("topic") or "unassigned")
        raw_idea = idea_by_id.get(job["idea_id"], {}).get("payload")
        idea_card = None
        if isinstance(raw_idea, Mapping):
            candidate_card = {
                key: value for key, value in raw_idea.items() if key in IDEA_CARD_FIELDS
            }
            try:
                validate_artifact("idea_card", candidate_card)
            except ValidationError:
                idea_card = None
            else:
                idea_card = candidate_card
        lane = lanes.get(pod)
        normalized_roles = list(
            explicit_roles
            if explicit_roles is not None
            else roles_for_lane(pod, registry=registry, fallback=DEFAULT_ROLES)
        )
        roles_by_lane[pod] = normalized_roles
        risk_profile = lane.get("risk_profile") if lane else "legacy_standard"
        required_gate_roles = set(lane.get("required_gate_roles", ())) if lane else {
            "rights",
            "qc",
            "final_review",
        }
        specialized_review_role = SPECIALIZED_REVIEW_ROLES.get(str(risk_profile))
        dependency: str | None = None
        tasks: list[dict[str, Any]] = []
        for position, role in enumerate(normalized_roles):
            response = dispatcher.enqueue(
                role=role,
                pod=pod,
                kind=f"{role}_job",
                payload={
                    "job_id": job["id"],
                    "idea_id": job["idea_id"],
                    "idea_card": idea_card,
                    "batch_id": job["batch_id"],
                    "lane_id": pod,
                    "risk_profile": risk_profile,
                    "gate_policy_version": registry["registry_version"],
                    "structured_gate_required": role in required_gate_roles,
                    "required_result_contract": ROLE_RESULT_CONTRACTS.get(role),
                    "specialized_review_role": specialized_review_role,
                    "human_gate": role == "final_review",
                    "checksum_bound": role == "final_review",
                    "publish_requires_final_review": role == "publisher",
                },
                job_id=job["id"],
                dependency_task_id=dependency,
                priority=100 - position,
                max_attempts=3,
                retry_backoff_seconds=60,
                # The position, not the requested role, owns the idempotency key.
                # A later call with a different chain must conflict at the first
                # changed position instead of silently creating a second publisher
                # branch for the same approved job.
                idempotency_key=f"launch:{job['id']}:{position}",
            )
            task = response["task"]
            tasks.append(task)
            dependency = task["id"]
        chains.append(
            {
                "job_id": job["id"],
                "idea_id": job["idea_id"],
                "lane_id": pod,
                "risk_profile": risk_profile,
                "roles": normalized_roles,
                "tasks": tasks,
            }
        )

    unique_chains = {tuple(value) for value in roles_by_lane.values()}
    reported_roles: list[str] | None
    if explicit_roles is not None:
        reported_roles = explicit_roles
    elif len(unique_chains) == 1:
        reported_roles = list(next(iter(unique_chains)))
    elif not unique_chains:
        reported_roles = list(DEFAULT_ROLES)
    else:
        reported_roles = None

    return {
        "ok": True,
        "command": "launch-approved",
        "batch_id": batch_id,
        "approved_jobs": len(approved),
        "chains_created": len(chains),
        "tasks_created_or_replayed": sum(len(chain["tasks"]) for chain in chains),
        "roles_mode": "explicit" if explicit_roles is not None else "lane_registry",
        "roles": reported_roles,
        "roles_by_lane": roles_by_lane,
        "registry_version": registry["registry_version"],
        "human_publish_gate_preserved": True,
        "chains": chains,
    }
