from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


FACTORY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = FACTORY_ROOT.parent
SCRIPT = FACTORY_ROOT / "tools" / "bootstrap_ubuntu_server.sh"

MANAGED_UNITS = (
    "video-factory-backup.service",
    "video-factory-backup.timer",
    "video-factory-metrics.service",
    "video-factory-metrics.timer",
    "video-factory-preflight.service",
    "video-factory-provider-worker@.service",
    "video-factory-recover.service",
    "video-factory-recover.timer",
    "video-factory-review-release.service",
    "video-factory-review-release.timer",
    "video-factory-runtime-worker@.service",
    "video-factory-voice.service",
    "video-factory-worker@.service",
)


def _find_bash() -> Path | None:
    candidates = (
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _posix(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return str(resolved)
    drive = resolved.drive.rstrip(":").lower()
    rest = resolved.as_posix()[3:]
    return f"/{drive}/{rest}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UbuntuBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        bash = _find_bash()
        if bash is None:
            self.skipTest("bash is required for executable bootstrap tests")
        self.bash = bash
        # Keep the executable sandbox off OneDrive and outside MSYS' /tmp mount:
        # reparse-point, realpath and atomic-rename semantics stay faithful.
        if os.name == "nt":
            temp_parent = Path.home() / "AppData" / "Local" / "CodexBootstrapTests"
            temp_parent.mkdir(exist_ok=True)
            self.temp = Path(
                tempfile.mkdtemp(prefix="bootstrap-test-", dir=temp_parent)
            )
        else:
            self.temp = Path(tempfile.mkdtemp(prefix="bootstrap-test-"))
        self.root = self.temp / "root"
        self.root.mkdir()
        (self.root / "etc").mkdir()
        (self.root / "etc" / "os-release").write_text(
            'ID="ubuntu"\nVERSION_ID="24.04"\n', encoding="utf-8"
        )
        self.shims = self.temp / "shims"
        self.shims.mkdir()
        self._install_shims()
        self.release = self._make_release("release-1")
        self.wheel = self.temp / "staging" / "video_factory_control-0.7.0-py3-none-any.whl"
        self.wheel.parent.mkdir()
        self.wheel.write_bytes(b"approved application wheel\n")
        self.wheelhouse = self.temp / "wheelhouse"
        self.wheelhouse.mkdir()
        dependency = self.wheelhouse / "dependency-1.0-py3-none-any.whl"
        dependency.write_bytes(b"approved dependency wheel\n")
        self.manifest = self.temp / "wheelhouse.sha256"
        self.manifest.write_text(
            f"{_sha256(dependency)}  {dependency.name}\n", encoding="ascii"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def _write_executable(self, path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def _make_release(self, release_id: str) -> Path:
        release = self.root / "opt" / "video-factory" / "releases" / release_id
        systemd = release / "factory" / "deployment" / "systemd"
        tools = release / "factory" / "tools"
        systemd.mkdir(parents=True)
        tools.mkdir(parents=True)
        (release / "factory" / "pyproject.toml").write_text(
            "[project]\nname='video-factory-control'\nversion='0.7.0'\n",
            encoding="utf-8",
        )
        (tools / "server_preflight.py").write_text(
            "raise SystemExit('test shim must execute instead')\n", encoding="utf-8"
        )
        for tool in (
            "backup_server_state.sh",
            "collect_server_metrics.sh",
            "render_hyperframes.sh",
        ):
            self._write_executable(tools / tool, "#!/usr/bin/env bash\nexit 0\n")
        template = release / "factory" / "deployment" / "server.env.example"
        template.write_text(
            "VIDEO_FACTORY_CAPTION_OBSERVER_EXECUTABLE="
            "/opt/video-factory/current/.venv/bin/video-factory-caption-observer\n"
            "VIDEO_FACTORY_RUNTIME_ROOT=/var/lib/video-factory\n",
            encoding="utf-8",
        )
        for name in MANAGED_UNITS:
            (systemd / name).write_text(
                "[Unit]\nDescription=bootstrap fixture\n"
                "[Service]\nType=oneshot\n"
                "ExecStart=/opt/video-factory/current/.venv/bin/python --version\n",
                encoding="utf-8",
            )
        return release

    def _install_shims(self) -> None:
        root = shlex.quote(_posix(self.root))
        venv_python = self.temp / "venv-python-template"
        self._write_executable(
            venv_python,
            f"""#!/usr/bin/env bash
set -euo pipefail
root={root}
if [[ "${{1:-}}" == -m && "${{2:-}}" == pip ]]; then
  printf 'pip\n' >>"$root/pip.log"
  exit 0
fi
runtime_env=
previous=
for argument in "$@"; do
  if [[ "$previous" == --runtime-env ]]; then runtime_env=$argument; fi
  previous=$argument
done
env | sort >"$root/preflight-env.last"
printf '%s\n' "$runtime_env" >>"$root/preflight-runtime.log"
if [[ -e "$root/fail-candidate-preflight" && "$runtime_env" != "$root/etc/video-factory/runtime.env" ]]; then
  printf '{{"ok":false,"stage":"candidate"}}\n'
  exit 2
fi
if [[ -e "$root/fail-postflight" && "$runtime_env" == "$root/etc/video-factory/runtime.env" ]]; then
  printf '{{"ok":false,"stage":"committed"}}\n'
  exit 2
fi
printf '{{"ok":true}}\n'
""",
        )
        self._write_executable(
            self.shims / "python3",
            f"""#!/usr/bin/env bash
set -euo pipefail
[[ "${{1:-}}" == -m && "${{2:-}}" == venv && -n "${{3:-}}" ]]
mkdir -p -- "$3/bin"
cp -- {shlex.quote(_posix(venv_python))} "$3/bin/python"
chmod 0755 "$3/bin/python"
""",
        )
        self._write_executable(
            self.shims / "runuser",
            """#!/usr/bin/env bash
set -euo pipefail
while (($#)); do
  if [[ "$1" == -- ]]; then shift; break; fi
  shift
done
exec "$@"
""",
        )
        self._write_executable(
            self.shims / "systemctl",
            f"""#!/usr/bin/env bash
set -euo pipefail
root={root}
command_name=${{1:-}}
case "$command_name" in
  list-units)
    if [[ -e "$root/hold-systemctl" ]]; then
      : >"$root/systemctl-entered"
      while [[ -e "$root/hold-systemctl" ]]; do sleep 0.05; done
    fi
    [[ ! -e "$root/active-unit" ]] || printf 'video-factory-worker@research.service loaded active running fixture\n'
    ;;
  list-jobs)
    [[ ! -e "$root/active-job" ]] || printf '1 video-factory-worker@research.service start running\n'
    ;;
  list-unit-files)
    [[ ! -e "$root/enabled-unit" ]] || printf 'video-factory-worker@research.service enabled enabled\n'
    ;;
  daemon-reload)
    printf 'reload\n' >>"$root/daemon-reload.log"
    ;;
  *) printf 'unexpected systemctl command: %s\n' "$command_name" >&2; exit 97 ;;
esac
""",
        )
        self._write_executable(
            self.shims / "systemd-analyze",
            f"""#!/usr/bin/env bash
set -euo pipefail
root={root}
count=0
[[ ! -f "$root/analyze-count" ]] || count=$(cat "$root/analyze-count")
count=$((count + 1))
printf '%s\n' "$count" >"$root/analyze-count"
if [[ -f "$root/fail-analyze-on" && $(cat "$root/fail-analyze-on") == "$count" ]]; then
  printf 'injected systemd verification failure\n' >&2
  exit 9
fi
""",
        )
        self._write_executable(self.shims / "getfacl", "#!/usr/bin/env bash\nexit 0\n")
        self._write_executable(self.shims / "flock", "#!/usr/bin/env bash\nexit 0\n")
        for command in ("getent", "groupadd", "useradd", "usermod"):
            self._write_executable(self.shims / command, "#!/usr/bin/env bash\nexit 0\n")

    def _base_args(self, release: Path | None = None) -> list[str]:
        selected_release = release or self.release
        return [
            "--release",
            _posix(selected_release),
            "--wheel",
            _posix(self.wheel),
            "--wheel-sha256",
            _sha256(self.wheel),
            "--wheelhouse",
            _posix(self.wheelhouse),
            "--wheelhouse-manifest",
            _posix(self.manifest),
            "--wheelhouse-manifest-sha256",
            _sha256(self.manifest),
        ]

    def _command(self, *extra: str, release: Path | None = None) -> list[str]:
        launcher = (
            'shim=$1; shift; export PATH="$shim:$PATH"; '
            'exec bash "$@"'
        )
        return [
            str(self.bash),
            "-lc",
            launcher,
            "bootstrap-test",
            _posix(self.shims),
            _posix(SCRIPT),
            *self._base_args(release),
            *extra,
        ]

    def _run(
        self,
        *extra: str,
        release: Path | None = None,
        check: bool = False,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "VIDEO_FACTORY_BOOTSTRAP_TEST_MODE": "1",
                "VIDEO_FACTORY_BOOTSTRAP_TEST_ROOT": _posix(self.root),
            }
        )
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            self._command(*extra, release=release),
            cwd=REPO_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=check,
        )

    def _make_symlink(self, target: Path, link: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return
        command = f"ln -s -- {shlex.quote(_posix(target))} {shlex.quote(_posix(link))}"
        subprocess.run(
            [str(self.bash), "-lc", command],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _readlink(self, link: Path) -> str:
        result = subprocess.run(
            [str(self.bash), "-lc", f"readlink -f -- {shlex.quote(_posix(link))}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    def _canonical_path(self, path: Path) -> str:
        result = subprocess.run(
            [str(self.bash), "-lc", f"realpath -e -- {shlex.quote(_posix(path))}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    def test_dry_run_is_read_only_and_stage_is_idempotent(self) -> None:
        dry_run = self._run()
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIn("DRY-RUN, STAGE", dry_run.stdout)
        self.assertFalse((self.release / ".venv").exists())
        self.assertFalse((self.root / "etc" / "video-factory" / "runtime.env").exists())

        first = self._run("--apply")
        self.assertEqual(first.returncode, 0, first.stderr)
        runtime_env = self.root / "etc" / "video-factory" / "runtime.env"
        marker = self.release / ".venv" / ".video-factory-bootstrap-binding"
        self.assertTrue(runtime_env.is_file())
        self.assertTrue(marker.is_file())
        self.assertFalse((self.root / "opt" / "video-factory" / "current").exists())
        self.assertFalse(
            (self.root / "etc" / "systemd" / "system" / MANAGED_UNITS[0]).exists()
        )
        before = (runtime_env.read_bytes(), marker.read_bytes())

        second = self._run("--apply")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(before, (runtime_env.read_bytes(), marker.read_bytes()))
        self.assertEqual((self.root / "pip.log").read_text().splitlines(), ["pip"])

    def test_candidate_failure_never_changes_current_config_or_units(self) -> None:
        old_release = self._make_release("release-old")
        current = self.root / "opt" / "video-factory" / "current"
        self._make_symlink(old_release, current)
        runtime_env = self.root / "etc" / "video-factory" / "runtime.env"
        (self.root / "fail-candidate-preflight").touch()

        result = self._run("--activate", "--replace-runtime-env", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate preflight failed", result.stderr)
        self.assertEqual(self._readlink(current), self._canonical_path(old_release))
        self.assertFalse(runtime_env.exists())
        self.assertFalse(
            (self.root / "etc" / "systemd" / "system" / MANAGED_UNITS[0]).exists()
        )

    def test_postflight_failure_rolls_back_exact_previous_state(self) -> None:
        old_release = self._make_release("release-old")
        current = self.root / "opt" / "video-factory" / "current"
        self._make_symlink(old_release, current)
        runtime_env = self.root / "etc" / "video-factory" / "runtime.env"
        runtime_env.parent.mkdir(parents=True)
        runtime_env.write_text("OLD_CONFIG=1\n", encoding="utf-8")
        unit_root = self.root / "etc" / "systemd" / "system"
        unit_root.mkdir(parents=True)
        old_unit = unit_root / MANAGED_UNITS[0]
        old_unit.write_text("OLD UNIT\n", encoding="utf-8")
        absent_unit = unit_root / MANAGED_UNITS[-1]
        (self.root / "fail-postflight").touch()

        result = self._run("--activate", "--replace-runtime-env", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("automatic rollback required", result.stderr)
        self.assertEqual(self._readlink(current), self._canonical_path(old_release))
        self.assertEqual(runtime_env.read_text(encoding="utf-8"), "OLD_CONFIG=1\n")
        self.assertEqual(old_unit.read_text(encoding="utf-8"), "OLD UNIT\n")
        self.assertFalse(absent_unit.exists())
        self.assertGreaterEqual(
            len((self.root / "daemon-reload.log").read_text().splitlines()), 2
        )

    def test_symlinked_runtime_path_fails_closed(self) -> None:
        cache = self.root / "var" / "lib" / "video-factory" / "cache"
        cache.parent.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        self._make_symlink(outside, cache)

        result = self._run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink path component is forbidden", result.stderr)

    def test_concurrent_apply_is_rejected_by_lock(self) -> None:
        hold = self.root / "hold-systemctl"
        entered = self.root / "systemctl-entered"
        hold.touch()
        env = os.environ.copy()
        env.update(
            {
                "VIDEO_FACTORY_BOOTSTRAP_TEST_MODE": "1",
                "VIDEO_FACTORY_BOOTSTRAP_TEST_ROOT": _posix(self.root),
            }
        )
        first = subprocess.Popen(
            self._command("--apply"),
            cwd=REPO_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not entered.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(entered.exists(), "first bootstrap did not reach locked section")
            second = self._run("--apply")
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("another bootstrap holds", second.stderr)
        finally:
            hold.unlink(missing_ok=True)
            stdout, stderr = first.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, f"{stdout}\n{stderr}")

    def test_enabled_unit_blocks_even_when_inactive(self) -> None:
        (self.root / "enabled-unit").touch()
        result = self._run("--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disable managed unit before bootstrap", result.stderr)
        self.assertFalse((self.release / ".venv").exists())

    def test_activation_uses_clean_environment_and_exact_allowlist(self) -> None:
        result = self._run(
            "--activate",
            "--apply",
            env_extra={"ROOT_SECRET_SENTINEL": "must-not-cross-runuser"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        preflight_env = (self.root / "preflight-env.last").read_text(encoding="utf-8")
        self.assertNotIn("ROOT_SECRET_SENTINEL", preflight_env)
        installed = {
            path.name
            for path in (self.root / "etc" / "systemd" / "system").glob(
                "video-factory-*"
            )
        }
        self.assertEqual(installed, set(MANAGED_UNITS))
        self.assertNotIn("publisher", " ".join(installed))


if __name__ == "__main__":
    unittest.main()
