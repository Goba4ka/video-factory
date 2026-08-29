from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from video_factory.artifact_store import ArtifactStore
from video_factory.errors import NotFoundError, ValidationError


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ArtifactStore(Path(self.temporary.name) / "store")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def put(self, kind: str, payload: dict, dependencies=None) -> dict:
        return self.store.put(
            job_id="job_000001",
            kind=kind,
            payload=payload,
            producer=f"{kind}_agent",
            producer_version="1.0.0",
            dependencies=dependencies,
            validate_contract=False,
        )

    def test_put_is_idempotent_and_content_is_verified(self) -> None:
        first = self.put("script", {"text": "hello"})
        replay = self.put("script", {"text": "hello"})
        self.assertEqual(first["artifact_id"], replay["artifact_id"])
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.read(first["artifact_id"]), {"text": "hello"})

        alternate_producer = self.store.put(
            job_id="job_000001",
            kind="script",
            payload={"text": "hello"},
            producer="recovery_agent",
            producer_version="2.0.0",
            validate_contract=False,
        )
        # Producer/prompt/model metadata is identity-bearing. Reprocessing the
        # same bytes with a changed producer must create a new auditable version
        # and supersede the former active pointer.
        self.assertNotEqual(first["artifact_id"], alternate_producer["artifact_id"])
        self.assertEqual(alternate_producer["version"], 2)
        self.assertEqual(len(self.store.list(status="active")), 1)

    def test_upstream_metadata_change_invalidates_downstream_transitively(self) -> None:
        research = self.store.put(
            job_id="job_000001",
            kind="research",
            payload={"claim": "stable"},
            producer="research_agent",
            producer_version="1.0.0",
            metadata={"source_snapshot_sha256": "a" * 64},
            validate_contract=False,
        )
        script = self.put("script", {"text": "stable"}, [research])
        render = self.put("render", {"path": "stable.mp4"}, [script])

        replacement = self.store.put(
            job_id="job_000001",
            kind="research",
            payload={"claim": "stable"},
            producer="research_agent",
            producer_version="1.0.0",
            metadata={"source_snapshot_sha256": "b" * 64},
            validate_contract=False,
        )

        self.assertEqual(replacement["sha256"], research["sha256"])
        self.assertNotEqual(replacement["metadata_sha256"], research["metadata_sha256"])
        statuses = {item["artifact_id"]: item["status"] for item in self.store.list()}
        self.assertEqual(statuses[research["artifact_id"]], "superseded")
        self.assertEqual(statuses[script["artifact_id"]], "invalidated")
        self.assertEqual(statuses[render["artifact_id"]], "invalidated")

    def test_new_upstream_version_invalidates_downstream_transitively(self) -> None:
        research = self.put("research", {"claim": "v1"})
        script = self.put("script", {"text": "v1"}, [research])
        render = self.put("render", {"path": "v1.mp4"}, [script])

        replacement = self.put("research", {"claim": "v2"})
        self.assertEqual(replacement["version"], 2)
        statuses = {item["artifact_id"]: item["status"] for item in self.store.list()}
        self.assertEqual(statuses[research["artifact_id"]], "superseded")
        self.assertEqual(statuses[script["artifact_id"]], "invalidated")
        self.assertEqual(statuses[render["artifact_id"]], "invalidated")

    def test_unrelated_job_is_not_invalidated(self) -> None:
        first = self.put("research", {"claim": "v1"})
        other = self.store.put(
            job_id="job_000002",
            kind="script",
            payload={"text": "other"},
            producer="script_agent",
            producer_version="1.0.0",
            dependencies=[],
            validate_contract=False,
        )
        self.put("research", {"claim": "v2"})
        self.assertEqual(self.store.current(job_id="job_000002", kind="script")["artifact_id"], other["artifact_id"])
        self.assertNotEqual(first["sha256"], other["sha256"])

    def test_identical_hash_in_another_job_does_not_cross_invalidate(self) -> None:
        upstream_one = self.put("research", {"claim": "identical"})
        upstream_two = self.store.put(
            job_id="job_000002",
            kind="research",
            payload={"claim": "identical"},
            producer="research_agent",
            producer_version="1.0.0",
            validate_contract=False,
        )
        downstream_two = self.store.put(
            job_id="job_000002",
            kind="script",
            payload={"text": "must remain active"},
            producer="script_agent",
            producer_version="1.0.0",
            dependencies=[upstream_two],
            validate_contract=False,
        )
        self.put("research", {"claim": "replacement"})
        self.assertEqual(
            self.store.current(job_id="job_000002", kind="script")["artifact_id"],
            downstream_two["artifact_id"],
        )
        self.assertEqual(upstream_one["sha256"], upstream_two["sha256"])

    def test_dependencies_must_resolve_to_matching_active_artifacts(self) -> None:
        ghost = {
            "artifact_id": "art_missing",
            "job_id": "job_000001",
            "kind": "research",
            "sha256": "0" * 64,
        }
        with self.assertRaisesRegex(NotFoundError, "not found"):
            self.put("script", {"text": "ghost"}, [ghost])

        upstream = self.put("research", {"claim": "v1"})
        self.put("research", {"claim": "v2"})
        with self.assertRaisesRegex(ValidationError, "not active"):
            self.put("script", {"text": "stale"}, [upstream])

    def test_same_payload_with_new_dependency_has_one_active_version(self) -> None:
        upstream_v1 = self.put("research", {"claim": "v1"})
        script_v1 = self.put("script", {"text": "stable copy"}, [upstream_v1])
        upstream_v2 = self.put("research", {"claim": "v2"})
        script_v2 = self.put("script", {"text": "stable copy"}, [upstream_v2])
        self.assertNotEqual(script_v1["artifact_id"], script_v2["artifact_id"])
        active = self.store.list(job_id="job_000001", kind="script", status="active")
        self.assertEqual([item["artifact_id"] for item in active], [script_v2["artifact_id"]])

    def test_reverting_payload_reactivates_exact_immutable_artifact(self) -> None:
        v1 = self.put("script", {"text": "v1"})
        self.put("script", {"text": "v2"})
        reverted = self.put("script", {"text": "v1"})
        self.assertEqual(reverted["artifact_id"], v1["artifact_id"])
        self.assertEqual(reverted["status"], "active")
        self.assertEqual(
            [item["artifact_id"] for item in self.store.list(status="active")],
            [v1["artifact_id"]],
        )

    def test_concurrent_writers_preserve_one_active_pointer(self) -> None:
        def write(index: int) -> str:
            store = ArtifactStore(Path(self.temporary.name) / "store")
            return store.put(
                job_id="job_000001",
                kind="script",
                payload={"text": f"v{index % 2}"},
                producer="script_agent",
                producer_version="1.0.0",
                validate_contract=False,
            )["artifact_id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(24)))
        records = self.store.list(job_id="job_000001", kind="script")
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(item["status"] == "active" for item in records), 1)

    def test_tampering_is_detected(self) -> None:
        record = self.put("script", {"text": "original"})
        path = Path(self.temporary.name) / "store" / record["path"]
        path.write_text(json.dumps({"text": "tampered"}), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "sha256"):
            self.store.read(record["artifact_id"])

    def test_identity_metadata_tampering_is_detected(self) -> None:
        self.store.put(
            job_id="job_000001",
            kind="script",
            payload={"text": "original"},
            producer="script_agent",
            producer_version="1.0.0",
            metadata={"source_snapshot": "a" * 64},
            validate_contract=False,
        )
        index_path = Path(self.temporary.name) / "store" / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["artifacts"][0]["metadata"]["source_snapshot"] = "b" * 64
        index_path.write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "metadata sha256"):
            self.store.list()

    def test_producer_identity_tampering_is_detected(self) -> None:
        self.put("script", {"text": "original"})
        index_path = Path(self.temporary.name) / "store" / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["artifacts"][0]["producer_version"] = "tampered"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "identity sha256"):
            self.store.list()

    def test_contract_validation_is_enabled_for_canonical_kinds(self) -> None:
        with self.assertRaisesRegex(ValidationError, "missing required fields"):
            self.store.put(
                job_id="job_000001",
                kind="idea_card",
                payload={"schema_version": "1.0.0"},
                producer="scout_agent",
                producer_version="1.0.0",
            )

    def test_path_components_cannot_escape_store(self) -> None:
        with self.assertRaisesRegex(ValidationError, "only letters"):
            self.store.put(
                job_id="../outside",
                kind="script",
                payload={"text": "x"},
                producer="script_agent",
                producer_version="1.0.0",
                validate_contract=False,
            )


if __name__ == "__main__":
    unittest.main()
