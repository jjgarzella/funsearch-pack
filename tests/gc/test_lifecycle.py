"""Actual hook entry points against a gc shim, plus a real toy-engine run."""
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tempfile
import time
import tomllib
import unittest

from engine.funsearch.config import Config
from tests.engine.pipeline_support import PipelineTestCase, ROOT

SCRIPTS = ROOT / "scripts"
SHIM = ROOT / "tests" / "gc" / "fake_gc.py"


class HookFixture:
    def setup_gc(self, root):
        self.city = root / "city with spaces"
        self.city.mkdir()
        self.shim_state = root / "shim"
        self.shim_state.mkdir()
        self.env = os.environ.copy()
        self.env.update(FS_GC=str(SHIM), FS_SHIM_DIR=str(self.shim_state),
                        GC_CITY_PATH=str(self.city), GC_RIG="example", FS_NOTIFY="operator")
        self.registry = self.city / ".gc" / "funsearch" / "active"

    def hook(self, name, *args, code=0):
        result = subprocess.run([str(SCRIPTS / f"{name}.sh"), *map(str, args)],
                                env=self.env, cwd=self.city, capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def beads(self):
        return json.loads((self.shim_state / "beads.json").read_text())

    def calls(self):
        return [json.loads(line)["args"] for line in (self.shim_state / "argv.jsonl").read_text().splitlines()]

    def mails(self):
        path = self.shim_state / "mail.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class LifecycleTests(HookFixture, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="funsearch-gc-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.setup_gc(self.root)

    def run_dir(self, name="run-one", pid=None):
        root = self.root / name
        root.mkdir()
        cfg = Config()
        cfg.search.mutators = 3
        cfg.search.tasks_per_session = 2
        metadata = {"run_id": name, "problem_dir": str(self.root / "problem"),
                    "instance": "n=1", "config": cfg.to_dict()}
        (root / "run.json").write_text(json.dumps(metadata))
        (root / "engine.pid").write_text(str(pid or os.getpid()))
        return root

    def summary(self, root, **changes):
        summary = {"run_id": root.name, "status": "completed", "reason": "max_children",
                   "best_score": 2, "seed_score": 0, "children_scored": 4,
                   "ok_rate": 0.75, "throughput_per_hour": 20}
        summary.update(changes)
        (root / "summary.json").write_text(json.dumps(summary))

    def test_start_finish_and_repeat(self):
        root = self.run_dir()
        self.hook("on-start", root)
        beads = self.beads()
        self.assertEqual(len(beads), 4)
        run = beads["test-1"]
        self.assertIn("funsearch-run", run["labels"])
        self.assertEqual(run["metadata"], {
            "fs.problem": str(self.root / "problem"), "fs.run_dir": str(root),
            "fs.instance": "n=1", "fs.pid": str(os.getpid()),
            "fs.status": "running", "fs.notify": "operator"})
        for index, row in enumerate(list(beads.values())[1:], 1):
            self.assertEqual(row["parent"], "test-1")
            self.assertEqual(row["metadata"]["fs.slot"], str(index))
            self.assertEqual(row["metadata"]["fs.tasks_per_session"], "2")
            self.assertEqual(row["metadata"]["gc.routed_to"], "example/funsearch.mutator")
            self.assertEqual(row["metadata"]["opt_model"], Config().mutator.model)
        self.assertEqual(sum(args[0] == "sling" for args in self.calls()), 3)
        entry = json.loads((self.registry / "run-one.json").read_text())
        self.assertEqual(entry, {"run_id": "run-one", "run_dir": str(root),
            "run_bead": "test-1", "rig": "example", "pid": os.getpid(), "notify": "operator"})
        self.assertEqual(json.loads((root / "run.json").read_text())["run_bead"], "test-1")
        count = len(self.calls())
        self.hook("on-start", root)
        self.assertEqual(len(self.calls()), count)
        self.summary(root)
        # Live slots are owned by mutator sessions, not by the finish hook.
        for row in beads.values():
            if row["parent"] == "test-1":
                row.update(status="in_progress", assignee="other-mutator-session")
        (self.shim_state / "beads.json").write_text(json.dumps(beads))
        # Saved context wins over the invoking sweep's unrelated rig/recipient.
        self.env.update(GC_RIG="other", FS_NOTIFY="wrong")
        self.hook("on-finish", root)
        beads = self.beads()
        self.assertTrue(all(row["status"] == "closed" for row in beads.values()))
        self.assertEqual(beads["test-1"]["metadata"]["fs.status"], "completed")
        for key, value in (("best_score", "2"), ("children_scored", "4"), ("ok_rate", "0.75")):
            self.assertEqual(beads["test-1"]["metadata"][f"fs.{key}"], value)
        self.assertIn(str(root / "best.c"), beads["test-1"]["notes"])
        self.assertIn("seed: 0", beads["test-1"]["notes"])
        self.assertEqual(self.mails()[0][2], "operator")
        self.assertFalse((self.registry / "run-one.json").exists())
        count = len(self.calls())
        self.hook("on-finish", root)
        self.hook("on-start", root)
        self.assertEqual(len(self.calls()), count)

    def test_mail_failure_is_retried_without_repeating_update_or_close(self):
        root = self.run_dir()
        self.hook("on-start", root)
        self.summary(root)
        self.env["FS_SHIM_FAIL"] = "mail"
        self.hook("on-finish", root, code=1)
        self.assertTrue((self.registry / "run-one.json").exists())
        del self.env["FS_SHIM_FAIL"]
        before = len(self.calls())
        self.hook("on-finish", root)
        self.assertFalse(any(args[:2] in (["bd", "update"], ["bd", "close"])
                             for args in self.calls()[before:]))
        self.assertEqual(len(self.mails()), 1)

    def test_partial_start_sling_retry_uses_existing_slot(self):
        root = self.run_dir()
        self.env["FS_SHIM_FAIL"] = "sling"
        self.hook("on-start", root, code=1)
        self.assertEqual(len(self.beads()), 2)
        del self.env["FS_SHIM_FAIL"]
        self.hook("on-start", root)
        self.assertEqual(len(self.beads()), 4)
        self.assertEqual(len(json.loads((root / "run.json").read_text())["fs"]["routed_slots"]), 3)

    def test_missing_context_fails_before_gc_mutation(self):
        root = self.run_dir()
        del self.env["FS_NOTIFY"]
        self.hook("on-start", root, code=1)
        self.assertFalse((self.shim_state / "argv.jsonl").exists())

    def test_city_run_id_collision_fails_before_bead_creation(self):
        root = self.run_dir()
        self.hook("on-start", root)
        other = self.run_dir("other")
        metadata = json.loads((other / "run.json").read_text())
        metadata["run_id"] = root.name
        (other / "run.json").write_text(json.dumps(metadata))
        self.hook("on-start", other, code=1)
        self.assertEqual(len(self.beads()), 4)
        self.assertEqual(json.loads((self.registry / "run-one.json").read_text())["run_dir"], str(root))

    def test_changed_engine_summary_after_hook_failure_updates_bead(self):
        root = self.run_dir()
        self.hook("on-start", root)
        self.summary(root)
        self.env["FS_SHIM_FAIL"] = "mail"
        self.hook("on-finish", root, code=1)
        del self.env["FS_SHIM_FAIL"]
        # The daemon marks the run failed if a finish hook exits nonzero.
        self.summary(root, status="failed", reason="on-finish hook: mail failed")
        self.hook("on-finish", root)
        self.assertEqual(self.beads()["test-1"]["metadata"]["fs.status"], "failed")
        self.assertIn("failed", self.mails()[0][4])

    def test_old_finish_retry_preserves_reused_run_id(self):
        root = self.run_dir()
        self.hook("on-start", root)
        self.summary(root)
        self.hook("on-finish", root)
        other = self.run_dir("later-run")
        metadata = json.loads((other / "run.json").read_text())
        metadata["run_id"] = root.name
        (other / "run.json").write_text(json.dumps(metadata))
        self.hook("on-start", other)
        self.hook("on-finish", root)
        entry = json.loads((self.registry / "run-one.json").read_text())
        self.assertEqual(entry["run_dir"], str(other))

    def test_sweep_check_empty_old_and_fresh(self):
        self.hook("sweep-check", code=1)
        self.registry.mkdir(parents=True)
        self.hook("sweep-check", code=1)
        (self.registry / "dead.json").write_text("{}")
        self.hook("sweep-check")
        stamp = self.registry.parent / "last-sweep"
        stamp.touch()
        os.utime(stamp, (time.time() - 1801,) * 2)
        self.hook("sweep-check")
        stamp.touch()
        self.hook("sweep-check", code=1)

    def test_sweep_dead_live_and_completed_finish_retry(self):
        dead = self.run_dir("dead", 99999999)
        live = self.run_dir("live")
        complete = self.run_dir("complete", 99999999)
        for root in (dead, live, complete):
            self.hook("on-start", root)
        self.summary(complete)
        self.hook("sweep")
        failed = json.loads((dead / "summary.json").read_text())
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["reason"], "engine died")
        self.assertFalse((live / "summary.json").exists())
        self.assertEqual(json.loads((complete / "summary.json").read_text())["status"], "completed")
        self.assertEqual([p.name for p in self.registry.glob("*.json")], ["live.json"])
        self.assertEqual(len(self.mails()), 2)
        self.assertTrue((self.registry.parent / "last-sweep").exists())
        self.hook("sweep")
        self.assertEqual(len(self.mails()), 2)

    def test_sweep_keeps_failed_delivery_entry_and_touches_timestamp(self):
        root = self.run_dir(pid=99999999)
        self.hook("on-start", root)
        self.env["FS_SHIM_FAIL"] = "mail"
        self.hook("sweep", code=1)
        self.assertTrue((self.registry / "run-one.json").exists())
        self.assertTrue((self.registry.parent / "last-sweep").exists())
        del self.env["FS_SHIM_FAIL"]
        self.hook("sweep")
        self.assertFalse((self.registry / "run-one.json").exists())

    def test_formula_and_order_contract(self):
        formula = tomllib.loads((ROOT / "formulas" / "funsearch-run.toml").read_text())
        self.assertEqual(len(formula["steps"]), 1)
        self.assertTrue(formula["vars"]["problem"]["required"])
        self.assertTrue(formula["vars"]["notify"]["required"])
        order = tomllib.loads((ROOT / "orders" / "funsearch-sweep.toml").read_text())["order"]
        self.assertEqual(order["trigger"], "condition")
        self.assertNotIn("pool", order)
        self.assertIn("sweep-check.sh", order["check"])


