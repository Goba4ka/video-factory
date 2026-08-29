"""Content-addressed cache for expensive derived media and QC artifacts.

The cache lives outside synchronized project folders by default.  Keys include
the byte digest of every source plus the transformation options and tool
version, so a repeat run can safely skip downloads, transcodes, transcription,
or analysis without treating a filename as proof that content is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .errors import ValidationError
from .validators import canonical_json


CACHE_SCHEMA_VERSION = 1
DEFAULT_LOCK_TIMEOUT_SECONDS = 120.0
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def default_runtime_root() -> Path:
    """Return a local, non-OneDrive runtime root unless explicitly overridden."""

    override = os.environ.get("VIDEO_FACTORY_RUNTIME_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "VideoFactoryRuntime").resolve()
    return (Path(tempfile.gettempdir()) / "VideoFactoryRuntime").resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_component(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_COMPONENT.fullmatch(value):
        raise ValidationError(
            f"{field} must contain only letters, digits, dot, underscore, or hyphen"
        )
    return value


class DerivedCache:
    """SQLite-indexed immutable file cache with per-key cross-process locks."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ):
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else default_runtime_root() / "cache"
        )
        if lock_timeout_seconds <= 0:
            raise ValidationError("lock_timeout_seconds must be positive")
        self.lock_timeout_seconds = float(lock_timeout_seconds)
        self.db_path = self.root / "cache.sqlite3"
        self.objects_dir = self.root / "objects"
        self.locks_dir = self.root / "locks"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_fingerprints (
                    path TEXT PRIMARY KEY,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entries (
                    cache_key TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    version TEXT NOT NULL,
                    relative_path TEXT NOT NULL UNIQUE,
                    suffix TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    accessed_at TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_cache_entries_accessed
                    ON entries(accessed_at, cache_key);
                """
            )
            connection.execute(f"PRAGMA user_version = {CACHE_SCHEMA_VERSION}")
            connection.commit()

    def file_fingerprint(self, source: str | Path) -> dict[str, Any]:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise ValidationError(f"cache source is not a file: {path}")
        stat = path.stat()
        path_text = str(path)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT size_bytes, mtime_ns, sha256 FROM source_fingerprints WHERE path = ?",
                (path_text,),
            ).fetchone()
            if (
                row is not None
                and row["size_bytes"] == stat.st_size
                and row["mtime_ns"] == stat.st_mtime_ns
            ):
                digest = row["sha256"]
                fingerprint_hit = True
            else:
                digest = _sha256_file(path)
                fingerprint_hit = False
                connection.execute(
                    """
                    INSERT INTO source_fingerprints(path, size_bytes, mtime_ns, sha256, checked_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size_bytes = excluded.size_bytes,
                        mtime_ns = excluded.mtime_ns,
                        sha256 = excluded.sha256,
                        checked_at = excluded.checked_at
                    """,
                    (path_text, stat.st_size, stat.st_mtime_ns, digest, _utc_now()),
                )
            connection.commit()
        return {
            "path": path_text,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": digest,
            "fingerprint_cache_hit": fingerprint_hit,
        }

    def cache_key(
        self,
        *,
        namespace: str,
        version: str,
        sources: Sequence[str | Path],
        options: Mapping[str, Any] | None = None,
        suffix: str = ".bin",
    ) -> tuple[str, list[dict[str, Any]], str]:
        namespace = _safe_component(namespace, "namespace")
        version = _safe_component(version, "version")
        if not sources:
            raise ValidationError("sources must not be empty")
        if not isinstance(suffix, str) or not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix):
            raise ValidationError("suffix must look like '.mp4' or '.json'")
        fingerprints = [self.file_fingerprint(source) for source in sources]
        normalized_sources = [
            {"sha256": item["sha256"], "size_bytes": item["size_bytes"]}
            for item in fingerprints
        ]
        payload = {
            "schema": CACHE_SCHEMA_VERSION,
            "namespace": namespace,
            "version": version,
            "sources": normalized_sources,
            "options": dict(options or {}),
            "suffix": suffix.lower(),
        }
        key = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        return key, fingerprints, canonical_json(dict(options or {}))

    def _lookup(self, key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM entries WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            path = (self.root / row["relative_path"]).resolve()
            if self.root not in path.parents or not path.is_file():
                connection.execute("DELETE FROM entries WHERE cache_key = ?", (key,))
                connection.commit()
                return None
            if (
                path.stat().st_size != row["size_bytes"]
                or _sha256_file(path) != row["sha256"]
            ):
                connection.execute("DELETE FROM entries WHERE cache_key = ?", (key,))
                connection.commit()
                return None
            now = _utc_now()
            connection.execute(
                "UPDATE entries SET accessed_at = ?, hit_count = hit_count + 1 WHERE cache_key = ?",
                (now, key),
            )
            connection.commit()
            return {
                "cache_key": key,
                "path": str(path),
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
                "created_at": row["created_at"],
                "cache_hit": True,
            }

    @contextmanager
    def _key_lock(self, key: str) -> Iterator[None]:
        lock_path = self.locks_dir / f"{key}.lock"
        deadline = time.monotonic() + self.lock_timeout_seconds
        while True:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                    handle.write(f"{os.getpid()} {time.time()}\n")
                break
            except FileExistsError as exc:
                try:
                    stale = time.time() - lock_path.stat().st_mtime > 3600
                except FileNotFoundError:
                    continue
                if stale:
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise ValidationError(f"cache key is busy: {key}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def get_or_build(
        self,
        *,
        namespace: str,
        version: str,
        sources: Sequence[str | Path],
        options: Mapping[str, Any] | None,
        suffix: str,
        builder: Callable[[Path], None],
    ) -> dict[str, Any]:
        key, fingerprints, options_json = self.cache_key(
            namespace=namespace,
            version=version,
            sources=sources,
            options=options,
            suffix=suffix,
        )
        cached = self._lookup(key)
        if cached is not None:
            cached["source_fingerprints"] = fingerprints
            return cached

        with self._key_lock(key):
            cached = self._lookup(key)
            if cached is not None:
                cached["source_fingerprints"] = fingerprints
                return cached
            namespace = _safe_component(namespace, "namespace")
            target_dir = self.objects_dir / namespace / key[:2]
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{key}{suffix.lower()}"
            fd, temporary_text = tempfile.mkstemp(
                prefix=f".{key}.", suffix=f"{suffix}.part", dir=target_dir
            )
            os.close(fd)
            temporary = Path(temporary_text)
            temporary.unlink(missing_ok=True)
            started = time.monotonic()
            try:
                builder(temporary)
                if not temporary.is_file():
                    raise ValidationError("cache builder did not create its output file")
                size = temporary.stat().st_size
                if size < 1:
                    raise ValidationError("cache builder created an empty output file")
                digest = _sha256_file(temporary)
                os.replace(temporary, target)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            now = _utc_now()
            relative = target.relative_to(self.root).as_posix()
            normalized_sources = [
                {"sha256": item["sha256"], "size_bytes": item["size_bytes"]}
                for item in fingerprints
            ]
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO entries(
                        cache_key, namespace, version, relative_path, suffix,
                        size_bytes, sha256, sources_json, options_json,
                        created_at, accessed_at, hit_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        key,
                        namespace,
                        version,
                        relative,
                        suffix.lower(),
                        size,
                        digest,
                        canonical_json(normalized_sources),
                        options_json,
                        now,
                        now,
                    ),
                )
                connection.commit()
            return {
                "cache_key": key,
                "path": str(target),
                "size_bytes": size,
                "sha256": digest,
                "created_at": now,
                "cache_hit": False,
                "build_seconds": round(time.monotonic() - started, 3),
                "source_fingerprints": fingerprints,
            }

    def stats(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            summary = connection.execute(
                """
                SELECT COUNT(*) AS entries, COALESCE(SUM(size_bytes), 0) AS bytes,
                       COALESCE(SUM(hit_count), 0) AS hits
                FROM entries
                """
            ).fetchone()
            namespaces = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT namespace, COUNT(*) AS entries, SUM(size_bytes) AS bytes,
                           SUM(hit_count) AS hits
                    FROM entries GROUP BY namespace ORDER BY namespace
                    """
                )
            ]
            source_fingerprints = connection.execute(
                "SELECT COUNT(*) AS count FROM source_fingerprints"
            ).fetchone()["count"]
        return {
            "ok": True,
            "command": "cache-status",
            "cache_root": str(self.root),
            "entries": summary["entries"],
            "size_bytes": summary["bytes"],
            "hit_count": summary["hits"],
            "source_fingerprints": source_fingerprints,
            "namespaces": namespaces,
        }

    def prune(
        self,
        *,
        max_bytes: int | None = None,
        older_than_days: int | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Plan or explicitly execute an LRU cleanup; dry-run is the default."""

        if max_bytes is None and older_than_days is None:
            raise ValidationError("prune requires max_bytes or older_than_days")
        if max_bytes is not None and (isinstance(max_bytes, bool) or max_bytes < 0):
            raise ValidationError("max_bytes must be a non-negative integer")
        if older_than_days is not None and (
            isinstance(older_than_days, bool) or older_than_days < 0
        ):
            raise ValidationError("older_than_days must be a non-negative integer")
        cutoff = (
            (datetime.now(UTC) - timedelta(days=older_than_days))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
            if older_than_days is not None
            else None
        )
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM entries ORDER BY accessed_at ASC, cache_key ASC"
            ).fetchall()
            total = sum(row["size_bytes"] for row in rows)
            selected: list[sqlite3.Row] = []
            remaining = total
            for row in rows:
                expired = cutoff is not None and row["accessed_at"] < cutoff
                over_budget = max_bytes is not None and remaining > max_bytes
                if not expired and not over_budget:
                    continue
                selected.append(row)
                remaining -= row["size_bytes"]
            if not dry_run:
                for row in selected:
                    path = (self.root / row["relative_path"]).resolve()
                    if self.root in path.parents:
                        path.unlink(missing_ok=True)
                    connection.execute(
                        "DELETE FROM entries WHERE cache_key = ?", (row["cache_key"],)
                    )
                connection.commit()
        return {
            "ok": True,
            "command": "cache-prune",
            "dry_run": bool(dry_run),
            "cache_root": str(self.root),
            "selected_entries": len(selected),
            "selected_bytes": sum(row["size_bytes"] for row in selected),
            "size_before_bytes": total,
            "size_after_bytes": remaining,
        }


__all__ = ["DerivedCache", "default_runtime_root"]
