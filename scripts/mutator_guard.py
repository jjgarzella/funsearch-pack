"""Fail-closed Claude PreToolUse policy for a mutator's claimed slot.

The guard returns allow only for literal engine/lifecycle commands and files
belonging to its current open task. This is a tool policy, not an OS sandbox.
"""

import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys


def literal_command(command):
    # Reject shell syntax even inside quotes. This intentionally trades unusual
    # path names for a small grammar; ordinary spaces can still be quoted.
    if not isinstance(command, str) or any(c in command for c in "\n\r;&|<>$`\\*?(){}[]!~"):
        raise ValueError("use one literal command; shell syntax is forbidden")
    return shlex.split(command)


def context(home):
    data = json.loads((home / ".funsearch-slot.json").read_text())
    if not os.environ.get("GC_SESSION_ID") or data["session"] != os.environ["GC_SESSION_ID"]:
        raise ValueError("slot context belongs to another session")
    return data


def open_task(data):
    root = Path(data["run_dir"]).resolve()
    uri = (root / "db.sqlite").as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        rows = db.execute("SELECT id FROM tasks WHERE slot=? AND status='open' ORDER BY id",
                          (data["slot"],)).fetchall()
    if len(rows) > 1:
        raise ValueError("slot has multiple unfinished tasks")
    return rows[0][0] if rows else None


def task_file(value, home, data, *, write=False):
    task = open_task(data)
    if task is None:
        raise ValueError("allocate a task first")
    path = Path(value)
    if not path.is_absolute():
        path = home / path
    path = path.resolve()
    directory = Path(data["run_dir"]) / "tasks" / str(task)
    # Resolving only the user path would otherwise accept a symlinked task dir.
    if directory.resolve() != directory or path.parent != directory:
        raise ValueError("file must belong to this slot's current task")
    if path.name not in ({"child.c"} if write else {"TASK.md", "child.c"}):
        raise ValueError("only TASK.md and child.c are exposed; write only child.c")
    return task


def authorize(event, home, pack):
    tool, inputs = event["tool_name"], event["tool_input"]
    argv = literal_command(inputs["command"]) if tool == "Bash" else None
    if argv == ["gc", "runtime", "drain-ack"]:
        return
    retired = home / ".funsearch-retired.json"
    if retired.exists() and json.loads(retired.read_text())["session"] == os.environ.get("GC_SESSION_ID"):
        raise ValueError("this session released/closed its slot; run gc runtime drain-ack and stop")
    if tool in ("Read", "Write", "Edit"):
        task_file(inputs["file_path"], home, context(home), write=tool != "Read")
        return
    if tool != "Bash":
        raise ValueError("mutator only has Bash, Read, Write, Edit")
    if (argv[:2] == ["gc", "hook"] and "--claim" in argv[2:]
            and "--json" in argv[2:] and len(argv[2:]) == len(set(argv[2:]))
            and set(argv[2:]) <= {"--claim", "--json", "--drain-ack"}):
        return
    if not argv or argv[0] != str(pack / "bin/funsearch"):
        raise ValueError("Bash is restricted to the absolute pack CLI and gc lifecycle")
    if len(argv) == 4 and argv[1:3] == ["slot", "show"]:
        # owned_slot() verifies the actual gc claim before writing context.
        return
    data = context(home)
    if len(argv) == 4 and argv[1] == "slot" and argv[2] in ("release", "close"):
        if argv[3] != data["bead"]:
            raise ValueError("wrong slot bead")
        return
    if len(argv) == 5 and argv[1] == "next-task":
        if argv[2:] != [data["run_dir"], "--slot", data["slot"]]:
            raise ValueError("next-task must target this run and slot")
        if open_task(data) is not None:
            raise ValueError("finish the current task before allocating another")
        return
    if len(argv) == 5 and argv[1] in ("try", "submit"):
        if argv[2] != data["run_dir"]:
            raise ValueError("wrong run")
        task = task_file(argv[4], home, data, write=True)
        if argv[3] != str(task):
            raise ValueError("wrong task")
        return
    raise ValueError("command is outside the mutator allowlist")


def main():
    try:
        home, pack = (Path(value).resolve() for value in sys.argv[1:])
        authorize(json.load(sys.stdin), home, pack)
        decision, reason = "allow", "FunSearch mutator allowlist"
    except Exception as exc:
        decision, reason = "deny", f"FunSearch mutator: {exc}"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
