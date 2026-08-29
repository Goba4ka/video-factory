"""Build and update the perceptual-dedup corpus from approved render bytes.

The corpus is an operational input to :mod:`video_factory.dedup_analyzer`, not
a place for hand-authored hashes.  Every ingest is authorized by a separate
human approval artifact, bound to the exact RenderManifest file and master
bytes.  The updater re-probes and re-hashes those bytes and uses the analyzer's
own frame extractor and dHash implementation before replacing the snapshot.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .contracts import validate_artifact
from .dedup_analyzer import (
    DEDUP_ALGORITHM,
    MINIMUM_SAMPLE_FRAMES,
    fingerprint_frames,
)
from .errors import ValidationError
from .media_tools import media_summary, probe_media
from .qc_analyzer_common import FrameExtractor, extract_gray_frames
from .validators import canonical_json


CORPUS_APPROVAL_CONFIRMATION = "INCLUDE_EXACT_MASTER_IN_DEDUP_CORPUS"
CORPUS_BUILDER_VERSION = "1.0.0"
DEFAULT_SAMPLE_INTERVAL_SECONDS = 1.0
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
MAX_APPROVAL_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024

MediaProber = Callable[[str | Path], dict[str, Any]]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_timestamp(value: datetime | None = None) -> str:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValidationError("timestamp source must include a timezone")
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _absolute_regular_file(
    value: str | Path, field: str, *, max_bytes: int | None = None
) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ValidationError(f"{field} must be an absolute path")
    if raw.is_symlink():
        raise ValidationError(f"{field} must not be a symlink")
    path = raw.resolve()
    if not path.is_file():
        raise ValidationError(f"{field} must be an existing regular file")
    size = path.stat().st_size
    if size < 1:
        raise ValidationError(f"{field} must not be empty")
    if max_bytes is not None and size > max_bytes:
        raise ValidationError(f"{field} exceeds the {max_bytes}-byte limit")
    return path


def _absolute_output(value: str | Path, field: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ValidationError(f"{field} must be an absolute path")
    if raw.is_symlink():
        raise ValidationError(f"{field} must not be a symlink")
    path = raw.resolve()
    if path.exists() and not path.is_file():
        raise ValidationError(f"{field} must be a regular file path")
    return path


def _load_json_file(path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{field} is not readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must contain a JSON object")
    return value


def _approval_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("dedup corpus approval timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("dedup corpus approval timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValidationError("dedup corpus approval timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _load_render_manifest(path: Path) -> dict[str, Any]:
    manifest = _load_json_file(path, "render_manifest")
    validate_artifact("render_manifest", manifest)
    return manifest


def _verify_probe(
    master_path: Path,
    manifest: Mapping[str, Any],
    *,
    media_prober: MediaProber,
) -> dict[str, Any]:
    summary = media_summary(media_prober(master_path))
    video = summary.get("video")
    audio = summary.get("audio")
    if not isinstance(video, Mapping) or not isinstance(audio, Mapping):
        raise ValidationError("approved master must contain video and audio streams")
    technical = manifest["technical"]
    checks = (
        (video.get("width"), technical["width"], "width"),
        (video.get("height"), technical["height"], "height"),
        (video.get("codec"), technical["video_codec"], "video codec"),
        (audio.get("codec"), technical["audio_codec"], "audio codec"),
        (
            audio.get("sample_rate_hz"),
            technical["audio_sample_rate_hz"],
            "audio sample rate",
        ),
    )
    for actual, expected, label in checks:
        if actual != expected:
            raise ValidationError(
                f"approved master {label} does not match RenderManifest"
            )
    fps = video.get("fps")
    if not isinstance(fps, (int, float)) or isinstance(fps, bool):
        raise ValidationError("approved master frame rate is unavailable")
    if abs(float(fps) - float(technical["fps"])) > 0.02:
        raise ValidationError("approved master frame rate does not match RenderManifest")
    duration = summary.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        raise ValidationError("approved master duration is unavailable")
    if not math.isfinite(float(duration)) or abs(
        float(duration) - float(technical["duration_seconds"])
    ) > 0.25:
        raise ValidationError("approved master duration does not match RenderManifest")
    return summary


def _verify_manifest_and_master(
    manifest_path: Path,
    master_path: Path,
    *,
    manifest_file_sha256: str | None = None,
    master_sha256: str | None = None,
    master_size_bytes: int | None = None,
    media_prober: MediaProber = probe_media,
) -> tuple[dict[str, Any], str, int, dict[str, Any]]:
    actual_manifest_sha256 = _sha256_file(manifest_path)
    if (
        manifest_file_sha256 is not None
        and actual_manifest_sha256 != manifest_file_sha256
    ):
        raise ValidationError("RenderManifest file checksum does not match approval")
    manifest = _load_render_manifest(manifest_path)
    actual_size = master_path.stat().st_size
    if master_size_bytes is not None and actual_size != master_size_bytes:
        raise ValidationError("approved master size does not match approval")
    actual_master_sha256 = _sha256_file(master_path)
    if master_sha256 is not None and actual_master_sha256 != master_sha256:
        raise ValidationError("approved master checksum does not match approval")
    if actual_master_sha256 != manifest["output_sha256"]:
        raise ValidationError("approved master bytes do not match RenderManifest")
    summary = _verify_probe(master_path, manifest, media_prober=media_prober)
    if (
        master_path.stat().st_size != actual_size
        or _sha256_file(master_path) != actual_master_sha256
    ):
        raise ValidationError("approved master changed during media probe")
    if _sha256_file(manifest_path) != actual_manifest_sha256:
        raise ValidationError("RenderManifest changed during media probe")
    return manifest, actual_master_sha256, actual_size, summary


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValidationError(f"refusing to replace symlink output: {path}")
    data = (
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


@contextmanager
def _snapshot_lock(snapshot_path: Path, timeout_seconds: float) -> Iterator[None]:
    if not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 300:
        raise ValidationError("lock_timeout_seconds must be from 0.1 to 300")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = snapshot_path.with_name(f".{snapshot_path.name}.write-lock")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            lock_path.mkdir()
            break
        except (FileExistsError, PermissionError) as exc:
            if time.monotonic() >= deadline:
                raise ValidationError(
                    f"dedup corpus is busy: {snapshot_path}"
                ) from exc
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            lock_path.rmdir()
        except OSError as exc:
            raise ValidationError(f"cannot release dedup corpus lock: {lock_path}") from exc


def create_corpus_approval(
    render_manifest_path: str | Path,
    master_path: str | Path,
    output_path: str | Path,
    *,
    approved_by: str,
    approval_note: str,
    human_confirm: str,
    approved_at: datetime | None = None,
    media_prober: MediaProber = probe_media,
) -> dict[str, Any]:
    """Create one explicit human approval for corpus inclusion.

    This approval authorizes only dedup-corpus ingestion.  It is not a publish
    approval and cannot advance ``final_review`` or ``publisher``.
    """

    if human_confirm != CORPUS_APPROVAL_CONFIRMATION:
        raise ValidationError(
            "human_confirm must exactly acknowledge dedup corpus inclusion"
        )
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValidationError("approved_by must be a non-empty string")
    if not isinstance(approval_note, str) or len(approval_note.strip()) < 5:
        raise ValidationError("approval_note must contain at least five characters")
    manifest_path = _absolute_regular_file(
        render_manifest_path, "render_manifest_path", max_bytes=MAX_MANIFEST_BYTES
    )
    master = _absolute_regular_file(master_path, "master_path")
    destination = _absolute_output(output_path, "output_path")
    manifest, master_sha256, size_bytes, _ = _verify_manifest_and_master(
        manifest_path, master, media_prober=media_prober
    )
    timestamp = _utc_timestamp(approved_at)
    identity = canonical_json(
        {
            "job_id": manifest["job_id"],
            "render_id": manifest["render_id"],
            "render_sha256": master_sha256,
            "approved_by": approved_by.strip(),
            "approved_at": timestamp,
        }
    )
    approval = {
        "schema_version": "1.0.0",
        "approval_id": f"dedupapproval_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}",
        "decision": "approved_for_dedup_corpus",
        "approved_by": approved_by.strip(),
        "approved_at": timestamp,
        "approval_note": approval_note.strip(),
        "job_id": manifest["job_id"],
        "render_id": manifest["render_id"],
        "render_manifest": {
            "path": str(manifest_path),
            "file_sha256": _sha256_file(manifest_path),
        },
        "master": {
            "path": str(master),
            "sha256": master_sha256,
            "size_bytes": size_bytes,
        },
    }
    validate_artifact("dedup_corpus_approval", approval)
    if destination.exists():
        existing = _load_json_file(destination, "existing dedup_corpus_approval")
        validate_artifact("dedup_corpus_approval", existing)
        reusable = (
            existing["decision"] == "approved_for_dedup_corpus"
            and existing["approved_by"] == approval["approved_by"]
            and existing["approval_note"] == approval["approval_note"]
            and existing["job_id"] == approval["job_id"]
            and existing["render_id"] == approval["render_id"]
            and existing["render_manifest"] == approval["render_manifest"]
            and existing["master"] == approval["master"]
        )
        if not reusable:
            raise ValidationError(
                "existing dedup corpus approval conflicts; use a new immutable path"
            )
        approval = existing
    else:
        _atomic_json(destination, approval)
    return {
        "ok": True,
        "command": "dedup-corpus-approve",
        "approval": approval,
        "approval_path": str(destination),
        "approval_file_sha256": _sha256_file(destination),
        "authority": "dedup_corpus_only",
    }


def _load_approval(path_value: str | Path) -> tuple[dict[str, Any], Path, str]:
    path = _absolute_regular_file(
        path_value, "approval_path", max_bytes=MAX_APPROVAL_BYTES
    )
    value = _load_json_file(path, "dedup_corpus_approval")
    validate_artifact("dedup_corpus_approval", value)
    _approval_time(value["approved_at"])
    return value, path, _sha256_file(path)


def _load_existing_snapshot(
    snapshot_path: Path, sample_interval_seconds: float
) -> dict[str, Any] | None:
    if not snapshot_path.exists():
        return None
    if snapshot_path.is_symlink() or not snapshot_path.is_file():
        raise ValidationError("snapshot_path must not be a symlink or special file")
    snapshot = _load_json_file(snapshot_path, "dedup_corpus_snapshot")
    validate_artifact("dedup_corpus_snapshot", snapshot)
    if snapshot["algorithm"] != DEDUP_ALGORITHM:
        raise ValidationError("existing corpus uses an unsupported algorithm")
    try:
        generated_at = datetime.fromisoformat(
            snapshot["generated_at"].replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValidationError("existing corpus generated_at is invalid") from exc
    if generated_at.tzinfo is None:
        raise ValidationError("existing corpus generated_at must include a timezone")
    if abs(float(snapshot["sample_interval_seconds"]) - sample_interval_seconds) > 1e-9:
        raise ValidationError("existing corpus sample interval does not match update")
    identities: set[tuple[str, str]] = set()
    for entry in snapshot["entries"]:
        identity = (entry["job_id"], entry["render_id"])
        if identity in identities:
            raise ValidationError("existing corpus contains duplicate render identity")
        identities.add(identity)
    return snapshot


def _comparison_id(job_id: str, render_id: str) -> str:
    identity = canonical_json({"job_id": job_id, "render_id": render_id})
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"cmp_{digest[:24]}"


def _fingerprint_approved_master(
    approval: Mapping[str, Any],
    *,
    sample_interval_seconds: float,
    frame_extractor: FrameExtractor,
    media_prober: MediaProber,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_descriptor = approval["render_manifest"]
    master_descriptor = approval["master"]
    manifest_path = _absolute_regular_file(
        manifest_descriptor["path"],
        "approval.render_manifest.path",
        max_bytes=MAX_MANIFEST_BYTES,
    )
    master_path = _absolute_regular_file(
        master_descriptor["path"], "approval.master.path"
    )
    manifest, master_sha256, size_bytes, summary = _verify_manifest_and_master(
        manifest_path,
        master_path,
        manifest_file_sha256=manifest_descriptor["file_sha256"],
        master_sha256=master_descriptor["sha256"],
        master_size_bytes=master_descriptor["size_bytes"],
        media_prober=media_prober,
    )
    if manifest["job_id"] != approval["job_id"]:
        raise ValidationError("approval job_id does not match RenderManifest")
    if manifest["render_id"] != approval["render_id"]:
        raise ValidationError("approval render_id does not match RenderManifest")
    duration = float(manifest["technical"]["duration_seconds"])
    maximum_frames = min(
        400,
        max(
            MINIMUM_SAMPLE_FRAMES,
            math.ceil(duration / sample_interval_seconds) + 2,
        ),
    )
    frames = frame_extractor(
        master_path,
        interval_seconds=sample_interval_seconds,
        width=9,
        height=8,
        maximum_frames=maximum_frames,
    )
    hashes = fingerprint_frames(frames)
    # Fail closed on a file replacement or mutation during probe/decode.
    final_stat = master_path.stat()
    if final_stat.st_size != size_bytes or _sha256_file(master_path) != master_sha256:
        raise ValidationError("approved master changed during corpus fingerprinting")
    if _sha256_file(manifest_path) != manifest_descriptor["file_sha256"]:
        raise ValidationError("RenderManifest changed during corpus fingerprinting")
    entry = {
        "comparison_id": _comparison_id(manifest["job_id"], manifest["render_id"]),
        "job_id": manifest["job_id"],
        "render_id": manifest["render_id"],
        "render_sha256": master_sha256,
        "frame_hashes": hashes,
    }
    observation = {
        "approval_id": approval["approval_id"],
        "job_id": manifest["job_id"],
        "render_id": manifest["render_id"],
        "render_sha256": master_sha256,
        "sampled_frame_count": len(hashes),
        "probe": summary,
    }
    return entry, observation


def update_dedup_corpus(
    snapshot_path: str | Path,
    approval_paths: Sequence[str | Path],
    *,
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    frame_extractor: FrameExtractor = extract_gray_frames,
    media_prober: MediaProber = probe_media,
    generated_at: datetime | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Atomically merge one or more explicitly approved masters into a corpus."""

    if not isinstance(sample_interval_seconds, (int, float)) or isinstance(
        sample_interval_seconds, bool
    ):
        raise ValidationError("sample_interval_seconds must be a number")
    interval = float(sample_interval_seconds)
    if not math.isfinite(interval) or not 0.25 <= interval <= 5.0:
        raise ValidationError("sample_interval_seconds must be from 0.25 to 5.0")
    if not approval_paths:
        raise ValidationError("at least one explicit corpus approval is required")
    snapshot = _absolute_output(snapshot_path, "snapshot_path")

    loaded: list[tuple[dict[str, Any], Path, str]] = []
    approval_ids: set[str] = set()
    for value in approval_paths:
        approval, path, approval_sha256 = _load_approval(value)
        if approval["approval_id"] in approval_ids:
            raise ValidationError("approval_id values must be unique in one update")
        approval_ids.add(approval["approval_id"])
        loaded.append((approval, path, approval_sha256))

    # Decode every input before taking the short snapshot write lock.  A second
    # byte/hash check inside _fingerprint_approved_master closes each read.
    candidates: list[tuple[dict[str, Any], dict[str, Any], Path, str]] = []
    batch_identities: dict[tuple[str, str], dict[str, Any]] = {}
    for approval, approval_path, approval_sha256 in loaded:
        entry, observation = _fingerprint_approved_master(
            approval,
            sample_interval_seconds=interval,
            frame_extractor=frame_extractor,
            media_prober=media_prober,
        )
        identity = (entry["job_id"], entry["render_id"])
        prior = batch_identities.get(identity)
        if prior is not None and prior != entry:
            raise ValidationError("conflicting approvals target the same render identity")
        batch_identities[identity] = entry
        observation["approval_path"] = str(approval_path)
        observation["approval_file_sha256"] = approval_sha256
        candidates.append((entry, observation, approval_path, approval_sha256))

    with _snapshot_lock(snapshot, lock_timeout_seconds):
        # Close the decode-to-commit window.  The heavy frame extraction stays
        # outside the lock, while exact approved bytes are checked once more
        # immediately before the corpus replacement.
        for approval, _, _ in loaded:
            manifest_descriptor = approval["render_manifest"]
            master_descriptor = approval["master"]
            manifest_path = _absolute_regular_file(
                manifest_descriptor["path"],
                "approval.render_manifest.path",
                max_bytes=MAX_MANIFEST_BYTES,
            )
            master_path = _absolute_regular_file(
                master_descriptor["path"], "approval.master.path"
            )
            if _sha256_file(manifest_path) != manifest_descriptor["file_sha256"]:
                raise ValidationError("RenderManifest changed before corpus commit")
            if (
                master_path.stat().st_size != master_descriptor["size_bytes"]
                or _sha256_file(master_path) != master_descriptor["sha256"]
            ):
                raise ValidationError("approved master changed before corpus commit")
        existing = _load_existing_snapshot(snapshot, interval)
        entries_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
        comparison_owners: dict[str, tuple[str, str]] = {}
        for entry in (existing or {}).get("entries", []):
            identity = (entry["job_id"], entry["render_id"])
            entries_by_identity[identity] = dict(entry)
            comparison_owners[entry["comparison_id"]] = identity

        added = 0
        replaced = 0
        unchanged_inputs = 0
        observations: list[dict[str, Any]] = []
        for candidate, observation, _, _ in sorted(
            candidates,
            key=lambda item: (
                item[0]["job_id"],
                item[0]["render_id"],
                item[1]["approval_id"],
            ),
        ):
            identity = (candidate["job_id"], candidate["render_id"])
            current = entries_by_identity.get(identity)
            if current is not None:
                # Preserve a pre-existing comparison_id so an established
                # identity never changes merely because the builder was added.
                candidate = {**candidate, "comparison_id": current["comparison_id"]}
                if candidate == current:
                    unchanged_inputs += 1
                    action = "unchanged"
                else:
                    entries_by_identity[identity] = candidate
                    replaced += 1
                    action = "replaced"
            else:
                owner = comparison_owners.get(candidate["comparison_id"])
                if owner is not None and owner != identity:
                    raise ValidationError("deterministic comparison_id collision")
                entries_by_identity[identity] = candidate
                comparison_owners[candidate["comparison_id"]] = identity
                added += 1
                action = "added"
            observations.append({**observation, "action": action})

        entries = sorted(
            entries_by_identity.values(), key=lambda item: item["comparison_id"]
        )
        if not entries:
            raise ValidationError("refusing to create an empty dedup corpus")
        identity_payload = {
            "algorithm": DEDUP_ALGORITHM,
            "sample_interval_seconds": interval,
            "entries": entries,
        }
        snapshot_id = (
            "dedup_"
            + hashlib.sha256(
                canonical_json(identity_payload).encode("utf-8")
            ).hexdigest()[:24]
        )
        existing_identity = (
            {
                "algorithm": existing["algorithm"],
                "sample_interval_seconds": float(
                    existing["sample_interval_seconds"]
                ),
                "entries": existing["entries"],
            }
            if existing is not None
            else None
        )
        changed = (
            existing is None
            or existing["snapshot_id"] != snapshot_id
            or existing_identity != identity_payload
        )
        if changed:
            document = {
                "schema_version": "1.0.0",
                "snapshot_id": snapshot_id,
                "generated_at": _utc_timestamp(generated_at),
                **identity_payload,
            }
            validate_artifact("dedup_corpus_snapshot", document)
            _atomic_json(snapshot, document)
        else:
            document = existing
            assert document is not None

    return {
        "ok": True,
        "command": "dedup-corpus-update",
        "builder_version": CORPUS_BUILDER_VERSION,
        "snapshot_path": str(snapshot),
        "snapshot_file_sha256": _sha256_file(snapshot),
        "snapshot": document,
        "changed": changed,
        "counts": {
            "entries": len(document["entries"]),
            "added": added,
            "replaced": replaced,
            "unchanged_inputs": unchanged_inputs,
        },
        "ingests": observations,
    }


__all__ = [
    "CORPUS_APPROVAL_CONFIRMATION",
    "CORPUS_BUILDER_VERSION",
    "create_corpus_approval",
    "update_dedup_corpus",
]
