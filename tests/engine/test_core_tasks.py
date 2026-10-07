from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from engine.funsearch.config import load_config
from engine.funsearch.db import Database
from engine.funsearch.evolve import seed_islands
from engine.funsearch.tasks import create_task

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "toy-problem"


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.cfg = load_config(FIXTURE)
        self.db = Database(self.run / "db.sqlite")
        self.addCleanup(self.db.close)
        self.seeds = seed_islands(self.db, self.cfg, (FIXTURE / "seed.c").read_text(), score=0, msg="seed message")

    def test_sections_parent_order_and_last_ten_ideas(self):
        for n in range(12):
            self.db.add_program(0, f"// IDEA: tried-{n}\nint x={n};", parent_ids=[self.seeds[0].id],
                                score=None, status="INVALID", msg="bad")
        high = self.db.add_program(0, '// IDEA: improve\ndouble f(void){return 5.0;} /* ``` */',
                                   score=5, msg="winner message", parent_ids=[self.seeds[0].id])
        task_id = create_task(self.db, self.cfg, FIXTURE, self.run, "slot-1", rng=random.Random(3))
        text = (self.run / "tasks" / str(task_id) / "TASK.md").read_text()
        task = self.db.get_task(task_id)
        self.assertEqual(task.slot, "slot-1")
        self.assertEqual(set(task.parent_ids), {self.seeds[0].id, high.id})
        self.assertTrue(text.startswith((FIXTURE / "problem.md").read_text()))
        self.assertIn((FIXTURE / "candidate.h").read_text(), text)
        headings = ["## Required exports", "## Parent programs", "## Already tried on this island", "## Instructions"]
        self.assertEqual([text.index(h) for h in headings], sorted(text.index(h) for h in headings))
        self.assertLess(text.index("Score: 0.0"), text.index("Score: 5.0"))
        self.assertIn("### v0", text)
        self.assertIn("### v1", text)
        self.assertIn("seed message", text)
        self.assertIn("winner message", text)
        self.assertIn("````c", text)
        recent = text.split("## Already tried on this island")[1].split("## Instructions")[0]
        self.assertEqual(recent.count("- "), 10)
        self.assertNotIn("tried-2 —", recent)
        self.assertIn("tried-3 — status: INVALID; score: None", recent)
        self.assertIn("child.c", text)
        self.assertIn("// IDEA: <what you changed and why>", text)
        self.assertIn("up to 3 times, then submit", text)

    def test_round_robin(self):
        ids = [create_task(self.db, self.cfg, FIXTURE, self.run, rng=random.Random(i)) for i in range(6)]
        self.assertEqual([self.db.get_task(i).island for i in ids], [0, 1, 2, 3, 0, 1])

    def test_problem_text_preserves_line_endings(self):
        problem = self.run / "problem"
        problem.mkdir()
        statement = b"# Statement\r\n\r\nPreserve verbatim.\r\n"
        header = b"double f(void);\r\n"
        (problem / "problem.md").write_bytes(statement)
        (problem / "candidate.h").write_bytes(header)
        task_id = create_task(self.db, self.cfg, problem, self.run)
        text = (self.run / "tasks" / str(task_id) / "TASK.md").read_bytes()
        self.assertTrue(text.startswith(statement))
        self.assertIn(header, text)

    def test_orphan_directory_is_preserved_and_allocation_recovers(self):
        directory = self.run / "tasks" / "1"
        directory.mkdir(parents=True)
        (directory / "keep").write_text("existing")
        task_id = create_task(self.db, self.cfg, FIXTURE, self.run)
        self.assertEqual(task_id, 1)
        self.assertEqual(self.db.get_state("next_island"), 1)
        orphan, = (self.run / "tasks").glob(".orphan-1-*")
        self.assertEqual((orphan / "keep").read_text(), "existing")
        self.assertTrue((directory / "TASK.md").exists())

    def test_client_death_before_outer_commit_does_not_stall_slots(self):
        script = """
import os, sys
from engine.funsearch.config import load_config
from engine.funsearch.db import Database
from engine.funsearch.tasks import create_task
with Database(sys.argv[1]) as db, db.transaction():
    create_task(db, load_config(sys.argv[2]), sys.argv[2], sys.argv[3], 'dead')
    os._exit(77)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.run / "db.sqlite"),
                                 str(FIXTURE), str(self.run)], timeout=10)
        self.assertEqual(result.returncode, 77)
        self.assertEqual(self.db.list_tasks(), [])
        original = (self.run / "tasks" / "1" / "TASK.md").read_text()
        for slot in ("1", "2", "3"):
            task_id = create_task(self.db, self.cfg, FIXTURE, self.run, slot)
            self.assertEqual(self.db.get_task(task_id).slot, slot)
            self.assertTrue((self.run / "tasks" / str(task_id) / "TASK.md").exists())
        orphan, = (self.run / "tasks").glob(".orphan-1-*")
        self.assertEqual((orphan / "TASK.md").read_text(), original)
        # Another allocation leaves every committed task directory in place.
        committed = (self.run / "tasks" / "1" / "TASK.md").read_text()
        create_task(self.db, self.cfg, FIXTURE, self.run, "4")
        self.assertEqual((self.run / "tasks" / "1" / "TASK.md").read_text(), committed)

    def test_unseeded_island_rejected(self):
        self.db.archive_island(0)
        with self.assertRaisesRegex(ValueError, "seed"):
            create_task(self.db, self.cfg, FIXTURE, self.run)
        self.assertEqual(self.db.list_tasks(), [])
