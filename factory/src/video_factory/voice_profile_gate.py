"""Fail-closed selection of human-approved Fish Audio voice profiles.

The queue's job-specific ``voice_rights_approval`` is intentionally not enough
to choose a voice.  This module binds the exact Fish ``reference_id`` to a
human-approved catalog entry and to the bytes of a reviewed golden WAV before
the provider may be called.  It never performs provider/network operations and
never falls back to another profile.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .contracts import _validate
from .errors import ValidationError
from .validators import canonical_json, digest_text, require_nonempty_string


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,127}$")
_APPROVED_RIGHTS = frozenset(
    {"approved_owned_voice", "approved_licensed_voice"}
)
_RIGHTS_BASIS = {
    "approved_owned_voice": "voice_owner_confirmation",
    "approved_licensed_voice": "commercial_license",
}
_NARRATED_LANES = frozenset(
    {"war_history", "celebrity_news", "chinese_medicine", "health"}
)
_CATALOG_MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class ApprovedVoiceProfile:
    """Validated catalog approval plus immutable hashes for later revalidation."""

    approval: dict[str, Any]
    binding: dict[str, Any]


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_catalog_path() -> Path:
    """Return the configured catalog without creating any trusted directories."""

    configured = os.environ.get("VIDEO_FACTORY_VOICE_PROFILE_CATALOG")
    if configured:
        return Path(configured).expanduser().resolve()

    # Source-tree default.  Deployed wheels must point at their provisioned,
    # external voice-profile store via VIDEO_FACTORY_VOICE_PROFILE_CATALOG.
    source_catalog = (
        Path(__file__).resolve().parents[2] / "voice_profiles" / "catalog.json"
    )
    if source_catalog.is_file():
        return source_catalog
    runtime_root = Path(
        os.environ.get("VIDEO_FACTORY_RUNTIME_ROOT", str(Path.home() / ".video-factory"))
    ).expanduser()
    return (runtime_root / "voice_profiles" / "catalog.json").resolve()


def _load_catalog(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink():
        raise ValidationError("voice profile catalog must not be a symlink")
    try:
        size = path.stat().st_size
        if size <= 0 or size > _CATALOG_MAX_BYTES:
            raise ValidationError("voice profile catalog has an unsafe size")
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValidationError(f"voice profile catalog is missing: {path}") from exc
    except OSError as exc:
        raise ValidationError("voice profile catalog is unreadable") from exc
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("voice profile catalog is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ValidationError("voice profile catalog must contain one JSON object")
    if document.get("schema_version") != "1.0.0":
        raise ValidationError("unsupported voice profile catalog schema_version")
    policy = document.get("selection_policy")
    if not isinstance(policy, Mapping):
        raise ValidationError("voice profile catalog selection_policy is missing")
    required_policy = {
        "automatic_fallback_allowed": False,
        "eligible_state": "approved",
        "golden_sample_sha256_required": True,
        "human_quality_approval_required": True,
        "job_bound_rights_approval_required": True,
        "maximum_fish_generations_per_video": 2,
    }
    for field, expected in required_policy.items():
        if policy.get(field) != expected:
            raise ValidationError(
                f"voice profile catalog selection_policy.{field} must equal {expected!r}"
            )
    profiles = document.get("profiles")
    if not isinstance(profiles, list):
        raise ValidationError("voice profile catalog profiles must be an array")
    if any(not isinstance(item, Mapping) for item in profiles):
        raise ValidationError("voice profile catalog entries must be objects")
    return document, _sha256_bytes(raw)


def _validate_datetime(value: Any, field: str) -> None:
    text = require_nonempty_string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO 8601 date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} must include a timezone")


def _validate_approval_schema(profile: dict[str, Any]) -> None:
    schema_path = Path(__file__).resolve().with_name("schemas") / (
        "voice_profile_approval.schema.json"
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("voice profile approval schema is unavailable") from exc
    if not isinstance(schema, Mapping):
        raise ValidationError("voice profile approval schema must be an object")
    _validate(profile, schema, "voice_profile_approval")


def _safe_golden_path(root: Path, value: Any) -> Path:
    relative = require_nonempty_string(value, "voice_profile_approval.golden_sample.path")
    if (
        "\\" in relative
        or relative.startswith("/")
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", relative)
        or ".." in Path(relative).parts
    ):
        raise ValidationError("golden sample path must be a safe relative POSIX path")
    root = root.expanduser().resolve()
    unresolved = root / Path(relative)
    if unresolved.is_symlink():
        raise ValidationError("golden sample must not be a symlink")
    path = unresolved.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValidationError("golden sample escapes the configured profile root") from exc
    if path.suffix.lower() != ".wav":
        raise ValidationError("golden sample must use the .wav extension")
    return path


def _verify_golden_sample(
    profile: Mapping[str, Any], *, profile_root: Path
) -> tuple[Path, str, int]:
    golden = profile["golden_sample"]
    path = _safe_golden_path(profile_root, golden["path"])
    try:
        before = path.stat()
    except FileNotFoundError as exc:
        raise ValidationError(f"golden voice WAV is missing: {path}") from exc
    except OSError as exc:
        raise ValidationError("golden voice WAV is unreadable") from exc
    if not path.is_file():
        raise ValidationError("golden voice WAV must be a regular file")
    if before.st_size != golden["size_bytes"]:
        raise ValidationError("golden voice WAV size does not match catalog approval")
    actual_sha = _sha256_file(path)
    if actual_sha != golden["sha256"]:
        raise ValidationError("golden voice WAV SHA-256 does not match catalog approval")
    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getnframes() <= 0 or reader.getframerate() <= 0:
                raise ValidationError("golden voice WAV contains no decodable audio")
            reader.readframes(min(reader.getnframes(), reader.getframerate()))
    except (wave.Error, EOFError, OSError) as exc:
        raise ValidationError("golden voice WAV is not a decodable PCM WAV") from exc
    try:
        after = path.stat()
    except OSError as exc:
        raise ValidationError("golden voice WAV changed during validation") from exc
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or _sha256_file(path) != actual_sha
    ):
        raise ValidationError("golden voice WAV changed during validation")
    return path, actual_sha, before.st_size


def load_approved_voice_profile(
    *,
    reference_id: str,
    lane_id: str,
    language: str,
    catalog_path: str | Path | None = None,
    profile_root: str | Path | None = None,
    expected_profile_id: str | None = None,
) -> ApprovedVoiceProfile:
    """Load and validate exactly one approved profile; never choose a fallback."""

    reference_id = require_nonempty_string(reference_id, "reference_id")
    if not _SAFE_ID.fullmatch(reference_id):
        raise ValidationError("reference_id contains unsafe characters")
    lane_id = require_nonempty_string(lane_id, "lane_id")
    if lane_id not in _NARRATED_LANES:
        raise ValidationError(f"lane {lane_id!r} does not use Fish voice profiles")
    language = require_nonempty_string(language, "language")
    if expected_profile_id is not None:
        expected_profile_id = require_nonempty_string(
            expected_profile_id, "voice_profile_id"
        )
        if not _SAFE_ID.fullmatch(expected_profile_id):
            raise ValidationError("voice_profile_id contains unsafe characters")

    catalog = Path(catalog_path).expanduser().resolve() if catalog_path else default_catalog_path()
    document, catalog_sha = _load_catalog(catalog)
    matches = [
        dict(item)
        for item in document["profiles"]
        if item.get("reference_id") == reference_id
    ]
    if not matches:
        raise ValidationError(
            f"no voice profile exists for exact reference_id {reference_id!r}; "
            "automatic fallback is forbidden"
        )
    if len(matches) != 1:
        raise ValidationError(
            f"voice profile catalog has duplicate exact reference_id {reference_id!r}"
        )
    profile = matches[0]
    if profile.get("state") != "approved":
        raise ValidationError(
            f"voice profile for reference_id {reference_id!r} is "
            f"{profile.get('state', 'not approved')!r}; automatic fallback is forbidden"
        )
    if expected_profile_id is not None and profile.get("profile_id") != expected_profile_id:
        raise ValidationError("voice_profile_id does not match the exact reference_id")

    _validate_approval_schema(profile)
    if lane_id not in profile["eligible_lanes"]:
        raise ValidationError(
            f"voice profile {profile['profile_id']!r} is not approved for lane {lane_id!r}"
        )
    if language not in profile["languages"]:
        raise ValidationError(
            f"voice profile {profile['profile_id']!r} is not approved for language {language!r}"
        )
    if profile["rights_status"] not in _APPROVED_RIGHTS:
        raise ValidationError("voice profile rights_status is not approved")
    rights = profile["rights_review"]
    if rights["basis"] != _RIGHTS_BASIS[profile["rights_status"]]:
        raise ValidationError("voice profile rights basis contradicts rights_status")
    for review_name in ("rights_review", "quality_review"):
        review = profile[review_name]
        if not review["reviewed_by"].strip():
            raise ValidationError(f"voice profile {review_name}.reviewed_by is blank")
        _validate_datetime(
            review["reviewed_at"], f"voice_profile_approval.{review_name}.reviewed_at"
        )

    resolved_root = (
        Path(profile_root).expanduser().resolve()
        if profile_root is not None
        else Path(
            os.environ.get("VIDEO_FACTORY_VOICE_PROFILE_ROOT", str(catalog.parent))
        ).expanduser().resolve()
    )
    golden_path, golden_sha, golden_bytes = _verify_golden_sample(
        profile, profile_root=resolved_root
    )
    # Deep-copy through canonical JSON so callers cannot mutate the catalog
    # document behind the hashes returned in the binding.
    approval = json.loads(canonical_json(profile))
    approval_sha = digest_text(canonical_json(approval))
    binding = {
        "schema_version": "1.0.0",
        "profile_id": approval["profile_id"],
        "provider": approval["provider"],
        "reference_id": approval["reference_id"],
        "lane_id": lane_id,
        "language": language,
        "rights_status": approval["rights_status"],
        "profile_approval_sha256": approval_sha,
        "catalog_path": str(catalog),
        "catalog_sha256": catalog_sha,
        "golden_sample_path": str(golden_path),
        "golden_sample_sha256": golden_sha,
        "golden_sample_bytes": golden_bytes,
    }
    return ApprovedVoiceProfile(approval=approval, binding=binding)


__all__ = [
    "ApprovedVoiceProfile",
    "default_catalog_path",
    "load_approved_voice_profile",
]
