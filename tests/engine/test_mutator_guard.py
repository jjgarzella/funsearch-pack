import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engine.funsearch.db import Database
from scripts.mutator_guard import authorize
from scripts.mutator_setup import setup

PACK = Path(__file__).resolve().parents[2]


class MutatorGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mutator test ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.run = self.home / "problem/runs/one"
        self.run.mkdir(parents=True)
        self.db = Database(self.run / "db.sqlite")
        self.addCleanup(self.db.close)
        self.task = self.db.add_task(0, [], slot="1")
        self.other = self.db.add_task(0, [], slot="2")
        self.directory = self.run / "tasks" / str(self.task.id)
        self.directory.mkdir(parents=True)
        self.context = {"session": "session-1", "bead": "fs-slot.1", "run_dir": str(self.run),
                        "slot": "1", "tasks_per_session": 2}
        (self.home / ".funsearch-slot.json").write_text(json.dumps(self.context))
        self.env = patch.dict(os.environ, GC_SESSION_ID="session-1")
        self.env.start()
        self.addCleanup(self.env.stop)

    def check(self, tool, **inputs):
        authorize({"tool_name": tool, "tool_input": inputs}, self.home, PACK)

    def command(self, *argv):
        self.check("Bash", command=shlex.join(argv))

    def test_all_loop_commands_and_current_task_files(self):
        self.command("gc", "hook", "--claim", "--drain-ack", "--json")
        self.command("gc", "runtime", "drain-ack")
        cli = str(PACK / "bin/funsearch")
        for verb in ("show", "release", "close"):
            self.command(cli, "slot", verb, "fs-slot.1")
        for verb in ("try", "submit"):
            self.command(cli, verb, str(self.run), str(self.task.id), str(self.directory / "child.c"))
        for tool in ("Read", "Write", "Edit"):
            self.check(tool, file_path=str(self.directory / "child.c"))
        self.check("Read", file_path=str(self.directory / "TASK.md"))
        self.db.close_task(self.task.id)
        self.command(cli, "next-task", str(self.run), "--slot", "1")

    def test_denies_shell_composition_and_other_commands(self):
        for command in ("gc hook --claim --json; cat /etc/passwd", "gc hook --claim --json > x",
                        "gc hook --claim --json\ngit status", "gc hook $(cat secret) --json",
                        "gc hook `id` --json", "gc hook --claim --json && id", "gc bd close fs-slot.1",
                        "gc hook --claim --json [ab]", "gc hook --claim --json !id",
                        "gc hook other-agent --claim --json", "gc hook --claim --json --inject",
                        "git status", "python3 -c 'print(1)'", str(PACK / "bin/funsearch") + " run start /tmp"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.check("Bash", command=command)

    def test_denies_evaluator_settings_cross_slot_and_wrong_task(self):
        for tool, path in (("Read", self.run / "../../evaluator/source.c"),
                           ("Write", self.home / ".claude/settings.json"),
                           ("Read", self.home / ".funsearch-slot.json"),
                           ("Write", self.directory / "TASK.md"),
                           ("Read", self.run / "tasks" / str(self.other.id) / "TASK.md")):
            with self.subTest(tool=tool, path=path), self.assertRaises(ValueError):
                self.check(tool, file_path=str(path))
        cli = str(PACK / "bin/funsearch")
        for argv in ((cli, "next-task", str(self.run), "--slot", "1"),
                     (cli, "submit", str(self.run), str(self.other.id), str(self.directory / "child.c")),
                     (cli, "slot", "release", "other-bead")):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                self.command(*argv)
        with self.assertRaises(ValueError):
            self.check("WebSearch", query="evaluator")

    def test_symlinks_and_stale_session_are_denied(self):
        (self.directory / "child.c").symlink_to(self.home / "secret.c")
        with self.assertRaises(ValueError):
            self.check("Read", file_path=str(self.directory / "child.c"))
        with patch.dict(os.environ, GC_SESSION_ID="other"), self.assertRaises(ValueError):
            self.check("Read", file_path=str(self.directory / "TASK.md"))

    def test_installed_hook_emits_allow_and_fail_closed_deny(self):
        setup(self.home, PACK)
        settings = json.loads((self.home / ".claude/settings.json").read_text())
        argv = shlex.split(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        for event, expected in (({"tool_name": "Read", "tool_input": {
                "file_path": str(self.directory / "TASK.md")}}, "allow"),
                ({"tool_name": "Bash", "tool_input": {"command": "cat /etc/passwd"}}, "deny"),
                ({}, "deny")):
            result = subprocess.run(argv, input=json.dumps(event), text=True, capture_output=True,
                                    env=dict(os.environ, GC_SESSION_ID="session-1"), timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], expected)
