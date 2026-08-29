"""Managed Pexels video discovery with a fail-closed rights ledger.

The adapter searches only portrait video, keeps provider metadata in a bounded
TTL cache, and enforces a durable local request budget before contacting the
API.  Discovery never means rights clearance: every returned candidate is
marked for item-level human review because the Pexels API does not expose
model/property release evidence for a particular clip.

The API key is read from ``PEXELS_API_KEY_FILE`` (preferred on a runtime host)
or ``PEXELS_API_KEY`` (local/managed-secret environments).  It is sent in the
``Authorization`` header, never added to a URL, cache record, result artifact,
or error message.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .contracts import validate_artifact
from .errors import FactoryError, ValidationError
from .validators import canonical_json, digest_text, require_nonempty_string


PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/v1/videos/search"
PEXELS_LICENSE_URL = "https://www.pexels.com/license/"
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_REQUESTS_PER_HOUR = 180
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_MINIMUM_WIDTH = 720
DEFAULT_MINIMUM_HEIGHT = 1280

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_ALLOWED_LANES = frozenset(
    {
        "war_history",
        "celebrity_news",
        "motivation",
        "chinese_medicine",
        "health",
    }
)
_ALLOWED_SIZES = frozenset({"small", "medium", "large"})
_PAYLOAD_FIELDS = frozenset(
    {
        "job_id",
        "lane_id",
        "required_result_contract",
        "query",
        "orientation",
        "size",
        "locale",
        "page",
        "per_page",
    }
)


@dataclass(frozen=True)
class HttpResponse:
    """Small transport boundary used by tests and the urllib implementation."""

    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, Mapping[str, str], float], HttpResponse]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _ensure_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be an ISO 8601 date-time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO 8601 date-time") from exc
    return _ensure_utc(parsed, field)


def _positive_int(value: Any, field: str, *, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{field} must be at most {maximum}")
    return value


def _positive_number(value: Any, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or float(value) <= 0
    ):
        raise ValidationError(f"{field} must be a positive number")
    return float(value)


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a positive integer") from exc
    return _positive_int(value, name)


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a positive number") from exc
    return _positive_number(value, name)


def _safe_id(value: Any, field: str) -> str:
    normalized = require_nonempty_string(value, field)
    if not _SAFE_ID.fullmatch(normalized) or ".." in normalized:
        raise ValidationError(f"{field} contains unsafe characters")
    return normalized


def _query_text(value: Any) -> str:
    query = require_nonempty_string(value, "payload.query")
    if not 2 <= len(query) <= 200:
        raise ValidationError("payload.query must contain 2 to 200 characters")
    if any(ord(character) < 32 for character in query):
        raise ValidationError("payload.query must not contain control characters")
    return " ".join(query.split())


def _api_key() -> str:
    credential_path = os.environ.get("PEXELS_API_KEY_FILE")
    if credential_path:
        path = Path(credential_path).expanduser()
        if not path.is_absolute() or path.is_symlink():
            raise ValidationError(
                "PEXELS_API_KEY_FILE must be an absolute regular credential file"
            )
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= 4096:
                raise ValidationError(
                    "PEXELS_API_KEY_FILE is not a small credential file"
                )
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = None
                raw = handle.read(4097)
            if not 1 <= len(raw) <= 4096:
                raise ValidationError(
                    "PEXELS_API_KEY_FILE is not a small credential file"
                )
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValidationError("PEXELS_API_KEY_FILE is unreadable") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        # systemd credentials normally end in one newline.  Remove CR/LF only;
        # spaces and every other control character remain invalid.
        value = text.rstrip("\r\n")
    else:
        value = os.environ.get("PEXELS_API_KEY", "")
    if (
        not value
        or value != value.strip()
        or any(character.isspace() or ord(character) < 33 for character in value)
    ):
        raise ValidationError("Pexels API credential is required for a cache miss")
    return value


def _https_url(value: Any, field: str, *, allowed_domain: str) -> str:
    url = require_nonempty_string(value, field)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(
            f"{field} must be an HTTPS URL on {allowed_domain}"
        ) from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not (host == allowed_domain or host.endswith(f".{allowed_domain}"))
    ):
        raise ValidationError(
            f"{field} must be an HTTPS URL on {allowed_domain}"
        )
    return url


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(dict(payload)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json_object(path: Path, field: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{field} is unreadable or corrupt") from exc
    if not isinstance(document, dict):
        raise ValidationError(f"{field} must contain a JSON object")
    return document


@contextmanager
def _exclusive_lock(path: Path, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    """Use an atomic lock file so concurrent agents share one request budget."""

    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > 60.0
            except FileNotFoundError:
                continue
            if stale:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise ValidationError("Pexels cache lock is busy")
            time.sleep(0.025)
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


class _MetadataCache:
    def __init__(self, root: Path, *, ttl_seconds: int) -> None:
        self.root = root.resolve()
        self.ttl_seconds = _positive_int(ttl_seconds, "cache_ttl_seconds")
        self.entries = self.root / "metadata"
        self.entries.mkdir(parents=True, exist_ok=True)

    def _path(self, cache_key: str) -> Path:
        if re.fullmatch(r"[a-f0-9]{64}", cache_key) is None:
            raise ValidationError("cache key is invalid")
        return self.entries / f"{cache_key}.json"

    def load(
        self,
        cache_key: str,
        request_spec: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], datetime, dict[str, Any]] | None:
        path = self._path(cache_key)
        if not path.is_file():
            return None
        record = _read_json_object(path, "Pexels metadata cache entry")
        expected_fields = {
            "schema_version",
            "provider",
            "cache_key",
            "request",
            "fetched_at",
            "payload_sha256",
            "provider_rate_limit",
            "payload",
        }
        if set(record) != expected_fields:
            raise ValidationError("Pexels metadata cache entry has invalid fields")
        if (
            record["schema_version"] != "1.0.0"
            or record["provider"] != "pexels"
            or record["cache_key"] != cache_key
            or record["request"] != dict(request_spec)
        ):
            raise ValidationError("Pexels metadata cache binding does not match request")
        payload = record["payload"]
        rate_limit = record["provider_rate_limit"]
        if not isinstance(payload, dict) or not isinstance(rate_limit, dict):
            raise ValidationError("Pexels metadata cache payload is invalid")
        actual_hash = digest_text(canonical_json(payload))
        if record["payload_sha256"] != actual_hash:
            raise ValidationError("Pexels metadata cache payload checksum does not match")
        fetched_at = _parse_datetime(record["fetched_at"], "cache.fetched_at")
        if fetched_at > now + timedelta(minutes=5):
            raise ValidationError("Pexels metadata cache timestamp is in the future")
        if now >= fetched_at + timedelta(seconds=self.ttl_seconds):
            return None
        return payload, fetched_at, dict(rate_limit)

    def save(
        self,
        cache_key: str,
        request_spec: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        fetched_at: datetime,
        provider_rate_limit: Mapping[str, Any],
    ) -> str:
        payload_copy = dict(payload)
        payload_sha256 = digest_text(canonical_json(payload_copy))
        record = {
            "schema_version": "1.0.0",
            "provider": "pexels",
            "cache_key": cache_key,
            "request": dict(request_spec),
            "fetched_at": _iso(fetched_at),
            "payload_sha256": payload_sha256,
            "provider_rate_limit": dict(provider_rate_limit),
            "payload": payload_copy,
        }
        _atomic_json_write(self._path(cache_key), record)
        return payload_sha256


class _RateLimiter:
    def __init__(self, root: Path, *, hourly_limit: int) -> None:
        self.root = root.resolve()
        self.hourly_limit = _positive_int(hourly_limit, "requests_per_hour")
        self.path = self.root / "rate_limit.json"
        self.lock_path = self.root / ".rate_limit.lock"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _window_start(now: datetime) -> datetime:
        return now.replace(minute=0, second=0, microsecond=0)

    def _default(self, now: datetime) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "provider": "pexels",
            "local_window_started_at": _iso(self._window_start(now)),
            "local_requests_in_window": 0,
            "provider_limit": None,
            "provider_remaining": None,
            "provider_reset_at": None,
        }

    def _load(self, now: datetime) -> dict[str, Any]:
        if not self.path.is_file():
            return self._default(now)
        state = _read_json_object(self.path, "Pexels rate-limit state")
        expected_fields = set(self._default(now))
        if set(state) != expected_fields:
            raise ValidationError("Pexels rate-limit state has invalid fields")
        if state["schema_version"] != "1.0.0" or state["provider"] != "pexels":
            raise ValidationError("Pexels rate-limit state has invalid identity")
        window = _parse_datetime(
            state["local_window_started_at"], "rate_limit.local_window_started_at"
        )
        requests = state["local_requests_in_window"]
        if not isinstance(requests, int) or isinstance(requests, bool) or requests < 0:
            raise ValidationError("Pexels local request count is invalid")
        current_window = self._window_start(now)
        if window != current_window:
            state["local_window_started_at"] = _iso(current_window)
            state["local_requests_in_window"] = 0

        for field in ("provider_limit", "provider_remaining"):
            value = state[field]
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValidationError(f"Pexels {field} is invalid")
        reset_text = state["provider_reset_at"]
        if reset_text is not None:
            reset_at = _parse_datetime(reset_text, "rate_limit.provider_reset_at")
            if reset_at <= now:
                state["provider_remaining"] = None
                state["provider_reset_at"] = None
        return state

    def reserve(self, now: datetime) -> None:
        with _exclusive_lock(self.lock_path):
            state = self._load(now)
            reset_text = state["provider_reset_at"]
            provider_blocked = state["provider_remaining"] == 0 and (
                reset_text is None
                or _parse_datetime(reset_text, "rate_limit.provider_reset_at") > now
            )
            if provider_blocked:
                raise ValidationError("Pexels provider rate limit is exhausted")
            if state["local_requests_in_window"] >= self.hourly_limit:
                raise ValidationError("Pexels local hourly request limit is exhausted")
            state["local_requests_in_window"] += 1
            _atomic_json_write(self.path, state)

    def record_headers(self, headers: Mapping[str, str], now: datetime) -> None:
        parsed = _parse_rate_headers(headers, now)
        if all(value is None for value in parsed.values()):
            return
        with _exclusive_lock(self.lock_path):
            state = self._load(now)
            for field, value in parsed.items():
                if value is not None:
                    state[field] = value
            if state["provider_remaining"] == 0 and state["provider_reset_at"] is None:
                state["provider_reset_at"] = _iso(self._window_start(now) + timedelta(hours=1))
            _atomic_json_write(self.path, state)

    def snapshot(self, now: datetime) -> dict[str, Any]:
        state = self._load(now)
        return {
            "local_hourly_limit": self.hourly_limit,
            "local_requests_in_window": state["local_requests_in_window"],
            "local_window_started_at": state["local_window_started_at"],
            "provider_limit": state["provider_limit"],
            "provider_remaining": state["provider_remaining"],
            "provider_reset_at": state["provider_reset_at"],
        }


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value).strip()
    return None


def _nonnegative_header_int(headers: Mapping[str, str], name: str) -> int | None:
    raw = _header(headers, name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationError(f"Pexels returned an invalid {name} header") from exc
    if value < 0:
        raise ValidationError(f"Pexels returned an invalid {name} header")
    return value


def _parse_rate_headers(headers: Mapping[str, str], now: datetime) -> dict[str, Any]:
    reset_raw = _header(headers, "X-Ratelimit-Reset")
    reset_at: str | None = None
    if reset_raw is not None:
        try:
            reset_epoch = int(reset_raw)
            parsed = datetime.fromtimestamp(reset_epoch, timezone.utc)
        except (ValueError, OSError, OverflowError) as exc:
            raise ValidationError(
                "Pexels returned an invalid X-Ratelimit-Reset header"
            ) from exc
        if parsed < now - timedelta(days=1):
            raise ValidationError(
                "Pexels returned a stale X-Ratelimit-Reset header"
            )
        reset_at = _iso(parsed)
    return {
        "provider_limit": _nonnegative_header_int(headers, "X-Ratelimit-Limit"),
        "provider_remaining": _nonnegative_header_int(
            headers, "X-Ratelimit-Remaining"
        ),
        "provider_reset_at": reset_at,
    }


def _cache_rate_headers(headers: Mapping[str, str], now: datetime) -> dict[str, Any]:
    return _parse_rate_headers(headers, now)


class PexelsDiscoveryClient:
    """Pexels portrait-video search with injected transport and persistent guards."""

    def __init__(
        self,
        *,
        cache_root: str | Path,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        requests_per_hour: int = DEFAULT_REQUESTS_PER_HOUR,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        minimum_width: int = DEFAULT_MINIMUM_WIDTH,
        minimum_height: int = DEFAULT_MINIMUM_HEIGHT,
        transport: Transport | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.cache_root = Path(cache_root).expanduser().resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.cache_ttl_seconds = _positive_int(
            cache_ttl_seconds, "cache_ttl_seconds"
        )
        self.timeout_seconds = _positive_number(timeout_seconds, "timeout_seconds")
        self.max_response_bytes = _positive_int(
            max_response_bytes, "max_response_bytes"
        )
        self.minimum_width = _positive_int(minimum_width, "minimum_width")
        self.minimum_height = _positive_int(minimum_height, "minimum_height")
        self._clock = clock or _utc_now
        self._transport = transport or self._default_transport
        self._cache = _MetadataCache(
            self.cache_root, ttl_seconds=self.cache_ttl_seconds
        )
        self._rate_limiter = _RateLimiter(
            self.cache_root, hourly_limit=requests_per_hour
        )

    @classmethod
    def from_environment(cls) -> "PexelsDiscoveryClient":
        runtime_root = Path(
            os.environ.get(
                "VIDEO_FACTORY_RUNTIME_ROOT",
                str(Path.home() / ".video-factory"),
            )
        ).expanduser()
        cache_root = Path(
            os.environ.get(
                "VIDEO_FACTORY_PEXELS_CACHE_ROOT",
                str(runtime_root / "discovery" / "pexels"),
            )
        ).expanduser()
        return cls(
            cache_root=cache_root,
            cache_ttl_seconds=_positive_int_env(
                "VIDEO_FACTORY_PEXELS_CACHE_TTL_SECONDS",
                DEFAULT_CACHE_TTL_SECONDS,
            ),
            requests_per_hour=_positive_int_env(
                "VIDEO_FACTORY_PEXELS_REQUESTS_PER_HOUR",
                DEFAULT_REQUESTS_PER_HOUR,
            ),
            timeout_seconds=_positive_float_env(
                "VIDEO_FACTORY_PEXELS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
            ),
            max_response_bytes=_positive_int_env(
                "VIDEO_FACTORY_PEXELS_MAX_RESPONSE_BYTES",
                DEFAULT_MAX_RESPONSE_BYTES,
            ),
            minimum_width=_positive_int_env(
                "VIDEO_FACTORY_PEXELS_MINIMUM_WIDTH", DEFAULT_MINIMUM_WIDTH
            ),
            minimum_height=_positive_int_env(
                "VIDEO_FACTORY_PEXELS_MINIMUM_HEIGHT", DEFAULT_MINIMUM_HEIGHT
            ),
        )

    def _default_transport(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read(self.max_response_bytes + 1)
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=body,
                )
        except HTTPError as exc:
            body = exc.read(self.max_response_bytes + 1)
            return HttpResponse(
                status=int(exc.code),
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=body,
            )
        except (URLError, OSError, TimeoutError) as exc:
            raise ValidationError("Pexels API request failed") from exc

    def _request_payload(
        self,
        query_spec: Mapping[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        api_key = _api_key()
        self._rate_limiter.reserve(now)
        parameters = {
            "query": query_spec["text"],
            "orientation": "portrait",
            "size": query_spec["size"],
            "locale": query_spec["locale"],
            "page": query_spec["page"],
            "per_page": query_spec["per_page"],
        }
        url = f"{PEXELS_VIDEO_SEARCH_URL}?{urlencode(parameters)}"
        headers = {
            "Authorization": api_key,
            "Accept": "application/json",
            "User-Agent": "video-factory-control/0.7 pexels-discovery",
        }
        try:
            response = self._transport(url, headers, self.timeout_seconds)
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError("Pexels API request failed") from exc
        if not isinstance(response, HttpResponse):
            raise ValidationError("Pexels transport returned an invalid response")
        if len(response.body) > self.max_response_bytes:
            raise ValidationError("Pexels response exceeded the configured byte limit")
        self._rate_limiter.record_headers(response.headers, now)
        if response.status != 200:
            raise ValidationError(f"Pexels API returned HTTP {response.status}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Pexels API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("Pexels API response must be a JSON object")
        rate_headers = _cache_rate_headers(response.headers, now)
        return payload, rate_headers

    def search(
        self,
        *,
        job_id: str,
        lane: str,
        query: str,
        size: str = "medium",
        locale: str = "ru-RU",
        page: int = 1,
        per_page: int = 20,
    ) -> dict[str, Any]:
        bound_job = _safe_id(job_id, "job_id")
        bound_lane = require_nonempty_string(lane, "lane")
        if bound_lane not in _ALLOWED_LANES:
            raise ValidationError("lane is not a configured production lane")
        normalized_query = _query_text(query)
        if size not in _ALLOWED_SIZES:
            raise ValidationError("size must be small, medium, or large")
        if not isinstance(locale, str) or _LOCALE.fullmatch(locale) is None:
            raise ValidationError("locale must use ll-CC syntax")
        normalized_page = _positive_int(page, "page")
        normalized_per_page = _positive_int(per_page, "per_page", maximum=80)
        now = _ensure_utc(self._clock(), "clock result")
        query_spec = {
            "text": normalized_query,
            "orientation": "portrait",
            "size": size,
            "locale": locale,
            "page": normalized_page,
            "per_page": normalized_per_page,
            "minimum_width": self.minimum_width,
            "minimum_height": self.minimum_height,
        }
        request_spec = {
            "endpoint": PEXELS_VIDEO_SEARCH_URL,
            **query_spec,
        }
        cache_key = digest_text(canonical_json(request_spec))
        cached = self._cache.load(cache_key, request_spec, now)
        cache_hit = cached is not None
        if cached is None:
            request_lock = self.cache_root / "request_locks" / f"{cache_key}.lock"
            with _exclusive_lock(
                request_lock,
                timeout_seconds=max(5.0, min(60.0, self.timeout_seconds + 5.0)),
            ):
                cached = self._cache.load(cache_key, request_spec, now)
                if cached is None:
                    payload, provider_rate_limit = self._request_payload(query_spec, now)
                    fetched_at = now
                    payload_sha256 = self._cache.save(
                        cache_key,
                        request_spec,
                        payload,
                        fetched_at=fetched_at,
                        provider_rate_limit=provider_rate_limit,
                    )
                else:
                    payload, fetched_at, _provider_rate_limit = cached
                    payload_sha256 = digest_text(canonical_json(payload))
                    cache_hit = True
        else:
            payload, fetched_at, _provider_rate_limit = cached
            payload_sha256 = digest_text(canonical_json(payload))

        candidates, duplicates_removed = _normalize_candidates(
            payload,
            retrieved_at=fetched_at,
            minimum_width=self.minimum_width,
            minimum_height=self.minimum_height,
        )
        if not candidates:
            raise ValidationError(
                "Pexels response contained no eligible portrait video candidates"
            )
        rate_limit = self._rate_limiter.snapshot(now)

        artifact = {
            "schema_version": "1.0.0",
            "job_id": bound_job,
            "lane": bound_lane,
            "provider": "pexels",
            "generated_at": _iso(now),
            "query": query_spec,
            "cache": {
                "cache_key": cache_key,
                "hit": cache_hit,
                "ttl_seconds": self.cache_ttl_seconds,
                "fetched_at": _iso(fetched_at),
                "expires_at": _iso(
                    fetched_at + timedelta(seconds=self.cache_ttl_seconds)
                ),
                "payload_sha256": payload_sha256,
            },
            "rate_limit": rate_limit,
            "candidates": candidates,
            "decision": {
                "discovery_passed": True,
                "rights_cleared": False,
                "needs_human_review": True,
                "candidate_count": len(candidates),
                "duplicates_removed": duplicates_removed,
            },
        }
        validate_artifact("media_discovery_manifest", artifact)
        return artifact


def _select_file(
    raw_files: Any,
    *,
    candidate_index: int,
    minimum_width: int,
    minimum_height: int,
) -> dict[str, Any]:
    if not isinstance(raw_files, list) or not raw_files:
        raise ValidationError(
            f"Pexels videos[{candidate_index}].video_files must be a non-empty array"
        )
    eligible: list[dict[str, Any]] = []
    for raw in raw_files:
        if not isinstance(raw, Mapping) or raw.get("file_type") != "video/mp4":
            continue
        width = raw.get("width")
        height = raw.get("height")
        if (
            not isinstance(width, int)
            or isinstance(width, bool)
            or not isinstance(height, int)
            or isinstance(height, bool)
            or width < minimum_width
            or height < minimum_height
            or height <= width
        ):
            continue
        try:
            download_url = _https_url(
                raw.get("link"),
                f"Pexels videos[{candidate_index}].video_files.link",
                allowed_domain="pexels.com",
            )
        except ValidationError:
            continue
        file_id = raw.get("id")
        if not isinstance(file_id, (int, str)) or isinstance(file_id, bool):
            continue
        quality = raw.get("quality")
        if not isinstance(quality, str) or not quality.strip():
            continue
        fps_raw = raw.get("fps")
        if fps_raw is None:
            fps: float | None = None
        elif (
            isinstance(fps_raw, (int, float))
            and not isinstance(fps_raw, bool)
            and float(fps_raw) > 0
        ):
            fps = float(fps_raw)
        else:
            continue
        eligible.append(
            {
                "provider_file_id": str(file_id),
                "download_url": download_url,
                "content_type": "video/mp4",
                "quality": quality.strip(),
                "width": width,
                "height": height,
                "fps": fps,
            }
        )
    if not eligible:
        raise ValidationError(
            f"Pexels videos[{candidate_index}] has no eligible portrait MP4 file"
        )

    target_width, target_height = 1080, 1920
    target_or_better = [
        item
        for item in eligible
        if item["width"] >= target_width and item["height"] >= target_height
    ]
    if target_or_better:
        return min(
            target_or_better,
            key=lambda item: (
                item["width"] * item["height"],
                item["provider_file_id"],
            ),
        )
    return max(
        eligible,
        key=lambda item: (
            item["width"] * item["height"],
            item["provider_file_id"],
        ),
    )


def _normalize_candidate(
    raw: Mapping[str, Any],
    *,
    index: int,
    retrieved_at: datetime,
    minimum_width: int,
    minimum_height: int,
) -> dict[str, Any]:
    provider_id = raw.get("id")
    if not isinstance(provider_id, int) or isinstance(provider_id, bool) or provider_id < 1:
        raise ValidationError(f"Pexels videos[{index}].id must be a positive integer")
    width = _positive_int(raw.get("width"), f"Pexels videos[{index}].width")
    height = _positive_int(raw.get("height"), f"Pexels videos[{index}].height")
    if height <= width:
        raise ValidationError(f"Pexels videos[{index}] is not portrait")
    duration = _positive_number(
        raw.get("duration"), f"Pexels videos[{index}].duration"
    )
    landing_url = _https_url(
        raw.get("url"),
        f"Pexels videos[{index}].url",
        allowed_domain="pexels.com",
    )
    thumbnail_url = _https_url(
        raw.get("image"),
        f"Pexels videos[{index}].image",
        allowed_domain="pexels.com",
    )
    user = raw.get("user")
    if not isinstance(user, Mapping):
        raise ValidationError(f"Pexels videos[{index}].user must be an object")
    creator_name = require_nonempty_string(
        user.get("name"), f"Pexels videos[{index}].user.name"
    )
    creator_url = _https_url(
        user.get("url"),
        f"Pexels videos[{index}].user.url",
        allowed_domain="pexels.com",
    )
    selected_file = _select_file(
        raw.get("video_files"),
        candidate_index=index,
        minimum_width=minimum_width,
        minimum_height=minimum_height,
    )
    provider_id_text = str(provider_id)
    attribution_text = f"Video by {creator_name} on Pexels: {landing_url}"
    return {
        "asset_id": f"pexels_video_{provider_id_text}",
        "provider_asset_id": provider_id_text,
        "media_type": "video",
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "landing_url": landing_url,
        "thumbnail_url": thumbnail_url,
        "selected_file": selected_file,
        "ledger": {
            "source": {
                "provider": "pexels",
                "provider_asset_id": provider_id_text,
                "landing_url": landing_url,
                "download_url": selected_file["download_url"],
                "creator_name": creator_name,
                "creator_url": creator_url,
                "retrieved_at": _iso(retrieved_at),
            },
            "license": {
                "name": "Pexels License",
                "url": PEXELS_LICENSE_URL,
                "commercial_use": True,
                "modification_allowed": True,
                "attribution_required_by_license": False,
                "api_linkback_required": True,
            },
            "attribution": {
                "apply": True,
                "text": attribution_text,
                "source_url": landing_url,
                "creator_url": creator_url,
            },
            "clearance": {
                "rights_status": "human_review",
                "model_release": "unknown",
                "property_release": "unknown",
                "requires_item_level_review": True,
                "review_reasons": [
                    "Pexels API metadata has no item-level model/property release evidence.",
                    "Rights review must approve the exact frozen bytes and intended editorial use.",
                ],
            },
        },
    }


def _normalize_candidates(
    payload: Mapping[str, Any],
    *,
    retrieved_at: datetime,
    minimum_width: int,
    minimum_height: int,
) -> tuple[list[dict[str, Any]], int]:
    raw_videos = payload.get("videos")
    if not isinstance(raw_videos, list):
        raise ValidationError("Pexels API response.videos must be an array")
    candidates: list[dict[str, Any]] = []
    by_id: dict[str, str] = {}
    by_url: dict[str, str] = {}
    duplicates_removed = 0
    for index, raw in enumerate(raw_videos):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"Pexels videos[{index}] must be an object")
        candidate = _normalize_candidate(
            raw,
            index=index,
            retrieved_at=retrieved_at,
            minimum_width=minimum_width,
            minimum_height=minimum_height,
        )
        provider_id = candidate["provider_asset_id"]
        landing_url = candidate["landing_url"]
        existing_url = by_id.get(provider_id)
        existing_id = by_url.get(landing_url)
        if existing_url is not None or existing_id is not None:
            if existing_url == landing_url and existing_id == provider_id:
                duplicates_removed += 1
                continue
            raise ValidationError("Pexels response contains conflicting duplicate ids or URLs")
        by_id[provider_id] = landing_url
        by_url[landing_url] = provider_id
        candidates.append(candidate)
    return candidates, duplicates_removed


def handle_task(
    task: Mapping[str, Any],
    *,
    client: PexelsDiscoveryClient | None = None,
) -> dict[str, Any]:
    if task.get("role") != "media_discovery":
        raise ValidationError("pexels_discovery accepts only role='media_discovery'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    unknown = set(payload) - _PAYLOAD_FIELDS
    if unknown:
        raise ValidationError(
            "task.payload contains unsupported fields: " + ", ".join(sorted(unknown))
        )
    if payload.get("required_result_contract") != "media_discovery_manifest":
        raise ValidationError(
            "media_discovery task must declare "
            "required_result_contract='media_discovery_manifest'"
        )
    job_id = _safe_id(task.get("job_id"), "task.job_id")
    if payload.get("job_id") != job_id:
        raise ValidationError("payload.job_id is not bound to task.job_id")
    lane = require_nonempty_string(payload.get("lane_id"), "payload.lane_id")
    if task.get("pod") != lane:
        raise ValidationError("payload.lane_id is not bound to task.pod")
    orientation = payload.get("orientation", "portrait")
    if orientation != "portrait":
        raise ValidationError("Pexels media discovery supports only portrait video")
    active_client = client or PexelsDiscoveryClient.from_environment()
    artifact = active_client.search(
        job_id=job_id,
        lane=lane,
        query=_query_text(payload.get("query")),
        size=payload.get("size", "medium"),
        locale=payload.get("locale", "ru-RU"),
        page=payload.get("page", 1),
        per_page=payload.get("per_page", 20),
    )
    return {
        "artifact": artifact,
        "discovery_execution": {
            "provider": "pexels",
            "cache_hit": artifact["cache"]["hit"],
            "network_access": not artifact["cache"]["hit"],
            "candidate_count": artifact["decision"]["candidate_count"],
            "rights_cleared": False,
        },
    }


def _redact_error(message: str) -> str:
    secrets: list[str] = []
    raw_secret = os.environ.get("PEXELS_API_KEY")
    if raw_secret:
        secrets.append(raw_secret.strip())
    credential_path = os.environ.get("PEXELS_API_KEY_FILE")
    if credential_path:
        path = Path(credential_path).expanduser()
        try:
            if path.is_absolute() and not path.is_symlink() and path.is_file():
                data = path.read_bytes()
                if 1 <= len(data) <= 4096:
                    secrets.append(data.decode("utf-8").rstrip("\r\n"))
        except (OSError, UnicodeDecodeError):
            pass
    redacted = message
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def main(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    *,
    client: PexelsDiscoveryClient | None = None,
) -> int:
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    try:
        task = json.load(source)
        if not isinstance(task, dict):
            raise ValidationError("handler stdin must contain one JSON object")
        result = handle_task(task, client=client)
    except (
        FactoryError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        message = _redact_error(str(exc))
        sys.stderr.write(
            f"pexels_discovery_error:{type(exc).__name__}:{message}\n"
        )
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_CACHE_TTL_SECONDS",
    "DEFAULT_REQUESTS_PER_HOUR",
    "HttpResponse",
    "PEXELS_LICENSE_URL",
    "PEXELS_VIDEO_SEARCH_URL",
    "PexelsDiscoveryClient",
    "handle_task",
    "main",
]
