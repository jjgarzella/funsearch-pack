"""Typed SQLite access shared by the engine daemon and its CLI clients.

Program history survives island resets; only active programs participate in
sampling and duplicate checks. All writes use short transactions. This class
owns the schema: the Gas City adapters and the mutator tool guard use these
methods rather than their own SQL.
"""

from contextlib import closing, contextmanager
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

    @property
    def length(self) -> int:
        return len(self.source)


@dataclass(frozen=True)
class ProgramSummary:
    """An active scored program's sampling fields, without its source text."""
    id: int
    status: str
    score: float
    sig: list[float]
    length: int


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
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 source_length INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY, island INTEGER NOT NULL, parent_ids TEXT NOT NULL,
 slot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open'
 CHECK(status IN ('open','done','abandoned')),
 trials_used INTEGER NOT NULL DEFAULT 0 CHECK(trials_used >= 0),
 created_at REAL NOT NULL, closed_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status,created_at);
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

# Ranking and sampling read only these covering indexes, in rank order, so
# they never sort or touch program source text: best first is higher score,
# then shorter source, then older program. programs_island_recent keeps each
# island in id order: recent_children walks it newest first and stops after its
# limit, and scored_summaries reads its fields from it without a sort.
_INDEXES = """
CREATE INDEX IF NOT EXISTS programs_hash ON programs(norm_hash);
CREATE INDEX IF NOT EXISTS programs_rank
 ON programs(status,score DESC,source_length,id,active,norm_hash);
CREATE INDEX IF NOT EXISTS programs_island_rank
 ON programs(island,active,status,score DESC,source_length,id,sig);
CREATE INDEX IF NOT EXISTS programs_island_recent
 ON programs(island,id,active,status,score,sig,source_length);
"""
_RANK_ORDER = "ORDER BY score DESC, source_length, id"

# Keys of the generic state(key,value) table (Database.get_state/set_state/
# increment_state), named here so a typo'd key is a NameError at the call
# site instead of a silently-defaulted read, and the set of keys that exist
# is this list instead of a grep across cli.py/daemon.py/evolve.py. Values
# are JSON-encoded by set_state; the type noted is what each key holds.
STATUS = "status"                    # str: one of runtime.py's lifecycle statuses
REASON = "reason"                    # str: stop_reason()'s reason or an error message
STARTED_AT = "started_at"            # float: time.time() when the run started
ENDED_AT = "ended_at"                # float: time.time() when the run ended
PID = "pid"                          # int: informational; engine_alive() is authoritative
STOP_REQUESTED = "stop_requested"    # bool
CHILDREN_SCORED = "children_scored"  # int: authoritative submitted-evaluation count
CHILDREN_OK = "children_ok"          # int
BEST_SCORE = "best_score"            # float
SEED_SCORE = "seed_score"            # float
PLATEAU_COUNT = "plateau_count"      # int: consecutive non-improving submissions
CLAIM_TIMEOUT_S = "claim_timeout_s"  # float: published client deadline, see workers.py
END_BY = "end_by"                    # float: published client deadline (epoch seconds)
ISLANDS = "islands"                  # int: island count at seeding time
NEXT_ISLAND = "next_island"          # int: round-robin cursor for create_task
ISLAND_RESETS = "island_resets"      # int: counter incremented by reset_weakest
SNAPSHOTS = "snapshots"              # int: counter used to number snapshot files

# PRAGMA user_version of the layout above. Version 0 is any database written
# before the layout was versioned, with or without trial_n and source_length.
SCHEMA_VERSION = 1
# Snapshot copy step (pages per step, pause between steps, and restarts by
# client writes before finishing in one pass); see Database.backup.
BACKUP_PAGES = 1024
BACKUP_SLEEP_S = 0.005
BACKUP_RESTARTS = 3


class _BackupRestarted(Exception):
    """Client writes restarted a stepped copy more than BACKUP_RESTARTS times."""


def _statements(script):
    return [statement for statement in script.split(";") if statement.strip()]


class SchemaMismatch(RuntimeError):
    """A run database's layout is not the one this code reads and writes."""


