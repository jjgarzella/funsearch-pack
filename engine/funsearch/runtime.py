"""Run metadata and hook utilities shared by the CLI and daemon."""

from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

from .config import Config, ConfigError
from .db import STATUS, STOP_REQUESTED


# Run lifecycle, recorded as the "status" state key by the engine daemon:
# starting -> running -> stopping -> one terminal status.
STARTING, RUNNING, STOPPING = "starting", "running", "stopping"
COMPLETED, STOPPED, FAILED = "completed", "stopped", "failed"
TERMINAL_STATUSES = frozenset({COMPLETED, STOPPED, FAILED})
HOOK_TIMEOUT_S = 120
# The engine holds an exclusive flock on this run-directory file for its life.
ENGINE_LOCK = "engine.lock"
RECOVERY_LOCK = "recovery.lock"


def is_terminal(status):
    return status in TERMINAL_STATUSES


class RunOver(RuntimeError):
    pass


class Rejected(RuntimeError):
    pass


def read_run(run_dir, *, require_db=True):
    root = Path(run_dir).resolve()
    if require_db and not (root / "db.sqlite").is_file():
        raise ConfigError(f"not a run directory: {root}")
    try:
        metadata = json.loads((root / "run.json").read_text())
        cfg = Config()
        for section, values in metadata["config"].items():
            setattr(cfg, section, type(getattr(cfg, section))(**values))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigError(f"cannot read run metadata: {exc}") from exc
    return root, metadata, cfg


def require_running(db):
    if db.get_state(STATUS) != RUNNING or db.get_state(STOP_REQUESTED, False):
        raise RunOver("RUN_OVER")


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        # Linux reports zombies as existing until the external reaper runs.
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
        return True
    except (ProcessLookupError, ValueError, OSError):
        return False


def engine_alive(run_dir):
    """Whether the run's engine process is still running.

    The run directory is the authority: the engine holds an exclusive flock on
    ENGINE_LOCK for its whole life, and the kernel drops it however the engine
    dies (SIGKILL, OOM, a container or host restart), so an unrelated process
    that later reuses its PID never looks like the engine. Recorded PIDs are
    informational. A missing lock means no engine: serve takes it before
    anything else.
    """
    try:
        handle = open(Path(run_dir) / ENGINE_LOCK, "rb")
    except FileNotFoundError:
        return False
    with handle:
        try:
            # Shared, so concurrent probes never mistake each other for the engine.
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def hold_engine_lock(root, timeout_s=5):
    """Take the run's engine lock for this process's life; return its handle.

    Probes hold the shared lock only for an instant, so wait briefly for them;
    a lock still held after timeout_s belongs to another live engine.
    """
    handle = open(Path(root) / ENGINE_LOCK, "ab")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise RuntimeError(f"another engine holds {Path(root) / ENGINE_LOCK}") from None
            time.sleep(0.01)


@contextmanager
def hold_recovery_lock(root):
    """Serialize recovery publishers and exclude a live or starting engine.

    The separate exclusive recovery lock makes competing recoveries wait.
    Holding a shared engine lock keeps an engine from starting during recovery
    while allowing liveness probes to distinguish recovery from a live engine.
    Keep both files in place: unlinking a lock file splits its ownership.
    """
    root = Path(root)
    with (root / RECOVERY_LOCK).open("ab") as recovery:
        fcntl.flock(recovery, fcntl.LOCK_EX)
        with (root / ENGINE_LOCK).open("ab") as engine:
            try:
                fcntl.flock(engine, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConfigError(f"engine is still running: {root}") from None
            yield


def run_hook(command, run_dir):
    if not command:
        return
    root = Path(run_dir).resolve()
    env = os.environ.copy()
    env["FS_RUN_DIR"] = str(root)
    process = subprocess.Popen(command + " " + shlex.quote(str(root)), shell=True,
                               env=env, start_new_session=True)
    try:
        code = process.wait(timeout=HOOK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        from ._process import kill_group
        kill_group(process)
        raise RuntimeError(f"hook timed out after {HOOK_TIMEOUT_S}s") from None
    if code:
        raise RuntimeError(f"hook exited with code {code}")


def top_programs(db, count=10):
    # Export unique candidates across islands, including archived history.
    # Ids stream best first from an index; only the chosen rows' source is read.
    chosen, seen = [], set()
    with closing(db.ranked_ids(active_only=False)) as ranked:
        for program_id, norm_hash in ranked:
            if norm_hash not in seen:
                chosen.append(program_id)
                seen.add(norm_hash)
            if len(chosen) >= count:
                break
    return [db.get_program(program_id) for program_id in chosen]
