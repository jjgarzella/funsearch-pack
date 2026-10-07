"""Real CLI/daemon/worker integration, with a scripted C mutator."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from engine.funsearch import daemon
from engine.funsearch.compile import compile_candidate
from engine.funsearch.db import Database
from engine.funsearch.evaluator import evaluator_digest
from engine.funsearch.runtime import engine_alive, hold_engine_lock, pid_alive
from engine.funsearch.workers import score_budget_s
from .pipeline_support import PipelineTestCase, ROOT


CLI = ROOT / "bin" / "funsearch"


class EndToEndTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        # Use the portable evaluator command when copying the toy fixture.
        config = self.problem / "problem.toml"
        text = config.read_text()
        text = re.sub(r'^build = .*$', "build = " + json.dumps(self.cfg.evaluator.build), text, flags=re.M)
        config.write_text(text)
        self.runs = []
        self.addCleanup(self.stop_runs)

    def cli(self, *args, code=0):
        result = subprocess.run([str(CLI), *map(str, args)], capture_output=True,
                                text=True, timeout=20, cwd=self.root)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout.strip()

    def start(self, *overrides, hooks=False):
        run_id = f"test-{len(self.runs)}"
        root = self.problem / "runs" / run_id
        # Register before starting, so test failures still stop the daemon.
        self.runs.append(root)
        args = ["run", "start", self.problem, "--run-id", run_id,
                "--set", "search.islands=2", "--set", "search.workers=1",
                "--set", "search.trial_budget=2", "--set", "stop.duration_s=60"]
        for override in overrides:
            args += ["--set", override]
        if hooks:
            script = self.root / "hook.py"
            script.write_text("import os,sys\nfrom pathlib import Path\n"
                              "assert sys.argv[2] == os.environ['FS_RUN_DIR']\n"
                              "Path(sys.argv[2], sys.argv[1]).touch()\n")
            args += ["--on-start", f"python3 {shlex.quote(str(script))} started.marker",
                     "--on-finish", f"python3 {shlex.quote(str(script))} finished.marker"]
        output = self.cli(*args)
        self.assertEqual(output, f"{run_id} {root}")
        self.assertTrue((root / "engine.pid").exists())
        return root

    def stop_runs(self):
        for root in self.runs:
            if (root / "engine.pid").exists():
                subprocess.run([str(CLI), "stop", str(root)], capture_output=True, timeout=10)
                deadline = time.monotonic() + 10
                while (root / "engine.pid").exists() and time.monotonic() < deadline:
                    time.sleep(0.03)
                if (root / "engine.pid").exists():
                    os.kill(int((root / "engine.pid").read_text()), signal.SIGTERM)
            if (root / "engine.log").exists():
                self.exit_evidence(root)

    def task(self, root):
        output = self.cli("next-task", root, "--slot", "scripted")
        _, task_id, directory = output.split(" ", 2)
        directory = Path(directory)
        self.assertTrue((directory / "TASK.md").exists())
        return int(task_id), directory

    def child(self, directory, value=1, *, source=None):
        path = directory / "child.c"
        path.write_text(source or f"// IDEA: improve to {value}\n#include \"candidate.h\"\ndouble f(void) {{ return {value}; }}\n")
        return path

    def assert_requests_compiled_out(self, root, sources):
        """Every finished request dropped its library and kept its source."""
        requests = root / "requests"
        self.assertEqual(list(requests.glob("*/*.so")), [])
        self.assertEqual(len(list(requests.glob("*/candidate.c"))), sources)

    def finished(self, root, status="completed"):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if (root / "summary.json").exists() and not (root / "engine.pid").exists():
                summary = json.loads((root / "summary.json").read_text())
                self.assertEqual(summary["status"], status, (root / "engine.log").read_text())
                return summary
            time.sleep(0.03)
        self.fail((root / "engine.log").read_text())

    def test_scripted_mutator_ten_children_and_outputs(self):
        root = self.start("stop.max_children=10", hooks=True)
        self.assertTrue((root / "started.marker").exists())
        self.assertEqual((root.parent / ".gitignore").read_text(), "*\n")
        with Database(root / "db.sqlite", readonly=True) as db:
            self.assertEqual(db.connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(db.get_program(0).source, (self.problem / "seed.c").read_text())
            # The engine publishes the deadlines its waiting clients use.
            claim_timeout_s = score_budget_s(self.cfg.evaluator.timeout_s) + daemon.CLAIM_SLACK_S
            self.assertEqual(db.get_state("claim_timeout_s"), claim_timeout_s)
            self.assertEqual(db.get_state("end_by"), db.get_state("started_at") + 60
                             + daemon.STOP_GRACE_S + claim_timeout_s)
        engine_pid = int((root / "engine.pid").read_text())
        # Linux process groups let us verify both pools have been shut down.
        process_text = subprocess.check_output(["ps", "-eo", "pid,ppid,args"], text=True)
        workers = [int(line.split()[0]) for line in process_text.splitlines()[1:]
                   if int(line.split()[1]) == engine_pid and "funsearch-worker" in line]
        self.assertEqual(len(workers), 2)
        for value in range(1, 11):
            task_id, directory = self.task(root)
            # Parent + 1, skipping globally known behaviours from other islands.
            with Database(root / "db.sqlite") as db:
                task = db.get_task(task_id)
                parent_best = max(db.get_program(i).score for i in task.parent_ids)
                known = {p.score for p in db.list_programs(status="OK")}
            candidate_value = parent_best + 1
            while candidate_value in known:
                candidate_value += 1
            self.assertEqual(candidate_value, value)
            child = self.child(directory, candidate_value)
            self.assertTrue(self.cli("try", root, task_id, child).startswith("RESULT OK"))
            self.assertTrue(self.cli("submit", root, task_id, child).startswith("ACCEPTED"))
            self.assertFalse((root / "db.sqlite-shm").exists())
            self.assertFalse((root / "db.sqlite-wal").exists())
        self.cli("next-task", root, code=3)
        summary = self.finished(root)
        for key in ("run_id", "instance", "status", "reason", "started_at", "ended_at",
                    "children_scored", "children_ok", "ok_rate", "best_score", "seed_score",
                    "best_program_id", "throughput_per_hour", "islands"):
            self.assertIn(key, summary)
        self.assertEqual(summary["reason"], "max_children")
        self.assertEqual(summary["children_scored"], 10)
        self.assertEqual(summary["children_ok"], 10)
        self.assertEqual(summary["best_score"], 10)
        self.assertEqual(summary["ok_rate"], 1)
        self.assertIn("return 10", (root / "best.c").read_text())
        self.assertEqual(len(list((root / "top").glob("*.c"))), 10)
        self.assert_requests_compiled_out(root, 20)
        self.assertTrue((root / "finished.marker").exists())
        self.assertFalse(pid_alive(engine_pid))
        self.assertTrue(all(not pid_alive(pid) for pid in workers))
        self.assertFalse(json.loads(self.cli("run", "status", root))["engine_alive"])
        self.assertEqual(json.loads(self.cli("best", root, "-k", 1))[0]["score"], 10)
        self.assertEqual(json.loads(self.cli("rescore", root, "best", "--instance", "n=2,scale=2"))["score"], 20)
        with sqlite3.connect(next((root / "snapshots").glob("*.sqlite"))) as saved:
            self.assertEqual(json.loads(saved.execute("SELECT value FROM state WHERE key='status'").fetchone()[0]), "completed")
        self.cli("try", root, task_id, child, code=3)
        self.cli("submit", root, task_id, child, code=3)

    def test_rejections_budget_compile_error_and_crash_recovery(self):
        root = self.start("stop.max_children=100")
        task_id, directory = self.task(root)
        child = self.child(directory, 1)
        self.cli("submit", root, task_id, child)
        other, directory = self.task(root)
        duplicate = self.child(directory, 1)
        self.cli("submit", root, other, duplicate, code=4)
        # A different normalized source with identical behaviour is rejected
        # authoritatively after scoring, without a retained program row.
        duplicate.write_text("double f(void) { double x = 1; return x; }\n")
        self.cli("submit", root, other, duplicate, code=4)
        with Database(root / "db.sqlite") as db:
            self.assertEqual(db.get_task(other).status, "open")
            self.assertEqual(len(db.list_programs()), 3)
        duplicate.write_text("broken seed or child\n")
        self.cli("submit", root, other, duplicate, code=4)
        self.assertIn("RESULT ERROR", self.cli("try", root, other, duplicate))
        self.child(directory, 2)
        self.cli("try", root, other, duplicate)
        self.cli("try", root, other, duplicate, code=4)
        with Database(root / "db.sqlite") as db:
            self.assertEqual(len(db.list_trials(other)), 2)
            self.assertEqual(db.get_task(other).status, "open")
        crash = self.child(directory, source="double f(void) { *(volatile int*)0 = 1; return 0; }\n")
        self.assertIn(" ERROR ", self.cli("submit", root, other, crash))
        task_id, directory = self.task(root)
        self.cli("submit", root, task_id, self.child(directory, 3))
        self.cli("stop", root)
        summary = self.finished(root, "stopped")
        self.assertEqual(summary["best_score"], 3)
        # Scored, failed, and rejected requests alike; the try over budget
        # was refused before it made a request.
        self.assert_requests_compiled_out(root, 8)
        with Database(root / "db.sqlite") as db:
            self.assertEqual(len(db.list_programs(status="ERROR")), 1)

    def test_distinct_stringified_macro_arguments_are_both_scored(self):
        root = self.start("stop.max_children=2")
        for expression, expected in (("a+b", 4), ("a + b", 6)):
            task, directory = self.task(root)
            child = self.child(directory, source=f"#define STR(x) #x\n"
                               f"double f(void) {{ return sizeof(STR({expression})); }}\n")
            output = self.cli("submit", root, task, child)
            self.assertEqual(output.split()[-2:], ["OK", str(expected)])
        self.assertEqual(self.finished(root)["best_score"], 6)

    def test_rescore_uses_requested_instance_for_init_and_scoring(self):
        root = self.start("stop.max_children=1")
        task, directory = self.task(root)
        self.cli("submit", root, task, self.child(directory, 2))
        self.finished(root)
        result = json.loads(self.cli("rescore", root, "best", "--instance", "scale=3"))
        self.assertEqual(result["score"], 6)
        self.assertEqual(result["sig"], [6, 1])
        self.cli("rescore", root, "best", "--instance", "fail=1", code=1)
        # Rescoring is observational; it leaves the stored score unchanged.
        self.assertEqual(json.loads(self.cli("best", root, "-k", 1))[0]["score"], 2)

    def test_check_broken_seed_config_errors_and_init_failure(self):
        self.assertEqual(json.loads(self.cli("check", self.problem))["status"], "OK")
        (self.problem / "seed.c").write_text("broken c\n")
        self.assertEqual(json.loads(self.cli("check", self.problem, code=1))["status"], "ERROR")
        self.cli("run", "start", self.problem, code=1)
        self.assertFalse((self.problem / "runs").exists())
        self.cli("check", self.root / "missing", code=2)
        self.cli("run", "start", self.problem, "--set", "stop.max_children=0", code=2)
        (self.problem / "seed.c").write_text("double f(void) { return -1; }")
        self.assertEqual(json.loads(self.cli("check", self.problem, code=1))["status"], "INVALID")
        self.cli("check", self.problem, "--instance", "fail=1", code=1)

    def test_duration_plateau_and_stop_without_children(self):
        root = self.start("stop.duration_s=1")
        self.assertEqual(self.finished(root)["reason"], "duration_s")
        root = self.start("stop.plateau_children=2")
        for value in (-1, -2):
            task_id, directory = self.task(root)
            self.assertIn(" INVALID ", self.cli("submit", root, task_id, self.child(directory, value)))
        self.assertEqual(self.finished(root)["reason"], "plateau_children")
        root = self.start(hooks=True)
        self.cli("stop", root)
        self.cli("next-task", root, code=3)
        self.assertEqual(self.finished(root, "stopped")["children_scored"], 0)
        self.assertTrue((root / "finished.marker").exists())

    def test_run_id_validation_and_existing_run(self):
        for run_id in ("", "..", ".", "a/b", "../escape", "/absolute", "line\nbreak", "line\rbreak"):
            with self.subTest(run_id=run_id):
                self.cli("run", "start", self.problem, "--run-id", run_id, code=2)
        self.assertFalse((self.problem / "runs").exists())
        self.assertFalse((self.problem / "escape").exists())
        root = self.start()
        before = (root / "run.json").read_text()
        self.cli("run", "start", self.problem, "--run-id", root.name, code=2)
        self.assertEqual((root / "run.json").read_text(), before)

    def test_run_scores_with_its_own_evaluator_snapshot(self):
        root = self.start()
        metadata = json.loads((root / "run.json").read_text())
        library = Path(metadata["evaluator_library"])
        self.assertEqual(library.parent, root / "evaluator")
        self.assertEqual(metadata["evaluator_sha256"], evaluator_digest(library.parent))
        # Rebuilding or breaking the problem's evaluator cannot reach the run:
        # a fresh rescore worker still loads the run-owned copy.
        Path(metadata["evaluator_source"]).write_bytes(b"not a shared library")
        task_id, directory = self.task(root)
        self.assertIn("ACCEPTED", self.cli("submit", root, task_id, self.child(directory, 2)))
        self.assertEqual(json.loads(self.cli("rescore", root, "best", "--instance", "n=1"))["status"], "OK")

    def test_same_task_concurrent_submissions_score_once(self):
        root = self.start()
        task_id, directory = self.task(root)
        slow = self.child(directory, source="#include <unistd.h>\ndouble f(void) { sleep(2); return 1; }\n")
        second = directory / "second.c"
        second.write_text("double f(void) { return 2; }\n")
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(self.cli, "submit", root, task_id, slow)
            self.wait_queue(root, 1)
            self.assertEqual(self.cli("submit", root, task_id, second, code=4),
                             "REJECTED task already has a pending submission")
            self.assertIn("ACCEPTED", first.result())
        # The second request compiled but was never queued.
        self.assert_requests_compiled_out(root, 2)
        with Database(root / "db.sqlite") as db:
            self.assertEqual(db.get_state("children_scored"), 1)
            self.assertEqual(db.get_task(task_id).status, "done")

    def test_closed_task_rejects_try_and_submit(self):
        root = self.start()
        task_id, directory = self.task(root)
        self.assertIn("ACCEPTED", self.cli("submit", root, task_id, self.child(directory, 1)))
        other = self.child(directory, 2)
        for verb in ("try", "submit"):
            with self.subTest(verb=verb):
                self.assertEqual(self.cli(verb, root, task_id, other, code=4),
                                 f"REJECTED task {task_id} is not open")
        with Database(root / "db.sqlite") as db:
            self.assertEqual(db.get_state("children_scored"), 1)
            self.assertEqual(db.list_trials(task_id), [])

    def test_atomic_trial_reservations_and_parallel_submissions(self):
        root = self.start("search.workers=2", "stop.max_children=2")
        task_id, directory = self.task(root)
        child = self.child(directory, 1)
        def attempt(_):
            return subprocess.run([str(CLI), "try", str(root), str(task_id), str(child)],
                                  capture_output=True, text=True, timeout=20)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(attempt, range(4)))
        self.assertEqual(sorted(r.returncode for r in results), [0, 0, 4, 4], results)
        with Database(root / "db.sqlite") as db:
            self.assertEqual(db.get_task(task_id).trials_used, 2)
            self.assertEqual(len(db.list_trials(task_id)), 2)
        other, other_dir = self.task(root)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.cli, "submit", root, tid, path) for tid, path in
                       [(task_id, child), (other, self.child(other_dir, 2))]]
            for future in futures:
                future.result()
        self.assertEqual(self.finished(root)["children_scored"], 2)

    def test_run_opens_even_if_launcher_dies_during_start_hook(self):
        root = self.problem / "runs" / "orphaned"
        self.runs.append(root)
        hook = self.root / "slow_hook.py"
        hook.write_text("import sys, time\nfrom pathlib import Path\n"
                        "Path(sys.argv[1], 'hook.started').touch()\ntime.sleep(2)\n"
                        "Path(sys.argv[1], 'started.marker').touch()\n")
        launcher = subprocess.Popen(
            [str(CLI), "run", "start", str(self.problem), "--run-id", root.name,
             "--set", "search.islands=2", "--set", "search.workers=1", "--set", "stop.duration_s=60",
             "--on-start", f"{sys.executable} {shlex.quote(str(hook))}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while not (root / "hook.started").exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        self.assertTrue((root / "hook.started").exists())
        launcher.kill()
        launcher.wait(timeout=5)
        # The daemon owns the start hook, so the run still opens for business:
        # requests queue while the hook runs and are scored after it finishes.
        task_id, directory = self.task(root)
        self.assertIn("ACCEPTED", self.cli("submit", root, task_id, self.child(directory, 2)))
        self.assertTrue((root / "started.marker").exists())
        self.assertIn("launcher exited before readiness", (root / "engine.log").read_text())

    def test_on_start_failure_still_finalizes_and_invokes_finish(self):
        root = self.problem / "runs" / "hook-fails"
        self.runs.append(root)
        hook = self.root / "finish.py"
        hook.write_text("import sys\nfrom pathlib import Path\nPath(sys.argv[1], 'finished.marker').touch()\n")
        self.cli("run", "start", self.problem, "--run-id", root.name,
                 "--on-start", "false", "--on-finish",
                 f"{sys.executable} {shlex.quote(str(hook))}", code=1)
        summary = self.finished(root, "failed")
        self.assertIn("on-start hook", summary["reason"])
        self.assertTrue((root / "finished.marker").exists())
        self.assertTrue((root / "best.c").exists())

    def tuned_start(self, name, tuning, *overrides):
        """Shorten wall-clock maintenance intervals without changing semantics."""
        root = self.problem / "runs" / name
        self.runs.append(root)
        bootstrap = ("import sys; "
                     f"sys.path.insert(0, {str(ROOT / 'engine')!r}); "
                     "import funsearch.daemon as daemon; " + tuning + "; "
                     "from funsearch.cli import main; sys.exit(main(sys.argv[1:]))")
        args = [sys.executable, "-c", bootstrap, "run", "start", str(self.problem),
                "--run-id", name, "--set", "search.islands=2", "--set", "search.workers=1",
                "--set", "evaluator.timeout_s=5", "--set", "stop.duration_s=60"]
        for override in overrides:
            args += ["--set", override]
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return root

    def wait_queue(self, root, count, state="running"):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with Database(root / "db.sqlite") as db:
                n = db.connection.execute("SELECT COUNT(*) FROM evalq WHERE state=?", (state,)).fetchone()[0]
            if n >= count:
                return
            time.sleep(0.02)
        self.fail("evaluation queue did not reach expected state")

    def test_stop_finishes_inflight_and_rejects_queued(self):
        root = self.start()
        first, first_dir = self.task(root)
        second, second_dir = self.task(root)
        slow = self.child(first_dir, source="#include <unistd.h>\ndouble f(void) { sleep(1); return 1; }\n")
        with ThreadPoolExecutor(max_workers=2) as executor:
            inflight = executor.submit(self.cli, "submit", root, first, slow)
            self.wait_queue(root, 1)
            queued = executor.submit(self.cli, "submit", root, second, self.child(second_dir, 2), code=3)
            self.wait_queue(root, 1, "queued")
            self.cli("stop", root)
            self.assertIn("ACCEPTED", inflight.result())
            self.assertEqual(queued.result(), "RUN_OVER")
        summary = self.finished(root, "stopped")
        self.assert_requests_compiled_out(root, 2)  # Including the cancelled one.
        self.assertEqual(summary["children_scored"], 1)
        with Database(root / "db.sqlite") as db:
            self.assertEqual(db.get_task(first).status, "done")
            self.assertEqual(db.get_task(second).status, "open")

    def test_shutdown_deadline_interrupts_long_evaluation(self):
        root = self.tuned_start("deadline", "daemon.STOP_GRACE_S = 0.1")
        task_id, directory = self.task(root)
        source = "#include <unistd.h>\ndouble f(void) { sleep(10); return 1; }\n"
        with ThreadPoolExecutor(max_workers=1) as executor:
            inflight = executor.submit(self.cli, "submit", root, task_id, self.child(directory, source=source))
            self.wait_queue(root, 1)
            started = time.monotonic()
            self.cli("stop", root)
            self.assertIn(" ERROR ", inflight.result())
        self.assertLess(time.monotonic() - started, 3)
        summary = self.finished(root, "stopped")
        self.assertEqual(summary["children_scored"], 1)
        self.assertEqual(summary["children_ok"], 0)

    def test_permanent_worker_failure_mid_run_fails_the_run(self):
        hook = self.root / "finish.py"
        hook.write_text("import sys\nfrom pathlib import Path\nPath(sys.argv[1], 'finished.marker').touch()\n")
        mark = self.root / "init-fails"
        root = self.problem / "runs" / "worker-fails"
        self.runs.append(root)
        with patch.dict(os.environ, {"TOY_INIT_FAIL_MARK": str(mark)}):
            self.cli("run", "start", self.problem, "--run-id", root.name,
                     "--set", "search.workers=1", "--set", "stop.duration_s=60",
                     "--set", 'evaluator.env=["TOY_INIT_FAIL_MARK"]',
                     "--on-finish", f"{sys.executable} {shlex.quote(str(hook))}")
        first, first_dir = self.task(root)
        second, second_dir = self.task(root)
        crash = self.child(first_dir, source="#include <signal.h>\n#include <unistd.h>\n"
                           "double f(void) { sleep(1); raise(SIGSEGV); return 1; }\n")
        with ThreadPoolExecutor(max_workers=2) as executor:
            inflight = executor.submit(self.cli, "submit", root, first, crash, code=3)
            self.wait_queue(root, 1)
            queued = executor.submit(self.cli, "submit", root, second, self.child(second_dir, 2), code=3)
            self.wait_queue(root, 1, "queued")
            # The crashed worker's replacement exits 3 in fs_init: the
            # submit pool is permanently broken while the run is live.
            mark.touch()
            started = time.monotonic()
            # Neither client waits for end_by: both learn the run is over.
            self.assertEqual(inflight.result(), "RUN_OVER")
            self.assertEqual(queued.result(), "RUN_OVER")
        self.assertLess(time.monotonic() - started, 10)
        summary = self.finished(root, "failed")
        # Whichever the engine sees first: the fatal line or exit code 3.
        self.assertRegex(summary["reason"], "fs_init returned 1|worker exited with code 3")
        self.assertEqual(summary["children_scored"], 0)
        self.assertTrue((root / "best.c").exists())
        self.assertTrue((root / "finished.marker").exists())

    def test_transient_database_lock_keeps_a_scored_result_pending(self):
        root = self.start()
        task_id, _ = self.task(root)
        # Enqueue directly: a waiting CLI client would itself fail on the lock.
        request = root / "requests" / "locked"
        request.mkdir(parents=True)
        source = request / "candidate.c"
        source.write_text("#include <unistd.h>\ndouble f(void) { sleep(1); return 2; }\n")
        ok, library, log = compile_candidate(self.cfg, source, request, "final")
        self.assertTrue(ok, log)
        with Database(root / "db.sqlite") as db:
            (programs,) = db.connection.execute("SELECT COUNT(*) FROM programs").fetchone()
            with db.transaction():
                evaluation = db.enqueue("submit", source, library, task_id=task_id)
        self.wait_queue(root, 1)
        # Held past two 5 s busy timeouts: the tick at ~5 s finds the score
        # done and its store_result fails at ~10 s, so the unstored result
        # must survive in pending until the lock is released.
        with closing(sqlite3.connect(root / "db.sqlite", isolation_level=None)) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            time.sleep(11.5)
            connection.execute("COMMIT")
        deadline = time.monotonic() + 10
        with Database(root / "db.sqlite") as db:
            while (done := db.get_evaluation(evaluation.id)).state != "done":
                self.assertLess(time.monotonic(), deadline, "result was never stored")
                time.sleep(0.05)
            self.assertEqual(done.result["status"], "OK")
            self.assertEqual(done.result["score"], 2)
            self.assertIn("program_id", done.result)
            self.assertEqual(db.get_state("children_scored"), 1)
            self.assertEqual(db.get_task(task_id).status, "done")
            # Stored exactly once: neither dropped nor double-counted.
            self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM programs").fetchone()[0],
                             programs + 1)
        self.assertGreaterEqual((root / "engine.log").read_text().count("database busy, retrying"), 2)
        self.assertFalse(library.exists())
        self.cli("stop", root)
        self.finished(root, "stopped")

    def test_database_lock_beyond_the_retry_budget_fails_the_run(self):
        # A zero budget fails on the first tick that outlasts the 5 s busy timeout.
        root = self.tuned_start("busy", "daemon.BUSY_RETRY_S = 0")
        with closing(sqlite3.connect(root / "db.sqlite", isolation_level=None)) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            time.sleep(6)
            connection.execute("COMMIT")
        summary = self.finished(root, "failed")
        self.assertRegex(summary["reason"], r"^database busy for 0s: database is locked")

    def test_maintenance_abandonment_reset_and_snapshot_retention(self):
        root = self.tuned_start("maintenance", "daemon.SNAPSHOT_PERIOD_S = 0.1; "
                                "daemon.ABANDON_PERIOD_S = 0.1", "search.reset_period_s=0.15")
        task_id, _ = self.task(root)
        with Database(root / "db.sqlite") as db:
            db.connection.execute("UPDATE tasks SET created_at=? WHERE id=?", (time.time() - 2000, task_id))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with Database(root / "db.sqlite") as db:
                if (db.get_task(task_id).status == "abandoned" and db.get_state("snapshots", 0) >= 7
                        and db.get_state("island_resets", 0) >= 2):
                    break
            time.sleep(0.03)
        else:
            self.fail("maintenance did not complete")
        self.cli("stop", root)
        self.finished(root, "stopped")
        snapshots = list((root / "snapshots").glob("*.sqlite"))
        self.assertEqual(len(snapshots), 5)
        with Database(root / "db.sqlite") as db:
            last = db.get_state("snapshots")
        with sqlite3.connect(root / "snapshots" / f"db-{last}.sqlite") as saved:
            self.assertEqual(json.loads(saved.execute("SELECT value FROM state WHERE key='status'").fetchone()[0]), "stopped")

    def test_parallel_requests_cannot_overshoot_child_limit(self):
        root = self.start("search.workers=3", "stop.max_children=1")
        requests = []
        for value in (1, 2, 3):
            task_id, directory = self.task(root)
            source = f"#include <unistd.h>\ndouble f(void) {{ usleep(400000); return {value}; }}\n"
            requests.append((task_id, self.child(directory, source=source)))
        def submit(request):
            task_id, child = request
            return subprocess.run([str(CLI), "submit", str(root), str(task_id), str(child)],
                                  capture_output=True, text=True, timeout=20)
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(submit, requests))
        self.assertEqual(sorted(r.returncode for r in results), [0, 3, 3], results)
        self.assertEqual(self.finished(root)["children_scored"], 1)
        with Database(root / "db.sqlite") as db:
            self.assertEqual(len(db.list_tasks(status="done")), 1)
            self.assertEqual(len(db.list_tasks(status="open")), 2)

    def test_finish_hook_failure_is_reported(self):
        root = self.problem / "runs" / "finish-fails"
        self.runs.append(root)
        self.cli("run", "start", self.problem, "--run-id", root.name,
                 "--set", "stop.duration_s=0.5", "--on-finish", "false")
        summary = self.finished(root)
        self.assertEqual(summary["reason"], "duration_s")
        error = json.loads((root / "finish-hook-error.json").read_text())
        self.assertIn("on-finish hook", error["error"])
        self.assertIn("Traceback", (root / "engine.log").read_text())

    def exit_evidence(self, root):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (root / "engine-exit.json").exists():
                return json.loads((root / "engine-exit.json").read_text())
            time.sleep(0.03)
        self.fail("exit observer did not record engine status")

    def test_shutdown_signals_are_logged_and_finalize(self):
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=signum):
                root = self.start(hooks=True)
                pid = int((root / "engine.pid").read_text())
                os.kill(pid, signum)
                self.assertEqual(self.finished(root, "stopped")["reason"], "stop requested")
                self.assertTrue((root / "finished.marker").exists())
                evidence = self.exit_evidence(root)
                self.assertEqual(evidence["pid"], pid)
                self.assertEqual(evidence["exitcode"], 0)
                self.assertIsNone(evidence["signal"])
                self.assertIn(f"received {signal.Signals(signum).name}",
                              (root / "engine.log").read_text())

    @unittest.skipUnless(Path("/proc/self/environ").exists(), "Linux process environment check")
    def test_detached_processes_do_not_inherit_launcher_identity(self):
        identity = {"GC_SESSION_ID": "fs-test-launcher", "GC_SESSION_NAME": "fs-launcher",
                    "GC_AGENT": "fs-launcher", "GC_AGENT_NAME": "fs-launcher", "GC_ALIAS": "fs-launcher"}
        with patch.dict(os.environ, {**identity, "FS_NOTIFY": "test-notify", "GC_RIG": "test-rig"}):
            root = self.start("stop.max_children=1")
        pid = int((root / "engine.pid").read_text())
        observer = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        for target in (pid, observer):
            entries = Path(f"/proc/{target}/environ").read_bytes().split(b"\0")
            env = dict(entry.split(b"=", 1) for entry in entries if b"=" in entry)
            for key in identity:
                self.assertNotIn(key.encode(), env)
            self.assertEqual(env[b"FS_NOTIFY"], b"test-notify")
            self.assertEqual(env[b"GC_RIG"], b"test-rig")
        task, directory = self.task(root)
        self.cli("submit", root, task, self.child(directory))
        self.finished(root)
        self.assertEqual(self.exit_evidence(root)["exitcode"], 0)

    def test_waiting_client_fails_promptly_when_engine_dies(self):
        root = self.start()
        pid = int((root / "engine.pid").read_text())
        process_text = subprocess.check_output(["ps", "-eo", "pid,ppid,args"], text=True)
        workers = [int(line.split()[0]) for line in process_text.splitlines()[1:]
                   if int(line.split()[1]) == pid and "funsearch-worker" in line]
        task_id, directory = self.task(root)
        slow = self.child(directory, source="#include <unistd.h>\ndouble f(void) { sleep(30); return 1; }\n")
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                waiting = executor.submit(subprocess.run, [str(CLI), "submit", str(root), str(task_id), str(slow)],
                                          capture_output=True, text=True, timeout=20)
                self.wait_queue(root, 1)
                started = time.monotonic()
                os.kill(pid, signal.SIGKILL)
                result = waiting.result()
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("engine process is not alive", result.stderr)
            self.assertNotIn("ACCEPTED", result.stdout)
            self.assertEqual(self.exit_evidence(root)["signal"], signal.SIGKILL)
        finally:
            for worker in workers:
                if pid_alive(worker):
                    os.kill(worker, signal.SIGKILL)
            (root / "engine.pid").unlink(missing_ok=True)
        self.cli("run", "recover", root)
        with Database(root / "db.sqlite") as db:
            states = db.connection.execute("SELECT state, result FROM evalq").fetchall()
            self.assertEqual(db.get_task(task_id).status, "open")
            self.assertEqual(db.get_state("children_scored"), 0)
        self.assertEqual([state for state, _ in states], ["done"])
        self.assertEqual(json.loads(states[0][1])["msg"], "engine died")
        self.assertEqual(json.loads((root / "summary.json").read_text())["status"], "failed")

    def test_uncatchable_death_and_fatal_trace_are_recorded(self):
        for signum in (signal.SIGKILL, signal.SIGABRT):
            with self.subTest(signal=signum):
                root = self.start()
                pid = int((root / "engine.pid").read_text())
                process_text = subprocess.check_output(["ps", "-eo", "pid,ppid,args"], text=True)
                workers = [int(line.split()[0]) for line in process_text.splitlines()[1:]
                           if int(line.split()[1]) == pid and "funsearch-worker" in line]
                try:
                    # The running engine holds its lock and refuses a second engine.
                    self.assertTrue(engine_alive(root))
                    self.assertTrue(json.loads(self.cli("run", "status", root))["engine_alive"])
                    with self.assertRaisesRegex(RuntimeError, "another engine holds"):
                        hold_engine_lock(root, timeout_s=0.05)
                    os.kill(pid, signum)
                    evidence = self.exit_evidence(root)
                    self.assertEqual(evidence["exitcode"], -signum)
                    self.assertEqual(evidence["signal"], signum)
                    # Its lock died with it, though its workers may still run
                    # and engine.pid now names a live process, as after reuse.
                    (root / "engine.pid").write_text(str(os.getpid()))
                    self.assertFalse(engine_alive(root))
                    self.assertFalse((root / "summary.json").exists())
                    log = (root / "engine.log").read_text()
                    self.assertIn(f"signal={signum}", log)
                    if signum == signal.SIGABRT:
                        self.assertIn("Fatal Python error: Aborted", log)
                        self.assertIn("daemon.py", log)
                finally:
                    for worker in workers:
                        if pid_alive(worker):
                            os.kill(worker, signal.SIGKILL)
                    (root / "engine.pid").unlink(missing_ok=True)
