from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .contracts import CONTRACT_FILES, validate_artifact
from .errors import NotFoundError, ValidationError
from .validators import canonical_json, require_nonempty_string


SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_component(value: str, field: str) -> str:
    value = require_nonempty_string(value, field)
    if not SAFE_ID.fullmatch(value):
        raise ValidationError(
            f"{field} must contain only letters, digits, dot, underscore, or hyphen"
        )
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(dict(payload)))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class ArtifactStore:
    """Content-addressed artifact registry with immutable files and active pointers."""

    def __init__(self, root: str | Path, *, lock_timeout_seconds: float = 5.0):
        self.root = Path(root).expanduser().resolve()
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / ".write-lock"
        self.lock_timeout_seconds = lock_timeout_seconds

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.lock_timeout_seconds
        while True:
            try:
                self.lock_path.mkdir()
                break
            except (FileExistsError, PermissionError) as exc:
                # Windows can briefly report WinError 5 while another thread is
                # removing the lock directory. It is the same observable state
                # as an existing lock, not a permanent permission failure. Retry
                # inside the bounded lock timeout and keep the store fail-closed.
                if time.monotonic() >= deadline:
                    raise ValidationError(f"artifact store is busy: {self.root}") from exc
                time.sleep(0.01)
        try:
            yield
        finally:
            release_deadline = time.monotonic() + self.lock_timeout_seconds
            while True:
                try:
                    self.lock_path.rmdir()
                    break
                except FileNotFoundError:
                    break
                except PermissionError as exc:
                    if time.monotonic() >= release_deadline:
                        raise ValidationError(
                            f"cannot release artifact store lock: {self.root}"
                        ) from exc
                    time.sleep(0.01)

    def _load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"schema_version": 1, "artifacts": []}
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot read artifact index: {exc}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("artifacts"), list)
        ):
            raise ValidationError("artifact index has an unsupported shape")
        for index, record in enumerate(payload["artifacts"]):
            if not isinstance(record, dict):
                raise ValidationError(f"artifact index record {index} must be an object")
            metadata = record.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValidationError(f"artifact index record {index} metadata must be an object")
            recorded_metadata_sha = record.get("metadata_sha256")
            if recorded_metadata_sha is not None:
                actual_metadata_sha = hashlib.sha256(
                    canonical_json(metadata).encode("utf-8")
                ).hexdigest()
                if recorded_metadata_sha != actual_metadata_sha:
                    raise ValidationError(
                        f"artifact index record {index} failed metadata sha256 verification"
                    )
            recorded_identity_sha = record.get("identity_sha256")
            if recorded_identity_sha is not None:
                identity_fields = {
                    "job_id": record.get("job_id"),
                    "kind": record.get("kind"),
                    "sha256": record.get("sha256"),
                    "dependencies": record.get("dependencies"),
                    "producer": record.get("producer"),
                    "producer_version": record.get("producer_version"),
                    "prompt_version": record.get("prompt_version"),
                    "model": record.get("model"),
                }
                if metadata:
                    identity_fields["metadata"] = metadata
                actual_identity_sha = hashlib.sha256(
                    canonical_json(identity_fields).encode("utf-8")
                ).hexdigest()
                if recorded_identity_sha != actual_identity_sha:
                    raise ValidationError(
                        f"artifact index record {index} failed identity sha256 verification"
                    )
                if record.get("artifact_id") != f"art_{actual_identity_sha[:24]}":
                    raise ValidationError(
                        f"artifact index record {index} id does not match identity"
                    )
        return payload

    @staticmethod
    def _normalize_dependencies(
        dependencies: Sequence[Mapping[str, Any]] | None,
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for index, dependency in enumerate(dependencies or []):
            if not isinstance(dependency, Mapping):
                raise ValidationError(f"dependencies[{index}] must be an object")
            dep = {
                "artifact_id": _safe_component(
                    dependency.get("artifact_id"),
                    f"dependencies[{index}].artifact_id",
                ),
                "job_id": _safe_component(dependency.get("job_id"), f"dependencies[{index}].job_id"),
                "kind": _safe_component(dependency.get("kind"), f"dependencies[{index}].kind"),
                "sha256": require_nonempty_string(
                    dependency.get("sha256"), f"dependencies[{index}].sha256"
                ),
            }
            if not SHA256.fullmatch(dep["sha256"]):
                raise ValidationError(f"dependencies[{index}].sha256 must be lowercase sha256")
            key = (dep["artifact_id"], dep["job_id"], dep["kind"], dep["sha256"])
            if key not in seen:
                normalized.append(dep)
                seen.add(key)
        return sorted(
            normalized,
            key=lambda item: (
                item["artifact_id"],
                item["job_id"],
                item["kind"],
                item["sha256"],
            ),
        )

    @staticmethod
    def _validate_dependencies(
        records: Sequence[Mapping[str, Any]],
        dependencies: Sequence[Mapping[str, str]],
        *,
        job_id: str,
        kind: str,
    ) -> None:
        by_id = {record.get("artifact_id"): record for record in records}
        for dependency in dependencies:
            if dependency["job_id"] == job_id and dependency["kind"] == kind:
                raise ValidationError("an artifact cannot depend on another version of itself")
            record = by_id.get(dependency["artifact_id"])
            if record is None:
                raise NotFoundError(
                    f"dependency artifact {dependency['artifact_id']!r} not found"
                )
            for field in ("job_id", "kind", "sha256"):
                if record.get(field) != dependency[field]:
                    raise ValidationError(
                        f"dependency artifact {dependency['artifact_id']!r} has mismatched {field}"
                    )
            if record.get("status") != "active":
                raise ValidationError(
                    f"dependency artifact {dependency['artifact_id']!r} is not active"
                )

    def put(
        self,
        *,
        job_id: str,
        kind: str,
        payload: Mapping[str, Any],
        producer: str,
        producer_version: str,
        dependencies: Sequence[Mapping[str, Any]] | None = None,
        prompt_version: str | None = None,
        model: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        validate_contract: bool = True,
    ) -> dict[str, Any]:
        job_id = _safe_component(job_id, "job_id")
        kind = _safe_component(kind, "kind")
        producer = require_nonempty_string(producer, "producer")
        producer_version = require_nonempty_string(producer_version, "producer_version")
        if not isinstance(payload, Mapping):
            raise ValidationError("payload must be a JSON object")
        document = dict(payload)
        if validate_contract and kind in CONTRACT_FILES:
            validate_artifact(kind, document)
        if metadata is None:
            normalized_metadata: dict[str, Any] = {}
        elif isinstance(metadata, Mapping):
            # Round-trip through canonical JSON to reject non-JSON values and to
            # freeze arbitrary mapping implementations into a plain object.
            normalized_metadata = json.loads(canonical_json(dict(metadata)))
        else:
            raise ValidationError("metadata must be a JSON object")
        normalized_dependencies = self._normalize_dependencies(dependencies)
        payload_json = canonical_json(document)
        content_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        metadata_sha = hashlib.sha256(
            canonical_json(normalized_metadata).encode("utf-8")
        ).hexdigest()
        identity_fields = {
            "job_id": job_id,
            "kind": kind,
            "sha256": content_sha,
            "dependencies": normalized_dependencies,
            "producer": producer,
            "producer_version": producer_version,
            "prompt_version": prompt_version,
            "model": model,
        }
        # Preserve identifiers written by schema-v1 stores when no extended
        # metadata exists, while making any metadata change identity-bearing.
        if normalized_metadata:
            identity_fields["metadata"] = normalized_metadata
        identity = canonical_json(identity_fields)
        identity_sha = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        artifact_id = f"art_{identity_sha[:24]}"

        with self._locked():
            index = self._load_index()
            records = index["artifacts"]
            self._validate_dependencies(
                records, normalized_dependencies, job_id=job_id, kind=kind
            )
            for record in records:
                if (
                    record["job_id"] == job_id
                    and record["kind"] == kind
                    and record["sha256"] == content_sha
                    and record["dependencies"] == normalized_dependencies
                    and record.get("producer") == producer
                    and record.get("producer_version") == producer_version
                    and record.get("prompt_version") == prompt_version
                    and record.get("model") == model
                    and record.get("metadata", {}) == normalized_metadata
                    and record["status"] == "active"
                ):
                    return dict(record)

            existing = next(
                (record for record in records if record["artifact_id"] == artifact_id),
                None,
            )
            if existing is not None:
                expected_identity = {
                    "job_id": job_id,
                    "kind": kind,
                    "sha256": content_sha,
                    "dependencies": normalized_dependencies,
                    "producer": producer,
                    "producer_version": producer_version,
                    "prompt_version": prompt_version,
                    "model": model,
                    "metadata": normalized_metadata,
                }
                if any(
                    existing.get(field, {} if field == "metadata" else None) != value
                    for field, value in expected_identity.items()
                ):
                    raise ValidationError(f"artifact id collision for {artifact_id!r}")
                # Do not reactivate a missing or corrupted immutable payload.
                self.read(existing["artifact_id"])
                candidate = existing
            else:
                versions = [
                    record["version"]
                    for record in records
                    if record["job_id"] == job_id and record["kind"] == kind
                ]
                version = max(versions, default=0) + 1
                relative_path = (
                    Path("artifacts")
                    / job_id
                    / kind
                    / f"v{version:04d}-{content_sha[:12]}.json"
                )
                absolute_path = (self.root / relative_path).resolve()
                if self.root not in absolute_path.parents:
                    raise ValidationError("artifact path escaped store root")
                _atomic_json(absolute_path, document)
                candidate = {
                    "artifact_id": artifact_id,
                    "job_id": job_id,
                    "kind": kind,
                    "version": version,
                    "sha256": content_sha,
                    "path": relative_path.as_posix(),
                    "status": "active",
                    "dependencies": normalized_dependencies,
                    "producer": producer,
                    "producer_version": producer_version,
                    "prompt_version": prompt_version,
                    "model": model,
                    "metadata": normalized_metadata,
                    "metadata_sha256": metadata_sha,
                    "identity_sha256": identity_sha,
                    "created_at": _utc_now(),
                    "invalidated_by": None,
                }

            stale_ids: set[str] = set()
            stale_keys: set[tuple[str, str, str]] = set()
            for record in records:
                if (
                    record["job_id"] == job_id
                    and record["kind"] == kind
                    and record["status"] == "active"
                    and record["artifact_id"] != artifact_id
                ):
                    record["status"] = "superseded"
                    record["invalidated_by"] = artifact_id
                    stale_ids.add(record["artifact_id"])
                    stale_keys.add((record["job_id"], record["kind"], record["sha256"]))

            changed = True
            while changed:
                changed = False
                for record in records:
                    if record["status"] != "active" or record["artifact_id"] == artifact_id:
                        continue
                    if any(
                        dep.get("artifact_id") in stale_ids
                        or (
                            dep.get("artifact_id") is None
                            and (dep["job_id"], dep["kind"], dep["sha256"])
                            in stale_keys
                        )
                        for dep in record["dependencies"]
                    ):
                        record["status"] = "invalidated"
                        record["invalidated_by"] = artifact_id
                        stale_ids.add(record["artifact_id"])
                        stale_keys.add(
                            (record["job_id"], record["kind"], record["sha256"])
                        )
                        changed = True

            candidate["status"] = "active"
            candidate["invalidated_by"] = None
            if existing is None:
                records.append(candidate)
            _atomic_json(self.index_path, index)
            return dict(candidate)

    def list(
        self,
        *,
        job_id: str | None = None,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if job_id is not None:
            job_id = _safe_component(job_id, "job_id")
        if kind is not None:
            kind = _safe_component(kind, "kind")
        if status is not None and status not in {"active", "superseded", "invalidated"}:
            raise ValidationError("status must be active, superseded, or invalidated")
        records = self._load_index()["artifacts"]
        return [
            dict(record)
            for record in records
            if (job_id is None or record["job_id"] == job_id)
            and (kind is None or record["kind"] == kind)
            and (status is None or record["status"] == status)
        ]

    def current(self, *, job_id: str, kind: str) -> dict[str, Any]:
        matches = self.list(job_id=job_id, kind=kind, status="active")
        if not matches:
            raise NotFoundError(f"no active {kind!r} artifact for job {job_id!r}")
        if len(matches) != 1:
            raise ValidationError(f"multiple active {kind!r} artifacts for job {job_id!r}")
        return matches[0]

    def read(self, artifact_id: str) -> dict[str, Any]:
        artifact_id = _safe_component(artifact_id, "artifact_id")
        records = [
            item for item in self._load_index()["artifacts"] if item["artifact_id"] == artifact_id
        ]
        if not records:
            raise NotFoundError(f"artifact {artifact_id!r} not found")
        path = (self.root / records[0]["path"]).resolve()
        if self.root not in path.parents:
            raise ValidationError("artifact path escaped store root")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot read artifact {artifact_id!r}: {exc}") from exc
        actual = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        if actual != records[0]["sha256"]:
            raise ValidationError(f"artifact {artifact_id!r} failed sha256 verification")
        return payload
