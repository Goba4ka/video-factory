"""Freeze one human-cleared music asset into an immutable local PCM WAV.

No discovery, download, license inference, or paid provider call occurs here.
The selected asset and its local license receipt must already be present in the
passed RightsManifest/FrozenMediaManifest pair.  Missing or mutable evidence
fails closed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, TextIO

from .contracts import validate_artifact
from .errors import FactoryError, ValidationError
from .media_freeze import MediaFreezeError, verify_frozen_media_manifest
from .media_tools import resolve_media_binary
from .music_catalog import load_catalog, resolve_music_selection
from .validators import canonical_json, digest_text, require_nonempty_string


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CHUNK_BYTES = 1024 * 1024
_RECIPE_VERSION = "bgm-freeze-1.1.0"
_TARGET_LUFS = -14.0
_TARGET_LRA = 7.0
_TARGET_TP = -1.5
_LOUDNESS_TOLERANCE = 0.5


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValidationError(f"cannot hash BGM file {path}: {exc}") from exc
    return digest.hexdigest()


def _artifact_sha256(value: Mapping[str, Any]) -> str:
    return digest_text(canonical_json(dict(value)))


def _safe_id(value: Any, field: str) -> str:
    normalized = require_nonempty_string(value, field)
    if not _SAFE_ID.fullmatch(normalized) or ".." in normalized:
        raise ValidationError(f"{field} contains unsafe path characters")
    return normalized


def _configured_root(name: str, default: Path, *, create: bool) -> Path:
    raw = Path(os.environ.get(name, str(default))).expanduser()
    if raw.is_symlink():
        raise ValidationError(f"{name} must not be a symlink")
    root = raw.resolve()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValidationError(f"{name} must point to an existing directory")
    return root


def _evidence_roots(runtime_root: Path) -> list[Path]:
    configured = os.environ.get("VIDEO_FACTORY_RIGHTS_EVIDENCE_ROOTS")
    if configured is None:
        return [
            _configured_root(
                "VIDEO_FACTORY_RIGHTS_EVIDENCE_ROOT",
                runtime_root / "rights_evidence",
                create=False,
            )
        ]
    roots: list[Path] = []
    for value in (item.strip() for item in configured.split(os.pathsep)):
        if not value:
            continue
        root = Path(value).expanduser()
        if root.is_symlink():
            raise ValidationError("rights evidence roots must not be symlinks")
        resolved = root.resolve()
        if not resolved.is_dir():
            raise ValidationError("rights evidence roots must be existing directories")
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise ValidationError("VIDEO_FACTORY_RIGHTS_EVIDENCE_ROOTS is empty")
    return roots


def _under_roots(path: Path, roots: list[Path], field: str) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise ValidationError(f"{field} must be an absolute regular file")
    resolved = raw.resolve()
    if not resolved.is_file():
        raise ValidationError(f"{field} must be an existing regular file")
    if not any(
        _is_relative_to(resolved, root)
        for root in roots
    ):
        raise ValidationError(f"{field} escapes configured rights evidence roots")
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _upstream_artifact(task: Mapping[str, Any], role: str, contract: str) -> dict[str, Any]:
    upstream = task.get("upstream_results")
    if not isinstance(upstream, list):
        raise ValidationError("task.upstream_results must be an array")
    matches: list[dict[str, Any]] = []
    for entry in upstream:
        if not isinstance(entry, Mapping) or entry.get("role") != role:
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(artifact, dict):
            matches.append(artifact)
    if len(matches) != 1:
        raise ValidationError(
            f"bgm task requires exactly one upstream {contract} from role={role!r}"
        )
    validate_artifact(contract, matches[0])
    return matches[0]


def _upstream_rights(
    task: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    upstream = task.get("upstream_results")
    if not isinstance(upstream, list):
        raise ValidationError("task.upstream_results must be an array")
    matches: list[Mapping[str, Any]] = []
    for entry in upstream:
        if not isinstance(entry, Mapping) or entry.get("role") != "rights":
            continue
        result = entry.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("artifact"), dict):
            matches.append(result)
    if len(matches) != 1:
        raise ValidationError("bgm task requires exactly one upstream rights result")
    artifact = dict(matches[0]["artifact"])
    validate_artifact("rights_manifest", artifact)
    approval = matches[0].get("human_approval")
    expected_fields = {
        "approved", "approved_by", "approved_at", "approval_note",
        "rights_manifest_sha256", "reviewed_asset_ids",
    }
    if not isinstance(approval, Mapping) or set(approval) != expected_fields:
        raise ValidationError("BGM requires an exact checksum-bound human rights approval")
    normalized = dict(approval)
    if normalized.get("approved") is not True:
        raise ValidationError("BGM human rights approval is not approved")
    for field in ("approved_by", "approved_at", "approval_note"):
        require_nonempty_string(normalized.get(field), f"human_approval.{field}")
    try:
        datetime.fromisoformat(str(normalized["approved_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("human_approval.approved_at must be ISO 8601") from exc
    rights_sha = _artifact_sha256(artifact)
    if normalized.get("rights_manifest_sha256") != rights_sha:
        raise ValidationError("BGM human approval is not bound to exact RightsManifest")
    reviewed = normalized.get("reviewed_asset_ids")
    asset_ids = [item["asset_id"] for item in artifact["assets"]]
    if (
        not isinstance(reviewed, list)
        or any(not isinstance(item, str) for item in reviewed)
        or len(reviewed) != len(set(reviewed))
        or set(reviewed) != set(asset_ids)
    ):
        raise ValidationError("BGM human approval does not cover every rights asset")
    return artifact, normalized, digest_text(canonical_json(normalized))


def _selected_asset(
    *,
    lane_id: str,
    payload: Mapping[str, Any],
    rights_manifest: Mapping[str, Any],
    frozen_manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], Path, dict[str, Any]]:
    selection = payload.get("bgm_selection")
    if not isinstance(selection, Mapping):
        raise ValidationError("payload.bgm_selection must be an exact catalog selection")
    catalog_path = os.environ.get(
        "VIDEO_FACTORY_MUSIC_CATALOG", "factory/music/lane_music_catalog.json"
    )
    catalog_selection = resolve_music_selection(
        load_catalog(catalog_path), lane_id=lane_id, selection=selection
    )
    asset_id = _safe_id(catalog_selection["asset_id"], "bgm_selection.asset_id")
    rights_matches = [
        dict(item) for item in rights_manifest["assets"] if item["asset_id"] == asset_id
    ]
    frozen_matches = [
        dict(item) for item in frozen_manifest["assets"] if item["asset_id"] == asset_id
    ]
    if len(rights_matches) != 1 or len(frozen_matches) != 1:
        raise ValidationError("selected BGM is not uniquely rights-bound and frozen")
    rights = rights_matches[0]
    frozen = frozen_matches[0]
    if (
        rights.get("rights_status") != "approved"
        or rights.get("commercial_use") is not True
        or rights.get("modification_allowed") is not True
    ):
        raise ValidationError("selected BGM is not approved for commercial modified use")
    if rights.get("attribution_required") is True and not (
        isinstance(rights.get("attribution_text"), str)
        and rights["attribution_text"].strip()
    ):
        raise ValidationError("selected BGM requires missing attribution text")
    expires = rights.get("expires_at")
    if isinstance(expires, str):
        try:
            expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("selected BGM expires_at is invalid") from exc
        if expiry <= datetime.now(UTC):
            raise ValidationError("selected BGM license has expired")
    if not str(frozen.get("content_type", "")).startswith("audio/"):
        raise ValidationError("selected BGM frozen asset must be audio")
    root = Path(frozen_manifest["frozen_root"]).expanduser().resolve()
    source = (root / Path(frozen["frozen_path"])).resolve()
    if not _is_relative_to(source, root) or source.is_symlink() or not source.is_file():
        raise ValidationError("selected BGM source is not a regular frozen file")
    if frozen["sha256"] != catalog_selection["local_wav_sha256"]:
        raise ValidationError("selected frozen BGM differs from approved catalog WAV")
    if set(rights["platforms"]) != set(catalog_selection["platform_scope"]):
        raise ValidationError("RightsManifest platform scope differs from music catalog")
    if set(rights.get("territories", [])) != set(catalog_selection["territories"]):
        raise ValidationError("RightsManifest territories differ from music catalog")
    comparisons = {
        "creator": "creator",
        "license": "license_name",
        "license_url": "license_url",
        "commercial_use": "commercial_use",
        "modification_allowed": "modification_allowed",
        "expires_at": "expires_at",
        "attribution_required": "attribution_required",
        "attribution_text": "attribution_text",
    }
    if any(rights[left] != catalog_selection[right] for left, right in comparisons.items()):
        raise ValidationError("RightsManifest license fields differ from music catalog")
    rights_receipt = Path(str(rights.get("license_receipt", ""))).expanduser().resolve()
    if rights_receipt != Path(catalog_selection["license_evidence_path"]):
        raise ValidationError("RightsManifest receipt differs from music catalog")
    return rights, frozen, source, catalog_selection


def _timeout() -> float:
    raw = os.environ.get("VIDEO_FACTORY_BGM_TIMEOUT_SECONDS", "600")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValidationError("VIDEO_FACTORY_BGM_TIMEOUT_SECONDS must be numeric") from exc
    if not math.isfinite(value) or not 0 < value <= 3600:
        raise ValidationError("VIDEO_FACTORY_BGM_TIMEOUT_SECONDS must be from 0 to 3600")
    return value


def _run(command: list[str], *, label: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_timeout(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError(f"{label} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().replace("\r", " ").replace("\n", " ")[-1200:]
        raise ValidationError(f"{label} exited {completed.returncode}: {detail}")
    return completed


def _ffmpeg_version(ffmpeg: str) -> str:
    completed = _run([ffmpeg, "-hide_banner", "-version"], label="FFmpeg version probe")
    first = completed.stdout.splitlines()[0].strip() if completed.stdout else ""
    if not first:
        raise ValidationError("FFmpeg version probe returned no version")
    return first[:512]


def _loudnorm_json(stderr: str) -> dict[str, float]:
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start < 0 or end <= start:
        raise ValidationError("FFmpeg BGM loudnorm returned no JSON measurements")
    try:
        raw = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValidationError("FFmpeg BGM loudnorm JSON is invalid") from exc
    result: dict[str, float] = {}
    for field in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        try:
            value = float(raw[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"FFmpeg BGM loudnorm lacks finite {field}") from exc
        if not math.isfinite(value):
            raise ValidationError(f"FFmpeg BGM loudnorm returned non-finite {field}")
        result[field] = value
    return result


def _measure_loudness(ffmpeg: str, source: Path) -> dict[str, float]:
    completed = _run(
        [
            ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
            "-af", "loudnorm=I=-14:LRA=7:TP=-1.5:print_format=json",
            "-f", "null", "-",
        ],
        label="FFmpeg BGM loudness measurement",
    )
    values = _loudnorm_json(completed.stderr)
    return {
        "integrated_loudness_lufs": values["input_i"],
        "loudness_range_lu": values["input_lra"],
        "true_peak_dbtp": values["input_tp"],
    }


def _verify_loudness(metrics: Mapping[str, float]) -> None:
    if abs(float(metrics["integrated_loudness_lufs"]) - _TARGET_LUFS) > _LOUDNESS_TOLERANCE:
        raise ValidationError("normalized BGM misses integrated loudness target")
    if float(metrics["true_peak_dbtp"]) > -1.4:
        raise ValidationError("normalized BGM exceeds true-peak ceiling")
    if not 0 <= float(metrics["loudness_range_lu"]) <= 20:
        raise ValidationError("normalized BGM loudness range is outside the contract")


def _normalize_wav(source: Path, destination: Path) -> tuple[str, dict[str, float]]:
    """Create a stable -14 LUFS bed so -9 dB pre-duck always means ~-23 LUFS."""

    ffmpeg = resolve_media_binary("ffmpeg")
    version = _ffmpeg_version(ffmpeg)
    first = _run(
        [
            ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-af", "loudnorm=I=-14:LRA=7:TP=-1.5:print_format=json",
            "-f", "null", "-",
        ],
        label="FFmpeg BGM loudness analysis",
    )
    measured = _loudnorm_json(first.stderr)
    loudnorm = (
        "loudnorm=I=-14:LRA=7:TP=-1.5:"
        f"measured_I={measured['input_i']:.6f}:"
        f"measured_LRA={measured['input_lra']:.6f}:"
        f"measured_TP={measured['input_tp']:.6f}:"
        f"measured_thresh={measured['input_thresh']:.6f}:"
        f"offset={measured['target_offset']:.6f}:linear=true:print_format=summary"
    )
    _run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-af", loudnorm,
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
            "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
            "-f", "wav", str(destination),
        ],
        label="FFmpeg deterministic BGM normalization",
    )
    metrics = _measure_loudness(ffmpeg, destination)
    _verify_loudness(metrics)
    return version, metrics


def _wav_info(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frames = audio.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise ValidationError("BGM output is not readable PCM WAV") from exc
    if channels != 2 or sample_width != 2 or sample_rate != 48_000 or frames < 1:
        raise ValidationError("BGM output must be stereo 48 kHz 16-bit PCM")
    return {
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bits": sample_width * 8,
        "frames": frames,
        "duration_seconds": frames / sample_rate,
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        if path.exists() or path.is_symlink():
            raise ValidationError("immutable BGM manifest already exists")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_existing(
    manifest_path: Path,
    *,
    job_id: str,
    lane: str,
    idea_id: str,
    rights_sha: str,
    frozen_sha: str,
    human_approval_sha: str,
    catalog_track_sha: str,
) -> dict[str, Any] | None:
    if not manifest_path.exists():
        return None
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValidationError("existing BGM manifest is not a regular file")
    try:
        artifact = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("existing BGM manifest is unreadable") from exc
    if not isinstance(artifact, dict):
        raise ValidationError("existing BGM manifest must be an object")
    validate_artifact("bgm_manifest", artifact)
    if (
        artifact["job_id"] != job_id
        or artifact["lane_id"] != lane
        or artifact["idea_id"] != idea_id
        or artifact["checksums"]["rights_manifest_sha256"] != rights_sha
        or artifact["checksums"]["frozen_media_manifest_sha256"] != frozen_sha
        or artifact["checksums"]["human_approval_sha256"] != human_approval_sha
        or artifact.get("music_selection", {}).get("track_record_sha256")
        != catalog_track_sha
    ):
        raise ValidationError("immutable BGM output conflicts with requested inputs")
    wav = Path(artifact["immutable_wav_path"]).expanduser().resolve()
    evidence = Path(artifact["rights"]["license_evidence_path"]).expanduser().resolve()
    if (
        not wav.is_file()
        or wav.is_symlink()
        or _sha256_file(wav) != artifact["checksums"]["immutable_wav_sha256"]
        or not evidence.is_file()
        or evidence.is_symlink()
        or _sha256_file(evidence) != artifact["checksums"]["license_evidence_sha256"]
    ):
        raise ValidationError("immutable BGM output or rights evidence changed")
    _wav_info(wav)
    _verify_loudness(artifact["audio"])
    return artifact


def handle_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if task.get("role") != "bgm":
        raise ValidationError("bgm_handler accepts only role='bgm'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "bgm_manifest":
        raise ValidationError("bgm task must require bgm_manifest")
    job_id = _safe_id(task.get("job_id"), "task.job_id")
    lane = _safe_id(payload.get("lane_id"), "payload.lane_id")
    idea_id = _safe_id(payload.get("idea_id"), "payload.idea_id")
    if payload.get("job_id") != job_id or task.get("pod") != lane:
        raise ValidationError("BGM task is not bound to job/lane")

    rights, human_approval, human_approval_sha = _upstream_rights(task)
    frozen = _upstream_artifact(task, "media", "frozen_media_manifest")
    if rights["idea_id"] != idea_id or frozen["idea_id"] != idea_id:
        raise ValidationError("BGM upstream artifacts are not bound to idea_id")
    if frozen["job_id"] != job_id:
        raise ValidationError("BGM frozen media is not bound to job_id")
    decision = rights["decision"]
    if (
        decision["passed"] is not True
        or decision["needs_human_review"] is not False
        or decision["missing_asset_ids"]
    ):
        raise ValidationError("BGM rights manifest has not passed its human gate")
    try:
        verify_frozen_media_manifest(frozen, rights_manifest=rights, expected_job_id=job_id)
    except MediaFreezeError as exc:
        raise ValidationError(f"BGM frozen media verification failed: {exc}") from exc

    selected_rights, selected_frozen, source, catalog_selection = _selected_asset(
        lane_id=lane,
        payload=payload,
        rights_manifest=rights,
        frozen_manifest=frozen,
    )
    source_sha = _sha256_file(source)
    if source_sha != selected_frozen["sha256"]:
        raise ValidationError("BGM source checksum differs from frozen manifest")

    runtime_root = _configured_root(
        "VIDEO_FACTORY_RUNTIME_ROOT", Path.home() / ".video-factory", create=True
    )
    evidence_raw = selected_rights.get("license_receipt")
    if not isinstance(evidence_raw, str) or not evidence_raw.strip():
        raise ValidationError("selected BGM lacks a local license receipt")
    evidence = _under_roots(
        Path(evidence_raw.strip()), _evidence_roots(runtime_root), "BGM license receipt"
    )
    evidence_sha = _sha256_file(evidence)
    output_root = _configured_root(
        "VIDEO_FACTORY_BGM_OUTPUT_ROOT", runtime_root / "bgm", create=True
    )
    job_root = (output_root / job_id).resolve()
    if job_root.parent != output_root:
        raise ValidationError("BGM output escaped configured root")
    job_root.mkdir(parents=True, exist_ok=True)
    manifest_path = job_root / "bgm_manifest.json"
    rights_sha = _artifact_sha256(rights)
    frozen_sha = _artifact_sha256(frozen)
    existing = _load_existing(
        manifest_path,
        job_id=job_id,
        lane=lane,
        idea_id=idea_id,
        rights_sha=rights_sha,
        frozen_sha=frozen_sha,
        human_approval_sha=human_approval_sha,
        catalog_track_sha=catalog_selection["track_record_sha256"],
    )
    if existing is not None:
        return {
            "artifact": existing,
            "output_path": existing["immutable_wav_path"],
            "manifest_path": str(manifest_path.resolve()),
            "bgm_execution": {"reused": True, "network_access": False},
        }

    recipe_hash = digest_text(_RECIPE_VERSION)
    final_wav = job_root / (
        f"{selected_frozen['asset_id']}-{source_sha[:16]}-{recipe_hash[:12]}.wav"
    )
    with tempfile.NamedTemporaryFile(
        prefix=".bgm.", suffix=".wav", dir=job_root, delete=False
    ) as handle:
        temporary_wav = Path(handle.name)
    temporary_wav.unlink(missing_ok=True)
    try:
        ffmpeg_version, loudness = _normalize_wav(source, temporary_wav)
        audio = {**_wav_info(temporary_wav), **loudness}
        if _sha256_file(source) != source_sha or _sha256_file(evidence) != evidence_sha:
            raise ValidationError("BGM source or rights evidence changed during freeze")
        output_sha = _sha256_file(temporary_wav)
        if final_wav.exists():
            if final_wav.is_symlink() or _sha256_file(final_wav) != output_sha:
                raise ValidationError("existing immutable BGM WAV conflicts with output")
            temporary_wav.unlink(missing_ok=True)
        else:
            os.replace(temporary_wav, final_wav)
        artifact = {
            "schema_version": "1.2.0",
            "job_id": job_id,
            "idea_id": idea_id,
            "lane_id": lane,
            "bgm_asset_id": selected_frozen["asset_id"],
            "immutable_wav_path": str(final_wav.resolve()),
            "checksums": {
                "rights_manifest_sha256": rights_sha,
                "frozen_media_manifest_sha256": frozen_sha,
                "source_asset_sha256": source_sha,
                "immutable_wav_sha256": output_sha,
                "license_evidence_sha256": evidence_sha,
                "human_approval_sha256": human_approval_sha,
                "catalog_track_record_sha256": catalog_selection[
                    "track_record_sha256"
                ],
                "catalog_human_approval_sha256": catalog_selection[
                    "human_approval_sha256"
                ],
            },
            "music_selection": {
                "catalog_id": catalog_selection["catalog_id"],
                "catalog_version": catalog_selection["catalog_version"],
                "track_id": catalog_selection["track_id"],
                "track_record_sha256": catalog_selection["track_record_sha256"],
                "archetype_id": catalog_selection["archetype_id"],
                "slot_id": catalog_selection["slot_id"],
                "reference_fingerprint_ids": catalog_selection[
                    "reference_fingerprint_ids"
                ],
                "requested_platforms": catalog_selection["requested_platforms"],
                "requested_territories": catalog_selection["requested_territories"],
                "requested_placements": catalog_selection["requested_placements"],
                "catalog_human_approval_sha256": catalog_selection[
                    "human_approval_sha256"
                ],
            },
            "audio": audio,
            "normalization": {
                "engine": "ffmpeg",
                "ffmpeg_version": ffmpeg_version,
                "recipe_version": _RECIPE_VERSION,
                "integrated_loudness_target_lufs": -14,
                "true_peak_target_dbtp": -1.5,
                "lra_target_lu": 7,
                "deterministic": True,
            },
            "rights": {
                "creator": selected_rights["creator"],
                "license": selected_rights["license"],
                "license_source": catalog_selection["license_source"],
                "license_url": selected_rights["license_url"],
                "license_evidence_path": str(evidence),
                "commercial_use": True,
                "modification_allowed": True,
                "attribution_required": selected_rights["attribution_required"],
                "attribution_text": selected_rights.get("attribution_text"),
                "platforms": list(selected_rights["platforms"]),
                "placements": list(catalog_selection["placements"]),
                "territories": list(selected_rights.get("territories", [])),
                "expires_at": selected_rights.get("expires_at"),
                "rights_status": "approved",
                "human_rights_gate_preserved": True,
                "human_approval": human_approval,
            },
            "created_at": _utc_now(),
        }
        validate_artifact("bgm_manifest", artifact)
        _atomic_json(manifest_path, artifact)
        return {
            "artifact": artifact,
            "output_path": str(final_wav.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "bgm_execution": {
                "reused": False,
                "network_access": False,
                "source_asset_id": selected_frozen["asset_id"],
            },
        }
    finally:
        temporary_wav.unlink(missing_ok=True)


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    try:
        task = json.load(source)
        if not isinstance(task, dict):
            raise ValidationError("handler stdin must contain one JSON object")
        result = handle_task(task)
    except (FactoryError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"bgm_handler_error:{type(exc).__name__}:{exc}\n")
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
