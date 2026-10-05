"""Gas City adapters, kept separate from the standalone engine."""

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess

from .config import ConfigError

CONTEXT_FILE = ".funsearch-slot.json"


def gc(*args):
    result = subprocess.run(["gc", *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"gc {' '.join(args[:3])}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def owned_slot(bead, *, allow_closed=False):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", bead):
        raise ConfigError("invalid slot bead id")
    session = os.environ.get("GC_SESSION_ID")
    if not session:
        raise ConfigError("slot helpers require GC_SESSION_ID")
    rows = json.loads(gc("bd", "show", bead, "--json"))
    row = rows[0] if isinstance(rows, list) and len(rows) == 1 else rows
    metadata = row.get("metadata", {})
    closed = allow_closed and row.get("status") == "closed"
    if row.get("id") != bead:
        raise ConfigError("gc returned the wrong slot")
    if not closed and (row.get("status") != "in_progress"
            or metadata.get("gc.session_id") != session
            or row.get("assignee") not in {value for value in
                (session, os.environ.get("GC_SESSION_NAME"), os.environ.get("GC_ALIAS")) if value}):
        raise ConfigError("slot is not claimed by this session")
    if not metadata.get("gc.routed_to"):
        raise ConfigError("slot has no routing metadata")
    root = Path(metadata.get("fs.run_dir", ""))
    if not root.is_absolute() or not str(metadata.get("fs.slot", "")):
        raise ConfigError("slot requires absolute fs.run_dir and fs.slot")
    try:
        count = int(metadata["fs.tasks_per_session"])
        if count < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ConfigError("fs.tasks_per_session must be a positive integer") from None
    context = {"bead": bead, "session": session, "run_dir": str(root.resolve()),
               "slot": str(metadata["fs.slot"]), "tasks_per_session": count}
    return row, context


def slot_command(command, bead):
    row, context = owned_slot(bead, allow_closed=command == "close")
    path = Path.cwd() / CONTEXT_FILE
    if command == "show":
        database = Path(context["run_dir"]) / "db.sqlite"
        if database.exists():
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                tasks = db.execute("SELECT id,trials_used FROM tasks WHERE slot=? AND status='open'",
                                   (context["slot"],)).fetchall()
            if len(tasks) > 1:
                raise ConfigError("slot has multiple unfinished tasks")
            if tasks:
                task, used = tasks[0]
                context["pending_task"] = {
                    "id": task, "dir": str(database.parent / "tasks" / str(task)),
                    "trials_used": used}
        # The agent's file tools cannot access this control file. A fresh
        # work_dir per session also prevents concurrent sessions sharing it.
        path.write_text(json.dumps(context) + "\n")
        path.chmod(0o600)
        print(json.dumps(context))
        return 0
    if command == "release":
        gc("bd", "update", bead, "--status=open", "--assignee=",
           f"--if-assignee={row['assignee']}", "--if-status=in_progress",
           "--unset-metadata=gc.session_id", "--unset-metadata=gc.session_name",
           "--unset-metadata=gc.claimed_at", "--unset-metadata=gc.work_dir",
           "--unset-metadata=gc.work_branch")
    elif command == "close":
        # Guard the ownership check and close in one update. As with bd close,
        # ordinary dependency/child checks still apply; never use --force.
        if row["status"] != "closed":
            gc("bd", "update", bead, "--status=closed",
               f"--if-assignee={row['assignee']}", "--if-status=in_progress")
    else:
        raise ConfigError("unknown slot command")
    path.unlink(missing_ok=True)
    print(f"SLOT_{command.upper()} {bead}")
    return 0
