from pathlib import Path
import sqlite3
import tempfile
import unittest

from engine.funsearch.db import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "db.sqlite"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)

    def test_program_round_trip_and_history(self):
        seed = self.db.add_program(0, "// IDEA: seed\nint f(){return 0;}", score=0, sig=[1, 2], msg="zero")
        child = self.db.add_program(0, "// IDEA: child\nint f(){return 2;}", parent_ids=[seed.id], score=2)
        self.assertEqual(self.db.get_program(seed.id), seed)
        self.assertEqual(seed.idea, "seed")
        self.assertEqual(child.parent_ids, [seed.id])
        self.assertEqual(self.db.best_program(0), child)
        self.assertTrue(self.db.has_normalized_hash(child.norm_hash, island=0))
        self.assertFalse(self.db.has_normalized_hash(child.norm_hash, island=1))
        self.assertEqual(self.db.recent_children(0), [child])
        self.db.archive_island(0)
        self.assertEqual(self.db.list_programs(), [])
        self.assertEqual(len(self.db.list_programs(active_only=False)), 2)
        self.assertFalse(self.db.get_program(seed.id).active)
        self.assertEqual(self.db.recent_children(0)[0].id, child.id)

    def test_task_trials_and_budget(self):
        task = self.db.add_task(2, [4, 5], slot="slot-a")
        self.assertEqual(task.parent_ids, [4, 5])
        self.assertEqual(task.status, "open")
        self.assertIsNone(task.closed_at)
        n = self.db.reserve_trial(task.id, 1)
        trial = self.db.add_trial(task.id, n, status="INVALID", msg="bad")
        self.assertEqual(self.db.list_trials(task.id), [trial])
        with self.assertRaisesRegex(ValueError, "exhausted"):
            self.db.reserve_trial(task.id, 1)
        with self.assertRaises(ValueError):
            self.db.add_trial(task.id, 2, status="OK")
        closed = self.db.close_task(task.id)
        self.assertEqual(closed.status, "done")
        self.assertIsNotNone(closed.closed_at)
        with self.assertRaises(ValueError):
            self.db.reserve_trial(task.id, 2)
        self.assertEqual(self.db.list_tasks(status="done"), [closed])
        other = self.db.add_task(0)
        self.assertEqual(self.db.close_task(other.id, status="abandoned").status, "abandoned")

    def test_eval_queue_round_trip_and_claim(self):
        task = self.db.add_task(0)
        first = self.db.enqueue("try", "/tmp/a.c", "/tmp/a.so", task_id=task.id)
        second = self.db.enqueue("seed", "/tmp/b.c", "/tmp/b.so")
        self.assertEqual(first.state, "queued")
        with Database(self.path) as client:
            claimed = client.claim_evaluation()
            self.assertEqual(claimed.id, first.id)
            self.assertIsNotNone(claimed.started_at)
            self.assertEqual(self.db.claim_evaluation().id, second.id)
            self.assertIsNone(client.claim_evaluation())
            result = {"status": "OK", "score": 1.5, "sig": [1], "msg": "✓"}
            done = client.finish_evaluation(first.id, result)
            self.assertEqual(self.db.get_evaluation(first.id), done)
            self.assertEqual(done.result, result)
            self.assertIsNotNone(done.finished_at)
            with self.assertRaises(ValueError):
                client.finish_evaluation(first.id, result)

    def test_delete_state_backup_and_rollback(self):
        self.assertEqual(self.db.connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(self.db.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.db.set_state("status", "running")
        self.db.set_state("stats", {"children": 1})
        self.assertEqual(self.db.increment_state("children"), 1)
        self.assertEqual(self.db.increment_state("children", 2), 3)
        with self.assertRaises(RuntimeError), self.db.transaction():
            self.db.set_state("status", "failed")
            self.db.add_task(0)
            raise RuntimeError("roll back")
        self.assertEqual(self.db.get_state("status"), "running")
        self.assertEqual(self.db.list_tasks(), [])
        backup = Path(self.temp.name) / "backup.sqlite"
        self.db.backup(backup)
        with Database(backup, readonly=True) as saved:
            self.assertEqual(saved.connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(saved.get_state("children"), 3)
            self.assertEqual(saved.get_state("stats"), {"children": 1})
        self.assertEqual(self.db.get_state("absent", "fallback"), "fallback")

    def test_readonly_client_and_missing_file(self):
        self.db.set_state("status", "running")
        with Database(self.path, readonly=True) as client:
            self.assertEqual(client.get_state("status"), "running")
            self.assertEqual(client.connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                client.set_state("status", "changed")
        missing = self.path.with_name("missing.sqlite")
        with self.assertRaises(sqlite3.OperationalError):
            Database(missing, readonly=True)
        self.assertFalse(missing.exists())

    def test_existing_wal_database_migrates_without_losing_committed_data(self):
        legacy = self.path.with_name("legacy.sqlite")
        with sqlite3.connect(legacy) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO state VALUES ('legacy', '42')")
        connection.close()
        with Database(legacy) as migrated:
            self.assertEqual(migrated.connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(migrated.get_state("legacy"), 42)
            migrated.set_state("new", "value")
        self.assertFalse(Path(str(legacy) + "-shm").exists())

    def test_invalid_data(self):
        for values in ({"score": float("nan")}, {"sig": [float("inf")]}, {"sig": [0] * 9}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.db.add_program(0, "x", **values)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.enqueue("unknown", "a", "b")
