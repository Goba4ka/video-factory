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
    "media_discovery_manifest": "media_discovery_manifest.schema.json",
    "frozen_media_manifest": "frozen_media_manifest.schema.json",
    "voice_manifest": "voice_manifest.schema.json",
    "source_audio_manifest": "source_audio_manifest.schema.json",
    "bgm_manifest": "bgm_manifest.schema.json",
    "program_audio_manifest": "program_audio_manifest.schema.json",
    "voice_defect": "voice_defect.schema.json",
    "voice_profile_approval": "voice_profile_approval.schema.json",
    "voice_rights_approval": "voice_rights_approval.schema.json",
    "script_package": "script_package.schema.json",
    "shotlist": "shotlist.schema.json",
    "project_manifest": "render_project_manifest.schema.json",
    "preview_approval": "preview_approval.schema.json",
    "render_manifest": "render_manifest.schema.json",
    "caption_transcript_manifest": "caption_transcript_manifest.schema.json",
    "word_transcript_evidence": "word_transcript_evidence.schema.json",
    "dedup_corpus_approval": "dedup_corpus_approval.schema.json",
    "dedup_corpus_snapshot": "dedup_corpus_snapshot.schema.json",
    "qc_analyzer_report": "qc_analyzer_report.schema.json",
    "qc_auto_evidence_manifest": "qc_auto_evidence_manifest.schema.json",
    "qc_evidence_bundle": "qc_evidence_bundle.schema.json",
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


QC_REQUIRED_CATEGORIES = frozenset(
    {
        "technical",
        "audio",
        "captions",
        "facts",
        "rights",
        "dedup",
        "policy",
        "visual",
    }
)