class ToyHookTests(HookFixture, PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.setup_gc(self.root)
        config = self.problem / "problem.toml"
        config.write_text(re.sub(r'^build = .*$', "build = " + json.dumps(self.cfg.evaluator.build),
                                 config.read_text(), flags=re.M))
        self.run = None
        self.addCleanup(self.stop_daemon)

    def cli(self, *args):
        result = subprocess.run([str(ROOT / "bin" / "funsearch"), *map(str, args)],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def stop_daemon(self):
        if self.run and (self.run / "engine.pid").exists():
            self.cli("stop", self.run)
            deadline = time.monotonic() + 10
            while (self.run / "engine.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.03)
            if (self.run / "engine.pid").exists():
                os.kill(int((self.run / "engine.pid").read_text()), signal.SIGKILL)

    def start(self):
        self.run = self.problem / "runs" / "gc-toy"
        self.cli("run", "start", self.problem, "--run-id", "gc-toy",
                 "--set", "search.workers=1", "--set", "search.mutators=2",
                 "--set", "stop.max_children=1", "--set", "stop.duration_s=60",
                 "--on-start", shlex.quote(str(SCRIPTS / "on-start.sh")),
                 "--on-finish", shlex.quote(str(SCRIPTS / "on-finish.sh")))

    def test_toy_engine_real_hooks(self):
        self.start()
        self.assertEqual(len(self.beads()), 3)
        _, task, directory = self.cli("next-task", self.run, "--slot", "1").split(" ", 2)
        child = Path(directory) / "child.c"
        child.write_text('double f(void) { return 2; }\n')
        self.cli("submit", self.run, task, child)
        deadline = time.monotonic() + 12
        while (self.run / "engine.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        self.assertFalse((self.run / "engine.pid").exists(), (self.run / "engine.log").read_text())
        summary = json.loads((self.run / "summary.json").read_text())
        self.assertEqual(summary["best_score"], 2)
        self.assertEqual(summary["children_scored"], 1)
        self.assertEqual(self.beads()["test-1"]["metadata"]["fs.status"], "completed")
        self.assertEqual(len(self.mails()), 1)
        self.assertFalse(list(self.registry.glob("*.json")))

    def test_launch_passes_instance_overrides_and_hooks(self):
        result = subprocess.run(["python3", str(SCRIPTS / "gc_lifecycle.py"), "launch",
                                 str(self.problem), "--notify", "launch-recipient",
                                 "--instance", "n=7", "--overrides",
                                 "search.workers=1 search.mutators=1 stop.duration_s=60"],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run_id, directory = result.stdout.strip().split(" ", 1)
        self.run = Path(directory)
        self.assertEqual(self.run.name, run_id)
        metadata = json.loads((self.run / "run.json").read_text())
        self.assertEqual(metadata["instance"], "n=7")
        self.assertEqual(metadata["config"]["search"]["mutators"], 1)
        self.assertEqual(metadata["fs"]["notify"], "launch-recipient")
        self.assertEqual(len(self.beads()), 2)

    def test_sweep_recovers_database_after_killed_engine(self):
        self.start()
        os.kill(int((self.run / "engine.pid").read_text()), signal.SIGKILL)
        from engine.funsearch.runtime import pid_alive
        deadline = time.monotonic() + 5
        while pid_alive(int((self.run / "engine.pid").read_text())) and time.monotonic() < deadline:
            time.sleep(0.03)
        self.hook("sweep")
        summary = json.loads((self.run / "summary.json").read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["best_score"], 0)
        self.assertTrue((self.run / "best.c").is_file())
        self.assertEqual(len(self.mails()), 1)
        # SIGKILL does not run the engine's PID-file cleanup.
        (self.run / "engine.pid").unlink()
