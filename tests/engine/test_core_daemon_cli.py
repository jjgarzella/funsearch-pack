"""Daemon result storage, crash recovery and CLI error reporting without a live engine."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from engine.funsearch import cli
from engine.funsearch.config import Config
from engine.funsearch.daemon import store_result
from engine.funsearch.db import Database

ROOT = Path(__file__).resolve().parents[2]


def seed_database(path, scored, *, started_at=None):
    with Database(path) as db:
        db.add_program(0, f"double f(void) {{ return {scored}; }}\n", score=scored)
        for key, value in {"started_at": started_at or time.time() - 10, "children_scored": scored,
                           "children_ok": scored, "seed_score": 0, "best_score": scored}.items():
            db.set_state(key, value)


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
        (self.run / "engine.pid").write_text(str(os.getpid()))
        self.assertIn("still running", self.recover(code=2).stderr)
        self.assertFalse((self.run / "summary.json").exists())


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
