from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from video_factory.codex_schema import materialize_codex_schema, strict_codex_schema
from video_factory.contracts import contracts_dir
from video_factory.errors import ValidationError


class CodexSchemaTests(unittest.TestCase):
    def test_strictens_objects_and_drops_unsupported_annotations(self) -> None:
        value = strict_codex_schema(
            {
                "$schema": "ignored",
                "type": "object",
                "properties": {
                    "version": {"const": "1.0", "format": "date"},
                    "tags": {
                        "type": "array",
                        "items": {"enum": ["a", "b"]},
                        "uniqueItems": True,
                    },
                },
            }
        )
        self.assertEqual(value["required"], ["version", "tags"])
        self.assertFalse(value["additionalProperties"])
        self.assertEqual(value["properties"]["version"]["type"], "string")
        self.assertEqual(value["properties"]["tags"]["items"]["type"], "string")
        self.assertNotIn("format", json.dumps(value))
        self.assertNotIn("uniqueItems", json.dumps(value))

    def test_rejects_combinator_instead_of_silently_weakening_it(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unsupported combinators"):
            strict_codex_schema(
                {"type": "object", "properties": {}, "allOf": [{"type": "object"}]}
            )

    def test_materializes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.json"
            destination = root / "nested" / "provider.json"
            source.write_text(
                json.dumps({"type": "object", "properties": {"ok": {"type": "boolean"}}}),
                encoding="utf-8",
            )
            result = materialize_codex_schema(source, destination)
            self.assertEqual(result, destination.resolve())
            loaded = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(loaded["required"], ["ok"])

    def test_all_autonomous_contracts_convert_to_strict_provider_schemas(self) -> None:
        for name in (
            "claim_ledger.schema.json",
            "safety_gate_report.schema.json",
            "rights_manifest.schema.json",
            "script_package.schema.json",
            "shotlist.schema.json",
        ):
            with self.subTest(name=name):
                source = json.loads((contracts_dir() / name).read_text(encoding="utf-8"))
                converted = strict_codex_schema(source)
                encoded = json.dumps(converted)
                self.assertNotIn("uniqueItems", encoded)
                self.assertNotIn('"format"', encoded)
                self.assertEqual(
                    set(converted["required"]), set(converted["properties"])
                )


if __name__ == "__main__":
    unittest.main()
