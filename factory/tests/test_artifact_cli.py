from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from video_factory.cli import main


class ArtifactCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = self.root / "artifacts"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_json(self, name: str, payload) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run_cli(self, *args: str) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        code = main(list(args), out=out, err=err)
        return code, json.loads(out.getvalue() if code == 0 else err.getvalue())

    def put(self, *, kind: str, file: Path, metadata: Path | None = None, dependencies: Path | None = None):
        arguments = [
            "artifact-put",
            "--root",
            str(self.store),
            "--job-id",
            "job_cli_001",
            "--kind",
            kind,
            "--file",
            str(file),
            "--producer",
            f"{kind}-agent",
            "--producer-version",
            "1.0.0",
            "--skip-contract-validation",
        ]
        if metadata is not None:
            arguments += ["--metadata", str(metadata)]
        if dependencies is not None:
            arguments += ["--dependencies", str(dependencies)]
        return self.run_cli(*arguments)

    def test_cli_metadata_change_reports_transitive_invalidation(self) -> None:
        research_file = self.write_json("research.json", {"claim": "stable"})
        metadata_one = self.write_json("metadata-1.json", {"snapshot": "a" * 64})
        code, research = self.put(
            kind="research", file=research_file, metadata=metadata_one
        )
        self.assertEqual(code, 0)
        dependency_file = self.write_json(
            "dependencies.json", [research["artifact"]]
        )
        script_file = self.write_json("script.json", {"text": "stable"})
        code, script = self.put(
            kind="script", file=script_file, dependencies=dependency_file
        )
        self.assertEqual(code, 0)

        metadata_two = self.write_json("metadata-2.json", {"snapshot": "b" * 64})
        code, replacement = self.put(
            kind="research", file=research_file, metadata=metadata_two
        )
        self.assertEqual(code, 0)
        statuses = {
            item["artifact_id"]: item["status"] for item in replacement["invalidated"]
        }
        self.assertEqual(statuses[research["artifact"]["artifact_id"]], "superseded")
        self.assertEqual(statuses[script["artifact"]["artifact_id"]], "invalidated")

        code, listed = self.run_cli(
            "artifact-list", "--root", str(self.store), "--status", "invalidated"
        )
        self.assertEqual(code, 0)
        self.assertEqual(listed["count"], 1)
        code, current = self.run_cli(
            "artifact-current",
            "--root",
            str(self.store),
            "--job-id",
            "job_cli_001",
            "--kind",
            "research",
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            current["artifact"]["artifact_id"], replacement["artifact"]["artifact_id"]
        )


if __name__ == "__main__":
    unittest.main()
