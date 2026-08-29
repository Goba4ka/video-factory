"""Fail-closed freezing of explicitly approved remote media.

The freezer accepts only direct HTTP(S) ``download_url`` values from a passed
rights manifest.  It streams each response through a byte limit, records a
SHA-256 digest and response metadata, then atomically moves the verified file
into a content-addressed name.  A frozen ledger snapshots both provenance and
the relevant rights decision.

No discovery, scraping, URL guessing, authentication, or license inference is
performed here.  The caller must provide an already approved manifest.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import validate_artifact
from .errors import FactoryError
from .validators import canonical_json


DEFAULT_MAX_BYTES = 500 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
CHUNK_BYTES = 64 * 1024
USER_AGENT = "video-factory-media-freeze/1.0"

_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_ALLOWED_CONTENT_PREFIXES = ("audio/", "image/", "video/")
_ALLOWED_GENERIC_CONTENT_TYPES = frozenset({"application/octet-stream"})
_CONTENT_TYPE_EXTENSIONS = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
}
_SAFE_URL_EXTENSIONS = frozenset(
    {
        ".aac",
        ".flac",
        ".gif",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".png",
        ".wav",
        ".webm",
        ".webp",
    }
)

_LOCAL_CONTENT_TYPES = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".m4a": "audio/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".ogg": "audio/ogg",
    ".png": "image/png",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
}


class MediaFreezeError(FactoryError):
    """Raised when a manifest or remote response cannot be frozen safely."""

    code = "media_freeze_error"


@dataclass(slots=True)
class _StagedAsset:
    asset: dict[str, Any]
    source_url: str
    final_url: str
    temporary_path: Path
    size_bytes: int
    sha256: str
    content_type: str
    response_metadata: dict[str, Any]
    probe_metadata: dict[str, Any] | None


@dataclass(slots=True)
class _ExplicitAsset:
    asset: dict[str, Any]
    source_kind: str
    source_input: str


@dataclass(slots=True)
class _StagedLocalAsset:
    explicit: _ExplicitAsset
    temporary_path: Path
    size_bytes: int
    sha256: str
    content_type: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MediaFreezeError(f"{field} must be an object")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaFreezeError(f"{field} must be a non-empty string")
    return value.strip()


def _parse_content_length(value: str | None, field: str) -> int | None:
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError as exc:
        raise MediaFreezeError(f"{field} returned an invalid Content-Length") from exc
    if length < 0:
        raise MediaFreezeError(f"{field} returned a negative Content-Length")
    return length


def _content_type(headers: Any, field: str) -> str:
    raw = headers.get("Content-Type")
    if not isinstance(raw, str) or not raw.strip():
        raise MediaFreezeError(f"{field} did not return Content-Type")
    value = raw.split(";", 1)[0].strip().lower()
    if not value:
        raise MediaFreezeError(f"{field} returned an empty Content-Type")
    if not value.startswith(_ALLOWED_CONTENT_PREFIXES) and value not in (
        _ALLOWED_GENERIC_CONTENT_TYPES
    ):
        raise MediaFreezeError(f"{field} returned unsupported Content-Type {value!r}")
    return value


def _validate_content_encoding(headers: Any, field: str) -> None:
    value = headers.get("Content-Encoding")
    if value and value.strip().lower() not in {"identity"}:
        raise MediaFreezeError(
            f"{field} returned unsupported Content-Encoding {value!r}"
        )


def _validate_url(url: Any, *, field: str, allow_private_hosts: bool) -> str:
    value = _require_string(url, field)
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise MediaFreezeError(f"{field} must use http or https")
    if not parts.hostname:
        raise MediaFreezeError(f"{field} must include a host")
    if parts.username is not None or parts.password is not None:
        raise MediaFreezeError(f"{field} must not contain URL credentials")
    if parts.fragment:
        raise MediaFreezeError(f"{field} must not contain a fragment")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise MediaFreezeError(f"{field} contains an invalid port") from exc

    if not allow_private_hosts:
        try:
            addresses = socket.getaddrinfo(
                parts.hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise MediaFreezeError(
                f"{field} host could not be resolved: {parts.hostname}"
            ) from exc
        if not addresses:
            raise MediaFreezeError(f"{field} host resolved to no addresses")
        for result in addresses:
            address = result[4][0].split("%", 1)[0]
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError as exc:
                raise MediaFreezeError(
                    f"{field} resolved to an invalid IP address"
                ) from exc
            if not parsed.is_global:
                raise MediaFreezeError(
                    f"{field} resolves to a private, loopback, link-local, or reserved address"
                )
    return value


class _ValidatedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, *, allow_private_hosts: bool):
        super().__init__()
        self.allow_private_hosts = allow_private_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        resolved = urljoin(req.full_url, newurl)
        _validate_url(
            resolved,
            field="redirect URL",
            allow_private_hosts=self.allow_private_hosts,
        )
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def _opener(*, allow_private_hosts: bool):
    return build_opener(
        _ValidatedRedirectHandler(allow_private_hosts=allow_private_hosts)
    )


def _request_metadata(response: Any, *, field: str) -> dict[str, Any]:
    final_url = response.geturl()
    status = getattr(response, "status", None) or response.getcode()
    if not isinstance(status, int) or not 200 <= status < 300:
        raise MediaFreezeError(f"{field} returned HTTP status {status!r}")
    _validate_content_encoding(response.headers, field)
    return {
        "status": status,
        "final_url": final_url,
        "content_type": _content_type(response.headers, field),
        "content_length": _parse_content_length(
            response.headers.get("Content-Length"), field
        ),
        "etag": response.headers.get("ETag"),
        "last_modified": response.headers.get("Last-Modified"),
    }


def _open_request(opener: Any, request: Request, *, timeout_seconds: float, field: str):
    try:
        return opener.open(request, timeout=timeout_seconds)
    except HTTPError as exc:
        raise MediaFreezeError(f"{field} failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError, socket.timeout) as exc:
        reason = getattr(exc, "reason", exc)
        raise MediaFreezeError(f"{field} failed: {reason}") from exc


def probe_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    allow_private_hosts: bool = False,
) -> dict[str, Any]:
    """Perform a strict HEAD probe without downloading a response body."""

    _validate_limits(max_bytes=max_bytes, timeout_seconds=timeout_seconds)
    source_url = _validate_url(
        url,
        field="download_url",
        allow_private_hosts=allow_private_hosts,
    )
    opener = _opener(allow_private_hosts=allow_private_hosts)
    request = Request(
        source_url,
        method="HEAD",
        headers={
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        },
    )
    with _open_request(
        opener,
        request,
        timeout_seconds=timeout_seconds,
        field="HEAD probe",
    ) as response:
        metadata = _request_metadata(response, field="HEAD probe")
    _validate_url(
        metadata["final_url"],
        field="HEAD final URL",
        allow_private_hosts=allow_private_hosts,
    )
    if metadata["content_length"] is not None and metadata["content_length"] > max_bytes:
        raise MediaFreezeError(
            "HEAD probe exceeds max_bytes "
            f"({metadata['content_length']} > {max_bytes})"
        )
    metadata["probed_at"] = _utc_now()
    return metadata


def _validate_limits(*, max_bytes: int, timeout_seconds: float) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise MediaFreezeError("max_bytes must be a positive integer")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < float(timeout_seconds) <= 600
    ):
        raise MediaFreezeError("timeout_seconds must be greater than 0 and at most 600")


def _parse_expiry(value: Any, *, asset_id: str) -> None:
    if value is None:
        return
    text = _require_string(value, f"assets[{asset_id}].expires_at")
    try:
        expiry = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MediaFreezeError(
            f"assets[{asset_id}].expires_at must be an ISO-8601 timestamp"
        ) from exc
    if expiry.tzinfo is None:
        raise MediaFreezeError(
            f"assets[{asset_id}].expires_at must include a timezone"
        )
    if expiry.astimezone(UTC) <= datetime.now(UTC):
        raise MediaFreezeError(f"assets[{asset_id}] rights have expired")


def _validate_manifest(
    manifest: Any,
    *,
    asset_ids: Sequence[str] | None,
    allow_private_hosts: bool,
) -> list[dict[str, Any]]:
    document = _require_mapping(manifest, "rights manifest")
    validate_artifact("rights_manifest", dict(document))
    decision = _require_mapping(document.get("decision"), "rights manifest decision")
    if decision.get("passed") is not True:
        raise MediaFreezeError("rights manifest decision.passed must be true")
    if decision.get("needs_human_review") is not False:
        raise MediaFreezeError(
            "rights manifest decision.needs_human_review must be false"
        )
    missing = decision.get("missing_asset_ids")
    if not isinstance(missing, list) or missing:
        raise MediaFreezeError(
            "rights manifest decision.missing_asset_ids must be an empty array"
        )
    assets = document.get("assets")
    if not isinstance(assets, list) or not assets:
        raise MediaFreezeError("rights manifest assets must be a non-empty array")

    by_id: dict[str, dict[str, Any]] = {}
    for index, raw_asset in enumerate(assets):
        asset = dict(_require_mapping(raw_asset, f"assets[{index}]"))
        asset_id = _require_string(asset.get("asset_id"), f"assets[{index}].asset_id")
        if not _ASSET_ID_RE.fullmatch(asset_id) or ".." in asset_id:
            raise MediaFreezeError(
                f"assets[{index}].asset_id contains unsafe filename characters"
            )
        if asset_id in by_id:
            raise MediaFreezeError(f"duplicate asset_id in manifest: {asset_id}")
        by_id[asset_id] = asset

    if asset_ids is None:
        selected_ids = list(by_id)
    else:
        selected_ids = []
        seen: set[str] = set()
        for value in asset_ids:
            asset_id = _require_string(value, "asset_ids[]")
            if asset_id in seen:
                raise MediaFreezeError(f"duplicate requested asset_id: {asset_id}")
            if asset_id not in by_id:
                raise MediaFreezeError(f"requested asset_id is absent: {asset_id}")
            seen.add(asset_id)
            selected_ids.append(asset_id)
        if not selected_ids:
            raise MediaFreezeError("asset_ids must not be empty when provided")

    selected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for asset_id in selected_ids:
        asset = by_id[asset_id]
        field = f"assets[{asset_id}]"
        if asset.get("rights_status") != "approved":
            raise MediaFreezeError(f"{field}.rights_status must be approved")
        if asset.get("commercial_use") is not True:
            raise MediaFreezeError(f"{field}.commercial_use must be true")
        if asset.get("modification_allowed") is not True:
            raise MediaFreezeError(f"{field}.modification_allowed must be true")
        _require_string(asset.get("landing_url"), f"{field}.landing_url")
        _require_string(asset.get("creator"), f"{field}.creator")
        _require_string(asset.get("license"), f"{field}.license")
        _require_string(asset.get("license_url"), f"{field}.license_url")
        _require_string(asset.get("retrieved_at"), f"{field}.retrieved_at")
        if not isinstance(asset.get("attribution_required"), bool):
            raise MediaFreezeError(f"{field}.attribution_required must be boolean")
        if asset.get("attribution_required") is True:
            _require_string(
                asset.get("attribution_text"), f"{field}.attribution_text"
            )
        platforms = asset.get("platforms")
        if not isinstance(platforms, list) or not platforms:
            raise MediaFreezeError(f"{field}.platforms must be a non-empty array")
        _parse_expiry(asset.get("expires_at"), asset_id=asset_id)
        download_url = _validate_url(
            asset.get("download_url"),
            field=f"{field}.download_url",
            allow_private_hosts=allow_private_hosts,
        )
        if download_url in seen_urls:
            raise MediaFreezeError(
                f"multiple selected assets use the same download_url: {download_url}"
            )
        seen_urls.add(download_url)
        asset["download_url"] = download_url
        selected.append(asset)
    return selected


def _extension_for(content_type: str, source_url: str) -> str:
    known = _CONTENT_TYPE_EXTENSIONS.get(content_type)
    if known:
        return known
    suffix = Path(urlsplit(source_url).path).suffix.lower()
    return suffix if suffix in _SAFE_URL_EXTENSIONS else ".bin"


def _stage_download(
    asset: dict[str, Any],
    *,
    output_dir: Path,
    max_bytes: int,
    timeout_seconds: float,
    probe: bool,
    allow_private_hosts: bool,
) -> _StagedAsset:
    source_url = asset["download_url"]
    probe_metadata = (
        probe_url(
            source_url,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            allow_private_hosts=allow_private_hosts,
        )
        if probe
        else None
    )
    opener = _opener(allow_private_hosts=allow_private_hosts)
    request = Request(
        source_url,
        method="GET",
        headers={
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        },
    )
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{asset['asset_id']}.",
        suffix=".part",
        dir=output_dir,
        delete=False,
    )
    temporary_path = Path(handle.name)
    digest = hashlib.sha256()
    size = 0
    started = time.monotonic()
    try:
        with handle:
            with _open_request(
                opener,
                request,
                timeout_seconds=timeout_seconds,
                field=f"GET {asset['asset_id']}",
            ) as response:
                response_metadata = _request_metadata(
                    response, field=f"GET {asset['asset_id']}"
                )
                _validate_url(
                    response_metadata["final_url"],
                    field=f"GET {asset['asset_id']} final URL",
                    allow_private_hosts=allow_private_hosts,
                )
                declared = response_metadata["content_length"]
                if declared is not None and declared > max_bytes:
                    raise MediaFreezeError(
                        f"asset {asset['asset_id']} exceeds max_bytes "
                        f"({declared} > {max_bytes})"
                    )
                if (
                    probe_metadata is not None
                    and probe_metadata["content_type"]
                    != response_metadata["content_type"]
                ):
                    raise MediaFreezeError(
                        f"asset {asset['asset_id']} Content-Type changed between HEAD and GET"
                    )
                while True:
                    if time.monotonic() - started > timeout_seconds:
                        raise MediaFreezeError(
                            f"asset {asset['asset_id']} exceeded timeout_seconds"
                        )
                    try:
                        chunk = response.read(
                            min(CHUNK_BYTES, max_bytes - size + 1)
                        )
                    except (OSError, TimeoutError, socket.timeout) as exc:
                        raise MediaFreezeError(
                            f"asset {asset['asset_id']} download failed or timed out: {exc}"
                        ) from exc
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaFreezeError(
                            f"asset {asset['asset_id']} exceeded max_bytes while streaming"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                if declared is not None and size != declared:
                    raise MediaFreezeError(
                        f"asset {asset['asset_id']} size does not match Content-Length "
                        f"({size} != {declared})"
                    )
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return _StagedAsset(
        asset=asset,
        source_url=source_url,
        final_url=response_metadata["final_url"],
        temporary_path=temporary_path,
        size_bytes=size,
        sha256=digest.hexdigest(),
        content_type=response_metadata["content_type"],
        response_metadata=response_metadata,
        probe_metadata=probe_metadata,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _rights_snapshot(asset: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "asset_id",
        "landing_url",
        "creator",
        "license",
        "license_url",
        "license_receipt",
        "retrieved_at",
        "commercial_use",
        "modification_allowed",
        "attribution_required",
        "attribution_text",
        "model_release",
        "property_release",
        "platforms",
        "territories",
        "expires_at",
        "rights_status",
        "notes",
    )
    return {field: asset.get(field) for field in fields if field in asset}


def _load_existing_ledger(path: Path) -> dict[str, Mapping[str, Any]]:
    """Load only records that have the expected immutable-ledger shape."""

    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(value, Mapping)
        or value.get("kind") != "frozen_media_ledger"
        or not isinstance(value.get("assets"), list)
        or not isinstance(value.get("decision"), Mapping)
        or value["decision"].get("passed") is not True
    ):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for record in value["assets"]:
        if not isinstance(record, Mapping):
            continue
        asset_id = record.get("asset_id")
        if isinstance(asset_id, str) and asset_id not in result:
            result[asset_id] = record
    return result


def _reuse_existing_record(
    asset: Mapping[str, Any],
    *,
    record: Mapping[str, Any] | None,
    ledger: Path,
    destination: Path,
) -> dict[str, Any] | None:
    """Verify a matching ledger record before any HTTP request is made."""

    if record is None:
        return None
    if record.get("source_url") != asset.get("download_url"):
        return None
    if record.get("rights") != _rights_snapshot(asset):
        return None
    relative = record.get("frozen_path")
    digest = record.get("sha256")
    size = record.get("size_bytes")
    if (
        not isinstance(relative, str)
        or not relative
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 1
    ):
        return None
    frozen_path = (ledger.parent / relative).resolve()
    if destination != frozen_path.parent and destination not in frozen_path.parents:
        return None
    if not frozen_path.is_file() or frozen_path.stat().st_size != size:
        return None
    if _sha256_file(frozen_path) != digest:
        return None
    reused = dict(record)
    reused["reused_existing"] = True
    reused["cache_revalidated"] = True
    reused["revalidated_at"] = _utc_now()
    return reused


def freeze_approved_media(
    manifest_path: str | Path,
    output_dir: str | Path,
    *,
    ledger_path: str | Path | None = None,
    asset_ids: Sequence[str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    probe: bool = False,
    allow_private_hosts: bool = False,
) -> dict[str, Any]:
    """Freeze selected approved manifest assets and atomically write a ledger.

    ``allow_private_hosts`` exists for controlled local test infrastructure.  It
    is false by default so an approved manifest cannot silently turn the worker
    into an SSRF client.
    """

    _validate_limits(max_bytes=max_bytes, timeout_seconds=timeout_seconds)
    source = Path(manifest_path).expanduser().resolve()
    try:
        raw_manifest = source.read_bytes()
    except FileNotFoundError as exc:
        raise MediaFreezeError(f"rights manifest does not exist: {source}") from exc
    try:
        manifest = json.loads(raw_manifest.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaFreezeError(f"rights manifest is not valid UTF-8 JSON: {source}") from exc

    selected = _validate_manifest(
        manifest,
        asset_ids=asset_ids,
        allow_private_hosts=allow_private_hosts,
    )
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    ledger = (
        Path(ledger_path).expanduser().resolve()
        if ledger_path is not None
        else destination / "frozen_media_ledger.json"
    )

    previous = _load_existing_ledger(ledger)
    records_by_id: dict[str, dict[str, Any]] = {}
    staged: list[_StagedAsset] = []
    created: list[Path] = []
    try:
        for asset in selected:
            reused = _reuse_existing_record(
                asset,
                record=previous.get(asset["asset_id"]),
                ledger=ledger,
                destination=destination,
            )
            if reused is not None:
                records_by_id[asset["asset_id"]] = reused
                continue
            staged.append(
                _stage_download(
                    asset,
                    output_dir=destination,
                    max_bytes=max_bytes,
                    timeout_seconds=float(timeout_seconds),
                    probe=probe,
                    allow_private_hosts=allow_private_hosts,
                )
            )

        for item in staged:
            extension = _extension_for(item.content_type, item.source_url)
            final_path = destination / (
                f"{item.asset['asset_id']}-{item.sha256}{extension}"
            )
            reused = False
            if final_path.exists():
                if not final_path.is_file():
                    raise MediaFreezeError(
                        f"frozen destination is not a file: {final_path}"
                    )
                if final_path.stat().st_size != item.size_bytes or _sha256_file(
                    final_path
                ) != item.sha256:
                    raise MediaFreezeError(
                        f"existing frozen file failed hash verification: {final_path}"
                    )
                item.temporary_path.unlink(missing_ok=True)
                reused = True
            else:
                os.replace(item.temporary_path, final_path)
                created.append(final_path)

            try:
                relative_path = final_path.relative_to(ledger.parent).as_posix()
            except ValueError:
                relative_path = os.path.relpath(final_path, ledger.parent).replace(
                    os.sep, "/"
                )
            records_by_id[item.asset["asset_id"]] = {
                "asset_id": item.asset["asset_id"],
                "source_url": item.source_url,
                "final_url": item.final_url,
                "frozen_path": relative_path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "content_type": item.content_type,
                "reused_existing": reused,
                "cache_revalidated": False,
                "frozen_at": _utc_now(),
                "http": item.response_metadata,
                "head_probe": item.probe_metadata,
                "rights": _rights_snapshot(item.asset),
            }

        records = [records_by_id[asset["asset_id"]] for asset in selected]

        result = {
            "schema_version": "1.0.0",
            "kind": "frozen_media_ledger",
            "created_at": _utc_now(),
            "rights_manifest": {
                "path": str(source),
                "sha256": hashlib.sha256(raw_manifest).hexdigest(),
                "schema_version": manifest.get("schema_version"),
                "idea_id": manifest.get("idea_id"),
            },
            "limits": {
                "max_bytes_per_asset": max_bytes,
                "timeout_seconds_per_request": float(timeout_seconds),
                "head_probe_enabled": probe,
                "private_hosts_allowed": allow_private_hosts,
            },
            "assets": records,
            "decision": {
                "passed": True,
                "asset_count": len(records),
                "all_hashes_verified": True,
                "network_downloads": len(staged),
                "cache_hits": len(records) - len(staged),
            },
        }
        _atomic_json(ledger, result)
        result["ledger_path"] = str(ledger)
        return result
    except Exception:
        for item in staged:
            item.temporary_path.unlink(missing_ok=True)
        for path in created:
            path.unlink(missing_ok=True)
        raise


def _rights_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash the canonical rights document, independent of JSON whitespace."""

    return hashlib.sha256(canonical_json(dict(manifest)).encode("utf-8")).hexdigest()


