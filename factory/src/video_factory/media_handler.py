"""Trusted JSON-stdio handler for rights-bound media freezing.

The handler performs no discovery and never infers a byte source.  Local files
must be named in the passed ``RightsManifest`` and stay inside configured input
roots.  Direct network downloads are disabled by default and require the
explicit ``VIDEO_FACTORY_MEDIA_ALLOW_RIGHTS_DOWNLOADS=true`` runtime switch;
even then, the exact URL must come from a fully passed ``RightsManifest``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, TextIO

from .contracts import validate_artifact
from .errors import FactoryError, ValidationError
from .media_freeze import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    MediaFreezeError,
    freeze_explicit_media,
    verify_frozen_media_manifest,
)
from .validators import canonical_json, require_nonempty_string


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MEDIA_INPUT_FIELDS = frozenset({"asset_id", "local_path", "download_url"})


def _configured_root(name: str, default: Path, *, create: bool) -> Path:
    root = Path(os.environ.get(name, str(default))).expanduser().resolve()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValidationError(f"{name} must point to an existing directory")
    return root


def _runtime_root() -> Path:
    return _configured_root(
        "VIDEO_FACTORY_RUNTIME_ROOT", Path.home() / ".video-factory", create=True
    )


def _input_roots(runtime_root: Path) -> list[Path]:
    configured = os.environ.get("VIDEO_FACTORY_MEDIA_INPUT_ROOTS")
    if configured is None:
        return [
            _configured_root(
                "VIDEO_FACTORY_MEDIA_INPUT_ROOT",
                runtime_root / "media_inputs",
                create=True,
            )
        ]
    values = [value.strip() for value in configured.split(os.pathsep) if value.strip()]
    if not values:
        raise ValidationError(
            "VIDEO_FACTORY_MEDIA_INPUT_ROOTS must contain at least one directory"
        )
    roots: list[Path] = []
    for value in values:
        root = Path(value).expanduser().resolve()
        if not root.is_dir():
            raise ValidationError(
                "VIDEO_FACTORY_MEDIA_INPUT_ROOTS contains a missing directory"
            )
        if root not in roots:
            roots.append(root)
    return roots


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ValidationError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValidationError(f"{name} must be a positive number")
    return value


def _boolean_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValidationError(f"{name} must be a boolean")


def _safe_id(value: Any, field: str) -> str:
    normalized = require_nonempty_string(value, field)
    if not _SAFE_ID.fullmatch(normalized) or ".." in normalized:
        raise ValidationError(f"{field} contains unsafe path characters")
    return normalized


def _upstream_rights_manifest(task: Mapping[str, Any]) -> dict[str, Any]:
    upstream = task.get("upstream_results")
    if not isinstance(upstream, list):
        raise ValidationError("task.upstream_results must be an array")
    matches: list[dict[str, Any]] = []
    for entry in upstream:
        if not isinstance(entry, Mapping) or entry.get("role") != "rights":
            continue
        result = entry.get("result")
        artifact = result.get("artifact") if isinstance(result, Mapping) else None
        if isinstance(artifact, dict):
            matches.append(artifact)
    if len(matches) != 1:
        raise ValidationError(
            "media task requires exactly one upstream rights_manifest from role='rights'"
        )
    validate_artifact("rights_manifest", matches[0])
    return matches[0]


def _media_inputs(
    payload: Mapping[str, Any],
    rights_manifest: Mapping[str, Any],
    *,
    allow_network_downloads: bool,
) -> list[dict[str, str]]:
    raw_inputs = payload.get("media_inputs")
    if raw_inputs is None:
        derived: list[dict[str, str]] = []
        for index, asset in enumerate(rights_manifest["assets"]):
            asset_id = _safe_id(
                asset.get("asset_id"), f"rights_manifest.assets[{index}].asset_id"
            )
            local_path = asset.get("local_path")
            download_url = asset.get("download_url")
            has_local = isinstance(local_path, str) and bool(local_path.strip())
            has_download = isinstance(download_url, str) and bool(download_url.strip())
            if has_local == has_download:
                raise ValidationError(
                    f"rights_manifest asset {asset_id} must provide exactly one byte locator"
                )
            if has_download:
                if not allow_network_downloads:
                    raise ValidationError(
                        "rights-bound network downloads are disabled for media_handler"
                    )
                derived.append(
                    {"asset_id": asset_id, "download_url": download_url.strip()}
                )
            else:
                derived.append(
                    {"asset_id": asset_id, "local_path": local_path.strip()}
                )
        raw_inputs = derived
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise ValidationError("payload.media_inputs must be a non-empty array")
    normalized: list[dict[str, str]] = []
    for index, raw in enumerate(raw_inputs):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"payload.media_inputs[{index}] must be an object")
        unknown = set(raw) - _MEDIA_INPUT_FIELDS
        if unknown:
            raise ValidationError(
                f"payload.media_inputs[{index}] contains unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        asset_id = _safe_id(raw.get("asset_id"), f"payload.media_inputs[{index}].asset_id")
        has_local = raw.get("local_path") is not None
        has_download = raw.get("download_url") is not None
        if has_local == has_download:
            raise ValidationError(
                f"payload.media_inputs[{index}] must provide exactly one locator"
            )
        if has_local:
            local_text = require_nonempty_string(
                raw.get("local_path"), f"payload.media_inputs[{index}].local_path"
            )
            local_path = Path(local_text).expanduser()
            if not local_path.is_absolute():
                raise ValidationError(
                    f"payload.media_inputs[{index}].local_path must be absolute"
                )
            normalized.append(
                {"asset_id": asset_id, "local_path": str(local_path.resolve())}
            )
        else:
            if not allow_network_downloads:
                raise ValidationError(
                    "rights-bound network downloads are disabled for media_handler"
                )
            normalized.append(
                {
                    "asset_id": asset_id,
                    "download_url": require_nonempty_string(
                        raw.get("download_url"),
                        f"payload.media_inputs[{index}].download_url",
                    ),
                }
            )
    return normalized


def handle_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if task.get("role") != "media":
        raise ValidationError("media_handler accepts only role='media'")
    payload = task.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("task.payload must be an object")
    if payload.get("required_result_contract") != "frozen_media_manifest":
        raise ValidationError(
            "media task must declare required_result_contract='frozen_media_manifest'"
        )

    job_id = _safe_id(task.get("job_id"), "task.job_id")
    if payload.get("job_id") != job_id:
        raise ValidationError("payload.job_id is not bound to task.job_id")
    lane = require_nonempty_string(payload.get("lane_id"), "payload.lane_id")
    if task.get("pod") != lane:
        raise ValidationError("payload.lane_id is not bound to task.pod")

    rights_manifest = _upstream_rights_manifest(task)
    idea_id = require_nonempty_string(payload.get("idea_id"), "payload.idea_id")
    if rights_manifest["idea_id"] != idea_id:
        raise ValidationError("rights_manifest is not bound to payload.idea_id")
    decision = rights_manifest["decision"]
    if decision["passed"] is not True:
        raise ValidationError("upstream rights_manifest has not passed")
    if decision["needs_human_review"] is not False:
        raise ValidationError("upstream rights_manifest still needs human review")
    if decision["missing_asset_ids"]:
        raise ValidationError("upstream rights_manifest still has missing assets")

    allow_network_downloads = _boolean_env(
        "VIDEO_FACTORY_MEDIA_ALLOW_RIGHTS_DOWNLOADS", False
    )
    media_inputs = _media_inputs(
        payload,
        rights_manifest,
        allow_network_downloads=allow_network_downloads,
    )
    runtime_root = _runtime_root()
    output_root = _configured_root(
        "VIDEO_FACTORY_MEDIA_OUTPUT_ROOT",
        runtime_root / "frozen_media",
        create=True,
    )
    job_root = (output_root / job_id).resolve()
    if job_root.parent != output_root:
        raise ValidationError("media output escaped the configured output root")
    manifest_existed = (job_root / "frozen_media_manifest.json").is_file()

    try:
        frozen = freeze_explicit_media(
            rights_manifest,
            media_inputs,
            job_root,
            job_id=job_id,
            allowed_local_roots=_input_roots(runtime_root),
            max_bytes=_positive_int_env(
                "VIDEO_FACTORY_MEDIA_MAX_BYTES", DEFAULT_MAX_BYTES
            ),
            timeout_seconds=_positive_float_env(
                "VIDEO_FACTORY_MEDIA_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
            ),
            probe=False,
            allow_private_hosts=False,
        )
        artifact = frozen["artifact"]
        verify_frozen_media_manifest(
            artifact,
            rights_manifest=rights_manifest,
            expected_job_id=job_id,
        )
    except MediaFreezeError as exc:
        raise ValidationError(f"rights-bound media freeze failed: {exc}") from exc

    if not allow_network_downloads and any(
        item["source"]["kind"] != "local_file" for item in artifact["assets"]
    ):
        raise ValidationError("media_handler refuses non-local frozen assets")
    if (
        not allow_network_downloads
        and artifact["decision"]["network_downloads"] != 0
    ):
        raise ValidationError("media_handler refuses network downloads")
    manifest_path = Path(frozen["manifest_path"]).expanduser().resolve()
    if manifest_path.parent != job_root or manifest_path.name != "frozen_media_manifest.json":
        raise ValidationError("frozen media manifest is not job-scoped")
    return {
        "artifact": artifact,
        "manifest_path": str(manifest_path),
        "media_execution": {
            "provider": "rights_bound_freeze",
            "asset_count": artifact["decision"]["asset_count"],
            "cache_hits": artifact["decision"]["cache_hits"],
            "reused": manifest_existed
            or artifact["decision"]["cache_hits"]
            == artifact["decision"]["asset_count"],
            "network_access": artifact["decision"]["network_downloads"] > 0,
        },
    }


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    try:
        task = json.load(source)
        if not isinstance(task, dict):
            raise ValidationError("handler stdin must contain one JSON object")
        result = handle_task(task)
    except (
        FactoryError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        sys.stderr.write(f"media_handler_error:{type(exc).__name__}:{exc}\n")
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
