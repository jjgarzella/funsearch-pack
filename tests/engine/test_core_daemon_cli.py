"""Daemon result storage, crash recovery and CLI error reporting without a live engine."""

import contextlib
import fcntl
import io
import json
import os
import signal
import sqlite3
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engine.funsearch import cli
from engine.funsearch import daemon
from engine.funsearch.config import Config
from engine.funsearch.daemon import store_result, write_outputs
from engine.funsearch.db import Database
from engine.funsearch.runtime import engine_alive, hold_engine_lock
from tests.engine.recovery_support import recovery_process, wait_for_path

ROOT = Path(__file__).resolve().parents[2]


def seed_database(path, scored, *, started_at=None):
    with Database(path) as db:
        db.add_program(0, f"double f(void) {{ return {scored}; }}\n", score=scored)
        for key, value in {"started_at": started_at or time.time() - 10, "children_scored": scored,
                           "children_ok": scored, "seed_score": 0, "best_score": scored}.items():
            db.set_state(key, value)


class BusyRetryTests(unittest.TestCase):
    def test_positive_budget_expires_and_success_resets_it(self):
        for outcomes, expected_times in (([False] * 4, [1, 2, 3, 4]),
                                         ([False, False, True] + [False] * 4,
                                          [1, 2, 3, 4, 5, 6, 7])):
            with self.subTest(outcomes=outcomes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                seed_database(root / "db.sqlite", 0)
                cfg = Config()
                cfg.search.reset_period_s = 1000
                clock = [1]
                ticks = []
                results = iter(outcomes)

                def tick(db, _cfg):
                    self.assertEqual(db.get_state("status"), "running")
                    ticks.append(clock[0])
                    if not next(results):
                        raise sqlite3.OperationalError("database is locked")
                    return None

                def sleep(_seconds):
                    clock[0] += 1

                metadata = {"run_id": "budget", "evaluator_library": "unused"}
                with patch.object(daemon, "read_run", return_value=(root, metadata, cfg)), \
                        patch.object(daemon, "WorkerPool"), \
                        patch.object(daemon, "stop_reason", side_effect=tick), \
                        patch.object(daemon, "_dispatch"), \
                        patch.object(daemon, "snapshot"), \
                        patch.object(daemon.signal, "signal"), \
                        patch.object(daemon.time, "monotonic", side_effect=lambda: clock[0]), \
                        patch.object(daemon.time, "sleep", side_effect=sleep), \
                        contextlib.redirect_stderr(io.StringIO()):
                    daemon.serve(root, os.open(os.devnull, os.O_WRONLY),
                                 snapshot_period_s=1000, stop_grace_s=1,
                                 abandon_period_s=1000, busy_retry_s=3)
                self.assertEqual(ticks, expected_times)
                summary = json.loads((root / "summary.json").read_text())
                self.assertEqual(summary["status"], "failed")
                self.assertEqual(summary["reason"], "database busy for 3s: database is locked")


class StoreResultTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = Database(self.root / "db.sqlite")
        self.addCleanup(self.db.close)
        self.db.add_program(0, "double f(void) { return 0; }\n", score=0)
        for key, value in {"best_score": 0, "children_scored": 0, "children_ok": 0,
                           "plateau_count": 0}.items():
            self.db.set_state(key, value)

    def evaluation(self, task, value):
        source = self.root / f"child-{value}.c"
        source.write_text(f"double f(void) {{ return {value}; }}\n")
        self.db.enqueue("submit", source, self.root / "child.so", task_id=task.id)
        return self.db.claim_evaluation("submit")

    def test_task_closed_before_scoring_is_rejected_but_counted(self):
        task = self.db.add_task(0, [0], slot="1")
        evaluation = self.evaluation(task, 5)
        # Maintenance abandons the task while its submission is being scored.
        self.db.close_task(task.id, status="abandoned")
        store_result(self.db, evaluation, {"status": "OK", "score": 5, "sig": [], "msg": ""})
        result = self.db.get_evaluation(evaluation.id).result
        self.assertEqual(result["rejected"], "task is no longer open")
        self.assertNotIn("program_id", result)
        self.assertEqual(len(self.db.list_programs()), 1)
        self.assertEqual(self.db.get_task(task.id).status, "abandoned")
        # The evaluation was spent, so it counts toward max_children, and an
        # improvement that was never stored cannot reset the plateau.
        self.assertEqual(self.db.get_state("children_scored"), 1)
        self.assertEqual(self.db.get_state("children_ok"), 1)
        self.assertEqual(self.db.get_state("best_score"), 0)
        self.assertEqual(self.db.get_state("plateau_count"), 1)

    def test_accepted_submission_closes_task(self):
        task = self.db.add_task(0, [0], slot="1")
        evaluation = self.evaluation(task, 3)
        store_result(self.db, evaluation, {"status": "OK", "score": 3, "sig": [], "msg": ""})
        self.assertIn("program_id", self.db.get_evaluation(evaluation.id).result)
        self.assertEqual(self.db.get_task(task.id).status, "done")
        self.assertEqual(self.db.get_state("best_score"), 3)
        self.assertEqual(self.db.get_state("plateau_count"), 0)

    def test_cli_rejects_done_and_abandoned_tasks_before_compiling(self):
        self.db.set_state("status", "running")
        for status in ("done", "abandoned"):
            task = self.db.add_task(0)
            self.db.close_task(task.id, status=status)
            for verb in ("try", "submit"):
                args = SimpleNamespace(command=verb, task=task.id, source="unused")
                with self.subTest(status=status, verb=verb), \
                        patch.object(cli, "compile_candidate") as compile_, \
                        self.assertRaisesRegex(cli.Rejected, "is not open"):
                    cli.evaluate(args, self.root, Config(), self.db)
                compile_.assert_not_called()
            self.assertEqual(self.db.get_task(task.id).trials_used, 0)
        self.assertEqual(self.db.get_state("children_scored"), 0)

    def test_result_lines_print_a_numeric_score(self):
        self.db.set_state("status", "running")
        (self.root / "candidate.h").write_text("double f(void);\n")
        cases = [("try", {"status": "INVALID", "score": None, "msg": "bad cap"}, "RESULT INVALID 0 bad cap"),
                 ("try", {"status": "ERROR", "score": 2.5, "msg": "crash"}, "RESULT ERROR 2.5 crash"),
                 ("submit", {"status": "ERROR", "score": None, "msg": "", "program_id": 7},
                  "ACCEPTED 7 ERROR 0"),
                 ("submit", {"status": "OK", "score": 3, "msg": "", "program_id": 8}, "ACCEPTED 8 OK 3")]
        for n, (verb, result, line) in enumerate(cases):
            task = self.db.add_task(0)
            source = self.root / f"child-{n}.c"
            source.write_text(f"double f(void) {{ return {n}; }}\n")
            args = SimpleNamespace(command=verb, task=task.id, source=str(source))
            stdout = io.StringIO()
            compiled = lambda _cfg, _source, request, _mode: (True, request / "candidate.so", "")
            with self.subTest(verb=verb, result=result), \
                    patch.object(cli, "compile_candidate", side_effect=compiled), \
                    patch.object(cli, "wait_result", return_value=result), \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.evaluate(args, self.root, Config(), self.db), 0)
                self.assertEqual(stdout.getvalue(), line + "\n")


class RecoverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.run = Path(temp.name) / "run"
        (self.run / "snapshots").mkdir(parents=True)
        cfg = Config()
        cfg.search.islands = 1
        (self.run / "run.json").write_text(json.dumps(
            {"run_id": "run", "instance": "n=1", "config": cfg.to_dict()}))

    def recover(self, code=0):
        result = subprocess.run([str(ROOT / "bin" / "funsearch"), "run", "recover", str(self.run)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_corrupt_live_database_falls_back_to_newest_snapshot(self):
        older, newer = self.run / "snapshots" / "db-9.sqlite", self.run / "snapshots" / "db-10.sqlite"
        seed_database(older, 3)
        seed_database(newer, 5)
        os.utime(older, (time.time() - 100,) * 2)
        (self.run / "db.sqlite").write_bytes(b"not a database " * 100)
        result = self.recover()
        self.assertEqual(json.loads(result.stdout)["recovered_from"], str(newer))
        summary = json.loads((self.run / "summary.json").read_text())
        self.assertEqual((summary["status"], summary["reason"]), ("failed", "engine died"))
        self.assertEqual((summary["children_scored"], summary["best_score"]), (5, 5))
        self.assertIn("return 5", (self.run / "best.c").read_text())

    def test_without_any_database_or_with_live_engine(self):
        result = self.recover(code=1)
        self.assertIn("no readable run database", result.stderr)
        self.assertFalse((self.run / "summary.json").exists())
        seed_database(self.run / "db.sqlite", 1)
        # engine.pid names a live process throughout, as after PID reuse.
        (self.run / "engine.pid").write_text(str(os.getpid()))
        with hold_engine_lock(self.run):
            self.assertIn("still running", self.recover(code=2).stderr)
        self.assertFalse((self.run / "summary.json").exists())
        # Released with no exit record, as when the whole process tree is lost.
        self.recover()
        self.assertEqual(json.loads((self.run / "summary.json").read_text())["status"], "failed")

    def test_finished_run_keeps_its_outcome(self):
        seed_database(self.run / "db.sqlite", 2)
        summary = json.dumps({"status": "completed", "reason": "children budget reached"})
        (self.run / "summary.json").write_text(summary)
        self.assertIn("already finished with status completed", self.recover(code=2).stderr)
        self.assertEqual((self.run / "summary.json").read_text(), summary)
        # A summary that is not terminal (e.g. torn or never finished) still recovers.
        (self.run / "summary.json").write_text("{")
        self.recover()
        self.assertEqual(json.loads((self.run / "summary.json").read_text())["status"], "failed")

    def test_competing_recoveries_wait_and_preserve_first_publication(self):
        parent = self.run
        metadata = (parent / "run.json").read_text()
        for entry in ("cli", "direct"):
            with self.subTest(entry=entry):
                self.run = parent / entry
                self.run.mkdir()
                (self.run / "run.json").write_text(metadata)
                seed_database(self.run / "db.sqlite", 2)
                # Adapter ownership does not prevent or deadlock standalone recovery.
                with (self.run / ".gc-lifecycle.lock").open("a") as adapter:
                    fcntl.flock(adapter, fcntl.LOCK_EX)
                    first = recovery_process(self, self.run, "first")
                    wait_for_path(self, self.run / "first-publishing")
                    self.assertFalse(engine_alive(self.run))
                    with self.assertRaisesRegex(RuntimeError, "another engine holds"):
                        hold_engine_lock(self.run, timeout_s=0.05)
                    second = recovery_process(self, self.run, "second", entry)
                    wait_for_path(self, self.run / "second-attempt")
                    with self.assertRaises(subprocess.TimeoutExpired):
                        second.communicate(timeout=0.2)
                    self.assertFalse((self.run / "second-acquired").exists())
                    self.assertFalse((self.run / "second-publishing").exists())
                    (self.run / "release").touch()
                    output, errors = first.communicate(timeout=10)
                    self.assertEqual(first.returncode, 0, output + errors)
                    output, errors = second.communicate(timeout=10)
                    self.assertEqual(second.returncode, 2, output + errors)
                    self.assertIn("already finished with status failed", errors)
                self.assertTrue((self.run / "second-acquired").exists())
                self.assertFalse((self.run / "second-publishing").exists())
                self.assertEqual((self.run / "summary.json").read_bytes(),
                                 (self.run / "first-summary").read_bytes())
                with Database(self.run / "db.sqlite") as db:
                    self.assertEqual(db.get_state("ended_at"),
                                     json.loads((self.run / "first-summary").read_text())["ended_at"])

    def test_killed_recovery_releases_ownership_and_allows_retry(self):
        seed_database(self.run / "db.sqlite", 2)
        first = recovery_process(self, self.run, "first")
        wait_for_path(self, self.run / "first-publishing")
        first.kill()
        first.communicate(timeout=10)
        self.assertFalse((self.run / "summary.json").exists())
        self.recover()
        self.assertEqual((self.run / "best.c").read_text(),
                         (self.run / "top/1-2.c").read_text())
        self.assertEqual(json.loads((self.run / "summary.json").read_text())["status"], "failed")

    def test_failed_candidate_exports_do_not_publish_completion_and_can_recover(self):
        parent = self.run
        metadata = json.loads((parent / "run.json").read_text())
        cfg = Config()
        cfg.search.islands = 1
        for blocked in ("best.c", "top/1-2.c"):
            with self.subTest(blocked=blocked):
                self.run = parent / blocked.split("/")[0]
                self.run.mkdir()
                (self.run / "run.json").write_text(json.dumps(metadata))
                seed_database(self.run / "db.sqlite", 2)
                obstruction = self.run / blocked
                obstruction.mkdir(parents=True)
                with Database(self.run / "db.sqlite") as db, self.assertRaises(OSError):
                    write_outputs(db, self.run, metadata, cfg, "completed", "max_children")
                self.assertFalse((self.run / "summary.json").exists())
                obstruction.rmdir()
                self.recover()
                summary = json.loads((self.run / "summary.json").read_text())
                self.assertEqual(summary["status"], "failed")
                self.assertEqual(summary["best_score"], 2)
                self.assertEqual((self.run / "best.c").read_text(),
                                 (self.run / "top/1-2.c").read_text())
                self.assertIn("return 2", (self.run / "best.c").read_text())

    def test_interrupted_write_preserves_previous_complete_best(self):
        seed_database(self.run / 'db.sqlite', 2)
        best = self.run / 'best.c'
        previous = 'double f(void) { return 1; }\n'
        best.write_text(previous)
        real_open = Path.open

        @contextlib.contextmanager
        def interrupted_open(path, *args, **kwargs):
            with real_open(path, *args, **kwargs) as file:
                if path == best or path == best.with_name('best.c.tmp'):
                    def partial_write(text):
                        file.write(text[:10])
                        raise OSError('interrupted write')
                    yield SimpleNamespace(write=partial_write)
                else:
                    yield file

        metadata = json.loads((self.run / 'run.json').read_text())
        with Database(self.run / 'db.sqlite') as db, \
                patch.object(Path, 'open', interrupted_open), \
                self.assertRaisesRegex(OSError, 'interrupted write'):
            write_outputs(db, self.run, metadata, Config(), 'completed', 'max_children')
        self.assertEqual(best.read_text(), previous)
        self.assertFalse((self.run / 'summary.json').exists())
        self.recover()
        self.assertIn('return 2', best.read_text())

    def test_export_flush_failures_leave_completion_unpublished(self):
        parent = self.run
        for stage in ('best-file', 'top-file', 'best-directory', 'top-directory'):
            with self.subTest(stage=stage):
                self.run = parent / stage
                self.run.mkdir()
                seed_database(self.run / 'db.sqlite', 2)
                best = self.run / 'best.c'
                best.write_text('previous complete candidate\n')
                metadata = {'run_id': 'run'}
                real_fsync = os.fsync
                files = 0
                directories = 0

                def fail_flush(descriptor):
                    nonlocal files, directories
                    directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
                    if directory:
                        directories += 1
                    else:
                        files += 1
                    # Directory flushes: best.c's parent, new top/ entry's
                    # parent, then the top candidate's parent.
                    target = {'best-file': (False, 1), 'top-file': (False, 2),
                              'best-directory': (True, 1), 'top-directory': (True, 3)}[stage]
                    if (directory, directories if directory else files) == target:
                        raise OSError('injected flush failure')
                    real_fsync(descriptor)

                with Database(self.run / 'db.sqlite') as db, \
                        patch.object(daemon.os, 'fsync', side_effect=fail_flush), \
                        self.assertRaisesRegex(OSError, 'injected flush failure'):
                    write_outputs(db, self.run, metadata, Config(), 'completed', 'max_children')
                self.assertFalse((self.run / 'summary.json').exists())
                if stage == 'best-file':
                    self.assertEqual(best.read_text(), 'previous complete candidate\n')

    def test_exports_flush_and_replace_before_summary_installation(self):
        seed_database(self.run / 'db.sqlite', 2)
        events = []
        descriptors = {}
        real_open, real_os_open = Path.open, os.open
        real_fsync, real_replace = os.fsync, Path.replace

        @contextlib.contextmanager
        def track_file(path, *args, **kwargs):
            with real_open(path, *args, **kwargs) as file:
                descriptors[file.fileno()] = path
                yield file

        def track_directory(path, flags, *args, **kwargs):
            descriptor = real_os_open(path, flags, *args, **kwargs)
            descriptors[descriptor] = Path(path)
            return descriptor

        def track_flush(descriptor):
            path = descriptors[descriptor]
            real_fsync(descriptor)
            events.append(('flush', path))

        def track_replace(path, target):
            events.append(('replace', Path(target)))
            return real_replace(path, target)

        with Database(self.run / 'db.sqlite') as db, \
                patch.object(Path, 'open', track_file), \
                patch.object(daemon.os, 'open', side_effect=track_directory), \
                patch.object(daemon.os, 'fsync', side_effect=track_flush), \
                patch.object(Path, 'replace', track_replace):
            write_outputs(db, self.run, {'run_id': 'run'}, Config(), 'completed', 'max_children')
        summary_replace = events.index(('replace', self.run / 'summary.json'))
        for candidate in (self.run / 'best.c', self.run / 'top' / '1-2.c'):
            replace = events.index(('replace', candidate))
            flush = events.index(('flush', candidate.with_name(candidate.name + '.tmp')))
            directory_flush = events.index(('flush', candidate.parent), replace + 1)
            self.assertLess(flush, replace)
            self.assertLess(replace, directory_flush)
            self.assertLess(directory_flush, summary_replace)
        summary_flush = events.index(('flush', self.run / 'summary.json.tmp'))
        self.assertLess(summary_flush, summary_replace)
        self.assertEqual(events[-1], ('flush', self.run))
        self.assertEqual(json.loads((self.run / 'summary.json').read_text())['status'], 'completed')


class EngineAliveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.run = Path(temp.name)
        # A live process under the recorded PID, as after PID reuse.
        (self.run / "engine.pid").write_text(str(os.getpid()))

    def test_engine_lock_is_the_authority_over_a_reused_pid(self):
        self.assertFalse(engine_alive(self.run))  # No lock: no engine has started.
        with hold_engine_lock(self.run):
            self.assertTrue(engine_alive(self.run))
            self.assertTrue(engine_alive(self.run))  # Probes do not hold the lock.
            with self.assertRaisesRegex(RuntimeError, "another engine holds"):
                hold_engine_lock(self.run, timeout_s=0.05)
        # Released with no exit record, as when the whole process tree is lost.
        self.assertFalse(engine_alive(self.run))

    def test_killed_engine_releases_its_lock(self):
        engine = subprocess.Popen([sys.executable, "-c",
            "import sys, time; sys.path.insert(0, sys.argv[1]); "
            "from funsearch.runtime import hold_engine_lock; "
            "lock = hold_engine_lock(sys.argv[2]); print(flush=True); time.sleep(60)",
            str(ROOT / "engine"), str(self.run)], stdout=subprocess.PIPE)
        self.addCleanup(engine.wait)
        self.addCleanup(engine.kill)
        engine.stdout.readline()
        self.assertTrue(engine_alive(self.run))
        os.kill(engine.pid, signal.SIGKILL)
        engine.wait(timeout=10)
        self.assertFalse(engine_alive(self.run))


class WaitResultTests(unittest.TestCase):
    """The client waits within the deadlines the engine published, on a fake clock."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(Path(temp.name) / "db.sqlite")
        self.addCleanup(self.db.close)
        for key, value in {"status": "running", "pid": 1, "claim_timeout_s": 100,
                           "end_by": 1000}.items():
            self.db.set_state(key, value)
        self.evaluation = self.db.enqueue("try", "a.c", "a.so")
        self.now = 0.0

    def claim(self):
        self.db.claim_evaluation()
        self.db.connection.execute("UPDATE evalq SET started_at=? WHERE id=?", (self.now, self.evaluation.id))

    def wait(self, engine=lambda now: None):
        """Run wait_result; engine(now) acts for the engine after each 10 s poll."""
        def sleep(_):
            self.now += 10
            engine(self.now)
        clock = SimpleNamespace(time=lambda: self.now, sleep=sleep)
        with patch.object(cli, "time", clock), patch.object(cli, "engine_alive", return_value=True):
            return cli.wait_result(self.db, self.evaluation)

    def test_queue_time_is_not_charged(self):
        result = {"status": "OK", "score": 1, "sig": [], "msg": ""}
        def engine(now):
            if now == 500:  # Long past claim_timeout_s, though never claimed.
                self.claim()
            if now == 590:
                self.db.finish_evaluation(self.evaluation.id, result)
        self.assertEqual(self.wait(engine), result)

    def test_stuck_claimed_evaluation_times_out(self):
        self.now = 300
        self.claim()
        with self.assertRaisesRegex(RuntimeError, "timed out waiting"):
            self.wait()
        self.assertEqual(self.now, 410)

    def test_unclaimed_request_times_out_when_the_run_must_have_ended(self):
        with self.assertRaisesRegex(RuntimeError, "timed out waiting"):
            self.wait()
        self.assertEqual(self.now, 1010)

    def test_run_over_and_dead_engine(self):
        self.db.set_state("status", "stopped")
        with self.assertRaises(cli.RunOver):
            self.wait()
        self.db.set_state("status", "running")
        with patch.object(cli, "engine_alive", return_value=False), \
                self.assertRaisesRegex(RuntimeError, "engine process is not alive"):
            cli.wait_result(self.db, self.evaluation)

    def test_engine_without_published_deadlines_is_reported(self):
        self.db.connection.execute("DELETE FROM state WHERE key IN ('claim_timeout_s','end_by')")
        with self.assertRaisesRegex(RuntimeError, "published no client deadlines"):
            self.wait()
        self.assertEqual(self.now, 0)


class ErrorReportingTests(unittest.TestCase):
    def report(self, exc, **env):
        stderr = io.StringIO()
        with patch.object(cli, "dispatch", side_effect=exc), patch.dict(os.environ, env), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["stop", "unused"]), 1)
        return stderr.getvalue()

    def test_unexpected_errors_name_their_type(self):
        self.assertEqual(self.report(KeyError("pid")), "KeyError: 'pid'\n")
        self.assertEqual(self.report(RuntimeError()), "RuntimeError\n")
        self.assertEqual(self.report(KeyboardInterrupt()), "KeyboardInterrupt\n")
        self.assertIn("Traceback", self.report(ValueError("x"), FUNSEARCH_DEBUG="1"))


if __name__ == "__main__":
    unittest.main()
