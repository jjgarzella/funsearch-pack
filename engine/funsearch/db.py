"""Typed SQLite access shared by the engine daemon and its CLI clients.

Program history survives island resets; only active programs participate in
sampling and duplicate checks. All writes use short transactions.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time

from .normalize import extract_idea, normalized_hash


@dataclass(frozen=True)
class Program:
    id: int
    island: int
    parent_ids: list[int]
    source: str
    norm_hash: str
    status: str
    score: float | None
    sig: list[float]
    msg: str
    idea: str
    created_at: float
    active: bool


@dataclass(frozen=True)
class Task:
    id: int
    island: int
    parent_ids: list[int]
    slot: str
    status: str
    trials_used: int
    created_at: float
    closed_at: float | None


@dataclass(frozen=True)
class Trial:
    task_id: int
    n: int
    status: str
    score: float | None
    msg: str


@dataclass(frozen=True)
class Evaluation:
    id: int
    kind: str
    task_id: int | None
    src_path: str
    so_path: str
    state: str
    result: dict | None
    created_at: float
    started_at: float | None
    finished_at: float | None
    trial_n: int | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
 id INTEGER PRIMARY KEY, island INTEGER NOT NULL CHECK(island >= 0),
 parent_ids TEXT NOT NULL, source TEXT NOT NULL, norm_hash TEXT NOT NULL,
 status TEXT NOT NULL, score REAL, sig TEXT NOT NULL, msg TEXT NOT NULL,
 idea TEXT NOT NULL, created_at REAL NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE INDEX IF NOT EXISTS programs_island ON programs(island,active,status);
CREATE INDEX IF NOT EXISTS programs_hash ON programs(norm_hash);
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY, island INTEGER NOT NULL, parent_ids TEXT NOT NULL,
 slot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open'
 CHECK(status IN ('open','done','abandoned')),
 trials_used INTEGER NOT NULL DEFAULT 0 CHECK(trials_used >= 0),
 created_at REAL NOT NULL, closed_at REAL
);
CREATE TABLE IF NOT EXISTS trials (
 task_id INTEGER NOT NULL REFERENCES tasks(id), n INTEGER NOT NULL CHECK(n > 0),
 status TEXT NOT NULL, score REAL, msg TEXT NOT NULL,
 PRIMARY KEY(task_id,n)
);
CREATE TABLE IF NOT EXISTS evalq (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('try','submit','seed')),
 task_id INTEGER REFERENCES tasks(id), src_path TEXT NOT NULL, so_path TEXT NOT NULL,
 trial_n INTEGER,
 state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','done')),
 result TEXT, created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS evalq_state ON evalq(state,id);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Database:
    def __init__(self, path, *, busy_timeout_ms=5000, readonly=False):
        self.path = Path(path)
        target = self.path.resolve().as_uri() + "?mode=ro" if readonly else str(path)
        self.connection = sqlite3.connect(target, uri=readonly,
                                          timeout=busy_timeout_ms / 1000, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self.connection.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            # Host-mounted run directories may not support WAL's shared -shm
            # mmap reliably (live acceptance saw SIGBUS). Rollback journaling
            # avoids that mapping and also migrates existing WAL databases.
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.executescript(_SCHEMA)
            if "trial_n" not in {row[1] for row in self.connection.execute("PRAGMA table_info(evalq)")}:
                self.connection.execute("ALTER TABLE evalq ADD COLUMN trial_n INTEGER")
        self._depth = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        """Serialize writers, including composed accessor operations."""
        depth = self._depth
        self.connection.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT nested_{depth}")
        self._depth += 1
        try:
            yield self
        except BaseException:
            if depth == 0:
                self.connection.execute("ROLLBACK")
            else:
                self.connection.execute(f"ROLLBACK TO nested_{depth}")
                self.connection.execute(f"RELEASE nested_{depth}")
            raise
        else:
            self.connection.execute("COMMIT" if depth == 0 else f"RELEASE nested_{depth}")
        finally:
            self._depth -= 1

    @staticmethod
    def _program(row):
        if row is None:
            return None
        data = dict(row)
        data["parent_ids"] = json.loads(data["parent_ids"])
        data["sig"] = json.loads(data["sig"])
        data["active"] = bool(data["active"])
        return Program(**data)

    @staticmethod
    def _task(row):
        if row is None:
            return None
        data = dict(row)
        data["parent_ids"] = json.loads(data["parent_ids"])
        return Task(**data)

    @staticmethod
    def _evaluation(row):
        if row is None:
            return None
        data = dict(row)
        data["result"] = json.loads(data["result"]) if data["result"] is not None else None
        return Evaluation(**data)

    def add_program(self, island: int, source: str, *, parent_ids=(), status="OK",
                    score=None, sig=(), msg="", idea=None, norm_hash=None, program_id=None) -> Program:
        signature = list(sig)
        if len(signature) > 8:
            raise ValueError("signature must contain at most eight values")
        # Reject non-finite evaluation data rather than emitting invalid JSON.
        encoded_sig = json.dumps(signature, allow_nan=False)
        if score is not None:
            json.dumps(score, allow_nan=False)
        with self.transaction():
            cursor = self.connection.execute(
                "INSERT INTO programs(id,island,parent_ids,source,norm_hash,status,score,sig,msg,idea,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (program_id, island, json.dumps(list(parent_ids)), source, norm_hash or normalized_hash(source),
                 status, score, encoded_sig, msg, extract_idea(source) if idea is None else idea, time.time()))
            return self.get_program(cursor.lastrowid)

    def get_program(self, program_id: int) -> Program | None:
        return self._program(self.connection.execute("SELECT * FROM programs WHERE id=?", (program_id,)).fetchone())

    def list_programs(self, island=None, *, status=None, active_only=True) -> list[Program]:
        clauses, args = [], []
        if island is not None:
            clauses.append("island=?")
            args.append(island)
        if status is not None:
            clauses.append("status=?")
            args.append(status)
        if active_only:
            clauses.append("active=1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [self._program(row) for row in self.connection.execute("SELECT * FROM programs" + where + " ORDER BY id", args)]

    def best_program(self, island=None) -> Program | None:
        programs = [p for p in self.list_programs(island, status="OK") if p.score is not None]
        return max(programs, key=lambda p: (p.score, -len(p.source), -p.id), default=None)

    def has_normalized_hash(self, norm_hash: str, *, island=None) -> bool:
        query = "SELECT 1 FROM programs WHERE norm_hash=? AND active=1"
        args = [norm_hash]
        if island is not None:
            query += " AND island=?"
            args.append(island)
        return self.connection.execute(query + " LIMIT 1", args).fetchone() is not None

    def recent_children(self, island: int, limit=10) -> list[Program]:
        rows = self.connection.execute(
            "SELECT * FROM programs WHERE island=? AND parent_ids != '[]' ORDER BY id DESC LIMIT ?",
            (island, limit))
        return [self._program(row) for row in rows][::-1]

    def archive_island(self, island: int) -> None:
        with self.transaction():
            self.connection.execute("UPDATE programs SET active=0 WHERE island=?", (island,))

    def add_task(self, island: int, parent_ids=(), *, slot="") -> Task:
        with self.transaction():
            cursor = self.connection.execute(
                "INSERT INTO tasks(island,parent_ids,slot,created_at) VALUES (?,?,?,?)",
                (island, json.dumps(list(parent_ids)), str(slot), time.time()))
            return self.get_task(cursor.lastrowid)

    def get_task(self, task_id: int) -> Task | None:
        return self._task(self.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def list_tasks(self, *, status=None) -> list[Task]:
        query = "SELECT * FROM tasks"
        args = ()
        if status is not None:
            query += " WHERE status=?"
            args = (status,)
        return [self._task(row) for row in self.connection.execute(query + " ORDER BY id", args)]

    def close_task(self, task_id: int, *, status="done") -> Task:
        if status not in ("done", "abandoned"):
            raise ValueError("closed task status must be done or abandoned")
        with self.transaction():
            cursor = self.connection.execute("UPDATE tasks SET status=?,closed_at=? WHERE id=? AND status='open'",
                                             (status, time.time(), task_id))
            if cursor.rowcount != 1:
                raise ValueError(f"task {task_id} does not exist or is already closed")
            return self.get_task(task_id)

    def reserve_trial(self, task_id: int, budget: int) -> int:
        """Atomically consume a try slot before queuing its evaluation."""
        with self.transaction():
            task = self.get_task(task_id)
            if task is None or task.status != "open":
                raise ValueError(f"task {task_id} is not open")
            if task.trials_used >= budget:
                raise ValueError(f"task {task_id} trial budget exhausted")
            n = task.trials_used + 1
            self.connection.execute("UPDATE tasks SET trials_used=? WHERE id=?", (n, task_id))
            return n

    def add_trial(self, task_id: int, n: int, *, status: str, score=None, msg="") -> Trial:
        with self.transaction():
            task = self.get_task(task_id)
            if task is None or not 1 <= n <= task.trials_used:
                raise ValueError("trial number must have been reserved")
            self.connection.execute("INSERT INTO trials VALUES (?,?,?,?,?)", (task_id, n, status, score, msg))
        return Trial(task_id, n, status, score, msg)

    def list_trials(self, task_id: int) -> list[Trial]:
        return [Trial(**dict(row)) for row in self.connection.execute(
            "SELECT * FROM trials WHERE task_id=? ORDER BY n", (task_id,))]

    def enqueue(self, kind: str, src_path, so_path, *, task_id=None, trial_n=None) -> Evaluation:
        with self.transaction():
            cursor = self.connection.execute(
                "INSERT INTO evalq(kind,task_id,src_path,so_path,created_at,trial_n) VALUES (?,?,?,?,?,?)",
                (kind, task_id, str(src_path), str(so_path), time.time(), trial_n))
            return self.get_evaluation(cursor.lastrowid)

    def get_evaluation(self, evaluation_id: int) -> Evaluation | None:
        return self._evaluation(self.connection.execute("SELECT * FROM evalq WHERE id=?", (evaluation_id,)).fetchone())

    def claim_evaluation(self, kind=None) -> Evaluation | None:
        with self.transaction():
            query = "SELECT id FROM evalq WHERE state='queued'"
            args = ()
            if kind is not None:
                query += " AND kind=?"
                args = (kind,)
            row = self.connection.execute(query + " ORDER BY id LIMIT 1", args).fetchone()
            if row is None:
                return None
            self.connection.execute("UPDATE evalq SET state='running',started_at=? WHERE id=?", (time.time(), row["id"]))
            return self.get_evaluation(row["id"])

    def finish_evaluation(self, evaluation_id: int, result: dict) -> Evaluation:
        encoded = json.dumps(result, allow_nan=False)
        with self.transaction():
            cursor = self.connection.execute(
                "UPDATE evalq SET state='done',result=?,finished_at=? WHERE id=? AND state='running'",
                (encoded, time.time(), evaluation_id))
            if cursor.rowcount != 1:
                raise ValueError(f"evaluation {evaluation_id} is not running")
            return self.get_evaluation(evaluation_id)

    def set_state(self, key: str, value) -> None:
        with self.transaction():
            self.connection.execute("INSERT INTO state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                    (key, json.dumps(value, allow_nan=False)))

    def get_state(self, key: str, default=None):
        row = self.connection.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row is not None else default

    def increment_state(self, key: str, amount=1) -> int:
        with self.transaction():
            value = self.get_state(key, 0) + amount
            self.set_state(key, value)
            return value

    def backup(self, path) -> None:
        with sqlite3.connect(str(path)) as destination:
            destination.execute("PRAGMA journal_mode=DELETE")
            self.connection.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
