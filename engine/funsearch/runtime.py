"""Run metadata and hook utilities shared by the CLI and daemon."""

from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import subprocess

from .config import Config, ConfigError


# Run lifecycle, recorded as the "status" state key by the engine daemon:
# starting -> running -> stopping -> one terminal status.
STARTING, RUNNING, STOPPING = "starting", "running", "stopping"
COMPLETED, STOPPED, FAILED = "completed", "stopped", "failed"
TERMINAL_STATUSES = frozenset({COMPLETED, STOPPED, FAILED})
HOOK_TIMEOUT_S = 120


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
    if db.get_state("status") != RUNNING or db.get_state("stop_requested", False):
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
