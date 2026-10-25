import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest

from scripts.mutator_setup import setup

PACK = Path(__file__).resolve().parents[2]
FS_PYTHON = PACK / "scripts" / "fs-python"


def fake_python(directory, name, version_ok):
    """A stand-in interpreter that passes or fails the 3.11 probe."""
    path = Path(directory) / name
    path.write_text("#!/bin/sh\n"
                    f'if [ "$1" = "-c" ]; then exit {0 if version_ok else 1}; fi\n'
                    f'echo "RAN {name} $*"\n')
    path.chmod(0o755)
    return path


class PythonPinTests(unittest.TestCase):
    def run_pin(self, path_dirs, fs_python=None):
        env = {"PATH": os.pathsep.join([*map(str, path_dirs), "/bin", "/usr/bin"])}
        if fs_python is not None:
            env["FS_PYTHON"] = str(fs_python)
        return subprocess.run(["/bin/sh", str(FS_PYTHON), "script.py", "arg"],
                              env=env, capture_output=True, text=True, timeout=10)

    def test_versioned_interpreter_wins_over_old_python3(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_python(directory, "python3", version_ok=False)
            fake_python(directory, "python3.12", version_ok=True)
            result = self.run_pin([directory])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "RAN python3.12 script.py arg")

    def test_gated_minimum_is_preferred(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("python3.14", "python3.13", "python3.11", "python3"):
                fake_python(directory, name, version_ok=True)
            result = self.run_pin([directory])
        self.assertEqual(result.stdout.strip(), "RAN python3.11 script.py arg")

    def test_old_interpreters_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("python3.14", "python3.13", "python3.12", "python3.11", "python3"):
                fake_python(directory, name, version_ok=False)
            result = self.run_pin([directory])
        self.assertEqual(result.returncode, 127)
        self.assertIn("no Python >= 3.11", result.stderr)
        self.assertNotIn("RAN", result.stdout)

    def test_fs_python_is_explicit_and_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            chosen = fake_python(directory, "chosen-python", version_ok=True)
            fake_python(directory, "python3.13", version_ok=True)
            result = self.run_pin([directory], fs_python=chosen)
            self.assertEqual(result.stdout.strip(), "RAN chosen-python script.py arg")
            old = fake_python(directory, "old-python", version_ok=False)
            result = self.run_pin([directory], fs_python=old)
        self.assertEqual(result.returncode, 127)
        self.assertIn("is not Python >= 3.11", result.stderr)

    def test_pack_entry_points_use_the_pin(self):
        for agent in ("mutator", "mutator-codex"):
            config = tomllib.loads((PACK / "agents" / agent / "agent.toml").read_text())
            [command] = config["pre_start"]
            self.assertTrue(shlex.split(command)[1].endswith("/scripts/fs-python"), command)
        for hook in ("on-start.sh", "on-finish.sh", "sweep.sh", "sweep-check.sh"):
            text = (PACK / "scripts" / hook).read_text()
            self.assertIn('exec sh "$script_dir/fs-python" "$script_dir/gc_lifecycle.py"', text)
            self.assertNotIn("exec python3", text)
        formula = (PACK / "formulas" / "funsearch-run.toml").read_text()
        self.assertIn("sh <pack>/scripts/fs-python <pack>/scripts/gc_lifecycle.py launch", formula)

    def test_direct_entry_points_reexec_from_old_macos_python(self):
        if sys.platform != "darwin" or not Path("/usr/bin/python3").is_file():
            self.skipTest("requires macOS system Python")
        version = subprocess.run(
            ["/usr/bin/python3", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=5)
        if version.returncode or tuple(map(int, version.stdout.strip().split("."))) >= (3, 11):
            self.skipTest("system Python is already >= 3.11")

        interpreter_dir = Path(sys.executable).resolve().parent
        if not (interpreter_dir / "python3.11").is_file():
            self.skipTest("no python3.11 next to the test interpreter")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "python3").symlink_to("/usr/bin/python3")
            env = {"PATH": os.pathsep.join((directory, str(interpreter_dir), "/usr/bin", "/bin")),
                   "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C"}
            results = [
                subprocess.run([str(PACK / "bin/funsearch"), "--help"], cwd=PACK,
                               env=env, capture_output=True, text=True, timeout=15),
                subprocess.run(["/usr/bin/python3", str(PACK / "scripts/gc_lifecycle.py"), "--help"],
                               cwd=PACK, env=env, capture_output=True, text=True, timeout=15),
            ]
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout.lower())

    def test_claude_guard_runs_under_the_setup_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            setup(directory, PACK)
            settings = json.loads((Path(directory) / ".claude" / "settings.json").read_text())
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(shlex.split(command)[0], sys.executable)


if __name__ == "__main__":
    unittest.main()
