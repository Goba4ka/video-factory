from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError


RIGHTS_BASES = frozenset(
    {"licensed", "public_domain", "owned", "permission", "creative_commons"}
)
REQUIRED_QC_CHECKS = frozenset(
    {"duration", "aspect_ratio", "captions", "audio", "rights"}
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def require_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def load_json_file(path: str | Path) -> Any:
    source = Path(path)
    try:
        return json.loads(source.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValidationError(f"JSON file does not exist: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"invalid JSON in {source}: line {exc.lineno}, column {exc.colno}"
        ) from exc


def load_ideas(path: str | Path) -> list[dict[str, Any]]:
    document = load_json_file(path)
    if isinstance(document, dict):
        if "ideas" not in document:
            raise ValidationError("JSON object must contain an 'ideas' array")
        document = document["ideas"]
    if not isinstance(document, list):
        raise ValidationError("ideas JSON must be an array or {'ideas': [...]} object")
    if not document:
        raise ValidationError("ideas JSON must contain at least one idea")

    ideas: list[dict[str, Any]] = []
    for index, item in enumerate(document):
        if not isinstance(item, dict):
            raise ValidationError(f"idea at index {index} must be a JSON object")
        require_nonempty_string(item.get("title"), f"ideas[{index}].title")
        if "id" in item:
            idea_id = require_nonempty_string(item["id"], f"ideas[{index}].id")
            if len(idea_id) > 128:
                raise ValidationError(f"ideas[{index}].id must be at most 128 characters")
        ideas.append(item)
    return ideas


def normalize_idea(item: Mapping[str, Any]) -> dict[str, Any]:
    payload_json = canonical_json(dict(item))
    supplied_id = item.get("id")
    idea_id = (
        require_nonempty_string(supplied_id, "idea.id")
        if supplied_id is not None
        else f"idea_{digest_text(payload_json)[:20]}"
    )
    topic = item.get("topic")
    summary = item.get("summary")
    if topic is not None and not isinstance(topic, str):
        raise ValidationError("idea.topic must be a string when provided")
    if summary is not None and not isinstance(summary, str):
        raise ValidationError("idea.summary must be a string when provided")
    return {
        "id": idea_id,
        "title": require_nonempty_string(item.get("title"), "idea.title"),
        "topic": topic,
        "summary": summary,
        "payload_json": payload_json,
    }


def validate_batch_size(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise ValidationError("batch_size must be an integer from 1 to 100")
    return value


def validate_gate_result(value: str | None) -> str:
    if value not in {"pass", "fail"}:
        raise ValidationError("gate_result must be 'pass' or 'fail' for this state")
    return value


def _validate_failure_evidence(evidence: Any, gate: str) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise ValidationError(f"{gate} failure evidence must be a JSON object")
    require_nonempty_string(evidence.get("reason"), f"{gate}.reason")
    return evidence


def validate_rights_evidence(result: str, evidence: Any) -> dict[str, Any]:
    result = validate_gate_result(result)
    if result == "fail":
        return _validate_failure_evidence(evidence, "rights")
    if not isinstance(evidence, dict):
        raise ValidationError("rights evidence must be a JSON object")
    items = evidence.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError("rights pass requires a non-empty evidence.items array")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError(f"rights.items[{index}] must be an object")
        require_nonempty_string(item.get("asset"), f"rights.items[{index}].asset")
        basis = require_nonempty_string(item.get("basis"), f"rights.items[{index}].basis")
        if basis not in RIGHTS_BASES:
            allowed = ", ".join(sorted(RIGHTS_BASES))
            raise ValidationError(
                f"rights.items[{index}].basis must be one of: {allowed}"
            )
        require_nonempty_string(
            item.get("reference"), f"rights.items[{index}].reference"
        )
    return evidence


def validate_qc_evidence(result: str, evidence: Any) -> dict[str, Any]:
    result = validate_gate_result(result)
    if result == "fail":
        return _validate_failure_evidence(evidence, "qc")
    if not isinstance(evidence, dict):
        raise ValidationError("QC evidence must be a JSON object")
    checks = evidence.get("checks")
    if not isinstance(checks, dict):
        raise ValidationError("QC pass requires an evidence.checks object")
    missing = sorted(REQUIRED_QC_CHECKS.difference(checks))
    if missing:
        raise ValidationError(f"QC evidence is missing checks: {', '.join(missing)}")
    failed = sorted(name for name in REQUIRED_QC_CHECKS if checks.get(name) is not True)
    if failed:
        raise ValidationError(f"QC checks must be true: {', '.join(failed)}")
    return evidence

