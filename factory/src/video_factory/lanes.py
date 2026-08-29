from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ValidationError


LEGACY_LANE_IDS = ("space_technology", "nature_animals", "people_culture")
REQUIRED_PACKAGE_FILES = (
    "TOPIC_PACK.json",
    "EDITORIAL_PLAYBOOK.md",
    "SOURCE_POLICY.md",
    "candidate_pool.json",
    "STATUS.md",
)
SAFETY_FILES = {
    "war_sensitivity": "SAFETY.md",
    "privacy_defamation": "SAFETY.md",
    "editorial_standard": "SAFETY.md",
    "medical_safety": "MEDICAL_SAFETY.md",
}
SPECIALIZED_REVIEW_ROLES = {
    "war_sensitivity": "sensitivity_review",
    "privacy_defamation": "privacy_review",
    "medical_safety": "medical_review",
}
_LANE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_CHAT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def factory_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_registry_path() -> Path:
    return factory_root() / "lanes" / "registry.json"


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot load {label} from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object")
    return value


def _strings(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{label} must be a non-empty array")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValidationError(f"{label} must contain non-empty strings")
    result = [item.strip() for item in value]
    if len(result) != len(set(result)):
        raise ValidationError(f"{label} must not contain duplicates")
    return result


def load_lane_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = (
        Path(path).expanduser().resolve() if path is not None else default_registry_path()
    )
    registry = _load_object(registry_path, label="lane registry")
    if registry.get("schema_version") != "1.0.0":
        raise ValidationError("lane registry schema_version must equal '1.0.0'")
    lanes = registry.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise ValidationError("lane registry lanes must be a non-empty array")

    seen: set[str] = set()
    seen_chat_ids: set[str] = set()
    daily_totals = {"min": 0, "target": 0, "max": 0}
    for index, lane in enumerate(lanes):
        label = f"lane registry lanes[{index}]"
        if not isinstance(lane, dict):
            raise ValidationError(f"{label} must be an object")
        lane_id = lane.get("id")
        if not isinstance(lane_id, str) or _LANE_ID.fullmatch(lane_id) is None:
            raise ValidationError(f"{label}.id is invalid")
        if lane_id in seen:
            raise ValidationError(f"duplicate lane id: {lane_id}")
        seen.add(lane_id)
        chat_id = lane.get("chat_id")
        if not isinstance(chat_id, str) or _CHAT_ID.fullmatch(chat_id) is None:
            raise ValidationError(f"{label}.chat_id is invalid")
        if chat_id in seen_chat_ids:
            raise ValidationError(f"duplicate lane chat_id: {chat_id}")
        seen_chat_ids.add(chat_id)
        if not isinstance(lane.get("enabled"), bool):
            raise ValidationError(f"{label}.enabled must be boolean")
        if not isinstance(lane.get("package_dir"), str) or not lane["package_dir"]:
            raise ValidationError(f"{label}.package_dir must be a non-empty string")
        if Path(lane["package_dir"]).name != lane["package_dir"]:
            raise ValidationError(f"{label}.package_dir must be one directory name")
        roles = _strings(lane.get("roles"), label=f"{label}.roles")
        if roles[-2:] != ["final_review", "publisher"]:
            raise ValidationError(f"{label}.roles must end with final_review,publisher")
        for required in ("research", "rights", "qc"):
            if required not in roles:
                raise ValidationError(f"{label}.roles must include {required}")
        if lane_id == "motivation":
            if "source_audio" not in roles or "voice" in roles:
                raise ValidationError(
                    f"{label}.roles must use source_audio and must not use voice"
                )
        elif lane_id in {"war_history", "celebrity_news", "chinese_medicine", "health"}:
            if "voice" not in roles or "source_audio" in roles:
                raise ValidationError(
                    f"{label}.roles must use voice and must not use source_audio"
                )
        gate_roles = _strings(
            lane.get("required_gate_roles"), label=f"{label}.required_gate_roles"
        )
        if not set(gate_roles).issubset(roles):
            raise ValidationError(f"{label}.required_gate_roles must be present in roles")
        risk_profile = lane.get("risk_profile")
        if risk_profile not in SAFETY_FILES:
            raise ValidationError(f"{label}.risk_profile is unknown")
        specialized = SPECIALIZED_REVIEW_ROLES.get(risk_profile)
        if specialized and specialized not in gate_roles:
            raise ValidationError(f"{label} must require {specialized}")

        daily = lane.get("daily")
        if not isinstance(daily, dict):
            raise ValidationError(f"{label}.daily must be an object")
        values: list[int] = []
        for key in ("min", "target", "max"):
            value = daily.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(f"{label}.daily.{key} must be a non-negative integer")
            values.append(value)
        if not values[0] <= values[1] <= values[2]:
            raise ValidationError(f"{label}.daily must satisfy min <= target <= max")
        if lane["enabled"]:
            for key, value in zip(("min", "target", "max"), values):
                daily_totals[key] += value

    production = registry.get("production_contract")
    if not isinstance(production, dict):
        raise ValidationError("lane registry production_contract must be an object")
    expected = {
        "min": production.get("daily_min"),
        "target": production.get("daily_target"),
        "max": production.get("daily_max"),
    }
    if daily_totals != expected:
        raise ValidationError(
            f"enabled lane totals {daily_totals!r} do not match production contract {expected!r}"
        )
    if not 10 <= daily_totals["min"] <= daily_totals["target"] <= 15:
        raise ValidationError("enabled lane allocation must support 10-15 outputs per day")
    return registry


def lane_index(registry: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    source = dict(registry) if registry is not None else load_lane_registry()
    return {lane["id"]: dict(lane) for lane in source["lanes"]}


def enabled_lane_ids(registry: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    source = dict(registry) if registry is not None else load_lane_registry()
    return tuple(lane["id"] for lane in source["lanes"] if lane["enabled"])


def roles_for_lane(
    lane_id: str,
    *,
    registry: Mapping[str, Any] | None = None,
    fallback: Sequence[str] | None = None,
) -> tuple[str, ...]:
    lane = lane_index(registry).get(lane_id)
    if lane is not None and lane.get("enabled"):
        return tuple(lane["roles"])
    if fallback is not None:
        return tuple(fallback)
    raise ValidationError(f"unknown or disabled lane: {lane_id}")


def validate_lane_packages(
    *, registry_path: str | Path | None = None, minimum_candidates: int = 20
) -> dict[str, Any]:
    if isinstance(minimum_candidates, bool) or minimum_candidates < 1:
        raise ValidationError("minimum_candidates must be a positive integer")
    registry = load_lane_registry(registry_path)
    base = (
        Path(registry_path).expanduser().resolve().parent
        if registry_path is not None
        else default_registry_path().parent
    )
    reports: list[dict[str, Any]] = []
    total_candidates = 0
    errors: list[str] = []
    for lane in registry["lanes"]:
        if not lane["enabled"]:
            continue
        lane_dir = base / lane["package_dir"]
        required = list(REQUIRED_PACKAGE_FILES)
        required.append(SAFETY_FILES[lane["risk_profile"]])
        missing = [name for name in required if not (lane_dir / name).is_file()]
        lane_errors = [f"missing {name}" for name in missing]
        topic_pack: dict[str, Any] | None = None
        pool: dict[str, Any] | None = None
        if not missing:
            try:
                topic_pack = _load_object(lane_dir / "TOPIC_PACK.json", label="topic pack")
                pool = _load_object(lane_dir / "candidate_pool.json", label="candidate pool")
            except ValidationError as exc:
                lane_errors.append(str(exc))
        ideas = pool.get("ideas") if pool else None
        if pool is not None and (
            not isinstance(ideas, list) or not all(isinstance(item, dict) for item in ideas)
        ):
            lane_errors.append("candidate_pool.json ideas must be an array of objects")
            ideas = []
        ideas = ideas or []
        if len(ideas) < minimum_candidates:
            lane_errors.append(
                f"candidate_pool.json has {len(ideas)} ideas; need at least {minimum_candidates}"
            )
        ids = [item.get("id") for item in ideas]
        if any(not isinstance(item, str) or not item.strip() for item in ids):
            lane_errors.append("every candidate must have a non-empty string id")
        elif len(ids) != len(set(ids)):
            lane_errors.append("candidate ids must be unique within the lane")
        total_candidates += len(ideas)
        errors.extend(f"{lane['id']}: {item}" for item in lane_errors)
        reports.append(
            {
                "lane_id": lane["id"],
                "title_ru": lane["title_ru"],
                "chat_id": lane.get("chat_id"),
                "package_dir": str(lane_dir.resolve()),
                "candidate_count": len(ideas),
                "topic_pack_loaded": topic_pack is not None,
                "risk_profile": lane["risk_profile"],
                "roles": lane["roles"],
                "ok": not lane_errors,
                "errors": lane_errors,
            }
        )
    return {
        "ok": not errors,
        "registry_version": registry["registry_version"],
        "enabled_lanes": len(reports),
        "total_candidates": total_candidates,
        "minimum_candidates_per_lane": minimum_candidates,
        "daily_allocation": {
            key: sum(lane["daily"][key] for lane in registry["lanes"] if lane["enabled"])
            for key in ("min", "target", "max")
        },
        "lanes": reports,
        "errors": errors,
    }