def _validate_passed_rights_manifest(manifest: Any) -> dict[str, dict[str, Any]]:
    document = _require_mapping(manifest, "rights manifest")
    validate_artifact("rights_manifest", dict(document))
    decision = _require_mapping(document.get("decision"), "rights manifest decision")
    if decision.get("passed") is not True:
        raise MediaFreezeError("rights manifest decision.passed must be true")
    if decision.get("needs_human_review") is not False:
        raise MediaFreezeError(
            "rights manifest decision.needs_human_review must be false"
        )
    missing = decision.get("missing_asset_ids")
    if not isinstance(missing, list) or missing:
        raise MediaFreezeError(
            "rights manifest decision.missing_asset_ids must be an empty array"
        )

    by_id: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(document["assets"]):
        asset = dict(_require_mapping(value, f"assets[{index}]"))
        asset_id = _require_string(asset.get("asset_id"), f"assets[{index}].asset_id")
        if not _ASSET_ID_RE.fullmatch(asset_id) or ".." in asset_id:
            raise MediaFreezeError(
                f"assets[{index}].asset_id contains unsafe filename characters"
            )
        if asset_id in by_id:
            raise MediaFreezeError(f"duplicate asset_id in manifest: {asset_id}")
        if asset.get("rights_status") != "approved":
            raise MediaFreezeError(f"assets[{asset_id}].rights_status must be approved")
        if asset.get("commercial_use") is not True:
            raise MediaFreezeError(f"assets[{asset_id}].commercial_use must be true")
        if asset.get("modification_allowed") is not True:
            raise MediaFreezeError(
                f"assets[{asset_id}].modification_allowed must be true"
            )
        _parse_expiry(asset.get("expires_at"), asset_id=asset_id)
        if asset.get("attribution_required") is True:
            _require_string(
                asset.get("attribution_text"),
                f"assets[{asset_id}].attribution_text",
            )
        by_id[asset_id] = asset
    if not by_id:
        raise MediaFreezeError("rights manifest assets must be a non-empty array")
    return by_id


