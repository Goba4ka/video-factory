from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from video_factory.derived_cache import DerivedCache


class DerivedCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = DerivedCache(self.root / "cache")
        self.source = self.root / "source.txt"
        self.source.write_text("alpha", encoding="utf-8")

    def test_repeat_build_is_a_content_addressed_cache_hit(self) -> None:
        calls: list[Path] = []

        def builder(output: Path) -> None:
            calls.append(output)
            output.write_bytes(b"derived-alpha")

        first = self.cache.get_or_build(
            namespace="test",
            version="1.0.0",
            sources=[self.source],
            options={"quality": "draft"},
            suffix=".bin",
            builder=builder,
        )
        second = self.cache.get_or_build(
            namespace="test",
            version="1.0.0",
            sources=[self.source],
            options={"quality": "draft"},
            suffix=".bin",
            builder=builder,
        )

        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["cache_key"], second["cache_key"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(second["path"]).read_bytes(), b"derived-alpha")

    def test_source_or_options_change_invalidates_the_entry(self) -> None:
        builds = 0

        def builder(output: Path) -> None:
            nonlocal builds
            builds += 1
            output.write_text(str(builds), encoding="ascii")

        first = self.cache.get_or_build(
            namespace="test",
            version="1",
            sources=[self.source],
            options={"mode": "one"},
            suffix=".txt",
            builder=builder,
        )
        self.source.write_text("beta-content", encoding="utf-8")
        second = self.cache.get_or_build(
            namespace="test",
            version="1",
            sources=[self.source],
            options={"mode": "one"},
            suffix=".txt",
            builder=builder,
        )
        third = self.cache.get_or_build(
            namespace="test",
            version="1",
            sources=[self.source],
            options={"mode": "two"},
            suffix=".txt",
            builder=builder,
        )

        self.assertEqual(builds, 3)
        self.assertNotEqual(first["cache_key"], second["cache_key"])
        self.assertNotEqual(second["cache_key"], third["cache_key"])

    def test_cleanup_is_non_destructive_until_execute_is_explicit(self) -> None:
        entry = self.cache.get_or_build(
            namespace="test",
            version="1",
            sources=[self.source],
            options={},
            suffix=".bin",
            builder=lambda output: output.write_bytes(b"payload"),
        )
        preview = self.cache.prune(max_bytes=0)
        self.assertTrue(preview["dry_run"])
        self.assertTrue(Path(entry["path"]).exists())
        executed = self.cache.prune(max_bytes=0, dry_run=False)
        self.assertFalse(executed["dry_run"])
        self.assertFalse(Path(entry["path"]).exists())

    def test_same_size_cache_tampering_forces_a_rebuild(self) -> None:
        builds = 0

        def builder(output: Path) -> None:
            nonlocal builds
            builds += 1
            output.write_bytes(b"good")

        first = self.cache.get_or_build(
            namespace="test",
            version="1",
            sources=[self.source],
            options={},
            suffix=".bin",
            builder=builder,
        )
        Path(first["path"]).write_bytes(b"evil")
        second = self.cache.get_or_build(
            namespace="test",
            version="1",
            sources=[self.source],
            options={},
            suffix=".bin",
            builder=builder,
        )
        self.assertEqual(builds, 2)
        self.assertFalse(second["cache_hit"])
        self.assertEqual(Path(second["path"]).read_bytes(), b"good")


if __name__ == "__main__":
    unittest.main()
