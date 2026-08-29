from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import validate_artifact
from .errors import FactoryError, ValidationError


FISH_API_BASE = "https://api.fish.audio"
DEFAULT_MODEL = "s2.1-pro"
ALLOWED_MODELS = frozenset({"s1", "s2-pro", "s2.1-pro", "s2.1-pro-free"})
MAX_GENERATIONS_PER_VIDEO = 2
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SECRET_FILENAME = "fish_audio_api_key.dpapi"
RESERVATION_STALE_SECONDS = 1200
VOICE_RIGHTS_STATUSES = frozenset(
    {
        "user_confirmation_required",
        "approved_owned_voice",
        "approved_licensed_voice",
    }
)
RETRY_REASONS = frozenset(
    {
        "pronunciation",
        "pacing",
        "text_error",
        "technical_failure",
        "provider_failure",
    }
)
FISH_STREAMING_WAV_SIZE_PAIRS = frozenset(
    {
        (0xFFFFFF24, 0xFFFFFF00),
        (0xFFFFFFFF, 0xFFFFFFFF),
    }
)


def _default_state_root() -> Path:
    runtime_root = os.environ.get("VIDEO_FACTORY_RUNTIME_ROOT")
    if runtime_root:
        return Path(runtime_root)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "VideoFactory" / "State"
    return Path.home() / ".video-factory" / "state"


DEFAULT_USAGE_DB = os.environ.get(
    "FISH_USAGE_DB", str(_default_state_root() / "fish_audio_usage.sqlite3")
)


class FishAudioError(FactoryError):
    code = "fish_audio_error"

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        if error_code is not None:
            self.code = error_code


class FishAudioAuthError(FishAudioError):
    code = "fish_audio_auth_error"


class FishAudioLimitError(FishAudioError):
    code = "fish_audio_generation_limit"


class FishAudioInFlightError(FishAudioError):
    code = "fish_audio_generation_in_flight"


class FishAudioUnknownOutcomeError(FishAudioError):
    code = "fish_audio_unknown_outcome"


@dataclass(frozen=True)
class FishTTSRequest:
    video_id: str
    text: str
    output_path: Path
    reference_id: str | None = None
    model: str = DEFAULT_MODEL
    speed: float = 1.0
    temperature: float = 0.7
    top_p: float = 0.7
    timeout_seconds: float = 180.0
    voice_rights_status: str = "user_confirmation_required"
    retry_reason: str | None = None
    defect_reference: str | None = None


