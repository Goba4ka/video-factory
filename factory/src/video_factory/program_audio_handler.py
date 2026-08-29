"""Create one checksum-bound FFmpeg program mix from authoritative speech + BGM.

The voice/SourceAudio artifact remains authoritative for spoken content and
timing.  Only the music leg is dynamically ducked; the mixed WAV is normalized
to the factory loudness target and is the sole audio file consumed by the
HyperFrames compiler.
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
from .media_tools import resolve_media_binary
from .source_audio import (
    is_multisource_manifest,
    source_audio_duration,
    source_audio_is_publishable,
    verify_multisource_program,
)
from .validators import canonical_json, digest_text, require_nonempty_string


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CHUNK_BYTES = 1024 * 1024
_RECIPE_VERSION = "program-mix-1.0.0"
_TARGET_LUFS = -15.0
_TARGET_LRA = 7.0
_TARGET_TP = -1.0
_LOUDNESS_TOLERANCE = 0.5
_BGM_TARGET_LUFS = -14.0
_BGM_TARGET_TP = -1.5
_BGM_METRIC_TOLERANCE = 0.1
_PREMIX_FILTER_TEMPLATE = (
    "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
    "apad=pad_dur={duration},atrim=duration={duration},asetpts=PTS-STARTPTS,"
    "asplit=2[voice][sidechain];"
    "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
    "atrim=duration={duration},asetpts=PTS-STARTPTS,volume=0.35481339[bed];"
    "[bed][sidechain]sidechaincompress=threshold=0.02:ratio=10:attack=15:"
    "release=350:makeup=1[ducked];"
    "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0,"
    "alimiter=limit=0.89125094:attack=5:release=50[premix]"
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_id(value: Any, field: str) -> str:
    normalized = require_nonempty_string(value, field)
    if not _SAFE_ID.fullmatch(normalized) or ".." in normalized:
        raise ValidationError(f"{field} contains unsafe path characters")
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValidationError(f"cannot hash program audio file {path}: {exc}") from exc
    return digest.hexdigest()


def _artifact_sha256(value: Mapping[str, Any]) -> str:
    return digest_text(canonical_json(dict(value)))


def _configured_root(name: str, default: Path) -> Path:
    raw = Path(os.environ.get(name, str(default))).expanduser()
    if raw.is_symlink():
        raise ValidationError(f"{name} must not be a symlink")
    root = raw.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValidationError(f"{name} must point to a directory")
    return root


def _local_wav(value: Any, field: str) -> Path:
    text = require_nonempty_string(value, field)
    raw = Path(text).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise ValidationError(f"{field} must be an absolute regular WAV")
    path = raw.resolve()
    if not path.is_file() or path.suffix.lower() != ".wav":
        raise ValidationError(f"{field} must be an existing regular WAV")
    return path


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
            f"audio_mix task requires exactly one upstream {contract} from role={role!r}"
        )
    validate_artifact(contract, matches[0])
    return matches[0]


def _authoritative_audio(
    task: Mapping[str, Any], *, job_id: str, lane: str, duration: float
) -> tuple[str, dict[str, Any], Path, str, bool]:
    expected_role = "source_audio" if lane == "motivation" else "voice"
    forbidden_role = "voice" if expected_role == "source_audio" else "source_audio"
    upstream = task.get("upstream_results")
    if not isinstance(upstream, list):
        raise ValidationError("task.upstream_results must be an array")
    if any(
        isinstance(entry, Mapping) and entry.get("role") == forbidden_role
        for entry in upstream
    ):
        raise ValidationError("audio_mix received a forbidden cross-lane speech artifact")
    contract = (
        "source_audio_manifest" if expected_role == "source_audio" else "voice_manifest"
    )
    artifact = _upstream_artifact(task, expected_role, contract)
    if artifact["job_id"] != job_id:
        raise ValidationError("authoritative speech is not bound to audio_mix job")
    if contract == "source_audio_manifest":
        if artifact["lane"] != "motivation" or artifact["tts"] is not False:
            raise ValidationError("motivation speech must remain original SourceAudio")
        if not source_audio_is_publishable(artifact):
            raise ValidationError("motivation SourceAudio is not production-cleared")
        source = (
            verify_multisource_program(artifact)
            if is_multisource_manifest(artifact)
            else _local_wav(
                artifact["extracted_audio_path"],
                "source_audio_manifest.extracted_audio_path",
            )
        )
        expected_sha = artifact["checksums"]["extracted_audio_sha256"]
        source_duration = source_audio_duration(artifact)
        if abs(source_duration - duration) > 0.25:
            raise ValidationError("SourceAudio duration does not match ShotList")
        tts = False
    else:
        if artifact["video_id"] != job_id:
            raise ValidationError("voice manifest video_id is not bound to audio_mix job")
        if artifact["voice_rights_status"] not in {
            "approved_owned_voice", "approved_licensed_voice"
        }:
            raise ValidationError("voice is not rights-approved")
        source = _local_wav(
            artifact["immutable_output_path"], "voice_manifest.immutable_output_path"
        )
        expected_sha = artifact["output_sha256"]
        tolerance = max(0.5, duration * 0.03)
        if abs(float(artifact["audio"]["duration_seconds"]) - duration) > tolerance:
            raise ValidationError("voice duration does not match ShotList")
        if source.stat().st_size != artifact["output_bytes"]:
            raise ValidationError("voice output_bytes differs from actual WAV")
        tts = True
    if _sha256_file(source) != expected_sha:
        raise ValidationError("authoritative speech checksum differs from actual WAV")
    return contract, artifact, source, expected_sha, tts


def _timeout() -> float:
    raw = os.environ.get("VIDEO_FACTORY_AUDIO_MIX_TIMEOUT_SECONDS", "1200")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValidationError("VIDEO_FACTORY_AUDIO_MIX_TIMEOUT_SECONDS must be numeric") from exc
    if not math.isfinite(value) or not 0 < value <= 7200:
        raise ValidationError(
            "VIDEO_FACTORY_AUDIO_MIX_TIMEOUT_SECONDS must be from 0 to 7200"
        )
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
        detail = completed.stderr.strip().replace("\r", " ").replace("\n", " ")[-1600:]
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
        raise ValidationError("FFmpeg loudnorm returned no JSON measurements")
    try:
        raw = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValidationError("FFmpeg loudnorm JSON is invalid") from exc
    fields = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    parsed: dict[str, float] = {}
    for field in fields:
        try:
            value = float(raw[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"FFmpeg loudnorm lacks finite {field}") from exc
        if not math.isfinite(value):
            raise ValidationError(f"FFmpeg loudnorm returned non-finite {field}")
        parsed[field] = value
    return parsed


def _loudness_measure(ffmpeg: str, source: Path) -> dict[str, float]:
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
        "-af", "loudnorm=I=-15:LRA=7:TP=-1:print_format=json",
        "-f", "null", "-",
    ]
    completed = _run(command, label="FFmpeg loudness measurement")
    values = _loudnorm_json(completed.stderr)
    return {
        "integrated_loudness_lufs": values["input_i"],
        "loudness_range_lu": values["input_lra"],
        "true_peak_dbtp": values["input_tp"],
    }


def _mix_program_wav(
    *, voice: Path, bgm: Path, duration: float, destination: Path
) -> tuple[str, str, dict[str, float]]:
    ffmpeg = resolve_media_binary("ffmpeg")
    version = _ffmpeg_version(ffmpeg)
    duration_text = f"{duration:.6f}"
    filtergraph = _PREMIX_FILTER_TEMPLATE.format(duration=duration_text)
    with tempfile.NamedTemporaryFile(
        prefix=".premix.", suffix=".wav", dir=destination.parent, delete=False
    ) as handle:
        premix = Path(handle.name)
    premix.unlink(missing_ok=True)
    try:
        _run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(voice), "-stream_loop", "-1", "-i", str(bgm),
                "-filter_complex", filtergraph, "-map", "[premix]",
                "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
                "-f", "wav", str(premix),
            ],
            label="FFmpeg sidechain premix",
        )
        first_pass = _run(
            [
                ffmpeg, "-hide_banner", "-nostdin", "-i", str(premix),
                "-af", "loudnorm=I=-15:LRA=7:TP=-1:print_format=json",
                "-f", "null", "-",
            ],
            label="FFmpeg loudness analysis",
        )
        measured = _loudnorm_json(first_pass.stderr)
        loudnorm = (
            "loudnorm=I=-15:LRA=7:TP=-1:"
            f"measured_I={measured['input_i']:.6f}:"
            f"measured_LRA={measured['input_lra']:.6f}:"
            f"measured_TP={measured['input_tp']:.6f}:"
            f"measured_thresh={measured['input_thresh']:.6f}:"
            f"offset={measured['target_offset']:.6f}:linear=true:print_format=summary"
        )
        _run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(premix), "-af", loudnorm,
                "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
                "-f", "wav", str(destination),
            ],
            label="FFmpeg deterministic loudness normalization",
        )
        metrics = _loudness_measure(ffmpeg, destination)
    finally:
        premix.unlink(missing_ok=True)
    return version, filtergraph, metrics


def _wav_info(path: Path, expected_duration: float) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frames = audio.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise ValidationError("program mix is not readable PCM WAV") from exc
    if channels != 2 or sample_width != 2 or sample_rate != 48_000 or frames < 1:
        raise ValidationError("program mix must be stereo 48 kHz 16-bit PCM")
    duration = frames / sample_rate
    if abs(duration - expected_duration) > max(0.1, expected_duration * 0.005):
        raise ValidationError("program mix duration does not match ShotList")
    return {
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bits": sample_width * 8,
        "frames": frames,
        "duration_seconds": duration,
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
            raise ValidationError("immutable program audio manifest already exists")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_metrics(metrics: Mapping[str, float]) -> None:
    if abs(metrics["integrated_loudness_lufs"] - _TARGET_LUFS) > _LOUDNESS_TOLERANCE:
        raise ValidationError("program mix misses integrated loudness target")
    if metrics["true_peak_dbtp"] > -0.9:
        raise ValidationError("program mix exceeds true-peak ceiling")
    if not 0 <= metrics["loudness_range_lu"] <= 20:
        raise ValidationError("program mix loudness range is outside the contract")


def _verify_normalized_bgm(bgm: Mapping[str, Any], source: Path) -> None:
    """Re-measure the frozen bed before mixing; manifest claims alone never pass."""

    ffmpeg = resolve_media_binary("ffmpeg")
    if _ffmpeg_version(ffmpeg) != bgm["normalization"]["ffmpeg_version"]:
        raise ValidationError("BGM normalization FFmpeg version differs from mix runtime")
    actual = _loudness_measure(ffmpeg, source)
    declared = bgm["audio"]
    for field in (
        "integrated_loudness_lufs",
        "loudness_range_lu",
        "true_peak_dbtp",
    ):
        if abs(float(actual[field]) - float(declared[field])) > _BGM_METRIC_TOLERANCE:
            raise ValidationError(f"BGM measured {field} differs from manifest")
    if abs(actual["integrated_loudness_lufs"] - _BGM_TARGET_LUFS) > 0.5:
        raise ValidationError("BGM bytes do not meet the -14 LUFS normalization target")
    if actual["true_peak_dbtp"] > _BGM_TARGET_TP + 0.1:
        raise ValidationError("BGM bytes exceed the -1.5 dBTP normalization ceiling")


def _load_existing(
    path: Path,
    *,
    job_id: str,
    lane: str,
    idea_id: str,
    authority_manifest_sha: str,
    bgm_manifest_sha: str,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValidationError("existing program audio manifest is not a regular file")
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("existing program audio manifest is unreadable") from exc
    if not isinstance(artifact, dict):
        raise ValidationError("existing program audio manifest must be an object")
    validate_artifact("program_audio_manifest", artifact)
    if (
        artifact["job_id"] != job_id
        or artifact["lane_id"] != lane
        or artifact["idea_id"] != idea_id
        or artifact["source_authority"]["manifest_sha256"] != authority_manifest_sha
        or artifact["bgm"]["manifest_sha256"] != bgm_manifest_sha
    ):
        raise ValidationError("immutable program audio conflicts with requested inputs")
    output = Path(artifact["immutable_output_path"]).expanduser().resolve()
    if (
        output.is_symlink()
        or not output.is_file()
        or output.stat().st_size != artifact["output_bytes"]
        or _sha256_file(output) != artifact["output_sha256"]
    ):
        raise ValidationError("immutable program mix bytes changed")
    _verify_metrics(artifact["audio"])
    return artifact


def handle_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if task.get("role") != "audio_mix":
        raise ValidationError("program_audio_handler accepts only role='audio_mix'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "program_audio_manifest":
        raise ValidationError("audio_mix task must require program_audio_manifest")
    job_id = _safe_id(task.get("job_id"), "task.job_id")
    lane = _safe_id(payload.get("lane_id"), "payload.lane_id")
    idea_id = _safe_id(payload.get("idea_id"), "payload.idea_id")
    if payload.get("job_id") != job_id or task.get("pod") != lane:
        raise ValidationError("audio_mix task is not bound to job/lane")

    shotlist = _upstream_artifact(task, "editor", "shotlist")
    bgm = _upstream_artifact(task, "bgm", "bgm_manifest")
    duration = float(shotlist["duration_seconds"])
    if shotlist["idea_id"] != idea_id:
        raise ValidationError("ShotList is not bound to audio_mix idea_id")
    if (
        bgm["job_id"] != job_id
        or bgm["idea_id"] != idea_id
        or bgm["lane_id"] != lane
        or bgm["rights"]["human_rights_gate_preserved"] is not True
    ):
        raise ValidationError("BGM manifest is not bound to audio_mix job/lane/rights")
    bgm_path = _local_wav(bgm["immutable_wav_path"], "bgm_manifest.immutable_wav_path")
    if _sha256_file(bgm_path) != bgm["checksums"]["immutable_wav_sha256"]:
        raise ValidationError("BGM checksum differs from manifest")
    _verify_normalized_bgm(bgm, bgm_path)
    evidence = Path(bgm["rights"]["license_evidence_path"]).expanduser()
    if (
        not evidence.is_absolute()
        or evidence.is_symlink()
        or not evidence.is_file()
        or _sha256_file(evidence.resolve())
        != bgm["checksums"]["license_evidence_sha256"]
    ):
        raise ValidationError("BGM license evidence checksum differs from manifest")

    contract, authority, voice_path, voice_sha, tts = _authoritative_audio(
        task, job_id=job_id, lane=lane, duration=duration
    )
    authority_sha = _artifact_sha256(authority)
    bgm_sha = _artifact_sha256(bgm)
    runtime_root = _configured_root(
        "VIDEO_FACTORY_RUNTIME_ROOT", Path.home() / ".video-factory"
    )
    output_root = _configured_root(
        "VIDEO_FACTORY_PROGRAM_AUDIO_OUTPUT_ROOT", runtime_root / "program_audio"
    )
    job_root = (output_root / job_id).resolve()
    if job_root.parent != output_root:
        raise ValidationError("program audio output escaped configured root")
    job_root.mkdir(parents=True, exist_ok=True)
    manifest_path = job_root / "program_audio_manifest.json"
    existing = _load_existing(
        manifest_path,
        job_id=job_id,
        lane=lane,
        idea_id=idea_id,
        authority_manifest_sha=authority_sha,
        bgm_manifest_sha=bgm_sha,
    )
    if existing is not None:
        return {
            "artifact": existing,
            "output_path": existing["immutable_output_path"],
            "manifest_path": str(manifest_path.resolve()),
            "audio_mix_execution": {"reused": True, "network_access": False},
        }

    signature = digest_text(
        canonical_json(
            {
                "recipe": _RECIPE_VERSION,
                "job_id": job_id,
                "lane": lane,
                "duration": duration,
                "authority_manifest_sha256": authority_sha,
                "bgm_manifest_sha256": bgm_sha,
            }
        )
    )
    final_wav = job_root / f"program-{signature}.wav"
    with tempfile.NamedTemporaryFile(
        prefix=".program.", suffix=".wav", dir=job_root, delete=False
    ) as handle:
        temporary_wav = Path(handle.name)
    temporary_wav.unlink(missing_ok=True)
    try:
        ffmpeg_version, filtergraph, metrics = _mix_program_wav(
            voice=voice_path,
            bgm=bgm_path,
            duration=duration,
            destination=temporary_wav,
        )
        audio = _wav_info(temporary_wav, duration)
        _verify_metrics(metrics)
        if (
            _sha256_file(voice_path) != voice_sha
            or _sha256_file(bgm_path) != bgm["checksums"]["immutable_wav_sha256"]
            or _sha256_file(evidence.resolve())
            != bgm["checksums"]["license_evidence_sha256"]
        ):
            raise ValidationError("program mix inputs changed during FFmpeg execution")
        output_sha = _sha256_file(temporary_wav)
        if final_wav.exists():
            if final_wav.is_symlink() or _sha256_file(final_wav) != output_sha:
                raise ValidationError("existing immutable program WAV conflicts with output")
            temporary_wav.unlink(missing_ok=True)
        else:
            os.replace(temporary_wav, final_wav)
        artifact = {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "idea_id": idea_id,
            "lane_id": lane,
            "source_authority": {
                "contract": contract,
                "manifest_sha256": authority_sha,
                "audio_sha256": voice_sha,
                "authority": "spoken_content_and_timing",
                "tts": tts,
            },
            "bgm": {
                "asset_id": bgm["bgm_asset_id"],
                "manifest_sha256": bgm_sha,
                "audio_sha256": bgm["checksums"]["immutable_wav_sha256"],
                "license_evidence_sha256": bgm["checksums"]["license_evidence_sha256"],
                "human_approval_sha256": bgm["checksums"]["human_approval_sha256"],
            },
            "mix": {
                "engine": "ffmpeg",
                "ffmpeg_version": ffmpeg_version,
                "recipe_version": _RECIPE_VERSION,
                "filtergraph_sha256": digest_text(filtergraph),
                "loudness_target_lufs": -15,
                "true_peak_max_dbtp": -1,
                "lra_target_lu": 7,
                "mix_profile_id": "speech-forward-audible-bgm-v1",
                "bgm_preduck_gain_db": -9,
                "sidechain_threshold_dbfs": -34,
                "sidechain_ratio": 10,
                "sidechain_attack_ms": 15,
                "sidechain_release_ms": 350,
                "sidechain_ducking": True,
                "broll_audio_muted": True,
                "deterministic": True,
            },
            "immutable_output_path": str(final_wav.resolve()),
            "output_sha256": output_sha,
            "output_bytes": final_wav.stat().st_size,
            "audio": {**audio, **metrics},
            "created_at": _utc_now(),
        }
        validate_artifact("program_audio_manifest", artifact)
        _atomic_json(manifest_path, artifact)
        return {
            "artifact": artifact,
            "output_path": str(final_wav.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "audio_mix_execution": {
                "reused": False,
                "network_access": False,
                "voice_authority_preserved": True,
                "sidechain_ducking": True,
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
        sys.stderr.write(f"program_audio_handler_error:{type(exc).__name__}:{exc}\n")
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
