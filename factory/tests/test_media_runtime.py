from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from video_factory.errors import ValidationError
from video_factory.media_qc import run_media_qc
from video_factory.media_tools import resolve_media_binary, transcode_cached


class MediaRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        try:
            ffmpeg = resolve_media_binary("ffmpeg")
            resolve_media_binary("ffprobe")
        except ValidationError as exc:
            self.skipTest(str(exc))
        self.source = self.root / "source.mp4"
        completed = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=90x160:r=30:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=1",
                "-c:v",
                "mpeg4",
                "-q:v",
                "5",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-shortest",
                str(self.source),
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest("fixture FFmpeg cannot encode MPEG-4/AAC")

    def test_proxy_and_qc_are_cached_on_second_run(self) -> None:
        cache_root = self.root / "cache"
        first_proxy = transcode_cached(
            self.source,
            mode="proxy",
            cache_root=cache_root,
            prefer_gpu=False,
            ffmpeg_threads=1,
        )
        second_proxy = transcode_cached(
            self.source,
            mode="proxy",
            cache_root=cache_root,
            prefer_gpu=False,
            ffmpeg_threads=1,
        )
        self.assertFalse(first_proxy["cache_hit"])
        self.assertTrue(second_proxy["cache_hit"])
        self.assertEqual(first_proxy["output"], second_proxy["output"])
        self.assertEqual(first_proxy["probe"]["video"]["fps"], 30.0)

        first_qc = run_media_qc(
            second_proxy["output"],
            level="fast",
            profile_name="portrait_draft",
            cache_root=cache_root,
            ffmpeg_threads=1,
        )
        second_qc = run_media_qc(
            second_proxy["output"],
            level="fast",
            profile_name="portrait_draft",
            cache_root=cache_root,
            ffmpeg_threads=1,
        )
        self.assertTrue(first_qc["technical_pass"], first_qc["failures"])
        self.assertFalse(first_qc["cache"]["hit"])
        self.assertTrue(second_qc["cache"]["hit"])
        self.assertFalse(second_qc["publish_eligible"])


if __name__ == "__main__":
    unittest.main()
