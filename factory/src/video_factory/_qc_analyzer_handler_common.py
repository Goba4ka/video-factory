"""Trusted host boundary for pixel-level QC analyzer handlers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from ._qc_analyzer_common import file_sha256, safe_id
from .errors import ValidationError


_FORBIDDEN_PAYLOAD_FIELDS = frozenset(
    {
        "corpus_snapshot",
        "corpus_snapshot_path",
        "report_path",
        "contact_sheet_path",
        "thresholds",
        "face_observer",
        "speaker_required",
    }
)


def reject_untrusted_overrides(payload: Mapping[str, Any]) -> None:
    present = sorted(field for field in _FORBIDDEN_PAYLOAD_FIELDS if field in payload)
    if present:
        raise ValidationError(
            "analyzer task payload may not override trusted runtime settings: "
            + ", ".join(present)
        )


def evidence_paths(
    *, job_id: str, render_id: str, category: str
) -> tuple[Path, Path | None]:
    raw_root = os.environ.get("VIDEO_FACTORY_QC_EVIDENCE_ROOT")
    if not raw_root:
        raise ValidationError("VIDEO_FACTORY_QC_EVIDENCE_ROOT must be configured")
    candidate = Path(raw_root).expanduser()
    if not candidate.is_absolute():
        raise ValidationError("VIDEO_FACTORY_QC_EVIDENCE_ROOT must be absolute")
    if candidate.is_symlink():
        raise ValidationError("VIDEO_FACTORY_QC_EVIDENCE_ROOT must not be a symlink")
    root = candidate.resolve()
    if not root.is_dir():
        raise ValidationError("VIDEO_FACTORY_QC_EVIDENCE_ROOT must be an existing directory")
    directory = root / safe_id(job_id, "job_id") / safe_id(render_id, "render_id")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValidationError(f"cannot create analyzer evidence directory: {exc}") from exc
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError("analyzer evidence directory must be a regular directory")
    report = directory / f"{safe_id(category, 'category')}.json"
    if report.is_symlink():
        raise ValidationError("analyzer report path must not be a symlink")
    contact = directory / "visual-contact-sheet.pgm" if category == "visual" else None
    if contact is not None and contact.is_symlink():
        raise ValidationError("visual contact sheet path must not be a symlink")
    return report.resolve(), contact.resolve() if contact is not None else None


def configured_snapshot_descriptor() -> dict[str, str]:
    raw_path = os.environ.get("VIDEO_FACTORY_DEDUP_CORPUS_SNAPSHOT")
    if not raw_path:
        raise ValidationError("VIDEO_FACTORY_DEDUP_CORPUS_SNAPSHOT must be configured")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise ValidationError("VIDEO_FACTORY_DEDUP_CORPUS_SNAPSHOT must be absolute")
    if candidate.is_symlink():
        raise ValidationError("VIDEO_FACTORY_DEDUP_CORPUS_SNAPSHOT must not be a symlink")
    path = candidate.resolve()
    if not path.is_file() or path.suffix.lower() != ".json":
        raise ValidationError(
            "VIDEO_FACTORY_DEDUP_CORPUS_SNAPSHOT must be an existing JSON file"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValidationError(f"cannot inspect dedup corpus snapshot: {exc}") from exc
    if size <= 0 or size > 16 * 1024 * 1024:
        raise ValidationError("dedup corpus snapshot must contain 1..16777216 bytes")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("dedup corpus snapshot is not readable JSON") from exc
    if not isinstance(value, Mapping) or not isinstance(value.get("entries"), list):
        raise ValidationError("dedup corpus snapshot must contain an entries array")
    if not value["entries"]:
        raise ValidationError("dedup corpus snapshot entries must be non-empty")
    return {"path": str(path), "sha256": file_sha256(path)}


def require_configured_face_observer() -> Path:
    raw_path = os.environ.get("VIDEO_FACTORY_FACE_OBSERVER")
    if not raw_path:
        raise ValidationError("VIDEO_FACTORY_FACE_OBSERVER must be configured")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise ValidationError("VIDEO_FACTORY_FACE_OBSERVER must be absolute")
    if candidate.is_symlink():
        raise ValidationError("VIDEO_FACTORY_FACE_OBSERVER must not be a symlink")
    path = candidate.resolve()
    if not path.is_file():
        raise ValidationError("VIDEO_FACTORY_FACE_OBSERVER must be an existing file")
    return path


def verify_analyzer_result(
    result: Any,
    *,
    category: str,
    job_id: str,
    lane_id: str,
    render_id: str,
    render_sha256: str,
    report_path: Path,
    contact_sheet_path: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValidationError(f"{category} analyzer returned no result object")
    artifact = result.get("artifact")
    if not isinstance(artifact, dict):
        raise ValidationError(f"{category} analyzer returned no report artifact")
    expected_identity = {
        "category": category,
        "job_id": job_id,
        "lane_id": lane_id,
        "render_id": render_id,
        "render_sha256": render_sha256,
    }
    if any(artifact.get(field) != value for field, value in expected_identity.items()):
        raise ValidationError(f"{category} analyzer report identity is stale or cross-job")
    descriptor = result.get("evidence")
    if not isinstance(descriptor, Mapping) or set(descriptor) != {"path", "sha256"}:
        raise ValidationError(f"{category} analyzer evidence descriptor is invalid")
    if descriptor.get("path") != str(report_path) or not report_path.is_file():
        raise ValidationError(f"{category} analyzer evidence path is not trusted")
    actual_report_sha256 = file_sha256(report_path)
    if descriptor.get("sha256") != actual_report_sha256:
        raise ValidationError(f"{category} analyzer evidence checksum is stale")
    try:
        if report_path.stat().st_size > 8 * 1024 * 1024:
            raise ValidationError(f"{category} analyzer evidence exceeds 8 MiB")
        stored = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{category} analyzer evidence is unreadable JSON") from exc
    if stored != artifact:
        raise ValidationError(f"{category} analyzer evidence bytes differ from artifact")
    if contact_sheet_path is None:
        if "contact_sheet" in result:
            raise ValidationError("dedup analyzer must not return a contact sheet")
    else:
        contact = result.get("contact_sheet")
        if not isinstance(contact, Mapping) or set(contact) != {"path", "sha256"}:
            raise ValidationError("visual analyzer contact sheet descriptor is invalid")
        if contact.get("path") != str(contact_sheet_path) or not contact_sheet_path.is_file():
            raise ValidationError("visual analyzer contact sheet path is not trusted")
        if contact.get("sha256") != file_sha256(contact_sheet_path):
            raise ValidationError("visual analyzer contact sheet checksum is stale")
        if artifact.get("bindings", {}).get("contact_sheet_sha256") != contact.get(
            "sha256"
        ):
            raise ValidationError("visual analyzer report is not bound to contact sheet")
    return result


__all__ = [
    "configured_snapshot_descriptor",
    "evidence_paths",
    "reject_untrusted_overrides",
    "require_configured_face_observer",
    "verify_analyzer_result",
]
