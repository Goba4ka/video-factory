"""Fail-closed lane-aware music catalog resolution.

Reference fingerprints describe patterns observed in successful short videos;
they never grant a right to use the referenced recording.  Production tracks
are separate, checksum-bound local WAV records with explicit licence scope and
human approval.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError
from .validators import canonical_json, digest_text, require_nonempty_string


LANE_IDS = frozenset(
    {"war_history", "celebrity_news", "motivation", "chinese_medicine", "health"}
)
PLATFORMS = frozenset({"tiktok", "instagram_reels", "youtube_shorts"})
PLACEMENTS = frozenset(
    {"organic_feed", "paid_ads", "branded_content", "spark_ads"}
)
_SHA256 = frozenset("0123456789abcdef")
_TRACK_KEYS = frozenset(
    {
        "track_id",
        "asset_id",
        "lane_id",
        "archetype_id",
        "slot_id",
        "status",
        "local_wav_path",
        "sha256",
        "reference_fingerprint_ids",
        "rights",
        "human_approval",
    }
)
_RIGHTS_KEYS = frozenset(
    {
        "creator",
        "license_name",
        "license_source",
        "license_url",
        "license_evidence_path",
        "license_evidence_sha256",
        "commercial_use",
        "modification_allowed",
        "platform_scope",
        "territories",
        "placements",
        "expires_at",
        "attribution_required",
        "attribution_text",
    }
)
_APPROVAL_KEYS = frozenset(
    {
        "approved",
        "approved_by",
        "approved_at",
        "approval_note",
        "reviewed_track_id",
        "approved_track_sha256",
    }
)
_SELECTION_KEYS = frozenset(
    {
        "catalog_id",
        "catalog_version",
        "track_id",
        "asset_id",
        "archetype_id",
        "requested_platforms",
        "requested_territories",
        "requested_placements",
    }
)


def _nonempty(value: Any, field: str) -> str:
    return require_nonempty_string(value, field)


def _sha(value: Any, field: str) -> str:
    text = _nonempty(value, field)
    if len(text) != 64 or any(char not in _SHA256 for char in text):
        raise ValidationError(f"{field} must be a lowercase SHA-256")
    return text


def _string_list(
    value: Any,
    field: str,
    *,
    allowed: frozenset[str] | None = None,
    nonempty: bool = True,
) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValidationError(f"{field} must be a{' non-empty' if nonempty else ''} array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"{field} must contain non-empty strings")
    normalized = [item.strip() for item in value]
    if len(normalized) != len(set(normalized)):
        raise ValidationError(f"{field} must contain unique values")
    if allowed is not None and any(item not in allowed for item in normalized):
        raise ValidationError(f"{field} contains an unsupported value")
    return normalized


def _parse_datetime(value: Any, field: str) -> datetime:
    text = _nonempty(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _regular_file(path_value: Any, field: str) -> Path:
    raw = Path(_nonempty(path_value, field)).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise ValidationError(f"{field} must be an absolute regular file")
    resolved = raw.resolve()
    if not resolved.is_file():
        raise ValidationError(f"{field} must be an existing regular file")
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValidationError(f"cannot hash music catalog file {path}: {exc}") from exc
    return digest.hexdigest()


def _track_core(track: Mapping[str, Any]) -> dict[str, Any]:
    return {key: track[key] for key in sorted(track) if key != "human_approval"}


def approved_track_sha256(track: Mapping[str, Any]) -> str:
    """Return the checksum that a music curator must approve."""

    return digest_text(canonical_json(_track_core(track)))


def load_catalog(path: Path | str) -> dict[str, Any]:
    catalog_path = Path(path).expanduser()
    if not catalog_path.is_absolute():
        catalog_path = catalog_path.resolve()
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ValidationError("music catalog must be an existing regular JSON file")
    try:
        value = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"music catalog is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("music catalog root must be an object")
    validate_catalog(value)
    return value


def _validate_archetype(archetype: Mapping[str, Any], lane_id: str) -> None:
    required = {
        "archetype_id",
        "name_ru",
        "mood",
        "bpm_range",
        "energy_curve",
        "vocal_policy",
        "instrumentation",
        "cut_accent_behavior",
        "platform_notes",
    }
    if set(archetype) != required:
        raise ValidationError(f"{lane_id} archetype has unexpected or missing fields")
    _nonempty(archetype["archetype_id"], f"{lane_id}.archetype_id")
    _nonempty(archetype["name_ru"], f"{lane_id}.name_ru")
    _string_list(archetype["mood"], f"{lane_id}.mood")
    bpm = archetype["bpm_range"]
    if not isinstance(bpm, Mapping) or set(bpm) != {"min", "max"}:
        raise ValidationError(f"{lane_id}.bpm_range must contain min and max")
    low, high = bpm["min"], bpm["max"]
    if (
        isinstance(low, bool)
        or isinstance(high, bool)
        or not isinstance(low, (int, float))
        or not isinstance(high, (int, float))
        or not all(math.isfinite(float(item)) for item in (low, high))
        or not 40 <= float(low) <= float(high) <= 180
    ):
        raise ValidationError(f"{lane_id}.bpm_range is invalid")
    curve = archetype["energy_curve"]
    if not isinstance(curve, Mapping) or set(curve) != {"shape", "sections"}:
        raise ValidationError(f"{lane_id}.energy_curve is invalid")
    _nonempty(curve["shape"], f"{lane_id}.energy_curve.shape")
    _string_list(curve["sections"], f"{lane_id}.energy_curve.sections")
    if archetype["vocal_policy"] not in {
        "instrumental_only",
        "wordless_texture_only",
    }:
        raise ValidationError(f"{lane_id}.vocal_policy is invalid")
    instrumentation = archetype["instrumentation"]
    if not isinstance(instrumentation, Mapping) or set(instrumentation) != {
        "preferred",
        "forbidden",
    }:
        raise ValidationError(f"{lane_id}.instrumentation is invalid")
    _string_list(instrumentation["preferred"], f"{lane_id}.instrumentation.preferred")
    _string_list(
        instrumentation["forbidden"],
        f"{lane_id}.instrumentation.forbidden",
        nonempty=False,
    )
    behavior = archetype["cut_accent_behavior"]
    if not isinstance(behavior, Mapping) or set(behavior) != {
        "cut_grid",
        "accent_targets",
        "max_stinger_rate_seconds",
    }:
        raise ValidationError(f"{lane_id}.cut_accent_behavior is invalid")
    _nonempty(behavior["cut_grid"], f"{lane_id}.cut_accent_behavior.cut_grid")
    _string_list(
        behavior["accent_targets"], f"{lane_id}.cut_accent_behavior.accent_targets"
    )
    rate = behavior["max_stinger_rate_seconds"]
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 1 <= rate <= 30:
        raise ValidationError(f"{lane_id}.max_stinger_rate_seconds is invalid")
    notes = archetype["platform_notes"]
    if not isinstance(notes, Mapping) or set(notes) != PLATFORMS:
        raise ValidationError(f"{lane_id}.platform_notes must cover all platforms")
    for platform, note in notes.items():
        _nonempty(note, f"{lane_id}.platform_notes.{platform}")


def validate_catalog(catalog: Mapping[str, Any]) -> Mapping[str, Any]:
    required = {
        "schema_version",
        "catalog_id",
        "catalog_version",
        "generated_at",
        "reference_fingerprint_policy",
        "lanes",
        "track_slots",
        "tracks",
        "reference_fingerprints",
    }
    if set(catalog) != required:
        raise ValidationError("music catalog has unexpected or missing root fields")
    if catalog["schema_version"] != "1.0.0":
        raise ValidationError("unsupported music catalog schema_version")
    _nonempty(catalog["catalog_id"], "catalog_id")
    _nonempty(catalog["catalog_version"], "catalog_version")
    _parse_datetime(catalog["generated_at"], "generated_at")
    policy = catalog["reference_fingerprint_policy"]
    if not isinstance(policy, Mapping) or set(policy) != {
        "refresh_cadence",
        "sources",
        "required_success_evidence",
        "audio_reuse_prohibited",
        "reference_is_license_evidence",
    }:
        raise ValidationError("reference_fingerprint_policy is invalid")
    if policy["refresh_cadence"] not in {"nightly", "manual"}:
        raise ValidationError("reference fingerprint cadence must be nightly or manual")
    _string_list(policy["sources"], "reference_fingerprint_policy.sources")
    if policy["required_success_evidence"] is not True:
        raise ValidationError("reference fingerprints require success evidence")
    if policy["audio_reuse_prohibited"] is not True:
        raise ValidationError("reference audio reuse must be prohibited")
    if policy["reference_is_license_evidence"] is not False:
        raise ValidationError("a reference fingerprint must not be license evidence")

    lanes = catalog["lanes"]
    if not isinstance(lanes, Mapping) or set(lanes) != LANE_IDS:
        raise ValidationError("music catalog must define exactly the five production lanes")
    archetypes_by_lane: dict[str, set[str]] = {}
    for lane_id, lane in lanes.items():
        if not isinstance(lane, Mapping) or set(lane) != {
            "name_ru",
            "selection_policy",
            "archetypes",
        }:
            raise ValidationError(f"lane {lane_id} has an invalid music profile")
        _nonempty(lane["name_ru"], f"lanes.{lane_id}.name_ru")
        _nonempty(lane["selection_policy"], f"lanes.{lane_id}.selection_policy")
        archetypes = lane["archetypes"]
        if not isinstance(archetypes, list) or len(archetypes) < 2:
            raise ValidationError(f"lane {lane_id} requires at least two archetypes")
        ids: set[str] = set()
        for archetype in archetypes:
            if not isinstance(archetype, Mapping):
                raise ValidationError(f"lane {lane_id} archetype must be an object")
            _validate_archetype(archetype, lane_id)
            archetype_id = str(archetype["archetype_id"])
            if archetype_id in ids:
                raise ValidationError(f"lane {lane_id} repeats archetype_id")
            ids.add(archetype_id)
        archetypes_by_lane[lane_id] = ids

    slots = catalog["track_slots"]
    if not isinstance(slots, list) or not slots:
        raise ValidationError("music catalog track_slots must be non-empty")
    slot_ids: set[str] = set()
    for slot in slots:
        if not isinstance(slot, Mapping) or set(slot) != {
            "slot_id",
            "lane_id",
            "archetype_id",
            "status",
            "target_platform_scope",
            "target_territories",
            "target_placements",
            "license_requirement",
            "reference_fingerprint_slot",
        }:
            raise ValidationError("music catalog track slot is invalid")
        slot_id = _nonempty(slot["slot_id"], "track_slots.slot_id")
        if slot_id in slot_ids:
            raise ValidationError("music catalog repeats slot_id")
        slot_ids.add(slot_id)
        lane_id = slot["lane_id"]
        if lane_id not in LANE_IDS or slot["archetype_id"] not in archetypes_by_lane[lane_id]:
            raise ValidationError(f"track slot {slot_id} has an incompatible archetype")
        if slot["status"] not in {"pending_reference", "pending_license", "ready"}:
            raise ValidationError(f"track slot {slot_id} has an invalid status")
        _string_list(slot["target_platform_scope"], f"{slot_id}.target_platform_scope", allowed=PLATFORMS)
        _string_list(slot["target_territories"], f"{slot_id}.target_territories")
        _string_list(slot["target_placements"], f"{slot_id}.target_placements", allowed=PLACEMENTS)
        if slot["license_requirement"] not in {
            "independent_cross_platform_commercial_license",
            "tiktok_cml_exact_scope",
        }:
            raise ValidationError(f"track slot {slot_id} has an invalid license requirement")
        _nonempty(slot["reference_fingerprint_slot"], f"{slot_id}.reference_fingerprint_slot")

    fingerprints = catalog["reference_fingerprints"]
    if not isinstance(fingerprints, list):
        raise ValidationError("reference_fingerprints must be an array")
    fingerprint_ids: set[str] = set()
    for fingerprint in fingerprints:
        if not isinstance(fingerprint, Mapping):
            raise ValidationError("reference fingerprint must be an object")
        required_fingerprint = {
            "fingerprint_id",
            "lane_id",
            "source_platform",
            "source_url",
            "captured_at",
            "market",
            "observation_window",
            "success_evidence",
            "features",
            "audio_reuse_prohibited",
            "license_evidence",
        }
        if set(fingerprint) != required_fingerprint:
            raise ValidationError("reference fingerprint has unexpected or missing fields")
        fingerprint_id = _nonempty(fingerprint["fingerprint_id"], "fingerprint_id")
        if fingerprint_id in fingerprint_ids:
            raise ValidationError("reference fingerprint id is duplicated")
        fingerprint_ids.add(fingerprint_id)
        if fingerprint["lane_id"] not in LANE_IDS:
            raise ValidationError("reference fingerprint lane is invalid")
        if fingerprint["source_platform"] not in PLATFORMS:
            raise ValidationError("reference fingerprint platform is invalid")
        _nonempty(fingerprint["source_url"], "reference fingerprint source_url")
        _parse_datetime(fingerprint["captured_at"], "reference fingerprint captured_at")
        _nonempty(fingerprint["market"], "reference fingerprint market")
        _nonempty(fingerprint["observation_window"], "reference fingerprint observation_window")
        if not isinstance(fingerprint["success_evidence"], list) or not fingerprint["success_evidence"]:
            raise ValidationError("reference fingerprint requires success evidence")
        if not isinstance(fingerprint["features"], Mapping):
            raise ValidationError("reference fingerprint features must be an object")
        if fingerprint["audio_reuse_prohibited"] is not True or fingerprint["license_evidence"] is not False:
            raise ValidationError("reference fingerprint cannot authorize audio reuse")

    tracks = catalog["tracks"]
    if not isinstance(tracks, list):
        raise ValidationError("music catalog tracks must be an array")
    track_ids: set[str] = set()
    asset_ids: set[str] = set()
    for track in tracks:
        if not isinstance(track, Mapping) or set(track) != _TRACK_KEYS:
            raise ValidationError("music catalog track has unexpected or missing fields")
        track_id = _nonempty(track["track_id"], "tracks.track_id")
        asset_id = _nonempty(track["asset_id"], f"tracks.{track_id}.asset_id")
        if track_id in track_ids or asset_id in asset_ids:
            raise ValidationError("approved music track_id and asset_id must be unique")
        track_ids.add(track_id)
        asset_ids.add(asset_id)
        lane_id = track["lane_id"]
        if lane_id not in LANE_IDS or track["archetype_id"] not in archetypes_by_lane[lane_id]:
            raise ValidationError(f"track {track_id} has an incompatible lane/archetype")
        if track["slot_id"] not in slot_ids:
            raise ValidationError(f"track {track_id} names an unknown slot")
        matching_slot = next(item for item in slots if item["slot_id"] == track["slot_id"])
        if matching_slot["lane_id"] != lane_id or matching_slot["archetype_id"] != track["archetype_id"]:
            raise ValidationError(f"track {track_id} does not match its slot")
        if matching_slot["status"] != "ready":
            raise ValidationError(f"track {track_id} occupies a slot that is not ready")
        if track["status"] != "approved":
            raise ValidationError(f"track {track_id} is not production-approved")
        _nonempty(track["local_wav_path"], f"tracks.{track_id}.local_wav_path")
        _sha(track["sha256"], f"tracks.{track_id}.sha256")
        refs = _string_list(
            track["reference_fingerprint_ids"],
            f"tracks.{track_id}.reference_fingerprint_ids",
            nonempty=False,
        )
        if any(item not in fingerprint_ids for item in refs):
            raise ValidationError(f"track {track_id} names an unknown reference fingerprint")
        rights = track["rights"]
        if not isinstance(rights, Mapping) or set(rights) != _RIGHTS_KEYS:
            raise ValidationError(f"track {track_id} rights record is incomplete")
        for field in ("creator", "license_name", "license_source", "license_url", "license_evidence_path"):
            _nonempty(rights[field], f"tracks.{track_id}.rights.{field}")
        _sha(rights["license_evidence_sha256"], f"tracks.{track_id}.rights.license_evidence_sha256")
        if rights["commercial_use"] is not True or rights["modification_allowed"] is not True:
            raise ValidationError(f"track {track_id} lacks commercial modification rights")
        platforms = _string_list(rights["platform_scope"], f"tracks.{track_id}.rights.platform_scope", allowed=PLATFORMS)
        _string_list(rights["territories"], f"tracks.{track_id}.rights.territories")
        _string_list(rights["placements"], f"tracks.{track_id}.rights.placements", allowed=PLACEMENTS)
        if rights["license_source"] == "tiktok_commercial_music_library" and platforms != ["tiktok"]:
            raise ValidationError("TikTok CML rights cannot be expanded beyond TikTok")
        if rights["license_source"] == "tiktok_commercial_music_library" and matching_slot["license_requirement"] != "tiktok_cml_exact_scope":
            raise ValidationError("TikTok CML cannot fill a cross-platform music slot")
        expires = rights["expires_at"]
        if expires is not None:
            _parse_datetime(expires, f"tracks.{track_id}.rights.expires_at")
        if not isinstance(rights["attribution_required"], bool):
            raise ValidationError(f"track {track_id} attribution_required must be boolean")
        attribution = rights["attribution_text"]
        if rights["attribution_required"] and (not isinstance(attribution, str) or not attribution.strip()):
            raise ValidationError(f"track {track_id} requires attribution text")
        if attribution is not None and not isinstance(attribution, str):
            raise ValidationError(f"track {track_id} attribution_text is invalid")
        approval = track["human_approval"]
        if not isinstance(approval, Mapping) or set(approval) != _APPROVAL_KEYS:
            raise ValidationError(f"track {track_id} human approval is incomplete")
        if approval["approved"] is not True or approval["reviewed_track_id"] != track_id:
            raise ValidationError(f"track {track_id} lacks exact human approval")
        for field in ("approved_by", "approval_note"):
            _nonempty(approval[field], f"tracks.{track_id}.human_approval.{field}")
        _parse_datetime(approval["approved_at"], f"tracks.{track_id}.human_approval.approved_at")
        if approval["approved_track_sha256"] != approved_track_sha256(track):
            raise ValidationError(f"track {track_id} human approval checksum is stale")
    return catalog


def _territories_cover(granted: list[str], requested: list[str]) -> bool:
    return "worldwide" in granted or set(requested).issubset(granted)


def resolve_music_selection(
    catalog: Mapping[str, Any],
    *,
    lane_id: str,
    selection: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve an exact production track or fail closed.

    This function performs no discovery, download, provider call, or licence
    inference.  It verifies the selected local bytes and receipt at resolution
    time, then returns a frozen selection record for the BGM manifest.
    """

    validate_catalog(catalog)
    if lane_id not in LANE_IDS:
        raise ValidationError("music selection lane_id is unsupported")
    if not isinstance(selection, Mapping) or set(selection) != _SELECTION_KEYS:
        raise ValidationError(
            "bgm_selection must be an exact lane music catalog selection"
        )
    if selection["catalog_id"] != catalog["catalog_id"] or selection["catalog_version"] != catalog["catalog_version"]:
        raise ValidationError("bgm_selection is bound to a different music catalog")
    track_id = _nonempty(selection["track_id"], "bgm_selection.track_id")
    matches = [item for item in catalog["tracks"] if item["track_id"] == track_id]
    if len(matches) != 1:
        raise ValidationError("selected music track is not uniquely approved in the catalog")
    track = matches[0]
    if track["lane_id"] != lane_id:
        raise ValidationError("selected music track belongs to another lane")
    if selection["asset_id"] != track["asset_id"] or selection["archetype_id"] != track["archetype_id"]:
        raise ValidationError("bgm_selection does not match the approved track record")
    platforms = _string_list(selection["requested_platforms"], "bgm_selection.requested_platforms", allowed=PLATFORMS)
    territories = _string_list(selection["requested_territories"], "bgm_selection.requested_territories")
    placements = _string_list(selection["requested_placements"], "bgm_selection.requested_placements", allowed=PLACEMENTS)
    rights = track["rights"]
    if not set(platforms).issubset(rights["platform_scope"]):
        raise ValidationError("selected track license does not cover requested platforms")
    if not set(placements).issubset(rights["placements"]):
        raise ValidationError("selected track license does not cover requested placements")
    if not _territories_cover(list(rights["territories"]), territories):
        raise ValidationError("selected track license does not cover requested territories")
    if rights["license_source"] == "tiktok_commercial_music_library" and platforms != ["tiktok"]:
        raise ValidationError("TikTok CML selection may target TikTok only")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if rights["expires_at"] is not None and _parse_datetime(rights["expires_at"], "track rights expires_at") <= current:
        raise ValidationError("selected music track license has expired")
    wav = _regular_file(track["local_wav_path"], "music catalog local_wav_path")
    receipt = _regular_file(rights["license_evidence_path"], "music catalog license_evidence_path")
    if wav.suffix.lower() != ".wav":
        raise ValidationError("music catalog production source must be a WAV file")
    if _file_sha256(wav) != track["sha256"]:
        raise ValidationError("music catalog WAV checksum changed")
    if _file_sha256(receipt) != rights["license_evidence_sha256"]:
        raise ValidationError("music catalog license evidence checksum changed")
    return {
        "catalog_id": catalog["catalog_id"],
        "catalog_version": catalog["catalog_version"],
        "track_id": track_id,
        "track_record_sha256": approved_track_sha256(track),
        "asset_id": track["asset_id"],
        "lane_id": lane_id,
        "archetype_id": track["archetype_id"],
        "slot_id": track["slot_id"],
        "reference_fingerprint_ids": list(track["reference_fingerprint_ids"]),
        "local_wav_path": str(wav),
        "local_wav_sha256": track["sha256"],
        "license_evidence_path": str(receipt),
        "license_evidence_sha256": rights["license_evidence_sha256"],
        "license_source": rights["license_source"],
        "creator": rights["creator"],
        "license_name": rights["license_name"],
        "license_url": rights["license_url"],
        "commercial_use": rights["commercial_use"],
        "modification_allowed": rights["modification_allowed"],
        "platform_scope": list(rights["platform_scope"]),
        "territories": list(rights["territories"]),
        "placements": list(rights["placements"]),
        "expires_at": rights["expires_at"],
        "attribution_required": rights["attribution_required"],
        "attribution_text": rights["attribution_text"],
        "requested_platforms": platforms,
        "requested_territories": territories,
        "requested_placements": placements,
        "human_approval_sha256": digest_text(canonical_json(track["human_approval"])),
    }
