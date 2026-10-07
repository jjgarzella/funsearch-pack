#!/usr/bin/env python3
"""Persistent fake gc: argv log, canned bead ids, metadata and close gates."""
import json
import fcntl
import os
from pathlib import Path
import sys
import time

root = Path(os.environ["FS_SHIM_DIR"])
args = sys.argv[1:]
with (root / "argv.jsonl").open("a") as log:
    log.write(json.dumps({"args": args, "cwd": os.getcwd(), "rig": os.getenv("GC_RIG")}) + "\n")
if os.environ.get("FS_SHIM_FAIL") == args[0]:
    sys.exit("injected gc failure")
if os.environ.get("FS_SHIM_PAUSE") == " ".join(args[:2]):
    try:
        (root / "pause-owner").mkdir()
    except FileExistsError:
        pass
    else:
        (root / "pause-entered").touch()
        deadline = time.monotonic() + 10
        while not (root / "pause-release").exists():
            if time.monotonic() >= deadline:
                sys.exit("gc shim pause timed out")
            time.sleep(0.01)
# Separate hooks can use the shim concurrently, just as they share a city
# database. Serialize its tiny read/modify/write store to prevent lost writes.
store_lock = (root / "store.lock").open("a")
fcntl.flock(store_lock, fcntl.LOCK_EX)
state_file = root / "beads.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {}


def flag(name, default=None):
    return args[args.index(name) + 1] if name in args else default


if args[:2] == ["bd", "create"]:
    assert "--rig" in args
    bead = f"test-{len(state) + 1}"
    state[bead] = {"id": bead, "title": args[2], "status": "open",
                   "metadata": json.loads(flag("--metadata", "{}")),
                   "parent": flag("--parent"), "labels": [flag("--labels")],
                   "rig": flag("--rig")}
    print(bead)
elif args[:2] == ["bd", "update"]:
    row = state[args[2]]
    assert row["rig"] == flag("--rig")
    for index, arg in enumerate(args):
        if arg == "--set-metadata":
            key, value = args[index + 1].split("=", 1)
            row["metadata"][key] = value
    row["notes"] = row.get("notes", "") + flag("--append-notes", "")
elif args[:2] == ["bd", "list"]:
    assert flag("--limit") == "0" and "--all" in args
    print(json.dumps([row for row in state.values()
                      if row["parent"] == flag("--parent") and row["rig"] == flag("--rig")
                      and flag("--label") in row["labels"]]))
elif args[:2] == ["bd", "close"]:
    bead = args[2]
    assert state[bead]["rig"] == flag("--rig")
    if state[bead].get("assignee") and "--force" not in args:
        sys.exit("cannot close a slot claimed by another actor without --force")
    assert not any(row["parent"] == bead and row["status"] != "closed" for row in state.values())
    state[bead]["status"] = "closed"
    state[bead]["close_reason"] = flag("--reason")
elif args[0] == "sling":
    assert args[-1] == "--no-formula"
    state[args[2]]["metadata"]["gc.routed_to"] = args[1]
elif args[:2] == ["mail", "send"]:
    with (root / "mail.jsonl").open("a") as mail:
        mail.write(json.dumps(args) + "\n")
else:
    sys.exit(f"unexpected fake gc command: {args}")
state_file.write_text(json.dumps(state))
