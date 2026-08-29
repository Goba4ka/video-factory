from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, TextIO

from .contracts import QC_REQUIRED_CATEGORIES, validate_artifact
from .errors import FactoryError, NotFoundError, ValidationError
from .queue import _verify_preview_binding
from .source_audio import is_multisource_manifest, verify_multisource_program
from .validators import canonical_json, digest_text, require_nonempty_string


_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$")
_MAX_CHAIN_TASKS = 128
_CRITICAL_CHAIN = (
    ("compiler", "project_manifest"),
    ("preview_review", "preview_approval"),
    ("render", "render_manifest"),
    ("qc_auto_evidence", "qc_auto_evidence_manifest"),
    ("caption_transcript", "caption_transcript_manifest"),
    ("captions_analyzer", "qc_analyzer_report"),
    ("facts_analyzer", "qc_analyzer_report"),
    ("policy_analyzer", "qc_analyzer_report"),
    ("dedup_analyzer", "qc_analyzer_report"),
    ("visual_analyzer", "qc_analyzer_report"),
    ("qc_evidence_gate", "qc_evidence_bundle"),
    ("qc", "qc_report"),
)


def _safe_id(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    if not _SAFE_ID_RE.fullmatch(text):
        raise ValidationError(
            f"{field} must contain only letters, digits, underscore, or hyphen"
        )
    return text


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ValidationError(f"cannot hash immutable review input {path}: {exc}") from exc
    return digest.hexdigest()


def _sha256(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValidationError(f"{field} must be a lowercase SHA-256")
    return text


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be a JSON object")
    return value


def _row_json(row: sqlite3.Row, field: str, column: str) -> dict[str, Any]:
    try:
        value = json.loads(row[column])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{field} contains invalid JSON") from exc
    return _json_object(value, field)


def _utc_timestamp(value: Any, field: str) -> str:
    text = require_nonempty_string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _existing_file(value: Any, field: str) -> Path:
    raw = Path(require_nonempty_string(value, field)).expanduser()
    if raw.is_symlink():
        raise ValidationError(f"{field} must not be a symlink")
    try:
        path = raw.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"{field} does not identify an existing file") from exc
    if not path.is_file():
        raise ValidationError(f"{field} must identify a file")
    return path


def _chain(
    connection: sqlite3.Connection, final_review: sqlite3.Row
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    seen: set[str] = set()
    dependency_id = final_review["dependency_task_id"]
    while dependency_id is not None:
        if dependency_id in seen:
            raise ValidationError("release dependency chain contains a cycle")
        seen.add(dependency_id)
        if len(seen) > _MAX_CHAIN_TASKS:
            raise ValidationError("release dependency chain exceeds the task limit")
        row = connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (dependency_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"dependency task {dependency_id!r} does not exist")
        if row["status"] != "succeeded" or row["result_json"] is None:
            raise ValidationError(f"dependency task {dependency_id!r} has not succeeded")
        if row["job_id"] != final_review["job_id"]:
            raise ValidationError("release dependency chain crosses the job boundary")
        if row["pod"] != final_review["pod"]:
            raise ValidationError("release dependency chain crosses the lane boundary")
        rows.append(row)
        dependency_id = row["dependency_task_id"]
    rows.reverse()
    return rows


def _artifact_binding(
    row: sqlite3.Row, *, job_id: str, lane: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    payload = _row_json(row, f"task {row['id']} payload", "payload_json")
    result = _row_json(row, f"task {row['id']} result", "result_json")
    contract = payload.get("required_result_contract")
    if not isinstance(contract, str) or not contract:
        raise ValidationError(
            f"successful upstream task {row['id']!r} has no result contract"
        )
    artifact = _json_object(
        result.get("artifact"), f"task {row['id']} result.artifact"
    )
    validate_artifact(contract, artifact)
    if payload.get("job_id") != job_id or payload.get("lane_id") != lane:
        raise ValidationError(
            f"task {row['id']!r} payload is not bound to final review job/lane"
        )
    if artifact.get("job_id") not in {None, job_id}:
        raise ValidationError(
            f"task {row['id']!r} artifact crosses the final review job boundary"
        )
    for key in ("lane_id", "lane", "pod"):
        if key in artifact and artifact[key] != lane:
            raise ValidationError(
                f"task {row['id']!r} artifact crosses the final review lane boundary"
            )
    binding = {
        "task_id": row["id"],
        "dependency_task_id": row["dependency_task_id"],
        "role": row["role"],
        "contract": contract,
        "artifact_sha256": digest_text(canonical_json(artifact)),
        "result_sha256": digest_text(canonical_json(result)),
        "completed_at": _utc_timestamp(
            row["completed_at"], f"task {row['id']} completed_at"
        ),
    }
    return binding, result, artifact


def _verify_critical_chain(
    chain: list[sqlite3.Row], bindings: list[dict[str, Any]]
) -> dict[str, int]:
    positions: dict[str, int] = {}
    for role, contract in _CRITICAL_CHAIN:
        matches = [
            index
            for index, (row, binding) in enumerate(zip(chain, bindings, strict=True))
            if row["role"] == role and binding["contract"] == contract
        ]
        if len(matches) != 1:
            raise ValidationError(
                f"release requires exactly one upstream {role}/{contract} task"
            )
        positions[role] = matches[0]
    ordered = [positions[role] for role, _ in _CRITICAL_CHAIN]
    if ordered != sorted(ordered) or any(
        right != left + 1 for left, right in zip(ordered, ordered[1:])
    ):
        raise ValidationError(
            "critical release chain must include the contiguous preview/render/evidence/QC roles"
        )
    if positions["qc"] != len(chain) - 1:
        raise ValidationError("final_review must depend directly on successful semantic QC")
    return positions


def _verify_render_inputs(
    render: Mapping[str, Any],
    project: Mapping[str, Any],
    preview: Mapping[str, Any],
) -> None:
    actual: dict[str, str] = {}
    for item in render["input_hashes"]:
        path = item["path"]
        if path in actual:
            raise ValidationError(f"render input path {path!r} is duplicated")
        actual[path] = item["sha256"]
    required = {
        "project_manifest.json": digest_text(canonical_json(project)),
        "preview_approval.json": digest_text(canonical_json(preview)),
        **{
            f"project/{item['path']}": item["sha256"]
            for item in project["files"]
        },
    }
    mismatches = sorted(
        path for path, expected in required.items() if actual.get(path) != expected
    )
    if mismatches:
        raise ValidationError(
            "render is not bound to the exact approved project inputs: "
            + ", ".join(mismatches)
        )


def _unique_role_position(
    chain: list[sqlite3.Row], bindings: list[dict[str, Any]], role: str, contract: str
) -> int:
    matches = [
        index
        for index, (row, binding) in enumerate(zip(chain, bindings, strict=True))
        if row["role"] == role and binding["contract"] == contract
    ]
    if len(matches) != 1:
        raise ValidationError(
            f"release requires exactly one upstream {role}/{contract} task"
        )
    return matches[0]


def _verify_motivation_audio_chain(
    *,
    chain: list[sqlite3.Row],
    bindings: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    project_position: int,
    qc: Mapping[str, Any],
) -> None:
    source_position = _unique_role_position(
        chain, bindings, "source_audio", "source_audio_manifest"
    )
    program_position = _unique_role_position(
        chain, bindings, "audio_mix", "program_audio_manifest"
    )
    if not source_position < program_position < project_position:
        raise ValidationError("motivation audio authority tasks are out of order")
    source = artifacts[source_position]
    program = artifacts[program_position]
    project = artifacts[project_position]
    source_manifest_sha = digest_text(canonical_json(source))
    source_audio_sha = source["checksums"]["extracted_audio_sha256"]
    expected_program_authority = {
        "contract": "source_audio_manifest",
        "manifest_sha256": source_manifest_sha,
        "audio_sha256": source_audio_sha,
        "authority": "spoken_content_and_timing",
        "tts": False,
    }
    if program["source_authority"] != expected_program_authority:
        raise ValidationError(
            "ProgramAudioManifest is not bound to exact SourceAudioManifest"
        )
    expected_project_authority = {
        "contract": "source_audio_manifest",
        "schema_version": source["schema_version"],
        "job_id": source["job_id"],
        "sha256": source_manifest_sha,
        "audio_sha256": source_audio_sha,
    }
    if project["bindings"]["authoritative_audio"] != expected_project_authority:
        raise ValidationError(
            "ProjectManifest is not bound to exact SourceAudioManifest"
        )
    program_manifest_sha = digest_text(canonical_json(program))
    expected_program_binding = {
        "contract": "program_audio_manifest",
        "schema_version": program["schema_version"],
        "job_id": program["job_id"],
        "idea_id": program["idea_id"],
        "lane_id": program["lane_id"],
        "sha256": program_manifest_sha,
        "audio_sha256": program["output_sha256"],
        "project_path": "assets/audio/program_mix.wav",
        "size_bytes": program["output_bytes"],
    }
    if project["bindings"]["program_audio"] != expected_program_binding:
        raise ValidationError(
            "ProjectManifest is not bound to exact ProgramAudioManifest"
        )
    if is_multisource_manifest(source):
        verify_multisource_program(source)
    else:
        source_path = _existing_file(
            source["extracted_audio_path"], "SourceAudioManifest.extracted_audio_path"
        )
        if _sha256_file(source_path) != source_audio_sha:
            raise ValidationError("SourceAudioManifest output bytes changed")
    program_path = _existing_file(
        program["immutable_output_path"], "ProgramAudioManifest.immutable_output_path"
    )
    if (
        program_path.stat().st_size != program["output_bytes"]
        or _sha256_file(program_path) != program["output_sha256"]
    ):
        raise ValidationError("ProgramAudioManifest output bytes changed")

    rights_check = next(
        (check for check in qc["checks"] if check["category"] == "rights"), None
    )
    if not isinstance(rights_check, Mapping):
        raise ValidationError("QC report has no rights evidence check")
    rights_evidence_path = _existing_file(
        rights_check.get("artifact"), "QC rights evidence"
    )
    try:
        rights_report = json.loads(rights_evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("QC rights evidence is not readable JSON") from exc
    if not isinstance(rights_report, dict):
        raise ValidationError("QC rights evidence must be a JSON object")
    validate_artifact("qc_analyzer_report", rights_report)
    expected_bindings = {
        "source_audio_manifest_sha256": source_manifest_sha,
        "source_audio_segment_bindings_sha256": (
            source["checksums"]["segment_bindings_sha256"]
            if is_multisource_manifest(source)
            else source_manifest_sha
        ),
        "program_audio_manifest_sha256": program_manifest_sha,
        "project_manifest_sha256": digest_text(canonical_json(project)),
    }
    for field, expected in expected_bindings.items():
        if rights_report["bindings"].get(field) != expected:
            raise ValidationError(
                f"QC rights evidence is not bound to current {field}"
            )


def _verify_qc_evidence(qc_result: Mapping[str, Any], qc: Mapping[str, Any]) -> None:
    raw_hashes = qc_result.get("evidence_sha256")
    if not isinstance(raw_hashes, Mapping) or set(raw_hashes) != QC_REQUIRED_CATEGORIES:
        raise ValidationError(
            "semantic QC result must bind exactly the eight evidence categories"
        )
    evidence_hashes = {
        category: _sha256(raw_hashes[category], f"evidence_sha256.{category}")
        for category in QC_REQUIRED_CATEGORIES
    }
    by_category = {item["category"]: item for item in qc["checks"]}
    for category in QC_REQUIRED_CATEGORIES:
        check = by_category[category]
        path = _existing_file(check.get("artifact"), f"QC {category} evidence")
        expected = evidence_hashes[category]
        if _sha256_file(path) != expected:
            raise ValidationError(f"QC {category} evidence bytes changed after QC")
        if f"#sha256={expected}" not in check["evidence"]:
            raise ValidationError(f"QC {category} report is not bound to evidence SHA")

    media_qc_sha = _sha256(
        qc_result.get("technical_media_qc_sha256"),
        "technical_media_qc_sha256",
    )
    if f"media_qc_sha256={media_qc_sha}" not in by_category["technical"]["evidence"]:
        raise ValidationError("technical QC check is not bound to FULL media QC")

    contact_sha = _sha256(
        qc_result.get("visual_contact_sheet_sha256"),
        "visual_contact_sheet_sha256",
    )
    visual_evidence = by_category["visual"]["evidence"]
    marker = "; contact_sheet="
    if marker not in visual_evidence:
        raise ValidationError("visual QC check has no contact-sheet binding")
    descriptor = visual_evidence.split(marker, 1)[1]
    path_text, separator, embedded_sha = descriptor.rpartition("#sha256=")
    if not separator or embedded_sha != contact_sha:
        raise ValidationError("visual QC contact-sheet SHA binding is invalid")
    contact_path = _existing_file(path_text, "visual contact sheet")
    if _sha256_file(contact_path) != contact_sha:
        raise ValidationError("visual contact-sheet bytes changed after QC")


def _prepare_outbox_parent(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValidationError("immutable review output escapes its outbox root") from exc
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("review outbox root must be a regular directory")
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise ValidationError(
                    f"immutable outbox parent must be a regular directory: {current}"
                )
        else:
            current.mkdir(mode=0o755)


def _immutable_json(
    root: Path, path: Path, document: Mapping[str, Any]
) -> tuple[str, bool]:
    payload = (canonical_json(dict(document)) + "\n").encode("utf-8")
    expected_sha = hashlib.sha256(payload).hexdigest()
    _prepare_outbox_parent(root, path)
    if path.is_symlink():
        raise ValidationError(f"immutable outbox path must not be a symlink: {path}")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        if not path.is_file() or path.read_bytes() != payload:
            raise ValidationError(f"immutable outbox conflict at {path}")
        return expected_sha, False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            path.chmod(0o444)
        except OSError:
            pass
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return expected_sha, True


def _guard_existing_task_index(
    path: Path, *, bundle_id: str, event_id: str
) -> None:
    """Reject a changed handoff before creating orphan bundle/event files."""

    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"immutable outbox conflict at {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"immutable outbox conflict at {path}") from exc
    if not isinstance(document, Mapping) or (
        document.get("bundle_id") != bundle_id
        or document.get("event_id") != event_id
    ):
        raise ValidationError(f"immutable outbox conflict at {path}")


class ReviewReleaseBridge:
    """Materialize an immutable handoff to a human; never approve or publish."""

    def __init__(self, db_path: str | Path, outbox_root: str | Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.outbox_root = Path(outbox_root).expanduser().resolve()

    def materialize(self, final_review_task_id: str) -> dict[str, Any]:
        task_id = _safe_id(final_review_task_id, "final_review_task_id")
        if not self.db_path.is_file():
            raise NotFoundError("queue database does not exist")
        if self.outbox_root.is_symlink():
            raise ValidationError("review outbox root must not be a symlink")

        # The reconciler is deliberately not a queue writer.  Opening SQLite in
        # URI read-only mode makes that boundary effective below the SQL layer
        # as well as through PRAGMA query_only/systemd filesystem policy.
        database_uri = self.db_path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(database_uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            final_review = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if final_review is None:
                raise NotFoundError(f"final review task {task_id!r} does not exist")
            if final_review["role"] != "final_review" or final_review["kind"] != "final_review_job":
                raise ValidationError("release bridge accepts only a final_review task")
            if final_review["status"] != "queued" or final_review["result_json"] is not None:
                raise ValidationError(
                    "final_review must remain queued and uncompleted for human review"
                )
            job_id = _safe_id(final_review["job_id"], "final_review.job_id")
            lane = _safe_id(final_review["pod"], "final_review.pod")
            payload = _row_json(final_review, "final_review payload", "payload_json")
            if payload.get("job_id") != job_id or payload.get("lane_id") != lane:
                raise ValidationError("final_review payload is not bound to its job/lane")
            if payload.get("human_gate") is not True or payload.get("checksum_bound") is not True:
                raise ValidationError(
                    "final_review must be an explicit checksum-bound human gate"
                )
            if payload.get("required_result_contract") is not None:
                raise ValidationError("final_review must not have an autonomous result contract")

            chain = _chain(connection, final_review)
            bound = [
                _artifact_binding(row, job_id=job_id, lane=lane) for row in chain
            ]
            bindings = [item[0] for item in bound]
            positions = _verify_critical_chain(chain, bindings)
            results = [item[1] for item in bound]
            artifacts = [item[2] for item in bound]

        project = artifacts[positions["compiler"]]
        preview = artifacts[positions["preview_review"]]
        render_result = results[positions["render"]]
        render = artifacts[positions["render"]]
        qc_result = results[positions["qc"]]
        qc = artifacts[positions["qc"]]

        _verify_preview_binding(preview, project)
        _verify_render_inputs(render, project, preview)
        if render["job_id"] != job_id or qc["job_id"] != job_id:
            raise ValidationError("render/QC artifacts do not match final review job_id")
        if render["render_id"] != qc["render_id"]:
            raise ValidationError("semantic QC is not bound to the final render_id")
        if (
            qc["decision"]["passed"] is not True
            or qc["decision"]["needs_human_review"] is not False
            or qc["decision"]["blocking_check_ids"]
            or any(item["status"] != "pass" for item in qc["checks"])
        ):
            raise ValidationError("semantic QC has not passed its hard gate")

        render_path = _existing_file(render_result.get("output_path"), "render.output_path")
        qc_render_path = _existing_file(
            qc_result.get("render_output_path"), "qc.render_output_path"
        )
        if render_path != qc_render_path:
            raise ValidationError("semantic QC inspected a different render path")
        render_sha = _sha256(render["output_sha256"], "render.output_sha256")
        if _sha256_file(render_path) != render_sha:
            raise ValidationError("final render bytes changed after semantic QC")
        _verify_qc_evidence(qc_result, qc)
        has_audio_authority_tasks = any(
            row["role"] in {"source_audio", "audio_mix"} for row in chain
        )
        if lane == "motivation" and (
            has_audio_authority_tasks
            or project["bindings"]["authoritative_audio"]["schema_version"] == "1.1.0"
        ):
            _verify_motivation_audio_chain(
                chain=chain,
                bindings=bindings,
                artifacts=artifacts,
                project_position=positions["compiler"],
                qc=qc,
            )

        qc_report_sha = digest_text(canonical_json(qc))
        render_manifest_sha = digest_text(canonical_json(render))
        upstream_bindings_sha = digest_text(canonical_json(bindings))
        identity = {
            "final_review_task_id": task_id,
            "job_id": job_id,
            "lane_id": lane,
            "render_sha256": render_sha,
            "qc_report_sha256": qc_report_sha,
            "upstream_bindings_sha256": upstream_bindings_sha,
            "final_review_payload_sha256": digest_text(canonical_json(payload)),
        }
        bundle_id = f"review_{digest_text(canonical_json(identity))[:24]}"
        event_id = f"reviewevt_{digest_text(bundle_id + ':pending_human_review')[:24]}"
        index_path = self.outbox_root / "by-task" / f"{task_id}.json"
        _guard_existing_task_index(
            index_path, bundle_id=bundle_id, event_id=event_id
        )
        created_at = bindings[positions["qc"]]["completed_at"]
        bundle = {
            "schema_version": "1.0.0",
            "bundle_id": bundle_id,
            "status": "pending_human_review",
            "target_role": "final_review",
            "job_id": job_id,
            "lane_id": lane,
            "final_review_task_id": task_id,
            "final_review_payload_sha256": identity[
                "final_review_payload_sha256"
            ],
            "render": {
                "render_id": render["render_id"],
                "path": str(render_path),
                "sha256": render_sha,
                "manifest_sha256": render_manifest_sha,
            },
            "qc": {
                "report_sha256": qc_report_sha,
                "report": qc,
                "evidence_sha256": {
                    category: qc_result["evidence_sha256"][category]
                    for category in sorted(QC_REQUIRED_CATEGORIES)
                },
                "visual_contact_sheet_sha256": qc_result[
                    "visual_contact_sheet_sha256"
                ],
                "technical_media_qc_sha256": qc_result[
                    "technical_media_qc_sha256"
                ],
            },
            "upstream_bindings": bindings,
            "upstream_bindings_sha256": upstream_bindings_sha,
            "manual_gate": {
                "approval_required": True,
                "automatic_approval": False,
                "final_review_task_must_be_completed_by_human": True,
                "publish_outbox_created": False,
                "external_send_performed": False,
            },
            "created_at": created_at,
        }
        bundle_path = self.outbox_root / "bundles" / bundle_id / "review_bundle.json"
        bundle_sha, bundle_created = _immutable_json(
            self.outbox_root, bundle_path, bundle
        )
        event = {
            "schema_version": "1.0.0",
            "event_id": event_id,
            "event_type": "final_review_requested",
            "status": "pending_human_review",
            "target_role": "final_review",
            "job_id": job_id,
            "lane_id": lane,
            "final_review_task_id": task_id,
            "bundle_id": bundle_id,
            "bundle_path": str(bundle_path),
            "bundle_sha256": bundle_sha,
            "render_sha256": render_sha,
            "qc_report_sha256": qc_report_sha,
            "upstream_bindings_sha256": upstream_bindings_sha,
            "final_review_payload_sha256": identity[
                "final_review_payload_sha256"
            ],
            "automatic_approval": False,
            "publish_outbox_created": False,
            "external_send_performed": False,
            "created_at": created_at,
        }
        event_path = self.outbox_root / "events" / f"{event_id}.json"
        event_sha, event_created = _immutable_json(self.outbox_root, event_path, event)
        index = {
            "schema_version": "1.0.0",
            "final_review_task_id": task_id,
            "bundle_id": bundle_id,
            "bundle_sha256": bundle_sha,
            "event_id": event_id,
            "event_sha256": event_sha,
        }
        _, index_created = _immutable_json(self.outbox_root, index_path, index)
        created = bundle_created or event_created or index_created
        if created and not (bundle_created and event_created and index_created):
            # A partial prior attempt is safe to finish only when every existing
            # immutable object matches byte-for-byte.
            created = True
        return {
            "ok": True,
            "command": "release-review-bridge",
            "created": created,
            "final_review_task_id": task_id,
            "bundle_id": bundle_id,
            "bundle_path": str(bundle_path),
            "bundle_sha256": bundle_sha,
            "event_id": event_id,
            "event_path": str(event_path),
            "event_sha256": event_sha,
            "status": "pending_human_review",
            "automatic_approval": False,
            "publish_outbox_created": False,
            "external_send_performed": False,
        }


def handle_request(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ValidationError("release bridge stdin must contain one JSON object")
    allowed = {"db_path", "outbox_root", "final_review_task_id"}
    if set(request) != allowed:
        raise ValidationError("release bridge request fields are invalid")
    return ReviewReleaseBridge(
        request["db_path"], request["outbox_root"]
    ).materialize(request["final_review_task_id"])


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    try:
        request = json.load(source)
        result = handle_request(request)
    except (
        FactoryError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        sys.stderr.write(
            f"review_release_bridge_error:{type(exc).__name__}:{exc}\n"
        )
        return 2
    target.write(canonical_json(result) + "\n")
    target.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
