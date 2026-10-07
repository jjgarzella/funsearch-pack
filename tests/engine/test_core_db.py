from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engine.funsearch.db import BACKUP_RESTARTS, SCHEMA_VERSION, Database, SchemaMismatch
from engine.funsearch.runtime import top_programs


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
        self.assertEqual(sorted(p.name for p in backup.parent.glob("backup.sqlite*")), ["backup.sqlite"])
        # A copy taken in many small steps is still complete.
        with patch("engine.funsearch.db.BACKUP_PAGES", 1):
            self.db.backup(backup)
        with Database(backup, readonly=True) as saved:
            self.assertEqual(saved.get_state("children"), 3)
        self.assertEqual(self.db.get_state("absent", "fallback"), "fallback")

    def test_backup_restarted_by_client_writes_finishes_in_one_pass(self):
        for n in range(50):
            self.db.set_state(f"filler-{n}", "x" * 2000)
        writes = 0
        with Database(self.path) as client:
            def client_write(_seconds):
                # Every pause between steps admits a client write, which
                # restarts the stepped copy from its first page.
                nonlocal writes
                writes += 1
                self.assertLess(writes, 50, "backup never finished")
                client.set_state("writes", writes)

            backup = Path(self.temp.name) / "busy.sqlite"
            with patch("engine.funsearch.db.BACKUP_PAGES", 1), \
                    patch("engine.funsearch.db.time.sleep", client_write):
                self.db.backup(backup)
        self.assertEqual(writes, BACKUP_RESTARTS + 1)
        with Database(backup, readonly=True) as saved:
            self.assertEqual(saved.get_state("writes"), writes)
            self.assertEqual(saved.get_state("filler-49"), "x" * 2000)
        self.assertEqual(sorted(p.name for p in backup.parent.glob("busy.sqlite*")), ["busy.sqlite"])

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
        with Database(legacy, migrate=True) as migrated:
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

    def test_slot_pending_and_stale_task_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "db.sqlite") as db:
                stale = db.add_task(0, [], slot="1")
                busy = db.add_task(0, [], slot="2")
                fresh = db.add_task(0, [], slot="1")
                db.connection.execute("UPDATE tasks SET created_at=0 WHERE id IN (?,?)", (stale.id, busy.id))
                self.assertEqual([t.id for t in db.open_tasks_for_slot(1)], [stale.id, fresh.id])
                db.enqueue("submit", "a.c", "a.so", task_id=busy.id)
                self.assertTrue(db.has_pending_submission(busy.id))
                self.assertFalse(db.has_pending_submission(stale.id))
                running = db.claim_evaluation()
                self.assertEqual([e.id for e in db.running_evaluations()], [running.id])
                # A queued or running evaluation keeps an old task alive.
                self.assertEqual(db.abandon_stale_tasks(100), 1)
                self.assertEqual(db.get_task(stale.id).status, "abandoned")
                self.assertEqual(db.get_task(busy.id).status, "open")
                self.assertEqual(db.get_task(fresh.id).status, "open")
                db.finish_evaluation(running.id, {"status": "OK"})
                self.assertFalse(db.has_pending_submission(busy.id))
                self.assertEqual(db.abandon_stale_tasks(100), 1)
                db.set_state("status", "running")
                self.assertEqual(db.all_state(), {"status": "running"})
                plan = " ".join(row[3] for row in db.connection.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM tasks WHERE status='open' AND created_at<1"))
                self.assertIn("tasks_status", plan)

    def test_idle_abandonment_does_not_take_writer_lock(self):
        stale = self.db.add_task(0)
        busy = self.db.add_task(0)
        self.db.connection.execute("UPDATE tasks SET created_at=0 WHERE id=?", (busy.id,))
        self.db.enqueue("submit", "busy.c", "busy.so", task_id=busy.id)
        statements = []
        self.db.connection.set_trace_callback(statements.append)
        self.assertEqual(self.db.abandon_stale_tasks(100), 0)
        self.assertFalse(any(sql.startswith("BEGIN") or sql.startswith("UPDATE") for sql in statements))
        self.db.connection.execute("UPDATE tasks SET created_at=0 WHERE id=?", (stale.id,))
        self.assertEqual(self.db.abandon_stale_tasks(100), 1)
        self.assertEqual(self.db.get_task(busy.id).status, "open")

    def test_idle_queue_polling_does_not_take_writer_lock(self):
        queued = self.db.enqueue("try", "a.c", "a.so")
        statements = []
        self.db.connection.set_trace_callback(statements.append)
        for _ in range(3):
            self.assertIsNone(self.db.claim_evaluation("submit"))
        self.assertFalse(any(sql.startswith("BEGIN") or sql.startswith("UPDATE") for sql in statements))
        self.assertEqual(self.db.claim_evaluation("try").id, queued.id)
        statements.clear()
        self.assertIsNone(self.db.claim_evaluation())
        self.assertFalse(any(sql.startswith("BEGIN") or sql.startswith("UPDATE") for sql in statements))

    def test_scored_duplicate_needs_equal_score_and_signature_on_an_active_ok_program(self):
        self.db.add_program(0, "a", score=2, sig=[1.5, 2])
        self.db.add_program(1, "b", score=3, sig=[1])
        self.db.add_program(0, "c", status="INVALID", score=4, sig=[1])
        self.assertTrue(self.db.has_scored_duplicate(2, [1.5, 2]))
        self.assertTrue(self.db.has_scored_duplicate(2.0, (1.5, 2.0)))
        # Equal score alone is a different behaviour, not a duplicate.
        self.assertFalse(self.db.has_scored_duplicate(2, [1.5, 3]))
        self.assertFalse(self.db.has_scored_duplicate(2, []))
        self.assertFalse(self.db.has_scored_duplicate(4, [1]))
        self.db.archive_island(1)
        self.assertFalse(self.db.has_scored_duplicate(3, [1]))

    def test_ranking_order_filters_and_islands(self):
        add = self.db.add_program
        long = add(0, "long source", score=5)
        short = add(1, "short", score=5)
        older_tie = add(0, "tie-a", score=5)
        newer_tie = add(1, "tie-b", score=5, norm_hash=older_tie.norm_hash)
        add(0, "best but failed", status="INVALID", score=9)
        add(0, "unscored", score=None)
        archived = add(2, "archived best", score=7)
        low = add(1, "low", score=1)
        self.db.archive_island(2)
        # Higher score, then shorter source, then the older program.
        self.assertEqual([i for i, _ in self.db.ranked_ids()],
                         [short.id, older_tie.id, newer_tie.id, long.id, low.id])
        self.assertEqual(next(self.db.ranked_ids(active_only=False))[0], archived.id)
        self.assertEqual(self.db.best_program(), short)
        self.assertEqual(self.db.best_program(0), older_tie)
        self.assertEqual(self.db.best_program(1), short)
        self.assertIsNone(self.db.best_program(2))
        self.assertIsNone(self.db.best_program(3))
        # Exports keep archived history and drop repeated normalized sources.
        self.assertEqual([p.id for p in top_programs(self.db, 10)],
                         [archived.id, short.id, older_tie.id, long.id, low.id])
        self.assertEqual([p.id for p in top_programs(self.db, 2)], [archived.id, short.id])
        self.assertEqual([(s.id, s.length) for s in self.db.scored_summaries(0)],
                         [(long.id, 11), (older_tie.id, 5)])

    def test_ranking_and_sampling_never_read_program_source(self):
        plans = []
        for query in ("SELECT id,norm_hash FROM programs WHERE status='OK' AND score IS NOT NULL "
                      "ORDER BY score DESC, source_length, id",
                      "SELECT id FROM programs WHERE status='OK' AND score IS NOT NULL AND island=1 "
                      "AND active=1 ORDER BY score DESC, source_length, id",
                      "SELECT id,status,score,sig,source_length FROM programs WHERE island=1 "
                      "AND active=1 AND status='OK' AND score IS NOT NULL ORDER BY id"):
            plans.append(" ".join(row[3] for row in self.db.connection.execute("EXPLAIN QUERY PLAN " + query)))
        for plan in plans:
            self.assertIn("COVERING INDEX", plan)
        self.assertNotIn("TEMP B-TREE", plans[0] + plans[1])
        # recent_children walks the island newest first and stops at its limit.
        recent = " ".join(row[3] for row in self.db.connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM programs WHERE island=1 AND parent_ids != '[]' "
            "ORDER BY id DESC LIMIT 10"))
        self.assertIn("programs_island_recent", recent)
        self.assertNotIn("TEMP B-TREE", recent)

    def test_older_database_gains_source_length(self):
        legacy = self.path.with_name("legacy.sqlite")
        with sqlite3.connect(legacy) as connection:
            connection.executescript("""
                CREATE TABLE programs (id INTEGER PRIMARY KEY, island INTEGER NOT NULL,
                 parent_ids TEXT NOT NULL, source TEXT NOT NULL, norm_hash TEXT NOT NULL,
                 status TEXT NOT NULL, score REAL, sig TEXT NOT NULL, msg TEXT NOT NULL,
                 idea TEXT NOT NULL, created_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE INDEX programs_score ON programs(status,active,score);
                INSERT INTO programs VALUES (1,0,'[]','longer',  'a','OK',1,'[]','','',0,1);
                INSERT INTO programs VALUES (2,0,'[]','short',   'b','OK',1,'[]','','',0,1);""")
        connection.close()
        # Read-only clients of an unmigrated run still rank by source length.
        with Database(legacy, readonly=True) as client:
            self.assertEqual(client.best_program().id, 2)
            self.assertEqual([s.length for s in client.scored_summaries(0)], [6, 5])
        # Only the engine migrates: a client never upgrades a run that an older
        # engine may still be writing.
        with self.assertRaisesRegex(SchemaMismatch, "schema version 0"):
            Database(legacy)
        with Database(legacy, migrate=True) as migrated:
            self.assertEqual(migrated.best_program().id, 2)
            self.assertEqual(migrated._version(), SCHEMA_VERSION)
            self.assertIn("trial_n", migrated._columns("evalq"))
            indexes = {row[1] for row in migrated.connection.execute("PRAGMA index_list(programs)")}
            self.assertNotIn("programs_score", indexes)
            self.assertIn("programs_rank", indexes)
        with Database(legacy, readonly=True) as client:
            self.assertEqual([s.length for s in client.scored_summaries(0)], [6, 5])
        with Database(legacy) as client:
            self.assertEqual(client.best_program().id, 2)

    def test_migration_repairs_lengths_an_older_engine_left_at_zero(self):
        # An unversioned database that already has source_length, where an
        # older engine inserted a row and the column default stored 0.
        legacy = self.path.with_name("legacy.sqlite")
        self.db.add_program(0, "longer", score=1)
        self.db.backup(legacy)
        with sqlite3.connect(legacy) as connection:
            connection.execute("PRAGMA user_version=0")
            connection.execute("INSERT INTO programs(island,parent_ids,source,norm_hash,status,score,"
                               "sig,msg,idea,created_at,source_length) "
                               "VALUES (0,'[]','much longer','b','OK',1,'[]','','',0,0)")
        connection.close()
        with Database(legacy, migrate=True) as migrated:
            self.assertEqual([s.length for s in migrated.scored_summaries(0)], [6, 11])
            self.assertEqual(migrated.best_program().source, "longer")

    def test_newer_schema_is_refused(self):
        self.db.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        for options in ({}, {"migrate": True}, {"readonly": True}):
            with self.subTest(options=options), \
                    self.assertRaisesRegex(SchemaMismatch, f"schema version {SCHEMA_VERSION + 1}"):
                Database(self.path, **options)