Transport = Callable[[urllib.request.Request, float], tuple[int, Mapping[str, str], bytes]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_video_id(video_id: str) -> str:
    candidate = video_id.strip()
    if not VIDEO_ID_PATTERN.fullmatch(candidate):
        raise ValidationError(
            "video_id must be 1-128 characters using letters, digits, '.', '_' or '-'"
        )
    return candidate


def _read_user_environment_variable(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except (FileNotFoundError, OSError):
        return None
    return str(value)


def _secret_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise FishAudioAuthError("LOCALAPPDATA is unavailable for the Fish Audio key store")
    return Path(local_app_data) / "VideoFactory" / "Secrets" / SECRET_FILENAME


def _dpapi_transform(value: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise FishAudioAuthError("the local Fish Audio key store requires Windows DPAPI")
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    buffer = ctypes.create_string_buffer(value)
    source = DataBlob(
        len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    destination = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR if protect else ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    function.restype = wintypes.BOOL
    description = "VideoFactory Fish Audio API key" if protect else None
    description_pointer = description if protect else None
    if not function(
        ctypes.byref(source),
        description_pointer,
        None,
        None,
        None,
        0,
        ctypes.byref(destination),
    ):
        raise FishAudioAuthError(
            f"Windows DPAPI operation failed ({ctypes.get_last_error()})"
        )
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)


def store_api_key(api_key: str) -> Path:
    """Protect a key with the current Windows user account and store it off-project."""

    key = api_key.strip()
    if not key:
        raise FishAudioAuthError("Fish Audio API key must not be empty")
    encrypted = _dpapi_transform(key.encode("utf-8"), protect=True)
    path = _secret_path()
    _atomic_write(path, encrypted)
    return path


def _load_dpapi_api_key() -> str | None:
    if os.name != "nt":
        return None
    path = _secret_path()
    if not path.is_file():
        return None
    try:
        return _dpapi_transform(path.read_bytes(), protect=False).decode("utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise FishAudioAuthError("the protected Fish Audio key could not be read") from exc


def _load_file_api_key() -> str | None:
    """Load a container/systemd secret without exposing it in process output."""

    configured = os.environ.get("FISH_API_KEY_FILE")
    if not configured:
        return None
    path = Path(configured).expanduser()
    try:
        if not path.is_file():
            raise FishAudioAuthError("FISH_API_KEY_FILE does not point to a readable file")
        if path.stat().st_size > 16_384:
            raise FishAudioAuthError("FISH_API_KEY_FILE is unexpectedly large")
        value = path.read_text(encoding="utf-8").strip()
    except FishAudioAuthError:
        raise
    except OSError as exc:
        raise FishAudioAuthError("FISH_API_KEY_FILE could not be read") from exc
    if not value:
        raise FishAudioAuthError("FISH_API_KEY_FILE is empty")
    return value


def load_api_key() -> str:
    """Load Fish credentials without ever serializing them into factory output."""

    value = (
        _load_file_api_key()
        or os.environ.get("FISH_API_KEY")
        or _load_dpapi_api_key()
        or _read_user_environment_variable("FISH_API_KEY")
    )
    if not value or not value.strip():
        raise FishAudioAuthError(
            "Fish credentials are not configured via FISH_API_KEY_FILE, "
            "FISH_API_KEY, or the Windows user key store"
        )
    return value.strip()


def _default_transport(
    request: urllib.request.Request, timeout_seconds: float
) -> tuple[int, Mapping[str, str], bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        raise _http_error(exc.code, body) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        raise FishAudioUnknownOutcomeError(
            f"Fish Audio network outcome is unknown: {reason or exc}"
        ) from exc


def _http_error(status: int, body: bytes) -> FishAudioError:
    message = ""
    try:
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("reason") or "")
    except (UnicodeDecodeError, json.JSONDecodeError):
        message = ""
    message = message.strip().replace("\r", " ").replace("\n", " ")[:300]
    suffix = f": {message}" if message else ""
    if status in {401, 403}:
        return FishAudioAuthError(
            f"Fish Audio rejected the credential ({status}){suffix}",
            http_status=status,
        )
    return FishAudioError(
        f"Fish Audio TTS request failed ({status}){suffix}",
        http_status=status,
        error_code=f"fish_audio_http_{status}",
    )


def _validate_request(request: FishTTSRequest) -> FishTTSRequest:
    video_id = _validate_video_id(request.video_id)
    text = request.text.strip()
    if not text:
        raise ValidationError("Fish Audio text must not be empty")
    if len(text.encode("utf-8")) > 1_000_000:
        raise ValidationError("Fish Audio text exceeds the 1,000,000-byte safety ceiling")
    if request.model not in ALLOWED_MODELS:
        raise ValidationError(f"unsupported Fish Audio model: {request.model}")
    if not 0.5 <= request.speed <= 2.0:
        raise ValidationError("Fish Audio speed must be between 0.5 and 2.0")
    if not 0.0 <= request.temperature <= 1.0:
        raise ValidationError("Fish Audio temperature must be between 0 and 1")
    if not 0.0 <= request.top_p <= 1.0:
        raise ValidationError("Fish Audio top_p must be between 0 and 1")
    if not 1.0 <= request.timeout_seconds <= 900.0:
        raise ValidationError("Fish Audio timeout must be between 1 and 900 seconds")
    if request.voice_rights_status not in VOICE_RIGHTS_STATUSES:
        raise ValidationError("unsupported Fish Audio voice rights status")
    retry_reason = request.retry_reason.strip() if request.retry_reason else None
    defect_reference = (
        request.defect_reference.strip() if request.defect_reference else None
    )
    if (retry_reason is None) != (defect_reference is None):
        raise ValidationError(
            "Fish Audio retry_reason and defect_reference must be provided together"
        )
    if retry_reason is not None and retry_reason not in RETRY_REASONS:
        raise ValidationError("unsupported Fish Audio retry reason")
    if defect_reference is not None and not 3 <= len(defect_reference) <= 1024:
        raise ValidationError(
            "Fish Audio defect_reference must contain 3-1024 characters"
        )
    output_path = Path(request.output_path)
    if output_path.suffix.lower() != ".wav":
        raise ValidationError("factory Fish Audio output must use the .wav extension")
    configured_reference_id = (
        request.reference_id
        or os.environ.get("FISH_REFERENCE_ID")
        or _read_user_environment_variable("FISH_REFERENCE_ID")
    )
    reference_id = configured_reference_id.strip() if configured_reference_id else None
    return FishTTSRequest(
        video_id=video_id,
        text=text,
        output_path=output_path,
        reference_id=reference_id or None,
        model=request.model,
        speed=request.speed,
        temperature=request.temperature,
        top_p=request.top_p,
        timeout_seconds=request.timeout_seconds,
        voice_rights_status=request.voice_rights_status,
        retry_reason=retry_reason,
        defect_reference=defect_reference,
    )


def _tts_payload(request: FishTTSRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": request.text,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "prosody": {
            "speed": request.speed,
            "volume": 0,
            "normalize_loudness": True,
        },
        "chunk_length": 300,
        "normalize": True,
        "format": "wav",
        "sample_rate": 44100,
        "latency": "normal",
        "max_new_tokens": 1024,
        "repetition_penalty": 1.2,
        "min_chunk_length": 50,
        "condition_on_previous_chunks": True,
        "early_stop_threshold": 1,
    }
    if request.reference_id:
        payload["reference_id"] = request.reference_id
    return payload


def _request_hash(request: FishTTSRequest, payload: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        _canonical_json({"model": request.model, "payload": dict(payload)})
    )


def _connect_usage_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(40):
        connection = sqlite3.connect(path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
        CREATE TABLE IF NOT EXISTS fish_tts_generations (
            video_id TEXT NOT NULL,
            generation_no INTEGER NOT NULL CHECK (generation_no BETWEEN 1 AND 2),
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('reserved', 'succeeded', 'failed', 'failed_unknown')
            ),
            model TEXT NOT NULL,
            reference_id TEXT,
            output_path TEXT NOT NULL,
            text_bytes INTEGER NOT NULL CHECK (text_bytes > 0),
            text_sha256 TEXT NOT NULL,
            estimated_cost_usd REAL NOT NULL CHECK (estimated_cost_usd >= 0),
            created_at TEXT NOT NULL,
            completed_at TEXT,
            http_status INTEGER,
            error_code TEXT,
            output_sha256 TEXT,
            output_bytes INTEGER,
            duration_seconds REAL,
            retry_reason TEXT,
            defect_reference TEXT,
            defect_sha256 TEXT,
            retry_of_request_hash TEXT,
            retry_of_output_sha256 TEXT,
            retry_of_generation_status TEXT,
            PRIMARY KEY (video_id, generation_no)
        );
        CREATE INDEX IF NOT EXISTS idx_fish_tts_request_hash
            ON fish_tts_generations(video_id, request_hash, status);
        """
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(fish_tts_generations)"
                )
            }
            if "text_sha256" not in columns:
                connection.execute(
                    "ALTER TABLE fish_tts_generations "
                    "ADD COLUMN text_sha256 TEXT NOT NULL DEFAULT ''"
                )
                connection.commit()
            if "retry_reason" not in columns:
                connection.execute(
                    "ALTER TABLE fish_tts_generations ADD COLUMN retry_reason TEXT"
                )
            if "defect_reference" not in columns:
                connection.execute(
                    "ALTER TABLE fish_tts_generations ADD COLUMN defect_reference TEXT"
                )
            for column in (
                "defect_sha256",
                "retry_of_request_hash",
                "retry_of_output_sha256",
                "retry_of_generation_status",
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE fish_tts_generations ADD COLUMN {column} TEXT"
                    )
            connection.commit()
            return connection
        except sqlite3.OperationalError as exc:
            connection.close()
            if "locked" not in str(exc).lower() or attempt == 39:
                raise
            time.sleep(0.025 * (attempt + 1))
    raise AssertionError("unreachable Fish Audio database initialization state")


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _validated_retry_defect(
    request: FishTTSRequest, first_generation: Mapping[str, Any]
) -> dict[str, Any]:
    if request.retry_reason is None or request.defect_reference is None:
        raise ValidationError(
            "Fish Audio generation 2 requires retry_reason and defect_reference"
        )
    defect_path = Path(request.defect_reference).expanduser().resolve()
    if not defect_path.is_file():
        raise ValidationError(
            f"Fish Audio defect_reference is not a readable QA artifact: {defect_path}"
        )
    try:
        defect_bytes = defect_path.read_bytes()
        payload = json.loads(defect_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            f"Fish Audio defect_reference is not valid UTF-8 JSON: {defect_path}"
        ) from exc
    validate_artifact("voice_defect", payload)
    expected = {
        "job_id": request.video_id,
        "video_id": request.video_id,
        "generation_no": 1,
        "generation_status": first_generation["status"],
        "request_hash": first_generation["request_hash"],
        "output_sha256": first_generation["output_sha256"],
        "retry_reason": request.retry_reason,
    }
    mismatches = [key for key, value in expected.items() if payload.get(key) != value]
    if mismatches:
        raise ValidationError(
            "Fish Audio defect artifact does not match generation 1: "
            + ", ".join(mismatches)
        )
    return {
        "defect_reference": str(defect_path),
        "defect_sha256": _sha256_bytes(defect_bytes),
        "retry_of_request_hash": payload["request_hash"],
        "retry_of_output_sha256": payload["output_sha256"],
        "retry_of_generation_status": payload["generation_status"],
    }


def _reserve_generation(
    usage_db: Path,
    request: FishTTSRequest,
    request_hash: str,
    text_bytes: int,
) -> tuple[str, dict[str, Any]]:
    connection = _connect_usage_db(usage_db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stale_before = datetime.now(timezone.utc).timestamp() - RESERVATION_STALE_SECONDS
        rows = connection.execute(
            """
            SELECT * FROM fish_tts_generations
            WHERE video_id = ? ORDER BY generation_no
            """,
            (request.video_id,),
        ).fetchall()
        for row in rows:
            if row["status"] == "reserved":
                try:
                    created_at = datetime.fromisoformat(row["created_at"]).timestamp()
                except (TypeError, ValueError):
                    created_at = 0.0
                if created_at <= stale_before:
                    connection.execute(
                        """
                        UPDATE fish_tts_generations
                        SET status = 'failed_unknown', completed_at = ?,
                            error_code = 'stale_reservation'
                        WHERE video_id = ? AND generation_no = ? AND status = 'reserved'
                        """,
                        (_utc_now(), request.video_id, row["generation_no"]),
                    )
        rows = connection.execute(
            """
            SELECT * FROM fish_tts_generations
            WHERE video_id = ? ORDER BY generation_no
            """,
            (request.video_id,),
        ).fetchall()
        if any(row["status"] == "reserved" for row in rows):
            raise FishAudioInFlightError(
                f"another Fish Audio generation is still reserved for {request.video_id}"
            )
        retry_evidence: dict[str, Any] | None = None
        if request.retry_reason is not None:
            if not rows:
                raise ValidationError(
                    "Fish Audio retry metadata is only valid for generation 2"
                )
            retry_evidence = _validated_retry_defect(request, rows[0])
        rows_to_search = list(reversed(rows))
        for row in rows_to_search:
            if row["request_hash"] != request_hash:
                continue
            if request.retry_reason is not None and row["generation_no"] != 2:
                continue
            if request.retry_reason is not None and (
                row["retry_reason"] != request.retry_reason
                or row["defect_reference"] != retry_evidence["defect_reference"]
            ):
                continue
            if request.retry_reason is not None and any(
                row[key] != retry_evidence[key]
                for key in (
                    "defect_sha256",
                    "retry_of_request_hash",
                    "retry_of_output_sha256",
                    "retry_of_generation_status",
                )
            ):
                raise FishAudioError(
                    "Fish Audio retry evidence changed after generation 2 was reserved"
                )
            if row["status"] == "succeeded" and Path(row["output_path"]).is_file():
                connection.commit()
                return "reuse", _row_dict(row)
        if len(rows) >= MAX_GENERATIONS_PER_VIDEO:
            raise FishAudioLimitError(
                f"Fish Audio hard limit reached for {request.video_id}: "
                f"{MAX_GENERATIONS_PER_VIDEO}/{MAX_GENERATIONS_PER_VIDEO} generations used"
            )
        generation_no = len(rows) + 1
        if generation_no == 2 and retry_evidence is None:
            raise ValidationError(
                "Fish Audio generation 2 requires retry_reason and defect_reference"
            )
        estimated_cost = 0.0 if request.model == "s2.1-pro-free" else text_bytes * 15 / 1_000_000
        created_at = _utc_now()
        immutable_output = (
            request.output_path.parent
            / ".fish_audio"
            / request.video_id
            / f"v{generation_no:02d}-{request_hash[:12]}.wav"
        ).resolve()
        text_sha256 = _sha256_bytes(request.text.encode("utf-8"))
        connection.execute(
            """
            INSERT INTO fish_tts_generations(
                video_id, generation_no, request_hash, status, model, reference_id,
                output_path, text_bytes, text_sha256, estimated_cost_usd, created_at
                , retry_reason, defect_reference, defect_sha256,
                retry_of_request_hash, retry_of_output_sha256,
                retry_of_generation_status
            ) VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.video_id,
                generation_no,
                request_hash,
                request.model,
                request.reference_id,
                str(immutable_output),
                text_bytes,
                text_sha256,
                estimated_cost,
                created_at,
                request.retry_reason,
                retry_evidence["defect_reference"] if retry_evidence else None,
                retry_evidence["defect_sha256"] if retry_evidence else None,
                retry_evidence["retry_of_request_hash"] if retry_evidence else None,
                retry_evidence["retry_of_output_sha256"] if retry_evidence else None,
                retry_evidence["retry_of_generation_status"] if retry_evidence else None,
            ),
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT * FROM fish_tts_generations
            WHERE video_id = ? AND generation_no = ?
            """,
            (request.video_id, generation_no),
        ).fetchone()
        assert row is not None
        return "generate", _row_dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _complete_generation(
    usage_db: Path,
    video_id: str,
    generation_no: int,
    *,
    status: str,
    http_status: int | None = None,
    error_code: str | None = None,
    output_sha256: str | None = None,
    output_bytes: int | None = None,
    duration_seconds: float | None = None,
    require_reserved: bool = True,
) -> None:
    connection = _connect_usage_db(usage_db)
    try:
        cursor = connection.execute(
            """
            UPDATE fish_tts_generations
            SET status = ?, completed_at = ?, http_status = ?, error_code = ?,
                output_sha256 = ?, output_bytes = ?, duration_seconds = ?
            WHERE video_id = ? AND generation_no = ? AND status = 'reserved'
            """,
            (
                status,
                _utc_now(),
                http_status,
                error_code,
                output_sha256,
                output_bytes,
                duration_seconds,
                video_id,
                generation_no,
            ),
        )
        if require_reserved and cursor.rowcount != 1:
            connection.rollback()
            raise FishAudioUnknownOutcomeError(
                "Fish Audio reservation ownership was lost before completion"
            )
        connection.commit()
    finally:
        connection.close()


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _inspect_wav(path: Path) -> dict[str, Any]:
    try:
        file_size = path.stat().st_size
        with path.open("rb") as source:
            header = source.read(12)
            if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
                raise FishAudioError("Fish Audio output is not a RIFF/WAVE file")
            riff_declared_size = struct.unpack("<I", header[4:8])[0]
            streaming_riff = any(
                riff_declared_size == pair[0] for pair in FISH_STREAMING_WAV_SIZE_PAIRS
            )
            if not streaming_riff and riff_declared_size + 8 > file_size:
                raise FishAudioError("Fish Audio WAV has a truncated RIFF container")
            offset = 12
            fmt: tuple[int, int, int, int, int, int] | None = None
            data_bytes: int | None = None
            while offset + 8 <= file_size:
                source.seek(offset)
                chunk_header = source.read(8)
                if len(chunk_header) != 8:
                    break
                chunk_id = chunk_header[:4]
                declared_size = struct.unpack("<I", chunk_header[4:])[0]
                chunk_start = offset + 8
                remaining = max(0, file_size - chunk_start)
                truncated = declared_size > remaining
                streaming_data = (
                    chunk_id == b"data"
                    and (riff_declared_size, declared_size)
                    in FISH_STREAMING_WAV_SIZE_PAIRS
                )
                if truncated and not streaming_data:
                    raise FishAudioError(
                        f"Fish Audio WAV has a truncated {chunk_id!r} chunk"
                    )
                actual_size = remaining if streaming_data else declared_size
                if chunk_id == b"fmt ":
                    value = source.read(min(actual_size, 40))
                    if len(value) < 16:
                        raise FishAudioError("Fish Audio WAV has a truncated fmt chunk")
                    fmt = struct.unpack("<HHIIHH", value[:16])
                elif chunk_id == b"data":
                    data_bytes = actual_size
                    break
                if streaming_data:
                    break
                offset = chunk_start + declared_size + (declared_size % 2)
    except OSError as exc:
        raise FishAudioError("Fish Audio output is not a decodable WAV file") from exc
    if fmt is None or data_bytes is None:
        raise FishAudioError("Fish Audio WAV is missing fmt or data chunks")
    audio_format, channels, rate, byte_rate, block_align, bits_per_sample = fmt
    if (
        audio_format != 1
        or rate != 44100
        or channels != 1
        or bits_per_sample != 16
        or block_align != 2
        or byte_rate != 88200
        or data_bytes <= 0
        or data_bytes % block_align != 0
    ):
        raise FishAudioError(
            "Fish Audio WAV must be non-empty 44.1 kHz, 16-bit, mono PCM"
        )
    frames = data_bytes // block_align
    duration_seconds = frames / rate
    if duration_seconds < 0.01:
        raise FishAudioError("Fish Audio WAV must be at least 0.01 seconds long")
    return {
        "sample_rate_hz": rate,
        "channels": channels,
        "sample_width_bits": bits_per_sample,
        "frames": frames,
        "duration_seconds": duration_seconds,
    }


def _cache_is_valid(row: Mapping[str, Any]) -> bool:
    path = Path(str(row["output_path"]))
    expected_hash = row.get("output_sha256")
    if not path.is_file() or not expected_hash:
        return False
    try:
        value = path.read_bytes()
        if _sha256_bytes(value) != expected_hash:
            return False
        _inspect_wav(path)
    except (OSError, FishAudioError):
        return False
    return True


def _invalidate_cached_generation(
    usage_db: Path, video_id: str, generation_no: int
) -> None:
    connection = _connect_usage_db(usage_db)
    try:
        connection.execute(
            """
            UPDATE fish_tts_generations
            SET status = 'failed_unknown', completed_at = ?, error_code = 'cache_invalid'
            WHERE video_id = ? AND generation_no = ? AND status = 'succeeded'
            """,
            (_utc_now(), video_id, generation_no),
        )
        connection.commit()
    finally:
        connection.close()


def _refresh_cached_metadata(
    usage_db: Path,
    row: Mapping[str, Any],
    audio: Mapping[str, Any],
) -> None:
    connection = _connect_usage_db(usage_db)
    try:
        connection.execute(
            """
            UPDATE fish_tts_generations
            SET duration_seconds = ?, output_bytes = ?, output_sha256 = ?
            WHERE video_id = ? AND generation_no = ? AND status = 'succeeded'
            """,
            (
                audio["duration_seconds"],
                Path(str(row["output_path"])).stat().st_size,
                _sha256_bytes(Path(str(row["output_path"])).read_bytes()),
                row["video_id"],
                row["generation_no"],
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _public_result(
    row: Mapping[str, Any], *, reused: bool, active_output_path: Path, used_count: int
) -> dict[str, Any]:
    return {
        "ok": True,
        "provider": "fish_audio",
        "video_id": row["video_id"],
        "generation_no": row["generation_no"],
        "generation_limit": MAX_GENERATIONS_PER_VIDEO,
        "remaining_generations": max(0, MAX_GENERATIONS_PER_VIDEO - used_count),
        "reused": reused,
        "status": row["status"],
        "model": row["model"],
        "reference_id": row["reference_id"],
        "output_path": str(active_output_path.resolve()),
        "immutable_output_path": row["output_path"],
        "output_sha256": row["output_sha256"],
        "output_bytes": row["output_bytes"],
        "duration_seconds": row["duration_seconds"],
        "text_bytes": row["text_bytes"],
        "estimated_cost_usd": row["estimated_cost_usd"],
        "retry_reason": row["retry_reason"],
        "defect_reference": row["defect_reference"],
        "defect_sha256": row["defect_sha256"],
        "retry_of_request_hash": row["retry_of_request_hash"],
        "retry_of_output_sha256": row["retry_of_output_sha256"],
        "retry_of_generation_status": row["retry_of_generation_status"],
    }


def _voice_manifest_bytes(
    active_output_path: Path,
    row: Mapping[str, Any],
    audio: Mapping[str, Any],
    voice_rights_status: str,
) -> tuple[Path, bytes]:
    manifest_path = active_output_path.with_suffix(".voice.json")
    retry_evidence: dict[str, Any] = {
        "defect_sha256": None,
        "retry_of_request_hash": None,
        "retry_of_output_sha256": None,
        "retry_of_generation_status": None,
    }
    if row["generation_no"] == 2:
        defect_path = Path(str(row["defect_reference"]))
        try:
            defect_bytes = defect_path.read_bytes()
            defect = json.loads(defect_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FishAudioError(
                "Fish Audio generation 2 has unreadable retry evidence"
            ) from exc
        validate_artifact("voice_defect", defect)
        actual_evidence = {
            "defect_sha256": _sha256_bytes(defect_bytes),
            "retry_of_request_hash": defect["request_hash"],
            "retry_of_output_sha256": defect["output_sha256"],
            "retry_of_generation_status": defect["generation_status"],
        }
        if any(actual_evidence[key] != row[key] for key in actual_evidence):
            raise FishAudioError(
                "Fish Audio retry evidence changed after generation 2 was reserved"
            )
        retry_evidence = {key: row[key] for key in actual_evidence}
    manifest = {
        "schema_version": "1.0.0",
        "provider": "fish_audio",
        "job_id": row["video_id"],
        "video_id": row["video_id"],
        "generation_no": row["generation_no"],
        "generation_limit": MAX_GENERATIONS_PER_VIDEO,
        "request_hash": row["request_hash"],
        "text_sha256": row["text_sha256"],
        "text_bytes": row["text_bytes"],
        "model": row["model"],
        "reference_id": row["reference_id"],
        "voice_rights_status": voice_rights_status,
        "immutable_output_path": row["output_path"],
        "output_sha256": row["output_sha256"],
        "output_bytes": row["output_bytes"],
        "audio": dict(audio),
        "render_target_sample_rate_hz": 48000,
        "estimated_cost_usd": row["estimated_cost_usd"],
        "retry_reason": row["retry_reason"],
        "defect_reference": row["defect_reference"],
        **retry_evidence,
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }
    return manifest_path, _canonical_json(manifest) + b"\n"


def _promote_voice_pair(
    active_output_path: Path,
    audio_bytes: bytes,
    row: Mapping[str, Any],
    audio: Mapping[str, Any],
    voice_rights_status: str,
) -> Path:
    """Promote matched audio/manifest bytes only after all evidence is validated."""

    manifest_path, manifest_bytes = _voice_manifest_bytes(
        active_output_path, row, audio, voice_rights_status
    )
    immutable_path = Path(str(row["output_path"]))
    promote_audio = immutable_path.resolve() != active_output_path.resolve()
    old_audio = (
        active_output_path.read_bytes()
        if promote_audio and active_output_path.is_file()
        else None
    )
    old_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
    try:
        if promote_audio:
            _atomic_write(active_output_path, audio_bytes)
        _atomic_write(manifest_path, manifest_bytes)
    except Exception:
        try:
            if promote_audio:
                if old_audio is None:
                    active_output_path.unlink(missing_ok=True)
                else:
                    _atomic_write(active_output_path, old_audio)
            if old_manifest is None:
                manifest_path.unlink(missing_ok=True)
            else:
                _atomic_write(manifest_path, old_manifest)
        except OSError as rollback_error:
            raise FishAudioError(
                "Fish Audio active voice pair promotion and rollback both failed"
            ) from rollback_error
        raise
    return manifest_path


def generate_tts(
    request: FishTTSRequest,
    *,
    usage_db: str | Path = DEFAULT_USAGE_DB,
    api_key: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    request = _validate_request(request)
    key = api_key or load_api_key()
    payload = _tts_payload(request)
    text_bytes = len(request.text.encode("utf-8"))
    request_hash = _request_hash(request, payload)
    usage_path = Path(usage_db)
    action, row = _reserve_generation(
        usage_path, request, request_hash, text_bytes
    )
    if action == "reuse":
        if _cache_is_valid(row):
            immutable = Path(row["output_path"])
            audio_info = _inspect_wav(immutable)
            _refresh_cached_metadata(usage_path, row, audio_info)
            status = usage_status(request.video_id, usage_db=usage_path)
            row = next(
                item
                for item in status["generations"]
                if item["generation_no"] == row["generation_no"]
            )
            manifest_path = _promote_voice_pair(
                request.output_path,
                immutable.read_bytes(),
                row,
                audio_info,
                request.voice_rights_status,
            )
            public = _public_result(
                row,
                reused=True,
                active_output_path=request.output_path,
                used_count=len(status["generations"]),
            )
            public["voice_manifest_path"] = str(manifest_path.resolve())
            return public
        _invalidate_cached_generation(
            usage_path, request.video_id, int(row["generation_no"])
        )
        action, row = _reserve_generation(
            usage_path, request, request_hash, text_bytes
        )
        if action == "reuse":
            raise FishAudioError("Fish Audio cache reconciliation failed closed")

    generation_no = int(row["generation_no"])
    immutable_output_path = Path(row["output_path"])
    body = _canonical_json(payload)
    http_request = urllib.request.Request(
        f"{FISH_API_BASE}/v1/tts",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "audio/wav, application/octet-stream",
            "model": request.model,
            "User-Agent": "video-factory-control/0.5",
        },
    )
    sender = transport or _default_transport
    try:
        status, _headers, audio = sender(http_request, request.timeout_seconds)
        if status != 200:
            raise _http_error(status, audio)
        if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise FishAudioError("Fish Audio returned an invalid WAV payload")
        _atomic_write(immutable_output_path, audio)
        output_hash = _sha256_bytes(audio)
        audio_info = _inspect_wav(immutable_output_path)
        _complete_generation(
            usage_path,
            request.video_id,
            generation_no,
            status="succeeded",
            http_status=status,
            output_sha256=output_hash,
            output_bytes=len(audio),
            duration_seconds=audio_info["duration_seconds"],
        )
    except FishAudioUnknownOutcomeError as exc:
        _complete_generation(
            usage_path,
            request.video_id,
            generation_no,
            status="failed_unknown",
            error_code=exc.code,
            require_reserved=False,
        )
        raise
    except FishAudioError as exc:
        _complete_generation(
            usage_path,
            request.video_id,
            generation_no,
            status="failed",
            http_status=exc.http_status,
            error_code=exc.code,
            require_reserved=False,
        )
        raise
    except Exception as exc:
        _complete_generation(
            usage_path,
            request.video_id,
            generation_no,
            status="failed_unknown",
            error_code=type(exc).__name__,
            require_reserved=False,
        )
        raise FishAudioError(
            f"Fish Audio generation outcome is unknown: {type(exc).__name__}"
        ) from exc

    result = usage_status(request.video_id, usage_db=usage_path)
    generation = result["generations"][-1]
    manifest_path = _promote_voice_pair(
        request.output_path,
        audio,
        generation,
        audio_info,
        request.voice_rights_status,
    )
    public = _public_result(
        generation,
        reused=False,
        active_output_path=request.output_path,
        used_count=len(result["generations"]),
    )
    public["voice_manifest_path"] = str(manifest_path.resolve())
    return public


def usage_status(
    video_id: str | None = None, *, usage_db: str | Path = DEFAULT_USAGE_DB
) -> dict[str, Any]:
    usage_path = Path(usage_db)
    connection = _connect_usage_db(usage_path)
    try:
        if video_id is None:
            rows = connection.execute(
                "SELECT * FROM fish_tts_generations ORDER BY created_at, video_id, generation_no"
            ).fetchall()
        else:
            checked = _validate_video_id(video_id)
            rows = connection.execute(
                """
                SELECT * FROM fish_tts_generations
                WHERE video_id = ? ORDER BY generation_no
                """,
                (checked,),
            ).fetchall()
    finally:
        connection.close()
    generations = [_row_dict(row) for row in rows]
    used = len(generations) if video_id is not None else None
    return {
        "ok": True,
        "provider": "fish_audio",
        "video_id": video_id,
        "generation_limit_per_video": MAX_GENERATIONS_PER_VIDEO,
        "used": used,
        "remaining": MAX_GENERATIONS_PER_VIDEO - used if used is not None else None,
        "generations": generations,
    }


def list_owned_voices(
    *,
    page_size: int = 100,
    timeout_seconds: float = 30.0,
    api_key: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    if not 1 <= page_size <= 100:
        raise ValidationError("Fish Audio page_size must be between 1 and 100")
    key = api_key or load_api_key()
    query = urllib.parse.urlencode(
        {"self": "true", "page_size": page_size, "page_number": 1, "sort_by": "created_at"}
    )
    request = urllib.request.Request(
        f"{FISH_API_BASE}/model?{query}",
        method="GET",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "video-factory-control/0.5",
        },
    )
    sender = transport or _default_transport
    status, _headers, body = sender(request, timeout_seconds)
    if status != 200:
        raise _http_error(status, body)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FishAudioError("Fish Audio returned invalid voice-list JSON") from exc
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise FishAudioError("Fish Audio voice-list response has no items array")
    voices = []
    for item in items:
        if not isinstance(item, dict):
            continue
        voices.append(
            {
                "id": item.get("_id") or item.get("id"),
                "title": item.get("title"),
                "type": item.get("type"),
                "state": item.get("state"),
                "visibility": item.get("visibility"),
                "languages": item.get("languages") or [],
                "licensed": bool(item.get("licensed", False)),
            }
        )
    return {
        "ok": True,
        "provider": "fish_audio",
        "authenticated": True,
        "owned_voice_count": len(voices),
        "voices": voices,
    }