class Database:
    def __init__(self, path, *, busy_timeout_ms=5000, readonly=False, migrate=False):
        """Open a run database.

        A writable open creates an empty database at SCHEMA_VERSION. Only the
        engine passes migrate=True (the daemon, and recovery once the engine
        has exited): clients never upgrade a run that an older engine may still
        be writing, and refuse it with SchemaMismatch instead. A read-only open
        of an unversioned run reads it through a temporary view that supplies
        source_length; any other version mismatch is refused.
        """
        self.path = Path(path)
        target = self.path.resolve().as_uri() + "?mode=ro" if readonly else str(path)
        self.connection = sqlite3.connect(target, uri=readonly,
                                          timeout=busy_timeout_ms / 1000, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._depth = 0
        try:
            if readonly:
                self._check_readable()
            else:
                # Host-mounted run directories may not support WAL's shared
                # -shm mmap reliably (live acceptance saw SIGBUS). Rollback
                # journaling avoids that mapping and also migrates existing
                # WAL databases.
                self.connection.execute("PRAGMA journal_mode=DELETE")
                if self._version() != SCHEMA_VERSION:
                    with self.transaction():
                        self._prepare(migrate)
        except BaseException:
            self.connection.close()
            raise

    def _version(self):
        return self.connection.execute("PRAGMA main.user_version").fetchone()[0]

    def _columns(self, table):
        return {row[1] for row in self.connection.execute(f"PRAGMA main.table_info({table})")}

    def _mismatch(self, version):
        return SchemaMismatch(f"{self.path} has schema version {version}; this funsearch uses "
                              f"version {SCHEMA_VERSION}")

    def _check_readable(self):
        version = self._version()
        if version == 0 and "source_length" not in self._columns("programs"):
            # A read-only client cannot migrate an older run; present the same
            # columns through a temporary view, which shadows the table.
            self.connection.execute("CREATE TEMP VIEW programs AS "
                                    "SELECT *, length(source) AS source_length FROM main.programs")
        elif version not in (0, SCHEMA_VERSION):
            raise self._mismatch(version)

    def _prepare(self, migrate):
        """Create or upgrade the layout, inside the caller's writer transaction.

        The version is read again under the writer lock, so concurrent opens
        create or migrate a database once.
        """
        version = self._version()
        if version == SCHEMA_VERSION:
            return
        empty = self.connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
        if version > SCHEMA_VERSION or not (empty or migrate):
            raise self._mismatch(version)
        for statement in _statements(_SCHEMA):
            self.connection.execute(statement)
        # Migrations from version 0. Each one is a no-op on a new database.
        if "trial_n" not in self._columns("evalq"):
            self.connection.execute("ALTER TABLE evalq ADD COLUMN trial_n INTEGER")
        if "source_length" not in self._columns("programs"):
            self.connection.execute(
                "ALTER TABLE programs ADD COLUMN source_length INTEGER NOT NULL DEFAULT 0")
        # Also repairs rows that an older engine inserted into an unversioned
        # database after a client added the column with its default of 0.
        self.connection.execute(
            "UPDATE programs SET source_length=length(source) WHERE source_length != length(source)")
        for name in ("programs_island", "programs_score"):
            self.connection.execute(f"DROP INDEX IF EXISTS {name}")
        for statement in _statements(_INDEXES):
            self.connection.execute(statement)
        self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

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
            self.connection.execute("COMMIT" if depth == 0 else f"RELEASE nested_{depth}")
        except BaseException:
            if depth == 0:
                self.connection.execute("ROLLBACK")
            else:
                self.connection.execute(f"ROLLBACK TO nested_{depth}")
                self.connection.execute(f"RELEASE nested_{depth}")
            raise
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
        del data["source_length"]  # Program.length derives it from source.
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
                "INSERT INTO programs(id,island,parent_ids,source,norm_hash,status,score,sig,msg,idea,"
                "created_at,source_length) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (program_id, island, json.dumps(list(parent_ids)), source, norm_hash or normalized_hash(source),
                 status, score, encoded_sig, msg, extract_idea(source) if idea is None else idea, time.time(),
                 len(source)))
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

    def count_programs(self, island=None) -> int:
        """Active programs of every status."""
        query, args = "SELECT count(*) FROM programs WHERE active=1", []
        if island is not None:
            query += " AND island=?"
            args.append(island)
        return self.connection.execute(query, args).fetchone()[0]

    def scored_summaries(self, island: int) -> list[ProgramSummary]:
        """Active OK programs with a score, oldest first, without source text."""
        rows = self.connection.execute(
            "SELECT id,status,score,sig,source_length FROM programs "
            "WHERE island=? AND active=1 AND status='OK' AND score IS NOT NULL ORDER BY id",
            (island,))
        return [ProgramSummary(row["id"], row["status"], row["score"], json.loads(row["sig"]),
                               row["source_length"]) for row in rows]

    def ranked_ids(self, island=None, *, active_only=True):
        """Yield (id, norm_hash) of scored OK programs best first, lazily.

        The rows come straight from a rank-ordered index: stopping early reads
        only the prefix, and no source text is read at all.
        """
        clauses, args = "", []
        if island is not None:
            clauses += " AND island=?"
            args.append(island)
        if active_only:
            clauses += " AND active=1"
        query = ("SELECT id,norm_hash FROM programs WHERE status='OK' AND score IS NOT NULL"
                 + clauses + " " + _RANK_ORDER)
        with closing(self.connection.execute(query, args)) as cursor:
            while rows := cursor.fetchmany(64):
                yield from map(tuple, rows)

    def best_program(self, island=None) -> Program | None:
        with closing(self.ranked_ids(island)) as ranked:
            first = next(ranked, None)
        return None if first is None else self.get_program(first[0])

    def has_scored_duplicate(self, score: float, sig) -> bool:
        """Whether an active OK program has exactly this score and signature."""
        rows = self.connection.execute(
            "SELECT sig FROM programs WHERE status='OK' AND active=1 AND score=?", (score,))
        signature = list(sig)
        return any(json.loads(row["sig"]) == signature for row in rows)

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

    def open_tasks_for_slot(self, slot) -> list[Task]:
        """A slot's unfinished tasks; more than one means the slot is corrupt."""
        return [self._task(row) for row in self.connection.execute(
            "SELECT * FROM tasks WHERE slot=? AND status='open' ORDER BY id", (str(slot),))]

    def abandon_stale_tasks(self, cutoff: float) -> int:
        """Abandon open tasks created before cutoff with no queued/running evaluation."""
        # Avoid taking SQLite's writer lock when there is nothing to abandon.
        # Recheck under the transaction so an evaluation enqueued in between
        # cannot race maintenance into closing its task.
        predicate = ("status='open' AND created_at<? AND id NOT IN "
                     "(SELECT task_id FROM evalq WHERE state IN ('queued','running') "
                     "AND task_id IS NOT NULL)")
        if self.connection.execute("SELECT 1 FROM tasks WHERE " + predicate + " LIMIT 1",
                                   (cutoff,)).fetchone() is None:
            return 0
        with self.transaction():
            return self.connection.execute(
                "UPDATE tasks SET status='abandoned',closed_at=? WHERE " + predicate,
                (time.time(), cutoff)).rowcount

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

    def has_pending_submission(self, task_id: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM evalq WHERE task_id=? AND kind='submit' AND state IN ('queued','running') LIMIT 1",
            (task_id,)).fetchone() is not None

    def running_evaluations(self) -> list[Evaluation]:
        return [self._evaluation(row) for row in self.connection.execute(
            "SELECT * FROM evalq WHERE state='running' ORDER BY id")]

    def claim_evaluation(self, kind=None) -> Evaluation | None:
        query = "SELECT id FROM evalq WHERE state='queued'"
        args = ()
        if kind is not None:
            query += " AND kind=?"
            args = (kind,)
        query += " ORDER BY id LIMIT 1"
        # Empty queues are polled frequently. Read first without reserving a
        # writer; reselect under the transaction to preserve atomic claims.
        if self.connection.execute(query, args).fetchone() is None:
            return None
        with self.transaction():
            row = self.connection.execute(query, args).fetchone()
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

    def all_state(self) -> dict:
        return {row["key"]: json.loads(row["value"]) for row in self.connection.execute("SELECT * FROM state")}

    def increment_state(self, key: str, amount=1) -> int:
        with self.transaction():
            value = self.get_state(key, 0) + amount
            self.set_state(key, value)
            return value

    def backup(self, path) -> None:
        """Copy the database to path in steps, then rename it into place.

        Each step copies BACKUP_PAGES pages under the source read lock and then
        pauses BACKUP_SLEEP_S, so client writers wait for a step rather than a
        whole-database copy. A write by another connection restarts the copy
        from its first page; after BACKUP_RESTARTS restarts the copy finishes
        in one pass that blocks writers, so steady client writes cannot keep
        the engine's tick in an unbounded copy. A failed copy never leaves a
        partial snapshot under path.
        """
        path = Path(path)
        temporary = path.with_name(path.name + ".tmp")
        try:
            try:
                self._copy(temporary, BACKUP_PAGES)
            except _BackupRestarted:
                self._copy(temporary, -1)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _copy(self, destination_path, pages):
        last, restarts = None, 0

        def progress(status, remaining, _total):
            nonlocal last, restarts
            # A completed step that made no progress started over.
            if status == sqlite3.SQLITE_OK and last is not None and remaining >= last:
                restarts += 1
                if restarts > BACKUP_RESTARTS:
                    raise _BackupRestarted
            last = remaining
            if remaining:
                time.sleep(BACKUP_SLEEP_S)

        destination_path.unlink(missing_ok=True)
        with closing(sqlite3.connect(str(destination_path))) as destination:
            destination.execute("PRAGMA journal_mode=DELETE")
            self.connection.backup(destination, pages=pages, sleep=BACKUP_SLEEP_S,
                                   progress=progress if pages > 0 else None)
            destination.execute("PRAGMA journal_mode=DELETE")
