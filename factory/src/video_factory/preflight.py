from __future__ import annotations

import json
import re
from pathlib import Path
from statistics import median
from typing import Any

from .errors import ValidationError


TIME_RANGE_RE = re.compile(
    r"^\|\s*(?P<start>\d+(?::\d+(?:\.\d+)?)?)\s*[–-]\s*"
    r"(?P<end>\d+(?::\d+(?:\.\d+)?)?)\s*\|"
)
REFERENCE_RE = re.compile(r"\b(?P<prefix>[ACI])(?P<number>\d{2})\b")
WORD_RE = re.compile(r"\w+(?:[-‑]\w+)*", flags=re.UNICODE)
TIME_TAG_RE = re.compile(r"\*\*\[[^\]]+\]\*\*")


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"missing preproduction artifact: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must contain a JSON object")
    return value


def _seconds(value: str) -> float:
    if ":" not in value:
        return float(value)
    minutes, seconds = value.split(":", 1)
    return int(minutes) * 60 + float(seconds)


def _narration_text(markdown: str) -> str:
    lines = markdown.splitlines()
    headings = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if len(headings) < 2:
        raise ValidationError("SCRIPT_DRAFT.md must contain a narration section")
    body = "\n".join(lines[headings[0] + 1 : headings[1]])
    body = TIME_TAG_RE.sub("", body)
    body = re.sub(r"[`*_>#]", "", body)
    return body.strip()


def _shot_ranges(markdown: str) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    for line in markdown.splitlines():
        match = TIME_RANGE_RE.match(line)
        if not match:
            continue
        start = _seconds(match.group("start"))
        end = _seconds(match.group("end"))
        if end <= start:
            raise ValidationError(f"invalid shot range {start:g}-{end:g}")
        ranges.append((start, end))
    if not ranges:
        raise ValidationError("SHOTLIST_DRAFT.md contains no timed shot rows")
    return ranges


def _reference_ids(text: str, prefix: str) -> set[str]:
    return {
        match.group(0)
        for match in REFERENCE_RE.finditer(text)
        if match.group("prefix") == prefix
    }


