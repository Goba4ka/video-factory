from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_factory.fish_audio import FishAudioAuthError, load_api_key


class FishAudioSecretFileTestCase(unittest.TestCase):
    def test_file_secret_precedes_environment_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "fish_key"
            secret.write_text("server-secret\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"FISH_API_KEY_FILE": str(secret), "FISH_API_KEY": "env-secret"},
                clear=False,
            ):
                self.assertEqual(load_api_key(), "server-secret")

    def test_explicit_missing_secret_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with patch.dict(
                os.environ,
                {"FISH_API_KEY_FILE": str(missing), "FISH_API_KEY": "env-secret"},
                clear=False,
            ):
                with self.assertRaises(FishAudioAuthError):
                    load_api_key()


if __name__ == "__main__":
    unittest.main()