QC_ANALYZER_REQUIRED_BINDINGS = {
    "technical": frozenset({"output_sha256", "render_manifest_sha256"}),
    "audio": frozenset({"output_sha256", "render_manifest_sha256"}),
    "captions": frozenset(
        {
            "output_sha256",
            "render_manifest_sha256",
            "script_package_sha256",
            "machine_evidence_sha256",
        }
    ),
    "facts": frozenset(
        {
            "output_sha256",
            "render_manifest_sha256",
            "claim_ledger_sha256",
            "script_package_sha256",
            "shotlist_sha256",
        }
    ),
    "rights": frozenset(
        {
            "output_sha256",
            "render_manifest_sha256",
            "rights_manifest_sha256",
            "frozen_media_manifest_sha256",
            "shotlist_sha256",
        }
    ),
    "dedup": frozenset(
        {"output_sha256", "render_manifest_sha256", "corpus_snapshot_sha256"}
    ),
    "policy": frozenset(
        {
            "output_sha256",
            "render_manifest_sha256",
            "claim_ledger_sha256",
            "script_package_sha256",
        }
    ),
    "visual": frozenset(
        {
            "output_sha256",
            "render_manifest_sha256",
            "shotlist_sha256",
            "contact_sheet_sha256",
        }
    ),
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
    if name == "qc_analyzer_report":
        category = document["category"]
        required_bindings = set(QC_ANALYZER_REQUIRED_BINDINGS[category])
        if category == "policy" and document["lane_id"] != "motivation":
            required_bindings.add("safety_gate_report_sha256")
        bindings = document["bindings"]
        allowed_binding_sets = {frozenset(required_bindings)}
        if category == "rights" and document["lane_id"] == "motivation":
            allowed_binding_sets.add(
                frozenset(
                    required_bindings
                    | {
                        "source_audio_manifest_sha256",
                        "source_audio_segment_bindings_sha256",
                        "program_audio_manifest_sha256",
                        "project_manifest_sha256",
                    }
                )
            )
        if frozenset(bindings) not in allowed_binding_sets:
            raise ValidationError(
                f"qc_analyzer_report {category} bindings have no accepted exact shape"
            )
        if bindings["output_sha256"] != document["render_sha256"]:
            raise ValidationError(
                "qc_analyzer_report output_sha256 must equal render_sha256"
            )
        if document["status"] == "pass" and (
            document["needs_human_review"] is not False
            or document["warnings"]
            or document["findings"]
        ):
            raise ValidationError(
                "passing qc_analyzer_report must be clean and need no human review"
            )
        if not document["metrics"]:
            raise ValidationError("qc_analyzer_report metrics must not be empty")
    if name == "word_transcript_evidence":
        if document["status"] == "completed" and document["warnings"]:
            raise ValidationError(
                "completed word_transcript_evidence cannot contain warnings"
            )
        previous_start = -1.0
        duration = float(document["duration_seconds"])
        for index, word in enumerate(document["words"]):
            start = float(word["start_seconds"])
            end = float(word["end_seconds"])
            if end <= start:
                raise ValidationError(
                    f"word_transcript_evidence.words[{index}] has non-positive duration"
                )
            if start < previous_start:
                raise ValidationError(
                    "word_transcript_evidence words must be ordered by start_seconds"
                )
            if end > duration + 0.25:
                raise ValidationError(
                    f"word_transcript_evidence.words[{index}] exceeds transcript duration"
                )
            previous_start = start
    if name == "caption_transcript_manifest":
        if document["status"] != "completed" or document["warnings"]:
            raise ValidationError(
                "caption_transcript_manifest must be completed without warnings"
            )
        if set(document["evidence"]) != {"path", "sha256"}:
            raise ValidationError(
                "caption_transcript_manifest evidence must contain path and sha256"
            )
    if name == "dedup_corpus_approval":
        if not document["approved_by"].strip():
            raise ValidationError(
                "dedup_corpus_approval approved_by must not be blank"
            )
        if len(document["approval_note"].strip()) < 5:
            raise ValidationError(
                "dedup_corpus_approval approval_note must contain five non-space characters"
            )
        approved_at = datetime.fromisoformat(
            document["approved_at"].replace("Z", "+00:00")
        )
        if approved_at.tzinfo is None:
            raise ValidationError(
                "dedup_corpus_approval approved_at must include a timezone"
            )
    if name == "dedup_corpus_snapshot":
        comparison_ids = [row["comparison_id"] for row in document["entries"]]
        if len(comparison_ids) != len(set(comparison_ids)):
            raise ValidationError(
                "dedup_corpus_snapshot comparison_id values must be unique"
            )
    if name == "qc_auto_evidence_manifest":
        reports = document["reports"]
        categories = [report.get("category") for report in reports]
        if set(categories) != {"technical", "audio", "rights"} or len(categories) != 3:
            raise ValidationError(
                "qc_auto_evidence_manifest must contain technical, audio and rights"
            )
        for report in reports:
            validate_artifact("qc_analyzer_report", report, root=root)
            if any(
                report[field] != document[field]
                for field in ("job_id", "lane_id", "render_id", "render_sha256")
            ):
                raise ValidationError(
                    "qc_auto_evidence_manifest report identity does not match manifest"
                )
        if set(document["evidence"]) != {"technical", "audio", "rights"}:
            raise ValidationError(
                "qc_auto_evidence_manifest evidence categories are incomplete"
            )
        for category, descriptor in document["evidence"].items():
            if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256"}:
                raise ValidationError(
                    f"qc_auto_evidence_manifest evidence.{category} must contain path and sha256"
                )
    if name == "qc_evidence_bundle":
        categories = [row["category"] for row in document["reports"]]
        if set(categories) != QC_REQUIRED_CATEGORIES or len(categories) != len(
            QC_REQUIRED_CATEGORIES
        ):
            raise ValidationError(
                "qc_evidence_bundle must contain each QC category exactly once"
            )
        for row in document["reports"]:
            descriptor = row["evidence"]
            if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256"}:
                raise ValidationError(
                    f"qc_evidence_bundle {row['category']} evidence must contain path and sha256"
                )
        contact = document["contact_sheet"]
        if not isinstance(contact, Mapping) or set(contact) != {"path", "sha256"}:
            raise ValidationError(
                "qc_evidence_bundle contact_sheet must contain path and sha256"
            )
        decision = document["decision"]
        if decision["passed"] and (
            decision["needs_human_review"] or decision["blocking_categories"]
        ):
            raise ValidationError(
                "passing qc_evidence_bundle cannot need review or have blockers"
            )
    if name == "qc_report":
        check_ids: set[str] = set()
        categories: set[str] = set()
        for check in document["checks"]:
            check_id = check["check_id"]
            category = check["category"]
            if check_id in check_ids:
                raise ValidationError(
                    f"qc_report contains duplicate check_id {check_id!r}"
                )
            if category in categories:
                raise ValidationError(
                    f"qc_report contains duplicate category {category!r}"
                )
            check_ids.add(check_id)
            categories.add(category)

        missing_categories = sorted(QC_REQUIRED_CATEGORIES - categories)
        if missing_categories:
            raise ValidationError(
                "qc_report is missing required categories: "
                + ", ".join(missing_categories)
            )

        decision = document["decision"]
        unknown_blocking_ids = sorted(
            set(decision["blocking_check_ids"]) - check_ids
        )
        if unknown_blocking_ids:
            raise ValidationError(
                "qc_report blocking_check_ids reference unknown checks: "
                + ", ".join(unknown_blocking_ids)
            )

        if decision["passed"]:
            nonpassing_ids = [
                check["check_id"]
                for check in document["checks"]
                if check["status"] != "pass"
            ]
            if nonpassing_ids:
                raise ValidationError(
                    "qc_report cannot pass with non-pass checks: "
                    + ", ".join(nonpassing_ids)
                )
            if decision["needs_human_review"]:
                raise ValidationError(
                    "qc_report cannot pass while needs_human_review is true"
                )
            if decision["blocking_check_ids"]:
                raise ValidationError(
                    "qc_report cannot pass with blocking_check_ids"
                )
    if name == "source_audio_manifest":
        if document["schema_version"] == "1.0.0":
            if set(document["checksums"]) != {
                "source_video_sha256",
                "extracted_audio_sha256",
                "transcript_sha256",
            } or any(field in document for field in ("segments", "segment_count")):
                raise ValidationError(
                    "source_audio_manifest v1 must keep the exact single-source shape"
                )
            if document["source_out_seconds"] <= document["source_in_seconds"]:
                raise ValidationError(
                    "source_audio_manifest.source_out_seconds must be greater than source_in_seconds"
                )
        else:
            forbidden = {
                "source_video_uri_or_path",
                "source_in_seconds",
                "source_out_seconds",
                "speaker_name",
                "rights_evidence",
            }
            if forbidden.intersection(document):
                raise ValidationError(
                    "source_audio_manifest multi-source form cannot contain a hidden prejoined source"
                )
            if set(document["checksums"]) != {
                "extracted_audio_sha256",
                "transcript_sha256",
                "segment_bindings_sha256",
            }:
                raise ValidationError(
                    "source_audio_manifest multi-source checksums have the wrong shape"
                )
            segments = document["segments"]
            if document["segment_count"] != len(segments):
                raise ValidationError(
                    "source_audio_manifest segment_count does not match segments"
                )
            previous_program_out = 0.0
            transcripts: list[str] = []
            rights_statuses: list[str] = []
            for index, segment in enumerate(segments):
                if not isinstance(segment, Mapping):
                    raise ValidationError(
                        f"source_audio_manifest.segments[{index}] must be an object"
                    )
                segment_fields = {
                    "index",
                    "asset_id",
                    "source_video_uri_or_path",
                    "source_in_seconds",
                    "source_out_seconds",
                    "program_in_seconds",
                    "program_out_seconds",
                    "speaker_name",
                    "source_language",
                    "original_transcript",
                    "transcript",
                    "bilingual_review",
                    "rights_status",
                    "rights_evidence",
                    "extracted_audio_path",
                    "checksums",
                }
                if set(segment) != segment_fields:
                    raise ValidationError(
                        f"source_audio_manifest.segments[{index}] has the wrong fields"
                    )
                if (
                    not isinstance(segment["index"], int)
                    or isinstance(segment["index"], bool)
                    or not all(
                        isinstance(segment[field], str) and segment[field].strip()
                        for field in (
                            "asset_id",
                            "source_video_uri_or_path",
                            "speaker_name",
                            "source_language",
                            "original_transcript",
                            "transcript",
                            "rights_status",
                            "extracted_audio_path",
                        )
                    )
                    or segment["source_language"] not in {"ru", "en"}
                    or segment["rights_status"]
                    not in {
                        "consent_confirmed",
                        "commercial_license_confirmed",
                        "internal_prototype",
                    }
                ):
                    raise ValidationError(
                        f"source_audio_manifest.segments[{index}] has invalid identity/text fields"
                    )
                for numeric_field in (
                    "source_in_seconds",
                    "source_out_seconds",
                    "program_in_seconds",
                    "program_out_seconds",
                ):
                    value = segment[numeric_field]
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or float(value) < 0
                    ):
                        raise ValidationError(
                            f"source_audio_manifest.segments[{index}].{numeric_field} is invalid"
                        )
                checksum_fields = {
                    "source_video_sha256",
                    "extracted_audio_sha256",
                    "original_transcript_sha256",
                    "transcript_sha256",
                    "bilingual_review_sha256",
                }
                if not isinstance(segment["checksums"], Mapping) or set(
                    segment["checksums"]
                ) != checksum_fields:
                    raise ValidationError(
                        f"source_audio_manifest.segments[{index}] checksums have the wrong shape"
                    )
                for checksum_field in checksum_fields - {"bilingual_review_sha256"}:
                    checksum = segment["checksums"][checksum_field]
                    if not isinstance(checksum, str) or re.fullmatch(
                        r"[a-f0-9]{64}", checksum
                    ) is None:
                        raise ValidationError(
                            f"source_audio_manifest.segments[{index}] has invalid {checksum_field}"
                        )
                if segment["rights_status"] != "internal_prototype" and not (
                    isinstance(segment["rights_evidence"], str)
                    and segment["rights_evidence"].strip()
                ):
                    raise ValidationError(
                        f"source_audio_manifest.segments[{index}] lacks rights evidence"
                    )
                if segment["index"] != index:
                    raise ValidationError(
                        f"source_audio_manifest.segments[{index}].index is not ordered"
                    )
                source_duration = float(segment["source_out_seconds"]) - float(
                    segment["source_in_seconds"]
                )
                if source_duration <= 0:
                    raise ValidationError(
                        f"source_audio_manifest.segments[{index}] source range is invalid"
                    )
                program_in = float(segment["program_in_seconds"])
                program_out = float(segment["program_out_seconds"])
                if abs(program_in - previous_program_out) > 0.000_021:
                    raise ValidationError(
                        "source_audio_manifest segment program ranges must be contiguous and ordered"
                    )
                program_duration = program_out - program_in
                if program_duration <= 0:
                    raise ValidationError(
                        f"source_audio_manifest.segments[{index}] program range is invalid"
                    )
                if abs(program_duration - source_duration) > max(
                    0.1, source_duration * 0.02
                ):
                    raise ValidationError(
                        f"source_audio_manifest.segments[{index}] source/program durations differ"
                    )
                if segment["checksums"]["transcript_sha256"] != digest_text(
                    segment["transcript"]
                ):
                    raise ValidationError(
                        f"source_audio_manifest.segments[{index}] transcript hash is invalid"
                    )
                if segment["checksums"]["original_transcript_sha256"] != digest_text(
                    segment["original_transcript"]
                ):
                    raise ValidationError(
                        f"source_audio_manifest.segments[{index}] original transcript hash is invalid"
                    )
                review = segment["bilingual_review"]
                review_sha = segment["checksums"]["bilingual_review_sha256"]
                if segment["source_language"] == "ru":
                    if segment["original_transcript"] != segment["transcript"]:
                        raise ValidationError(
                            f"source_audio_manifest.segments[{index}] Russian transcripts differ"
                        )
                    if review is not None or review_sha is not None:
                        raise ValidationError(
                            f"source_audio_manifest.segments[{index}] Russian segment has bilingual review"
                        )
                else:
                    if not re.search(r"[А-Яа-яЁё]", segment["transcript"]):
                        raise ValidationError(
                            f"source_audio_manifest.segments[{index}] Russian display transcript is missing"
                        )
                    if not isinstance(review, Mapping):
                        raise ValidationError(
                            f"source_audio_manifest.segments[{index}] lacks bilingual human review"
                        )
                    required_review_fields = {
                        "approved",
                        "approved_by",
                        "approved_at",
                        "asset_id",
                        "source_in_seconds",
                        "source_out_seconds",
                        "original_transcript_sha256",
                        "russian_transcript_sha256",
                        "review_notes",
                    }
                    if set(review) != required_review_fields or review["approved"] is not True:
                        raise ValidationError(
                            f"source_audio_manifest.segments[{index}] bilingual review shape is invalid"
                        )
                    if not all(
                        isinstance(review[field], str) and review[field].strip()
                        for field in ("approved_by", "approved_at", "review_notes")
                    ):
                        raise ValidationError(
                            f"source_audio_manifest.segments[{index}] bilingual review identity is invalid"
                        )
                    try:
                        approved_at = datetime.fromisoformat(
                            review["approved_at"].replace("Z", "+00:00")
                        )
                    except ValueError as exc:
                        raise ValidationError(
                            f"source_audio_manifest.segments[{index}] bilingual review timestamp is invalid"
                        ) from exc
                    if approved_at.tzinfo is None:
                        raise ValidationError(
                            f"source_audio_manifest.segments[{index}] bilingual review timestamp must include a timezone"
                        )
                    expected_review = {
                        "asset_id": segment["asset_id"],
                        "source_in_seconds": segment["source_in_seconds"],
                        "source_out_seconds": segment["source_out_seconds"],
                        "original_transcript_sha256": digest_text(
                            segment["original_transcript"]
                        ),
                        "russian_transcript_sha256": digest_text(segment["transcript"]),
                    }
                    for field, expected in expected_review.items():
                        if review[field] != expected:
                            raise ValidationError(
                                f"source_audio_manifest.segments[{index}] bilingual review is not bound to {field}"
                            )
                    if review_sha != digest_text(canonical_json(review)):
                        raise ValidationError(
                            f"source_audio_manifest.segments[{index}] bilingual review hash is invalid"
                        )
                previous_program_out = program_out
                transcripts.append(segment["transcript"])
                rights_statuses.append(segment["rights_status"])
            aggregate_transcript = "\n".join(transcripts)
            if document["transcript"] != aggregate_transcript:
                raise ValidationError(
                    "source_audio_manifest transcript is not the ordered segment aggregate"
                )
            expected_rights_status = (
                "internal_prototype"
                if "internal_prototype" in rights_statuses
                else "consent_confirmed"
                if "consent_confirmed" in rights_statuses
                else "commercial_license_confirmed"
            )
            if document["rights_status"] != expected_rights_status:
                raise ValidationError(
                    "source_audio_manifest aggregate rights_status does not match segments"
                )
            bindings_sha256 = digest_text(canonical_json(segments))
            if document["checksums"]["segment_bindings_sha256"] != bindings_sha256:
                raise ValidationError(
                    "source_audio_manifest segment_bindings_sha256 does not match segments"
                )
            expected_asset_id = f"source-audio-program-{bindings_sha256[:24]}"
            if document["audio_asset_id"] != expected_asset_id:
                raise ValidationError(
                    "source_audio_manifest audio_asset_id does not match segment bindings"
                )
        transcript_sha256 = document["checksums"]["transcript_sha256"]
        if transcript_sha256 != digest_text(document["transcript"]):
            raise ValidationError(
                "source_audio_manifest transcript_sha256 does not match transcript"
            )
    if name == "bgm_manifest":
        rights = document["rights"]
        if rights["attribution_required"] and not (
            isinstance(rights["attribution_text"], str)
            and rights["attribution_text"].strip()
        ):
            raise ValidationError(
                "bgm_manifest attribution_text is required by the license"
            )
        approval = rights["human_approval"]
        if approval["rights_manifest_sha256"] != document["checksums"][
            "rights_manifest_sha256"
        ]:
            raise ValidationError(
                "bgm_manifest human approval is not bound to RightsManifest"
            )
        if digest_text(canonical_json(approval)) != document["checksums"][
            "human_approval_sha256"
        ]:
            raise ValidationError(
                "bgm_manifest human approval checksum does not match approval"
            )
        if document["bgm_asset_id"] not in approval["reviewed_asset_ids"]:
            raise ValidationError(
                "bgm_manifest BGM asset was not reviewed by the human rights gate"
            )
    if name == "program_audio_manifest":
        authority = document["source_authority"]
        expected_contract = (
            "source_audio_manifest"
            if document["lane_id"] == "motivation"
            else "voice_manifest"
        )
        if authority["contract"] != expected_contract:
            raise ValidationError(
                "program_audio_manifest source authority does not match lane"
            )
        if authority["tts"] is not (expected_contract == "voice_manifest"):
            raise ValidationError(
                "program_audio_manifest tts flag does not match source authority"
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
    if name == "media_discovery_manifest":
        provider_ids: set[str] = set()
        landing_urls: set[str] = set()
        for index, candidate in enumerate(document["candidates"]):
            provider_id = candidate["provider_asset_id"]
            landing_url = candidate["landing_url"]
            if provider_id in provider_ids:
                raise ValidationError(
                    "media_discovery_manifest contains duplicate provider_asset_id "
                    f"{provider_id!r}"
                )
            if landing_url in landing_urls:
                raise ValidationError(
                    "media_discovery_manifest contains duplicate landing_url "
                    f"{landing_url!r}"
                )
            provider_ids.add(provider_id)
            landing_urls.add(landing_url)

            selected = candidate["selected_file"]
            query = document["query"]
            if candidate["height"] <= candidate["width"]:
                raise ValidationError(
                    f"media_discovery_manifest.candidates[{index}] source is not portrait"
                )
            if selected["height"] <= selected["width"]:
                raise ValidationError(
                    f"media_discovery_manifest.candidates[{index}] is not portrait"
                )
            if (
                selected["width"] < query["minimum_width"]
                or selected["height"] < query["minimum_height"]
            ):
                raise ValidationError(
                    f"media_discovery_manifest.candidates[{index}] is below minimum dimensions"
                )

            ledger = candidate["ledger"]
            source = ledger["source"]
            attribution = ledger["attribution"]
            if source["provider_asset_id"] != provider_id:
                raise ValidationError(
                    f"media_discovery_manifest.candidates[{index}] source id does not match"
                )
            if source["landing_url"] != landing_url:
                raise ValidationError(
                    f"media_discovery_manifest.candidates[{index}] source URL does not match"
                )
            if source["download_url"] != selected["download_url"]:
                raise ValidationError(
                    f"media_discovery_manifest.candidates[{index}] download URL does not match"
                )
            if (
                attribution["source_url"] != landing_url
                or attribution["creator_url"] != source["creator_url"]
            ):
                raise ValidationError(
                    f"media_discovery_manifest.candidates[{index}] attribution does not match source"
                )

        decision = document["decision"]
        if decision["candidate_count"] != len(document["candidates"]):
            raise ValidationError(
                "media_discovery_manifest decision.candidate_count does not match candidates"
            )
    if name == "project_manifest":
        file_paths: set[str] = set()
        for index, item in enumerate(document["files"]):
            path = item["path"]
            if path in file_paths:
                raise ValidationError(
                    f"project_manifest contains duplicate file path {path!r}"
                )
            if (
                "\\" in path
                or path.startswith("/")
                or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path)
                or ".." in Path(path).parts
            ):
                raise ValidationError(
                    f"project_manifest.files[{index}].path must be a safe relative path"
                )
            file_paths.add(path)
        if [item["path"] for item in document["files"]] != sorted(file_paths):
            raise ValidationError("project_manifest files must be sorted by path")
        expected_tree_hash = digest_text(canonical_json(document["files"]))
        if document["project_tree_sha256"] != expected_tree_hash:
            raise ValidationError(
                "project_manifest project_tree_sha256 does not match files"
            )
        for index, asset in enumerate(document["assets"]):
            matching = [
                item for item in document["files"] if item["path"] == asset["project_path"]
            ]
            if len(matching) != 1:
                raise ValidationError(
                    f"project_manifest.assets[{index}] project_path is not in files"
                )
            if (
                matching[0]["sha256"] != asset["sha256"]
                or matching[0]["size_bytes"] != asset["size_bytes"]
            ):
                raise ValidationError(
                    f"project_manifest.assets[{index}] does not match its file record"
                )
        audio = document["bindings"]["program_audio"]
        audio_files = [
            item for item in document["files"] if item["path"] == audio["project_path"]
        ]
        if len(audio_files) != 1:
            raise ValidationError(
                "project_manifest program audio project_path is not in files"
            )
        if (
            audio_files[0]["sha256"] != audio["audio_sha256"]
            or audio_files[0]["size_bytes"] != audio["size_bytes"]
        ):
            raise ValidationError(
                "project_manifest program audio does not match its file record"
            )
        authority = document["bindings"]["authoritative_audio"]
        expected_audio_contract = (
            "source_audio_manifest"
            if document["lane_id"] == "motivation"
            else "voice_manifest"
        )
        if authority["contract"] != expected_audio_contract:
            raise ValidationError(
                "project_manifest authoritative audio contract does not match lane"
            )
        if authority["schema_version"] not in (
            {"1.0.0", "1.1.0"}
            if expected_audio_contract == "source_audio_manifest"
            else {"1.0.0"}
        ):
            raise ValidationError(
                "project_manifest authoritative audio schema version does not match contract"
            )
        if authority["job_id"] != document["job_id"]:
            raise ValidationError(
                "project_manifest authoritative audio is not bound to job_id"
            )
        if (
            audio["job_id"] != document["job_id"]
            or audio["idea_id"] != document["idea_id"]
            or audio["lane_id"] != document["lane_id"]
        ):
            raise ValidationError(
                "project_manifest program audio is not bound to job/idea/lane"
            )
    if name == "preview_approval":
        if document["approved"] is not True:
            raise ValidationError("preview_approval must record approved=true")
        parsed_studio = urlparse(document["studio_url"])
        if parsed_studio.scheme not in {"http", "https"} or not parsed_studio.netloc:
            raise ValidationError("preview_approval.studio_url must be an HTTP(S) URL")
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
