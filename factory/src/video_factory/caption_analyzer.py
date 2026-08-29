"""Checksum-bound, fail-closed word alignment analyzer for rendered captions.

The analyzer deliberately consumes word-level transcript measurements rather
than an upstream pass/fail assertion.  It binds those measurements, the
ScriptPackage, the RenderManifest and the actual rendered master bytes into a
single QC analyzer report.  Only an empty-warning, empty-finding report can
have status ``pass``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from .contracts import validate_artifact
from .errors import FactoryError, ValidationError
from .validators import canonical_json, digest_text, require_nonempty_string


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TOKEN = re.compile(r"[^\W_]+(?:[-'’][^\W_]+)*", re.UNICODE)
_DESCRIPTOR_FIELDS = frozenset({"path", "sha256"})
_TRANSCRIPT_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "render_id",
        "render_sha256",
        "status",
        "warnings",
        "language",
        "duration_seconds",
        "engine",
        "completed_at",
        "words",
    }
)
_WORD_FIELDS = frozenset({"text", "start_seconds", "end_seconds", "confidence"})
_CHECKER_FIELDS = frozenset({"name", "version", "run_id"})
_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "category",
        "job_id",
        "lane_id",
        "render_id",
        "render_sha256",
        "status",
        "needs_human_review",
        "warnings",
        "findings",
        "checker",
        "completed_at",
        "bindings",
        "metrics",
    }
)
_CAPTION_BINDINGS = frozenset(
    {
        "output_sha256",
        "render_manifest_sha256",
        "script_package_sha256",
        "machine_evidence_sha256",
    }
)
_TRANSCRIPT_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "lane_id",
        "render_id",
        "render_sha256",
        "status",
        "warnings",
        "observer",
        "evidence",
        "word_count",
        "created_at",
    }
)
_LANES = frozenset(
    {"war_history", "celebrity_news", "motivation", "chinese_medicine", "health"}
)
_DEFAULT_THRESHOLDS: dict[str, float | int] = {
    "alignment_ratio_min": 0.90,
    "caption_coverage_ratio_min": 0.90,
    "spoken_coverage_ratio_min": 0.90,
    "transcript_precision_ratio_min": 0.85,
    "p95_drift_seconds_max": 0.25,
    "absolute_drift_seconds_max": 0.45,
    "max_chars_per_line": 24,
    "caption_words_per_second_max": 4.50,
}


def _safe_id(value: Any, field: str) -> str:
    result = require_nonempty_string(value, field)
    if not _SAFE_ID.fullmatch(result) or ".." in result:
        raise ValidationError(f"{field} contains unsafe characters")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ValidationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _artifact_sha256(value: Mapping[str, Any]) -> str:
    return digest_text(canonical_json(dict(value)))


def _parse_datetime(value: Any, field: str) -> datetime:
    text = require_nonempty_string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{field} must be finite")
    return result


def _tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return _TOKEN.findall(normalized)


def _configured_evidence_root() -> Path:
    raw = os.environ.get("VIDEO_FACTORY_QC_EVIDENCE_ROOT")
    if not raw:
        raise ValidationError("VIDEO_FACTORY_QC_EVIDENCE_ROOT must be configured")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ValidationError("VIDEO_FACTORY_QC_EVIDENCE_ROOT must exist")
    return root


def _contained_regular_file(value: Any, field: str, root: Path) -> Path:
    text = require_nonempty_string(value, field)
    raw_path = Path(text).expanduser()
    if not raw_path.is_absolute():
        raise ValidationError(f"{field} must be absolute")
    if raw_path.is_symlink():
        raise ValidationError(f"{field} must not be a symlink")
    path = raw_path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{field} escapes VIDEO_FACTORY_QC_EVIDENCE_ROOT") from exc
    if not path.is_file():
        raise ValidationError(f"{field} does not exist: {path}")
    return path


def _evidence_descriptor(raw: Any, root: Path) -> tuple[Path, str]:
    if not isinstance(raw, Mapping) or set(raw) != _DESCRIPTOR_FIELDS:
        raise ValidationError("payload.transcript_evidence must contain exactly path and sha256")
    path = _contained_regular_file(raw.get("path"), "payload.transcript_evidence.path", root)
    if path.suffix.lower() != ".json":
        raise ValidationError("payload.transcript_evidence.path must name a JSON file")
    expected = require_nonempty_string(
        raw.get("sha256"), "payload.transcript_evidence.sha256"
    )
    if not _SHA256.fullmatch(expected):
        raise ValidationError("payload.transcript_evidence.sha256 must be lowercase SHA-256")
    if _sha256_file(path) != expected:
        raise ValidationError("transcript evidence checksum does not match actual bytes")
    return path, expected


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValidationError("transcript evidence exceeds the 16 MiB limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"transcript evidence is not readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("transcript evidence must contain one JSON object")
    return value


def _upstream(
    task: Mapping[str, Any], role: str, contract: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    values = task.get("upstream_results")
    if not isinstance(values, list):
        raise ValidationError("task.upstream_results must be an array")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in values:
        if not isinstance(entry, Mapping) or entry.get("role") != role:
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(result, dict) and isinstance(artifact, dict):
            matches.append((result, artifact))
    if len(matches) != 1:
        raise ValidationError(
            f"caption analyzer requires exactly one upstream {contract} from role={role!r}"
        )
    validate_artifact(contract, matches[0][1])
    return matches[0]


def _transcript_upstream(
    task: Mapping[str, Any],
    *,
    job_id: str,
    lane: str,
    render: Mapping[str, Any],
) -> dict[str, Any]:
    values = task.get("upstream_results")
    if not isinstance(values, list):
        raise ValidationError("task.upstream_results must be an array")
    matches: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for entry in values:
        if not isinstance(entry, Mapping) or entry.get("role") != "caption_transcript":
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(result, Mapping) and isinstance(artifact, Mapping):
            matches.append((result, artifact))
    if len(matches) != 1:
        raise ValidationError(
            "caption analyzer requires exactly one upstream caption_transcript_manifest"
        )
    result, manifest = matches[0]
    validate_artifact("caption_transcript_manifest", dict(manifest))
    if set(manifest) != _TRANSCRIPT_MANIFEST_FIELDS:
        raise ValidationError("caption_transcript_manifest fields are invalid")
    if manifest.get("schema_version") != "1.0.0":
        raise ValidationError("caption_transcript_manifest schema_version must be 1.0.0")
    expected_identity = {
        "job_id": job_id,
        "lane_id": lane,
        "render_id": render["render_id"],
        "render_sha256": render["output_sha256"],
    }
    if any(manifest.get(key) != value for key, value in expected_identity.items()):
        raise ValidationError("caption_transcript_manifest is not bound to this job/render")
    if manifest.get("status") != "completed" or manifest.get("warnings") != []:
        raise ValidationError("caption_transcript_manifest is not a clean completion")
    if not isinstance(manifest.get("word_count"), int) or manifest["word_count"] < 1:
        raise ValidationError("caption_transcript_manifest word_count is invalid")
    _parse_datetime(manifest.get("created_at"), "caption_transcript_manifest.created_at")
    descriptor = manifest.get("evidence")
    if not isinstance(descriptor, Mapping) or set(descriptor) != _DESCRIPTOR_FIELDS:
        raise ValidationError("caption_transcript_manifest evidence is invalid")
    if result.get("evidence") != descriptor:
        raise ValidationError("caption transcript result descriptor does not match its manifest")
    observer = manifest.get("observer")
    expected_observer = {"executable_sha256", "engine_name", "engine_version", "run_id"}
    if not isinstance(observer, Mapping) or set(observer) != expected_observer:
        raise ValidationError("caption_transcript_manifest observer is invalid")
    if not isinstance(observer.get("executable_sha256"), str) or not _SHA256.fullmatch(
        observer["executable_sha256"]
    ):
        raise ValidationError("caption_transcript_manifest observer checksum is invalid")
    for field in ("engine_name", "engine_version", "run_id"):
        require_nonempty_string(observer.get(field), f"caption_transcript_manifest.observer.{field}")
    return dict(manifest)


def _master_path(render_result: Mapping[str, Any], render: Mapping[str, Any]) -> Path:
    raw = require_nonempty_string(render_result.get("output_path"), "render.output_path")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValidationError("render.output_path must be absolute")
    if candidate.is_symlink():
        raise ValidationError("render.output_path must not be a symlink")
    output = candidate.resolve()
    if not output.is_file():
        raise ValidationError(f"render output does not exist: {output}")
    if output.name != Path(str(render["output"])).name:
        raise ValidationError("render output path does not match render_manifest.output")
    actual = _sha256_file(output)
    if actual != render["output_sha256"]:
        raise ValidationError("render master checksum does not match render_manifest")
    return output


def _validate_engine(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _CHECKER_FIELDS:
        raise ValidationError("transcript.engine must contain name, version and run_id")
    for key in sorted(_CHECKER_FIELDS):
        require_nonempty_string(value.get(key), f"transcript.engine.{key}")


def _validate_transcript(
    transcript: Mapping[str, Any],
    *,
    job_id: str,
    render: Mapping[str, Any],
) -> list[dict[str, Any]]:
    validate_artifact("word_transcript_evidence", dict(transcript))
    unknown = set(transcript) - _TRANSCRIPT_FIELDS
    missing = _TRANSCRIPT_FIELDS - set(transcript)
    if unknown or missing:
        raise ValidationError("transcript evidence fields are invalid")
    if transcript.get("schema_version") != "1.0.0":
        raise ValidationError("transcript schema_version must be 1.0.0")
    if transcript.get("job_id") != job_id:
        raise ValidationError("transcript is not bound to task.job_id")
    if transcript.get("render_id") != render["render_id"]:
        raise ValidationError("transcript is not bound to render_id")
    if transcript.get("render_sha256") != render["output_sha256"]:
        raise ValidationError("transcript is stale for the rendered master")
    if transcript.get("status") != "completed":
        raise ValidationError("transcript status must be completed, never warn/not_run/failed")
    if transcript.get("warnings") != []:
        raise ValidationError("transcript evidence contains warnings")
    if transcript.get("language") != "ru":
        raise ValidationError("transcript language must be ru")
    duration = _finite_number(transcript.get("duration_seconds"), "transcript.duration_seconds")
    render_duration = float(render["technical"]["duration_seconds"])
    if duration <= 0 or abs(duration - render_duration) > 0.25:
        raise ValidationError("transcript duration does not match render_manifest")
    _validate_engine(transcript.get("engine"))
    _parse_datetime(transcript.get("completed_at"), "transcript.completed_at")
    raw_words = transcript.get("words")
    if not isinstance(raw_words, list) or not raw_words:
        raise ValidationError("transcript requires non-empty word-level evidence")

    words: list[dict[str, Any]] = []
    previous_start = -1.0
    for index, raw_word in enumerate(raw_words):
        field = f"transcript.words[{index}]"
        if not isinstance(raw_word, Mapping) or set(raw_word) != _WORD_FIELDS:
            raise ValidationError(f"{field} fields are invalid")
        text = require_nonempty_string(raw_word.get("text"), f"{field}.text")
        tokenized = _tokens(text)
        if len(tokenized) != 1:
            raise ValidationError(f"{field}.text must contain exactly one word token")
        start = _finite_number(raw_word.get("start_seconds"), f"{field}.start_seconds")
        end = _finite_number(raw_word.get("end_seconds"), f"{field}.end_seconds")
        if start < 0 or end <= start:
            raise ValidationError(f"{field} has an invalid time range")
        if start < previous_start:
            raise ValidationError("transcript words must be ordered by start_seconds")
        if end > duration + 0.25:
            raise ValidationError(f"{field} extends beyond transcript duration")
        confidence = raw_word.get("confidence")
        if confidence is not None:
            confidence = _finite_number(confidence, f"{field}.confidence")
            if confidence < 0 or confidence > 1:
                raise ValidationError(f"{field}.confidence must be within 0..1")
        words.append(
            {
                "text": text,
                "token": tokenized[0],
                "start_seconds": start,
                "end_seconds": end,
                "midpoint": (start + end) / 2.0,
                "confidence": confidence,
            }
        )
        previous_start = start
    return words


def _thresholds(raw: Any) -> dict[str, float | int]:
    if raw is None:
        return dict(_DEFAULT_THRESHOLDS)
    if not isinstance(raw, Mapping):
        raise ValidationError("payload.thresholds must be an object")
    unknown = set(raw) - set(_DEFAULT_THRESHOLDS)
    if unknown:
        raise ValidationError("payload.thresholds contains unknown fields: " + ", ".join(sorted(unknown)))
    result: dict[str, float | int] = dict(_DEFAULT_THRESHOLDS)
    for key, value in raw.items():
        number = _finite_number(value, f"payload.thresholds.{key}")
        if key == "max_chars_per_line":
            if int(number) != number or not 12 <= int(number) <= 60:
                raise ValidationError("payload.thresholds.max_chars_per_line must be an integer 12..60")
            result[key] = int(number)
        elif key.endswith("_ratio_min"):
            if not 0.5 <= number <= 1.0:
                raise ValidationError(f"payload.thresholds.{key} must be within 0.5..1")
            result[key] = number
        elif number <= 0 or number > 10:
            raise ValidationError(f"payload.thresholds.{key} must be within 0..10")
        else:
            result[key] = number
    return result


def _equal_pairs(expected: Sequence[str], actual: Sequence[str]) -> list[tuple[int, int]]:
    matcher = difflib.SequenceMatcher(a=expected, b=actual, autojunk=False)
    pairs: list[tuple[int, int]] = []
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            pairs.append((block.a + offset, block.b + offset))
    return pairs


def _estimated_lines(text: str, max_chars: int) -> int:
    words = text.split()
    if not words:
        return 0
    lines = 1
    width = 0
    for word in words:
        length = len(word)
        if width and width + 1 + length > max_chars:
            lines += 1
            width = length
        else:
            width = length if width == 0 else width + 1 + length
    return lines


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[rank]


def _segment_words(
    words: Sequence[Mapping[str, Any]], start: float, end: float, *, final: bool
) -> list[Mapping[str, Any]]:
    return [
        word
        for word in words
        if word["midpoint"] >= start
        and (word["midpoint"] <= end if final else word["midpoint"] < end)
    ]


def _analyze(
    script: Mapping[str, Any],
    words: Sequence[Mapping[str, Any]],
    thresholds: Mapping[str, float | int],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    segments = script["segments"]
    max_words = int(script["caption_style"]["max_words_per_card"])
    max_lines = int(script["caption_style"]["max_lines"])
    max_chars = int(thresholds["max_chars_per_line"])

    caption_total = caption_matched = aligned_matched = 0
    spoken_total = spoken_matched = 0
    matched_actual_total = 0
    all_drifts: list[float] = []
    word_overflow = line_overflow = speed_overflow = 0
    segment_metrics: list[dict[str, Any]] = []

    for index, segment in enumerate(segments):
        start = float(segment["start_seconds"])
        end = float(segment["end_seconds"])
        duration = end - start
        caption_tokens = _tokens(segment["caption_text"])
        spoken_tokens = _tokens(segment["spoken_text"])
        actual_words = _segment_words(words, start, end, final=index == len(segments) - 1)
        actual_tokens = [str(word["token"]) for word in actual_words]

        caption_pairs = _equal_pairs(caption_tokens, actual_tokens)
        spoken_pairs = _equal_pairs(spoken_tokens, actual_tokens)
        caption_to_spoken = dict(_equal_pairs(caption_tokens, spoken_tokens))
        drifts: list[float] = []
        aligned = 0
        for caption_index, actual_index in caption_pairs:
            spoken_index = caption_to_spoken.get(caption_index)
            if spoken_index is None or not spoken_tokens:
                fraction = (caption_index + 0.5) / max(1, len(caption_tokens))
            else:
                fraction = (spoken_index + 0.5) / len(spoken_tokens)
            expected_time = start + duration * fraction
            drift = abs(float(actual_words[actual_index]["midpoint"]) - expected_time)
            drifts.append(drift)
            if drift <= float(thresholds["absolute_drift_seconds_max"]):
                aligned += 1

        caption_count = len(caption_tokens)
        spoken_count = len(spoken_tokens)
        estimated_lines = _estimated_lines(segment["caption_text"], max_chars)
        words_per_second = caption_count / duration
        if caption_count > max_words:
            word_overflow += 1
        if estimated_lines > max_lines:
            line_overflow += 1
        if words_per_second > float(thresholds["caption_words_per_second_max"]):
            speed_overflow += 1

        caption_total += caption_count
        caption_matched += len(caption_pairs)
        aligned_matched += aligned
        spoken_total += spoken_count
        spoken_matched += len(spoken_pairs)
        matched_actual_total += len({actual_index for _, actual_index in spoken_pairs})
        all_drifts.extend(drifts)
        segment_metrics.append(
            {
                "segment_id": segment["segment_id"],
                "caption_words": caption_count,
                "spoken_words": spoken_count,
                "transcript_words": len(actual_tokens),
                "caption_words_matched": len(caption_pairs),
                "spoken_words_matched": len(spoken_pairs),
                "aligned_caption_words": aligned,
                "caption_coverage_ratio": round(len(caption_pairs) / caption_count, 6)
                if caption_count
                else 0.0,
                "alignment_ratio": round(aligned / caption_count, 6)
                if caption_count
                else 0.0,
                "p95_drift_seconds": round(_percentile(drifts, 0.95), 6),
                "max_drift_seconds": round(max(drifts, default=0.0), 6),
                "estimated_lines": estimated_lines,
                "caption_words_per_second": round(words_per_second, 6),
            }
        )

    coverage = caption_matched / caption_total if caption_total else 0.0
    spoken_coverage = spoken_matched / spoken_total if spoken_total else 0.0
    alignment = aligned_matched / caption_total if caption_total else 0.0
    precision = matched_actual_total / len(words) if words else 0.0
    p95_drift = _percentile(all_drifts, 0.95)
    max_drift = max(all_drifts, default=0.0)
    confidences = [float(word["confidence"]) for word in words if word["confidence"] is not None]

    findings: list[dict[str, Any]] = []

    def add_finding(code: str, message: str, refs: Sequence[int]) -> None:
        findings.append(
            {
                "code": code,
                "message": message,
                "observation_refs": sorted(set(refs)) or [0],
            }
        )

    comparisons = (
        (
            coverage,
            float(thresholds["caption_coverage_ratio_min"]),
            "caption_coverage_below_threshold",
            "Caption words are not sufficiently covered by rendered speech",
            "caption_coverage_ratio",
        ),
        (
            spoken_coverage,
            float(thresholds["spoken_coverage_ratio_min"]),
            "spoken_coverage_below_threshold",
            "Scripted speech is not sufficiently covered by the transcript",
            None,
        ),
        (
            alignment,
            float(thresholds["alignment_ratio_min"]),
            "alignment_below_threshold",
            "Caption words are not sufficiently aligned to their scripted timing",
            "alignment_ratio",
        ),
        (
            precision,
            float(thresholds["transcript_precision_ratio_min"]),
            "transcript_precision_below_threshold",
            "Rendered speech contains too many words outside the ScriptPackage",
            None,
        ),
    )
    for observed_value, required, code, message, segment_key in comparisons:
        if observed_value < required:
            refs = (
                [
                    index
                    for index, metric in enumerate(segment_metrics)
                    if float(metric[segment_key]) < required
                ]
                if segment_key
                else list(range(len(segment_metrics)))
            )
            add_finding(code, message, refs)
    p95_limit = float(thresholds["p95_drift_seconds_max"])
    if p95_drift > p95_limit:
        add_finding(
            "p95_drift_above_threshold",
            "Caption word timing exceeds the p95 drift limit",
            [
                index
                for index, metric in enumerate(segment_metrics)
                if float(metric["p95_drift_seconds"]) > p95_limit
            ],
        )
    absolute_limit = float(thresholds["absolute_drift_seconds_max"])
    if max_drift > absolute_limit:
        add_finding(
            "absolute_drift_above_threshold",
            "Caption word timing exceeds the absolute drift limit",
            [
                index
                for index, metric in enumerate(segment_metrics)
                if float(metric["max_drift_seconds"]) > absolute_limit
            ],
        )
    if word_overflow:
        add_finding(
            "caption_word_overflow",
            "A caption card exceeds ScriptPackage max_words_per_card",
            [
                index
                for index, metric in enumerate(segment_metrics)
                if int(metric["caption_words"]) > max_words
            ],
        )
    if line_overflow:
        add_finding(
            "caption_line_overflow",
            "A caption card exceeds the configured line limit",
            [
                index
                for index, metric in enumerate(segment_metrics)
                if int(metric["estimated_lines"]) > max_lines
            ],
        )
    if speed_overflow:
        speed_limit = float(thresholds["caption_words_per_second_max"])
        add_finding(
            "caption_reading_speed_overflow",
            "A caption card exceeds the reading-speed limit",
            [
                index
                for index, metric in enumerate(segment_metrics)
                if float(metric["caption_words_per_second"]) > speed_limit
            ],
        )

    observed = {
        "caption_word_count": caption_total,
        "spoken_word_count": spoken_total,
        "transcript_word_count": len(words),
        "caption_words_matched": caption_matched,
        "spoken_words_matched": spoken_matched,
        "aligned_caption_words": aligned_matched,
        "caption_coverage_ratio": round(coverage, 6),
        "spoken_coverage_ratio": round(spoken_coverage, 6),
        "alignment_ratio": round(alignment, 6),
        "transcript_precision_ratio": round(precision, 6),
        "p95_drift_seconds": round(p95_drift, 6),
        "absolute_drift_seconds": round(max_drift, 6),
        "caption_word_overflow_segments": word_overflow,
        "caption_line_overflow_segments": line_overflow,
        "caption_reading_speed_overflow_segments": speed_overflow,
        "mean_transcript_confidence": round(sum(confidences) / len(confidences), 6)
        if confidences
        else None,
    }
    return {
        "thresholds": dict(thresholds),
        "observed": observed,
        "segments": segment_metrics,
    }, findings


def _validate_report(report: Mapping[str, Any]) -> None:
    if set(report) != _REPORT_FIELDS:
        raise ValidationError("qc_analyzer_report fields are invalid")
    if report.get("schema_version") != "1.0.0" or report.get("category") != "captions":
        raise ValidationError("caption analyzer report identity is invalid")
    if report.get("lane_id") not in _LANES:
        raise ValidationError("caption analyzer report lane_id is invalid")
    for field in ("render_sha256",):
        if not isinstance(report.get(field), str) or not _SHA256.fullmatch(report[field]):
            raise ValidationError(f"caption analyzer report {field} is invalid")
    bindings = report.get("bindings")
    if not isinstance(bindings, Mapping) or set(bindings) != _CAPTION_BINDINGS:
        raise ValidationError("caption analyzer report bindings are invalid")
    if any(not isinstance(value, str) or not _SHA256.fullmatch(value) for value in bindings.values()):
        raise ValidationError("caption analyzer report binding is not SHA-256")
    if bindings["output_sha256"] != report["render_sha256"]:
        raise ValidationError("caption analyzer report output binding is inconsistent")
    status = report.get("status")
    if status not in {"pass", "fail"}:
        raise ValidationError("caption analyzer report status must be pass or fail")
    warnings = report.get("warnings")
    findings = report.get("findings")
    if warnings != []:
        raise ValidationError("caption analyzer report must not contain warnings")
    if not isinstance(findings, list):
        raise ValidationError("caption analyzer report findings are invalid")
    for index, finding in enumerate(findings):
        if not isinstance(finding, Mapping) or set(finding) != {
            "code",
            "message",
            "observation_refs",
        }:
            raise ValidationError(f"caption analyzer report findings[{index}] is invalid")
        require_nonempty_string(finding.get("code"), f"caption_report.findings[{index}].code")
        require_nonempty_string(
            finding.get("message"), f"caption_report.findings[{index}].message"
        )
        refs = finding.get("observation_refs")
        if not isinstance(refs, list) or not refs or any(
            not isinstance(ref, int) or isinstance(ref, bool) or ref < 0 for ref in refs
        ):
            raise ValidationError(
                f"caption analyzer report findings[{index}].observation_refs is invalid"
            )
    if report.get("needs_human_review") is not False:
        raise ValidationError("caption analyzer report cannot delegate machine QC to human review")
    if status == "pass" and findings:
        raise ValidationError("caption analyzer report cannot pass with findings or review")
    if status == "fail" and not findings:
        raise ValidationError("caption analyzer report fail requires findings")
    checker = report.get("checker")
    if not isinstance(checker, Mapping) or set(checker) != _CHECKER_FIELDS:
        raise ValidationError("caption analyzer report checker is invalid")
    for key in _CHECKER_FIELDS:
        require_nonempty_string(checker.get(key), f"caption_report.checker.{key}")
    _parse_datetime(report.get("completed_at"), "caption_report.completed_at")
    if not isinstance(report.get("metrics"), Mapping):
        raise ValidationError("caption analyzer report metrics are invalid")
    validate_artifact("qc_analyzer_report", dict(report))


def _atomic_report(root: Path, job_id: str, run_id: str, report: Mapping[str, Any]) -> Path:
    directory = root / job_id
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValidationError("caption report directory must not be a symlink")
    path = directory / f"captions-{run_id}.json"
    if path.is_symlink():
        raise ValidationError("caption report path must not be a symlink")
    encoded = canonical_json(dict(report)) + "\n"
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError(f"cannot read existing caption report: {exc}") from exc
        if existing != encoded:
            raise ValidationError("immutable caption report path already contains different bytes")
        return path.resolve()
    temporary = directory / f".{path.name}.tmp-{os.getpid()}"
    try:
        temporary.write_text(encoded, encoding="utf-8", newline="\n")
        temporary.replace(path)
    except OSError as exc:
        raise ValidationError(f"cannot write caption report: {exc}") from exc
    return path.resolve()


def handle_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if task.get("role") != "captions_analyzer":
        raise ValidationError("caption_analyzer accepts only role='captions_analyzer'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "qc_analyzer_report":
        raise ValidationError("caption analyzer must require qc_analyzer_report")
    job_id = _safe_id(task.get("job_id") or payload.get("job_id"), "task.job_id")
    if payload.get("job_id") != job_id:
        raise ValidationError("payload.job_id is not bound to task.job_id")
    lane = require_nonempty_string(payload.get("lane_id"), "payload.lane_id")
    if lane not in _LANES or task.get("pod") != lane:
        raise ValidationError("payload.lane_id is not bound to a supported task.pod")

    _, script = _upstream(task, "script", "script_package")
    render_result, render = _upstream(task, "render", "render_manifest")
    if script["job_id"] != job_id or render["job_id"] != job_id:
        raise ValidationError("script/render artifacts are not bound to task.job_id")
    if script["lane_id"] != lane:
        raise ValidationError("script lane does not match caption analyzer lane")
    decision = script["decision"]
    if decision["passed"] is not True or decision["needs_human_review"] is not False:
        raise ValidationError("script package has not passed its hard gate")
    render_duration = float(render["technical"]["duration_seconds"])
    if abs(float(script["segments"][-1]["end_seconds"]) - render_duration) > 0.5:
        raise ValidationError("script timing does not match render duration")
    output = _master_path(render_result, render)

    transcript_manifest = _transcript_upstream(
        task, job_id=job_id, lane=lane, render=render
    )
    root = _configured_evidence_root()
    transcript_path, transcript_sha256 = _evidence_descriptor(
        transcript_manifest["evidence"], root
    )
    transcript = _load_json(transcript_path)
    words = _validate_transcript(transcript, job_id=job_id, render=render)
    if transcript_manifest["word_count"] != len(words):
        raise ValidationError("caption_transcript_manifest word_count does not match evidence")
    thresholds = _thresholds(payload.get("thresholds"))
    metrics, findings = _analyze(script, words, thresholds)

    bindings = {
        "output_sha256": render["output_sha256"],
        "render_manifest_sha256": _artifact_sha256(render),
        "script_package_sha256": _artifact_sha256(script),
        "machine_evidence_sha256": transcript_sha256,
    }
    run_material = {
        "category": "captions",
        "job_id": job_id,
        "lane_id": lane,
        "render_id": render["render_id"],
        "bindings": bindings,
        "thresholds": thresholds,
        "checker_version": "1.0.0",
    }
    run_id = "cap-" + digest_text(canonical_json(run_material))[:20]
    completed_at = _parse_datetime(
        transcript["completed_at"], "transcript.completed_at"
    ).isoformat().replace("+00:00", "Z")
    report = {
        "schema_version": "1.0.0",
        "category": "captions",
        "job_id": job_id,
        "lane_id": lane,
        "render_id": render["render_id"],
        "render_sha256": render["output_sha256"],
        "status": "fail" if findings else "pass",
        "needs_human_review": False,
        "warnings": [],
        "findings": findings,
        "checker": {
            "name": "word_alignment_caption_analyzer",
            "version": "1.0.0",
            "run_id": run_id,
        },
        "completed_at": completed_at,
        "bindings": bindings,
        "metrics": metrics,
    }
    _validate_report(report)
    report_path = _atomic_report(root, job_id, run_id, report)
    return {
        "artifact": report,
        "evidence": {"path": str(report_path), "sha256": _sha256_file(report_path)},
        "master_path": str(output),
    }


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    try:
        task = json.load(source)
        if not isinstance(task, dict):
            raise ValidationError("handler stdin must contain one JSON object")
        result = handle_task(task)
    except (
        FactoryError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        sys.stderr.write(f"caption_analyzer_error:{type(exc).__name__}:{exc}\n")
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
