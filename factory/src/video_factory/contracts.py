from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .errors import ValidationError
from .validators import canonical_json, digest_text


CONTRACT_FILES = {
    "idea_card": "idea_card.schema.json",
    "claim_ledger": "claim_ledger.schema.json",
    "rights_manifest": "rights_manifest.schema.json",
    "frozen_media_manifest": "frozen_media_manifest.schema.json",
    "voice_manifest": "voice_manifest.schema.json",
    "source_audio_manifest": "source_audio_manifest.schema.json",
    "voice_defect": "voice_defect.schema.json",
    "voice_rights_approval": "voice_rights_approval.schema.json",
    "script_package": "script_package.schema.json",
    "shotlist": "shotlist.schema.json",
    "render_manifest": "render_manifest.schema.json",
    "qc_report": "qc_report.schema.json",
    "publish_manifest": "publish_manifest.schema.json",
    "metrics_snapshot": "metrics_snapshot.schema.json",
    "daily_batch": "daily_batch.schema.json",
    "safety_gate_report": "safety_gate_report.schema.json",
}


REQUIRED_SAFETY_GATES = {
    "war_history": "war_sensitivity",
    "celebrity_news": "privacy_defamation",
    "chinese_medicine": "medical_safety",
    "health": "medical_safety",
}


def contracts_dir() -> Path:
    """Return the installed package's canonical JSON Schema directory.

    Schemas are runtime dependencies, not repository-only development files.
    Keeping the lookup relative to the importable package makes validation work
    identically from a source tree, a wheel and an sdist installation.
    """

    return Path(__file__).resolve().with_name("schemas")


def load_contract(name: str, *, root: str | Path | None = None) -> dict[str, Any]:
    try:
        filename = CONTRACT_FILES[name]
    except KeyError as exc:
        allowed = ", ".join(sorted(CONTRACT_FILES))
        raise ValidationError(f"unknown contract {name!r}; expected one of: {allowed}") from exc
    path = (Path(root) if root is not None else contracts_dir()) / filename
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot load contract {name!r} from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"contract {name!r} must be a JSON object")
    return payload


def _path(parent: str, child: str | int) -> str:
    if isinstance(child, int):
        return f"{parent}[{child}]"
    return f"{parent}.{child}" if parent else child


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    raise ValidationError(f"unsupported JSON Schema type {expected!r}")


def _validate_format(value: str, format_name: str, path: str) -> None:
    if format_name == "uri":
        parsed = urlparse(value)
        if not parsed.scheme or (parsed.scheme in {"http", "https"} and not parsed.netloc):
            raise ValidationError(f"{path} must be a valid URI")
        return
    if format_name == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{path} must be an ISO 8601 date-time") from exc
        return
    if format_name == "date":
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{path} must be an ISO 8601 date") from exc
        return
    raise ValidationError(f"unsupported JSON Schema format {format_name!r}")