def _is_within(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _explicit_assets(
    manifest: Mapping[str, Any],
    media_inputs: Any,
    *,
    allowed_local_roots: Sequence[str | Path],
    allow_private_hosts: bool,
) -> list[_ExplicitAsset]:
    rights_by_id = _validate_passed_rights_manifest(manifest)
    if not isinstance(media_inputs, list) or not media_inputs:
        raise MediaFreezeError("media_inputs must be a non-empty array")
    local_roots = [Path(item).expanduser().resolve() for item in allowed_local_roots]
    result_by_id: dict[str, _ExplicitAsset] = {}
    for index, raw in enumerate(media_inputs):
        item = _require_mapping(raw, f"media_inputs[{index}]")
        unknown = set(item) - {"asset_id", "local_path", "download_url"}
        if unknown:
            raise MediaFreezeError(
                f"media_inputs[{index}] contains unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        asset_id = _require_string(
            item.get("asset_id"), f"media_inputs[{index}].asset_id"
        )
        if asset_id in result_by_id:
            raise MediaFreezeError(f"duplicate media input for asset_id: {asset_id}")
        if asset_id not in rights_by_id:
            raise MediaFreezeError(
                f"media input references asset absent from RightsManifest: {asset_id}"
            )
        has_local = item.get("local_path") is not None
        has_download = item.get("download_url") is not None
        if has_local == has_download:
            raise MediaFreezeError(
                f"media_inputs[{index}] must provide exactly one of local_path or download_url"
            )
        asset = rights_by_id[asset_id]
        if has_local:
            raw_path = Path(
                _require_string(item.get("local_path"), f"media_inputs[{index}].local_path")
            ).expanduser()
            if not raw_path.is_absolute():
                raise MediaFreezeError(
                    f"media_inputs[{index}].local_path must be absolute"
                )
            if raw_path.is_symlink():
                raise MediaFreezeError(
                    f"media_inputs[{index}].local_path must not be a symlink"
                )
            source = raw_path.resolve()
            if not local_roots or not _is_within(source, local_roots):
                raise MediaFreezeError(
                    f"media_inputs[{index}].local_path is outside allowed local roots"
                )
            approved_path = asset.get("local_path")
            if not isinstance(approved_path, str) or not approved_path.strip():
                raise MediaFreezeError(
                    f"RightsManifest asset {asset_id} has no approved local_path"
                )
            if Path(approved_path).expanduser().resolve() != source:
                raise MediaFreezeError(
                    f"media input local_path does not match RightsManifest asset {asset_id}"
                )
            explicit = _ExplicitAsset(asset, "local_file", str(source))
        else:
            direct_url = _validate_url(
                item.get("download_url"),
                field=f"media_inputs[{index}].download_url",
                allow_private_hosts=allow_private_hosts,
            )
            if asset.get("download_url") != direct_url:
                raise MediaFreezeError(
                    f"media input download_url does not match RightsManifest asset {asset_id}"
                )
            explicit = _ExplicitAsset(asset, "direct_download", direct_url)
        result_by_id[asset_id] = explicit

    expected = set(rights_by_id)
    provided = set(result_by_id)
    if provided != expected:
        missing = sorted(expected - provided)
        extra = sorted(provided - expected)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise MediaFreezeError(
            "media_inputs must cover every RightsManifest asset (" + "; ".join(details) + ")"
        )
    return [result_by_id[asset_id] for asset_id in rights_by_id]


def _local_content_type(path: Path) -> str:
    content_type = _LOCAL_CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None:
        guessed, _ = mimetypes.guess_type(path.name)
        if not isinstance(guessed, str) or not guessed.startswith(
            _ALLOWED_CONTENT_PREFIXES
        ):
            raise MediaFreezeError(
                f"local media has an unsupported extension or content type: {path.name}"
            )
        content_type = guessed
    return content_type


def _stage_local_copy(
    explicit: _ExplicitAsset,
    *,
    output_dir: Path,
    max_bytes: int,
    timeout_seconds: float,
) -> _StagedLocalAsset:
    source = Path(explicit.source_input)
    try:
        source_stat = source.stat()
    except OSError as exc:
        raise MediaFreezeError(f"local media is unreadable: {source}") from exc
    if not source.is_file():
        raise MediaFreezeError(f"local media is not a regular file: {source}")
    if source_stat.st_size < 1:
        raise MediaFreezeError(f"local media is empty: {source}")
    if source_stat.st_size > max_bytes:
        raise MediaFreezeError(
            f"asset {explicit.asset['asset_id']} exceeds max_bytes "
            f"({source_stat.st_size} > {max_bytes})"
        )
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{explicit.asset['asset_id']}.",
        suffix=".part",
        dir=output_dir,
        delete=False,
    )
    temporary_path = Path(handle.name)
    digest = hashlib.sha256()
    copied = 0
    started = time.monotonic()
    try:
        with source.open("rb") as input_handle, handle:
            opened_stat = os.fstat(input_handle.fileno())
            while True:
                if time.monotonic() - started > timeout_seconds:
                    raise MediaFreezeError(
                        f"asset {explicit.asset['asset_id']} exceeded timeout_seconds"
                    )
                chunk = input_handle.read(CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise MediaFreezeError(
                        f"asset {explicit.asset['asset_id']} exceeded max_bytes while copying"
                    )
                digest.update(chunk)
                handle.write(chunk)
            final_stat = os.fstat(input_handle.fileno())
            if (
                opened_stat.st_size != final_stat.st_size
                or opened_stat.st_mtime_ns != final_stat.st_mtime_ns
                or copied != opened_stat.st_size
            ):
                raise MediaFreezeError(
                    f"local media changed while being frozen: {source}"
                )
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return _StagedLocalAsset(
        explicit=explicit,
        temporary_path=temporary_path,
        size_bytes=copied,
        sha256=digest.hexdigest(),
        content_type=_local_content_type(source),
    )


def _relative_frozen_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise MediaFreezeError("frozen media escaped frozen_root") from exc


def verify_frozen_media_manifest(
    document: Mapping[str, Any],
    *,
    rights_manifest: Mapping[str, Any] | None = None,
    expected_job_id: str | None = None,
) -> dict[str, Any]:
    """Validate the contract, rights binding, path containment, size and SHA-256."""

    manifest = dict(document)
    validate_artifact("frozen_media_manifest", manifest)
    if expected_job_id is not None and manifest["job_id"] != expected_job_id:
        raise MediaFreezeError("frozen media manifest is not bound to task.job_id")
    root_text = _require_string(manifest["frozen_root"], "frozen_root")
    root_input = Path(root_text).expanduser()
    if not root_input.is_absolute():
        raise MediaFreezeError("frozen_root must be absolute")
    root = root_input.resolve()
    if not root.is_dir():
        raise MediaFreezeError(f"frozen_root does not exist: {root}")

    rights_by_id: dict[str, dict[str, Any]] | None = None
    if rights_manifest is not None:
        rights_by_id = _validate_passed_rights_manifest(rights_manifest)
        expected_rights_sha = _rights_manifest_sha256(rights_manifest)
        if manifest["rights_manifest"]["sha256"] != expected_rights_sha:
            raise MediaFreezeError("frozen media rights manifest hash does not match")
        if manifest["rights_manifest"]["idea_id"] != rights_manifest["idea_id"]:
            raise MediaFreezeError("frozen media rights idea_id does not match")
        if set(rights_by_id) != {item["asset_id"] for item in manifest["assets"]}:
            raise MediaFreezeError("frozen media assets do not match RightsManifest")

    for item in manifest["assets"]:
        asset_id = item["asset_id"]
        relative = Path(item["frozen_path"])
        if relative.is_absolute():
            raise MediaFreezeError(f"frozen_path for {asset_id} must be relative")
        frozen = (root / relative).resolve()
        if not _is_within(frozen, [root]):
            raise MediaFreezeError(f"frozen_path for {asset_id} escapes frozen_root")
        if not frozen.is_file():
            raise MediaFreezeError(f"frozen file is missing for asset {asset_id}")
        if frozen.stat().st_size != item["size_bytes"]:
            raise MediaFreezeError(f"frozen file size mismatch for asset {asset_id}")
        if _sha256_file(frozen) != item["sha256"]:
            raise MediaFreezeError(f"frozen file hash mismatch for asset {asset_id}")
        if rights_by_id is not None:
            approved = rights_by_id[asset_id]
            if item["rights"] != approved:
                raise MediaFreezeError(
                    f"frozen rights snapshot does not match RightsManifest asset {asset_id}"
                )
            source = item["source"]
            if source["kind"] == "local_file":
                approved_input = approved.get("local_path")
                if not isinstance(approved_input, str) or (
                    Path(approved_input).expanduser().resolve()
                    != Path(source["input"]).expanduser().resolve()
                ):
                    raise MediaFreezeError(
                        f"frozen local source does not match RightsManifest asset {asset_id}"
                    )
            elif approved.get("download_url") != source["input"]:
                raise MediaFreezeError(
                    f"frozen download source does not match RightsManifest asset {asset_id}"
                )
    return manifest


def freeze_explicit_media(
    rights_manifest: Mapping[str, Any],
    media_inputs: Sequence[Mapping[str, Any]],
    output_dir: str | Path,
    *,
    job_id: str,
    allowed_local_roots: Sequence[str | Path],
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    probe: bool = False,
    allow_private_hosts: bool = False,
) -> dict[str, Any]:
    """Freeze every approved asset from explicit byte-bearing inputs.

    ``landing_url`` is retained only inside the immutable rights snapshot. It is
    never opened or treated as a byte source. Local and download locators must
    be repeated explicitly by the caller and exactly match the passed
    ``RightsManifest``.
    """

    _validate_limits(max_bytes=max_bytes, timeout_seconds=timeout_seconds)
    safe_job_id = _require_string(job_id, "job_id")
    if not _ASSET_ID_RE.fullmatch(safe_job_id) or ".." in safe_job_id:
        raise MediaFreezeError("job_id contains unsafe path characters")
    manifest = dict(_require_mapping(rights_manifest, "rights manifest"))
    explicit_assets = _explicit_assets(
        manifest,
        list(media_inputs),
        allowed_local_roots=allowed_local_roots,
        allow_private_hosts=allow_private_hosts,
    )
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "frozen_media_manifest.json"
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                raise MediaFreezeError("existing frozen media manifest is not an object")
            verify_frozen_media_manifest(
                existing,
                rights_manifest=manifest,
                expected_job_id=safe_job_id,
            )
            expected_sources = {
                item.asset["asset_id"]: (item.source_kind, item.source_input)
                for item in explicit_assets
            }
            actual_sources = {
                item["asset_id"]: (item["source"]["kind"], item["source"]["input"])
                for item in existing["assets"]
            }
            if actual_sources == expected_sources:
                return {"artifact": existing, "manifest_path": str(manifest_path)}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, MediaFreezeError):
            pass

    staged_local: list[_StagedLocalAsset] = []
    staged_remote: list[tuple[_ExplicitAsset, _StagedAsset]] = []
    created: list[Path] = []
    records: list[dict[str, Any]] = []
    try:
        for explicit in explicit_assets:
            if explicit.source_kind == "local_file":
                staged_local.append(
                    _stage_local_copy(
                        explicit,
                        output_dir=destination,
                        max_bytes=max_bytes,
                        timeout_seconds=float(timeout_seconds),
                    )
                )
            else:
                remote_asset = dict(explicit.asset)
                remote_asset["download_url"] = explicit.source_input
                staged_remote.append(
                    (
                        explicit,
                        _stage_download(
                            remote_asset,
                            output_dir=destination,
                            max_bytes=max_bytes,
                            timeout_seconds=float(timeout_seconds),
                            probe=probe,
                            allow_private_hosts=allow_private_hosts,
                        ),
                    )
                )

        ordered: dict[str, tuple[_ExplicitAsset, Path, int, str, str, str | None]] = {}
        for item in staged_local:
            ordered[item.explicit.asset["asset_id"]] = (
                item.explicit,
                item.temporary_path,
                item.size_bytes,
                item.sha256,
                item.content_type,
                None,
            )
        for explicit, item in staged_remote:
            ordered[explicit.asset["asset_id"]] = (
                explicit,
                item.temporary_path,
                item.size_bytes,
                item.sha256,
                item.content_type,
                item.final_url,
            )

        for explicit in explicit_assets:
            _, temporary, size, digest, content_type, final_url = ordered[
                explicit.asset["asset_id"]
            ]
            source_suffix = (
                Path(explicit.source_input).suffix.lower()
                if explicit.source_kind == "local_file"
                else Path(urlsplit(explicit.source_input).path).suffix.lower()
            )
            extension = (
                source_suffix
                if source_suffix in _SAFE_URL_EXTENSIONS
                else _extension_for(content_type, explicit.source_input)
            )
            final_path = destination / (
                f"{explicit.asset['asset_id']}-{digest}{extension}"
            )
            reused = False
            if final_path.exists():
                if not final_path.is_file() or final_path.stat().st_size != size:
                    raise MediaFreezeError(
                        f"existing frozen destination is invalid: {final_path}"
                    )
                if _sha256_file(final_path) != digest:
                    raise MediaFreezeError(
                        f"existing frozen file failed hash verification: {final_path}"
                    )
                temporary.unlink(missing_ok=True)
                reused = True
            else:
                os.replace(temporary, final_path)
                created.append(final_path)
            records.append(
                {
                    "asset_id": explicit.asset["asset_id"],
                    "source": {
                        "kind": explicit.source_kind,
                        "input": explicit.source_input,
                        "final_url": final_url,
                        "landing_url_is_byte_source": False,
                    },
                    "frozen_path": _relative_frozen_path(final_path, destination),
                    "sha256": digest,
                    "size_bytes": size,
                    "content_type": content_type,
                    "reused_existing": reused,
                    "rights": dict(explicit.asset),
                }
            )

        artifact = {
            "schema_version": "1.0.0",
            "job_id": safe_job_id,
            "idea_id": manifest["idea_id"],
            "created_at": _utc_now(),
            "frozen_root": str(destination),
            "rights_manifest": {
                "schema_version": manifest["schema_version"],
                "idea_id": manifest["idea_id"],
                "sha256": _rights_manifest_sha256(manifest),
            },
            "assets": records,
            "decision": {
                "passed": True,
                "all_rights_approved": True,
                "all_hashes_verified": True,
                "asset_count": len(records),
                "network_downloads": sum(
                    not item["reused_existing"]
                    and item["source"]["kind"] == "direct_download"
                    for item in records
                ),
                "local_copies": sum(
                    not item["reused_existing"]
                    and item["source"]["kind"] == "local_file"
                    for item in records
                ),
                "cache_hits": sum(item["reused_existing"] for item in records),
            },
        }
        verify_frozen_media_manifest(
            artifact,
            rights_manifest=manifest,
            expected_job_id=safe_job_id,
        )
        _atomic_json(manifest_path, artifact)
        return {"artifact": artifact, "manifest_path": str(manifest_path)}
    except Exception:
        for item in staged_local:
            item.temporary_path.unlink(missing_ok=True)
        for _, item in staged_remote:
            item.temporary_path.unlink(missing_ok=True)
        for path in created:
            path.unlink(missing_ok=True)
        raise


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MediaFreezeError",
    "freeze_explicit_media",
    "freeze_approved_media",
    "probe_url",
    "verify_frozen_media_manifest",
]
