import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from engine.funsearch.db import Database

PACK = Path(__file__).resolve().parents[2]


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = self.home / "bead.json"
        self.calls = self.home / "calls.jsonl"
        self.row = {"id": "fs-slot.1", "status": "in_progress", "assignee": "session-1",
                    "metadata": {"gc.session_id": "session-1", "gc.session_name": "worker-1",
                                 "gc.claimed_at": "yesterday", "gc.work_dir": "old/session/path",
                                 "gc.work_branch": "old-branch", "gc.routed_to": "rig/funsearch.mutator",
                                 "fs.run_dir": str(self.home / "run"), "fs.slot": "1",
                                 "fs.tasks_per_session": "2"}}
        self.store.write_text(json.dumps(self.row))
        shim = self.home / "gc"
        shim.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["FS_TEST_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\\n")
path = pathlib.Path(os.environ["FS_TEST_BEAD"])
row = json.loads(path.read_text())
if args[:2] == ["bd", "show"]:
    print(json.dumps([row]))
elif args[:2] == ["bd", "update"]:
    if os.environ.get("FS_TEST_FAIL"):
        print("guard lost ownership", file=sys.stderr)
        sys.exit(13)
    for arg in args[3:]:
        if arg.startswith("--if-assignee=") and row["assignee"] != arg.split("=", 1)[1]:
            sys.exit(13)
        if arg.startswith("--if-status=") and row["status"] != arg.split("=", 1)[1]:
            sys.exit(13)
    for arg in args[3:]:
        if arg.startswith("--status="):
            row["status"] = arg.split("=", 1)[1]
        if arg.startswith("--assignee="):
            row["assignee"] = arg.split("=", 1)[1]
        if arg.startswith("--unset-metadata="):
            row["metadata"].pop(arg.split("=", 1)[1], None)
    path.write_text(json.dumps(row))
else:
    sys.exit(9)
''')
        shim.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.home) + os.pathsep + os.environ["PATH"],
                        GC_SESSION_ID="session-1", GC_SESSION_NAME="worker-1",
                        GC_ALIAS="", FS_TEST_BEAD=str(self.store), FS_TEST_CALLS=str(self.calls))

    def call(self, command, **overrides):
        return subprocess.run([str(PACK / "bin/funsearch"), "slot", command, "fs-slot.1"],
                              cwd=self.home, env=dict(self.env, **overrides),
                              capture_output=True, text=True, timeout=10)

    def test_show_release_preserves_routing_and_clears_identity(self):
        result = self.call("show")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["tasks_per_session"], 2)
        context = self.home / ".funsearch-slot.json"
        self.assertEqual(context.stat().st_mode & 0o777, 0o600)
        result = self.call("release")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(self.store.read_text())
        self.assertEqual((row["status"], row["assignee"]), ("open", ""))
        self.assertEqual(row["metadata"]["gc.routed_to"], "rig/funsearch.mutator")
        self.assertFalse(any(key.startswith("gc.session") for key in row["metadata"]))
        self.assertNotIn("gc.claimed_at", row["metadata"])
        self.assertNotIn("gc.work_dir", row["metadata"])
        self.assertNotIn("gc.work_branch", row["metadata"])
        self.assertFalse(context.exists())
        self.assertEqual(json.loads((self.home / ".funsearch-retired.json").read_text()),
                         {"session": "session-1", "bead": "fs-slot.1"})
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertIn("--if-assignee=session-1", calls[-1])
        self.assertIn("--if-status=in_progress", calls[-1])
        self.assertFalse(any("drain-ack" in call for call in calls))

    def test_close_is_guarded_terminal_update(self):
        result = self.call("close")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.store.read_text())["status"], "closed")
        update = json.loads(self.calls.read_text().splitlines()[-1])
        self.assertIn("--if-assignee=session-1", update)
        self.assertIn("--if-status=in_progress", update)
        self.assertNotIn("--force", update)

    def test_show_returns_unfinished_task_for_fresh_session_recovery(self):
        run = self.home / "run"
        run.mkdir()
        with Database(run / "db.sqlite") as db:
            task = db.add_task(0, [], slot="1")
            db.reserve_trial(task.id, 3)
            db.add_task(0, [], slot="2")
        result = self.call("show")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pending_task"], {
            "id": task.id, "dir": str(run / "tasks" / str(task.id)), "trials_used": 1})

    def test_finish_hook_already_closed_slot_is_a_read_only_success(self):
        self.row["status"] = "closed"
        self.store.write_text(json.dumps(self.row))
        result = self.call("close")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["bd", "show"])

    def test_wrong_owner_and_invalid_metadata_never_mutate(self):
        for change in ({"assignee": "other"}, {"status": "open"},
                       {"metadata": dict(self.row["metadata"], **{"gc.session_id": "other"})},
                       {"metadata": dict(self.row["metadata"], **{"fs.run_dir": "relative"})},
                       {"metadata": dict(self.row["metadata"], **{"fs.tasks_per_session": "0"})}):
            with self.subTest(change=change):
                self.store.write_text(json.dumps(dict(self.row, **change)))
                self.calls.unlink(missing_ok=True)
                result = self.call("release")
                self.assertEqual(result.returncode, 2, result.stderr)
                calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
                self.assertEqual(len(calls), 1)

    def test_update_failure_preserves_context_and_claim(self):
        self.assertEqual(self.call("show").returncode, 0)
        result = self.call("release", FS_TEST_FAIL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("guard lost ownership", result.stderr)
        self.assertEqual(json.loads(self.store.read_text())["status"], "in_progress")
        self.assertTrue((self.home / ".funsearch-slot.json").exists())
        self.assertFalse((self.home / ".funsearch-retired.json").exists())

    def test_missing_session_does_not_call_gc(self):
        self.assertEqual(self.call("show", GC_SESSION_ID="").returncode, 2)
        self.assertFalse(self.calls.exists())