def _validate(value: Any, schema: Mapping[str, Any], path: str) -> None:
    all_of = schema.get("allOf", [])
    if not isinstance(all_of, list) or not all(
        isinstance(item, Mapping) for item in all_of
    ):
        raise ValidationError(f"invalid allOf declaration in schema at {path}")
    for item in all_of:
        _validate(value, item, path)

    condition = schema.get("if")
    if condition is not None:
        if not isinstance(condition, Mapping):
            raise ValidationError(f"invalid if declaration in schema at {path}")
        try:
            _validate(value, condition, path)
        except ValidationError:
            branch = schema.get("else")
        else:
            branch = schema.get("then")
        if branch is not None:
            if not isinstance(branch, Mapping):
                raise ValidationError(f"invalid conditional branch in schema at {path}")
            _validate(value, branch, path)

    expected = schema.get("type")
    if expected is not None:
        alternatives = [expected] if isinstance(expected, str) else expected
        if not isinstance(alternatives, list) or not all(
            isinstance(item, str) for item in alternatives
        ):
            raise ValidationError(f"invalid type declaration in schema at {path}")
        if not any(_type_matches(value, item) for item in alternatives):
            label = " or ".join(alternatives)
            raise ValidationError(f"{path} must be {label}")

    if "const" in schema and value != schema["const"]:
        raise ValidationError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path} must be one of {schema['enum']!r}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValidationError(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValidationError(f"{path} is longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValidationError(f"{path} does not match required pattern")
        if "format" in schema:
            _validate_format(value, schema["format"], path)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"{path} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"{path} is above maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValidationError(
                f"{path} must be greater than {schema['exclusiveMinimum']}"
            )

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValidationError(f"{path} has fewer items than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValidationError(f"{path} has more items than maxItems")
        if schema.get("uniqueItems"):
            keys = [canonical_json(item) for item in value]
            if len(keys) != len(set(keys)):
                raise ValidationError(f"{path} items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate(item, item_schema, _path(path, index))

    if isinstance(value, Mapping):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ValidationError(f"{path} is missing required fields: {', '.join(missing)}")
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ValidationError(f"invalid properties declaration in schema at {path}")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            item_path = _path(path, str(key))
            if key in properties:
                _validate(item, properties[key], item_path)
            elif additional is False:
                raise ValidationError(f"{item_path} is not allowed")
            elif isinstance(additional, Mapping):
                _validate(item, additional, item_path)


def validate_artifact(
    name: str,
    document: Any,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValidationError(f"{name} artifact must be a JSON object")
    _validate(document, load_contract(name, root=root), name)
    if name == "source_audio_manifest":
        if document["source_out_seconds"] <= document["source_in_seconds"]:
            raise ValidationError(
                "source_audio_manifest.source_out_seconds must be greater than source_in_seconds"
            )
        transcript_sha256 = document["checksums"]["transcript_sha256"]
        if transcript_sha256 != digest_text(document["transcript"]):
            raise ValidationError(
                "source_audio_manifest transcript_sha256 does not match transcript"
            )
    if name == "script_package":
        russian_fields = [
            document["hook"]["spoken_text"],
            document["hook"]["first_frame_text"],
            *(item["spoken_text"] for item in document["segments"]),
            *(item["caption_text"] for item in document["segments"]),
        ]
        if any(re.search(r"[А-Яа-яЁё]", value) is None for value in russian_fields):
            raise ValidationError("script_package spoken text and captions must be Russian")
        previous_end = 0.0
        max_words = document["caption_style"]["max_words_per_card"]
        for index, segment in enumerate(document["segments"]):
            start = float(segment["start_seconds"])
            end = float(segment["end_seconds"])
            if end <= start:
                raise ValidationError(
                    f"script_package.segments[{index}] end_seconds must exceed start_seconds"
                )
            if index == 0 and start > 0.25:
                raise ValidationError("script_package first segment must start by 0.25s")
            if start < previous_end - 0.001:
                raise ValidationError("script_package segments must not overlap")
            if start - previous_end > 1.0:
                raise ValidationError("script_package has an unexplained gap longer than 1s")
            if len(segment["caption_text"].split()) > max_words:
                raise ValidationError(
                    f"script_package.segments[{index}] caption exceeds max_words_per_card"
                )
            previous_end = end
        target = float(document["target_duration_seconds"])
        if abs(previous_end - target) > 2.0:
            raise ValidationError(
                "script_package final segment must end within 2s of target duration"
            )
        decision = document["decision"]
        if decision["passed"] and decision["needs_human_review"]:
            raise ValidationError(
                "script_package cannot pass while needs_human_review is true"
            )
    if name == "shotlist":
        previous_end = 0.0
        shot_ids: set[str] = set()
        for index, shot in enumerate(document["shots"]):
            shot_id = shot["shot_id"]
            if shot_id in shot_ids:
                raise ValidationError(f"shotlist contains duplicate shot_id {shot_id!r}")
            shot_ids.add(shot_id)
            start = float(shot["start"])
            end = float(shot["end"])
            if end <= start:
                raise ValidationError(
                    f"shotlist.shots[{index}] end must be greater than start"
                )
            if index == 0 and start > 0.25:
                raise ValidationError("shotlist must begin by 0.25 seconds")
            if start < previous_end - 0.001:
                raise ValidationError("shotlist shots must not overlap")
            if start - previous_end > 0.25:
                raise ValidationError("shotlist contains an unexplained gap over 0.25s")
            source_in = shot.get("source_in")
            source_out = shot.get("source_out")
            if (source_in is None) != (source_out is None):
                raise ValidationError(
                    f"shotlist.shots[{index}] source_in and source_out must appear together"
                )
            if source_in is not None:
                if float(source_out) <= float(source_in):
                    raise ValidationError(
                        f"shotlist.shots[{index}] source_out must exceed source_in"
                    )
                if (float(source_out) - float(source_in)) + 0.05 < (end - start):
                    raise ValidationError(
                        f"shotlist.shots[{index}] source range is shorter than timeline slot"
                    )
            previous_end = end
        if abs(previous_end - float(document["duration_seconds"])) > 0.25:
            raise ValidationError(
                "shotlist final shot must end within 0.25s of duration_seconds"
            )
    if name == "frozen_media_manifest":
        asset_ids: set[str] = set()
        network_downloads = 0
        local_copies = 0
        cache_hits = 0
        for index, asset in enumerate(document["assets"]):
            asset_id = asset["asset_id"]
            if asset_id in asset_ids:
                raise ValidationError(
                    f"frozen_media_manifest contains duplicate asset_id {asset_id!r}"
                )
            asset_ids.add(asset_id)
            if asset["rights"]["asset_id"] != asset_id:
                raise ValidationError(
                    f"frozen_media_manifest.assets[{index}].rights.asset_id does not match"
                )
            source = asset["source"]
            if source["kind"] == "local_file":
                if source["final_url"] is not None:
                    raise ValidationError(
                        f"frozen_media_manifest.assets[{index}] local source has final_url"
                    )
                if not asset["reused_existing"]:
                    local_copies += 1
            else:
                if not isinstance(source["final_url"], str) or not source["final_url"]:
                    raise ValidationError(
                        f"frozen_media_manifest.assets[{index}] download source lacks final_url"
                    )
                if not asset["reused_existing"]:
                    network_downloads += 1
            if asset["reused_existing"]:
                cache_hits += 1
        decision = document["decision"]
        if decision["asset_count"] != len(document["assets"]):
            raise ValidationError(
                "frozen_media_manifest decision.asset_count does not match assets"
            )
        counts = {
            "network_downloads": network_downloads,
            "local_copies": local_copies,
            "cache_hits": cache_hits,
        }
        for field, expected in counts.items():
            if decision[field] != expected:
                raise ValidationError(
                    f"frozen_media_manifest decision.{field} does not match assets"
                )
    return document


def validate_production_chain(
    *,
    idea_card: dict[str, Any],
    claim_ledger: dict[str, Any],
    rights_manifest: dict[str, Any],
    shotlist: dict[str, Any],
    safety_gate_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_artifact("idea_card", idea_card)
    validate_artifact("claim_ledger", claim_ledger)
    validate_artifact("rights_manifest", rights_manifest)
    validate_artifact("shotlist", shotlist)
    if safety_gate_report is not None:
        validate_artifact("safety_gate_report", safety_gate_report)

    idea_id = idea_card["idea_id"]
    errors: list[str] = []
    required_gate = REQUIRED_SAFETY_GATES.get(idea_card["pod"])
    if required_gate is not None:
        if safety_gate_report is None:
            errors.append(
                f"lane {idea_card['pod']} requires a passed {required_gate} safety gate"
            )
        else:
            if safety_gate_report["idea_id"] != idea_id:
                errors.append("safety_gate_report.idea_id does not match idea_card.idea_id")
            if safety_gate_report["lane"] != idea_card["pod"]:
                errors.append("safety_gate_report.lane does not match idea_card.pod")
            if safety_gate_report["gate_type"] != required_gate:
                errors.append(
                    f"safety_gate_report.gate_type must be {required_gate} for {idea_card['pod']}"
                )
            safety_decision = safety_gate_report["decision"]
            blocking = any(
                item["severity"] == "blocking" for item in safety_gate_report["findings"]
            )
            if (
                not safety_decision["passed"]
                or safety_decision["needs_human_review"]
                or blocking
            ):
                errors.append("safety gate report has not passed its hard gate")
    for name, artifact in (
        ("claim_ledger", claim_ledger),
        ("rights_manifest", rights_manifest),
        ("shotlist", shotlist),
    ):
        if artifact["idea_id"] != idea_id:
            errors.append(f"{name}.idea_id does not match idea_card.idea_id")

    source_ids = {item["source_id"] for item in claim_ledger["sources"]}
    if safety_gate_report is not None:
        unknown_safety_sources = sorted(
            set(safety_gate_report["source_ids_checked"]) - source_ids
        )
        if unknown_safety_sources:
            errors.append(
                "safety gate references unknown claim-ledger sources: "
                + ", ".join(unknown_safety_sources)
            )
    claim_ids = {item["claim_id"] for item in claim_ledger["claims"]}
    for claim in claim_ledger["claims"]:
        unknown = sorted(set(claim["source_ids"]) - source_ids)
        if unknown:
            errors.append(
                f"claim {claim['claim_id']} references unknown sources: {', '.join(unknown)}"
            )

    rights_by_asset = {item["asset_id"]: item for item in rights_manifest["assets"]}
    for shot in shotlist["shots"]:
        asset_id = shot["asset_id"]
        if asset_id not in rights_by_asset:
            errors.append(f"shot {shot['shot_id']} references unknown asset {asset_id}")
        else:
            rights = rights_by_asset[asset_id]
            if rights["rights_status"] != "approved":
                errors.append(f"asset {asset_id} is not rights-approved")
            if not rights["commercial_use"] or not rights["modification_allowed"]:
                errors.append(f"asset {asset_id} is not cleared for commercial editing")
        unknown_claims = sorted(set(shot["claim_ids"]) - claim_ids)
        if unknown_claims:
            errors.append(
                f"shot {shot['shot_id']} references unknown claims: {', '.join(unknown_claims)}"
            )

    claim_decision = claim_ledger["decision"]
    if not claim_decision["passed"] or claim_decision["needs_human_review"]:
        errors.append("claim ledger has not passed its hard gate")
    rights_decision = rights_manifest["decision"]
    if (
        not rights_decision["passed"]
        or rights_decision["needs_human_review"]
        or rights_decision["missing_asset_ids"]
    ):
        errors.append("rights manifest has not passed its hard gate")

    return {
        "ok": not errors,
        "idea_id": idea_id,
        "artifacts_valid": True,
        "production_ready": not errors,
        "errors": errors,
        "counts": {
            "sources": len(source_ids),
            "claims": len(claim_ids),
            "rights_assets": len(rights_by_asset),
            "shots": len(shotlist["shots"]),
            "safety_findings": len(safety_gate_report["findings"])
            if safety_gate_report is not None
            else 0,
        },
    }


def load_and_validate_chain(
    paths: Mapping[str, str | Path],
) -> dict[str, Any]:
    required = ("idea_card", "claim_ledger", "rights_manifest", "shotlist")
    missing = [name for name in required if name not in paths]
    if missing:
        raise ValidationError(f"chain paths missing: {', '.join(missing)}")
    artifacts: dict[str, dict[str, Any]] = {}
    optional = ("safety_gate_report",)
    for name in (*required, *(name for name in optional if name in paths)):
        path = Path(paths[name])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot load {name} from {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValidationError(f"{name} file must contain a JSON object")
        artifacts[name] = payload
    return validate_production_chain(**artifacts)
