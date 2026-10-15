import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from scripts.codex_mutator_setup import install_launcher

PACK = Path(__file__).resolve().parents[2]
EXTRAS = ["--dangerously-bypass-approvals-and-sandbox", "--model", "gpt-5.5",
          "-c", "model_reasoning_effort=xhigh", "resume", "session-key", "a prompt"]


class CodexProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "wd"
        locks = patch.dict(os.environ, FS_CODEX_LOCK_DIR=str(Path(self.tmp.name) / "locks"))
        locks.start()
        self.addCleanup(locks.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, executable=None):
        with patch.object(sys, "executable", executable or sys.executable):
            install_launcher(self.root, PACK)
        return self.root / ".funsearch-codex-launch"

    def test_launcher_is_fixed_and_forwards_no_arguments(self):
        launcher = self.install()
        expected = "#!/bin/sh\nexec " + shlex.join([
            sys.executable, str(PACK / "scripts/codex_mutator_launch.py"),
            str(self.root.resolve() / "slot"), str(PACK)]) + "\n"
        text = launcher.read_text()
        self.assertEqual(text, expected)
        for token in ("$@", "$*", "${", "$1"):
            self.assertNotIn(token, text)
        subprocess.run(["sh", "-n", str(launcher)], check=True, timeout=10)

    def test_launcher_drops_provider_appended_arguments(self):
        record = Path(self.tmp.name) / "argv"
        recorder = Path(self.tmp.name) / "recorder"
        recorder.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {shlex.quote(str(record))}\n")
        recorder.chmod(0o700)
        launcher = self.install(str(recorder))
        subprocess.run([str(launcher), *EXTRAS], check=True, timeout=10, cwd=self.root)
        self.assertEqual(record.read_text().splitlines(), [
            str(PACK / "scripts/codex_mutator_launch.py"), str(self.root.resolve() / "slot"), str(PACK)])

    def test_pack_provider_keeps_codex_family_and_launcher_command(self):
        pack = tomllib.loads((PACK / "pack.toml").read_text())
        self.assertEqual(pack["providers"]["funsearch-codex"], {
            "base": "builtin:codex", "command": "./.funsearch-codex-launch", "path_check": "sh",
            "resume_command": "./.funsearch-codex-launch"})

    def test_agent_uses_pack_provider_without_start_command(self):
        agent = tomllib.loads((PACK / "agents/mutator-codex/agent.toml").read_text())
        self.assertEqual(agent["provider"], "funsearch-codex")
        self.assertNotIn("start_command", agent)
        self.assertEqual(agent["prompt_mode"], "none")
        self.assertEqual(agent["process_names"], ["codex"])
        self.assertEqual(agent["lifecycle"], "one_shot")


if __name__ == "__main__":
    unittest.main()
