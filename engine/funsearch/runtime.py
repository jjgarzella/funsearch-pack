"""Run metadata and hook utilities shared by the CLI and daemon."""

import json
import os
from pathlib import Path
import shlex
import subprocess

from .config import Config, ConfigError


class RunOver(RuntimeError):
    pass


class Rejected(RuntimeError):
    pass


def read_run(run_dir):
    root = Path(run_dir).resolve()
    if not (root / "db.sqlite").is_file():
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
    if db.get_state("status") != "running" or db.get_state("stop_requested", False):
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
        code = process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        from ._process import kill_group
        kill_group(process)
        raise RuntimeError("hook timed out after 120s") from None
    if code:
        raise RuntimeError(f"hook exited with code {code}")


def top_programs(db, count=10):
    # Export unique candidates across islands, including archived history.
    programs = sorted((p for p in db.list_programs(status="OK", active_only=False)
                       if p.score is not None),
                      key=lambda p: (-p.score, len(p.source), p.id))
    unique, seen = [], set()
    for program in programs:
        if program.norm_hash not in seen:
            unique.append(program)
            seen.add(program.norm_hash)
        if len(unique) >= count:
            break
    return unique