def run_preflight(project: str | Path, profiles_file: str | Path) -> dict[str, Any]:
    root = Path(project).expanduser().resolve()
    profiles_path = Path(profiles_file).expanduser().resolve()
    idea = _load_object(root / "idea_card.json")
    ledger = _load_object(root / "claim_ledger.json")
    candidate_assets = _load_object(root / "candidate_assets.json")
    profiles = _load_object(profiles_path)

    script_path = root / "SCRIPT_DRAFT.md"
    shotlist_path = root / "SHOTLIST_DRAFT.md"
    try:
        script_markdown = script_path.read_text(encoding="utf-8")
        shotlist_markdown = shotlist_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValidationError(f"missing preproduction artifact: {exc.filename}") from exc

    format_data = idea.get("format")
    if not isinstance(format_data, dict):
        raise ValidationError("idea_card.format must be an object")
    profile_name = format_data.get("quality_profile")
    profile_table = profiles.get("profiles")
    if not isinstance(profile_table, dict) or profile_name not in profile_table:
        raise ValidationError(f"unknown quality profile: {profile_name!r}")
    profile = profile_table[profile_name]

    claims = ledger.get("claims")
    assets = candidate_assets.get("assets")
    if not isinstance(claims, list) or not isinstance(assets, list):
        raise ValidationError("claim_ledger.claims and candidate_assets.assets must be arrays")
    claim_ids = {item.get("id") for item in claims if isinstance(item, dict)}
    asset_ids = {item.get("id") for item in assets if isinstance(item, dict)}
    used_claim_ids = _reference_ids(script_markdown + "\n" + shotlist_markdown, "C")
    used_inference_ids = _reference_ids(script_markdown + "\n" + shotlist_markdown, "I")
    used_asset_ids = _reference_ids(shotlist_markdown, "A")
    missing_claims = sorted((used_claim_ids | used_inference_ids) - claim_ids)
    missing_assets = sorted(used_asset_ids - asset_ids)

    narration = _narration_text(script_markdown)
    word_count = len(WORD_RE.findall(narration))
    shot_ranges = _shot_ranges(shotlist_markdown)
    gaps: list[dict[str, float]] = []
    overlaps: list[dict[str, float]] = []
    for previous, current in zip(shot_ranges, shot_ranges[1:]):
        delta = current[0] - previous[1]
        if delta > 0.01:
            gaps.append({"from": previous[1], "to": current[0], "seconds": delta})
        elif delta < -0.01:
            overlaps.append(
                {"from": current[0], "to": previous[1], "seconds": abs(delta)}
            )
    durations = [end - start for start, end in shot_ranges]
    duration = shot_ranges[-1][1]
    target_duration = float(format_data.get("target_duration_seconds", duration))
    words_per_minute = word_count / duration * 60

    word_limits = profile.get("script_words", {})
    wpm_limits = profile.get("voice_words_per_minute", {})
    shot_limits = profile.get("shot_count", {})
    median_limits = profile.get("median_shot_seconds", {})

    review_assets = sorted(
        item.get("id")
        for item in assets
        if isinstance(item, dict)
        and item.get("rights_status")
        not in {"usable_with_nasa_media_guidelines", "approved", "public_domain"}
    )
    rejected_assets = sorted(
        item.get("id")
        for item in assets
        if isinstance(item, dict)
        and item.get("rights_status") in {"rejected", "blocked", "unusable"}
    )

    checks: dict[str, bool] = {
        "claim_references_resolve": not missing_claims,
        "asset_references_resolve": not missing_assets,
        "shotlist_starts_at_zero": abs(shot_ranges[0][0]) <= 0.01,
        "shotlist_is_contiguous": not gaps and not overlaps,
        "duration_matches_target": abs(duration - target_duration) <= 0.25,
        "script_words_match_profile": (
            word_limits.get("min", 0) <= word_count <= word_limits.get("max", 10**9)
        ),
        "voice_speed_matches_profile": (
            wpm_limits.get("min", 0)
            <= words_per_minute
            <= wpm_limits.get("max", 10**9)
        ),
        "shot_count_matches_profile": (
            shot_limits.get("min", 0)
            <= len(shot_ranges)
            <= shot_limits.get("max", 10**9)
        ),
        "median_shot_matches_profile": (
            median_limits.get("min", 0)
            <= median(durations)
            <= median_limits.get("max", 10**9)
        ),
        "no_rejected_asset_selected": not (used_asset_ids & set(rejected_assets)),
    }
    blockers: list[str] = []
    if not all(checks.values()):
        blockers.append("one or more preproduction integrity checks failed")
    if not idea.get("production_authorized", False):
        blockers.append("topic approval has not authorized production")
    selected_review_assets = sorted(used_asset_ids & set(review_assets))
    if selected_review_assets:
        blockers.append(
            "file-level rights confirmation is required for: "
            + ", ".join(selected_review_assets)
        )
    blockers.append("media files have not been downloaded, hashed, and frozen")

    return {
        "ok": all(checks.values()),
        "command": "preflight",
        "project": str(root),
        "phase": "preproduction",
        "profile": profile_name,
        "ready_for_topic_approval": all(checks.values()),
        "render_authorized": False,
        "checks": checks,
        "metrics": {
            "duration_seconds": duration,
            "target_duration_seconds": target_duration,
            "script_words": word_count,
            "voice_words_per_minute": round(words_per_minute, 2),
            "shot_count": len(shot_ranges),
            "median_shot_seconds": round(median(durations), 2),
            "used_claim_ids": sorted(used_claim_ids | used_inference_ids),
            "used_asset_ids": sorted(used_asset_ids),
        },
        "evidence": {
            "missing_claim_ids": missing_claims,
            "missing_asset_ids": missing_assets,
            "timeline_gaps": gaps,
            "timeline_overlaps": overlaps,
            "rights_review_asset_ids": selected_review_assets,
            "rejected_asset_ids": rejected_assets,
        },
        "blockers_before_render": blockers,
    }
