from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from video_factory.contracts import CONTRACT_FILES


FACTORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CONTRACTS = len(CONTRACT_FILES)


@unittest.skipUnless(
    os.environ.get("VIDEO_FACTORY_RUN_WHEEL_SMOKE") == "1",
    "set VIDEO_FACTORY_RUN_WHEEL_SMOKE=1 for the isolated wheel-install smoke",
)
class WheelInstallSmokeTests(unittest.TestCase):
    def probe_install(self, target: Path, *, cwd: Path) -> dict[str, object]:
        probe = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                (
                    "import json,sys;"
                    f"sys.path.insert(0,{str(target)!r});"
                    "from video_factory.contracts import "
                    "CONTRACT_FILES,contracts_dir,load_contract;"
                    "print(json.dumps({'count':len(CONTRACT_FILES),"
                    "'root':str(contracts_dir()),"
                    "'loaded':sorted(load_contract(name)['title'] "
                    "for name in CONTRACT_FILES)}))"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return json.loads(probe.stdout)

    def test_installed_wheel_contains_and_loads_every_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheelhouse = root / "wheelhouse"
            target = root / "installed"
            wheelhouse.mkdir()
            target.mkdir()
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    str(FACTORY_ROOT),
                    "--no-deps",
                    "--wheel-dir",
                    str(wheelhouse),
                ],
                check=True,
            )
            wheels = list(wheelhouse.glob("video_factory_control-*.whl"))
            self.assertEqual(len(wheels), 1)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    str(wheels[0]),
                    "--no-deps",
                    "--target",
                    str(target),
                ],
                check=True,
            )
            result = self.probe_install(target, cwd=root)
            self.assertEqual(result["count"], EXPECTED_CONTRACTS)
            self.assertEqual(len(result["loaded"]), EXPECTED_CONTRACTS)
            self.assertTrue(Path(result["root"]).is_dir())

    def test_sdist_contains_contracts_and_installs_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_lib = root / "build-lib"
            dist = root / "dist"
            target = root / "installed"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "build",
                    "--target",
                    str(build_lib),
                ],
                check=True,
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join(
                [str(build_lib), environment.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--sdist",
                    "--outdir",
                    str(dist),
                    str(FACTORY_ROOT),
                ],
                check=True,
                env=environment,
            )
            archives = list(dist.glob("video_factory_control-*.tar.gz"))
            self.assertEqual(len(archives), 1)
            with tarfile.open(archives[0], "r:gz") as archive:
                packaged_schemas = [
                    name
                    for name in archive.getnames()
                    if "/src/video_factory/schemas/" in name
                    and name.endswith(".schema.json")
                ]
            self.assertEqual(len(packaged_schemas), EXPECTED_CONTRACTS)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    str(archives[0]),
                    "--no-deps",
                    "--target",
                    str(target),
                ],
                check=True,
            )
            result = self.probe_install(target, cwd=root)
            self.assertEqual(result["count"], EXPECTED_CONTRACTS)
            self.assertEqual(len(result["loaded"]), EXPECTED_CONTRACTS)
            self.assertTrue(Path(result["root"]).is_dir())


if __name__ == "__main__":
    unittest.main()
