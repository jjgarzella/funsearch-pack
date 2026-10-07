#!/usr/bin/env python3
"""Gas City integration: run hooks, crash sweep, launch and mutator slots.

The standalone engine has no dependency on this module. This layer uses only
the engine's public surface: bin/funsearch verbs, and the Database class that
owns the run schema. bin/funsearch hands its `slot` verb to slot_main here.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import tomllib

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK / "engine"))
from funsearch.db import Database  # noqa: E402
from funsearch.runtime import FAILED, RUNNING, engine_alive, is_terminal  # noqa: E402

# run.json is the engine's write-once manifest. This layer keeps its own
# bead/slot/delivery bookkeeping beside it, under the run's lifecycle lock.
LIFECYCLE_FILE = "gc-lifecycle.json"
SLOT_CONTEXT_FILE = ".funsearch-slot.json"
SLOT_RETIRED_FILE = ".funsearch-retired.json"
# Formula/launch overrides tune the search only. Command-bearing keys
# (candidate.compile*, evaluator.build) stay in the problem's own problem.toml.
LAUNCH_OVERRIDE_SECTIONS = ("search.", "stop.")
MUTATOR_AGENT = PACK / "agents" / "mutator" / "agent.toml"


class UsageError(ValueError):
    """Bad input or ownership: exit 2, and never mutate city state."""


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


@contextmanager
def lock(path, *, nonblocking=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def city_path():
    value = os.environ.get("FS_CITY_PATH") or os.environ.get("GC_CITY_PATH")
    if not value or not Path(value).is_absolute():
        raise ValueError("FS_CITY_PATH or GC_CITY_PATH must name the absolute city directory")
    return Path(value).resolve()


def read_lifecycle(root, metadata):
    path = root / LIFECYCLE_FILE
    # Runs started before the split kept this state in run.json under "fs".
    return read_json(path) if path.exists() else dict(metadata.get("fs", {}))


def write_lifecycle(root, fs):
    write_json(root / LIFECYCLE_FILE, fs)


def context(saved):
    city = Path(saved["city"]) if saved.get("city") else city_path()
    rig = saved.get("rig") or os.environ.get("FS_RIG") or os.environ.get("GC_RIG")
    notify = saved.get("notify") or os.environ.get("FS_NOTIFY")
    if not rig or not notify:
        raise ValueError("run hooks require a rig (FS_RIG/GC_RIG) and FS_NOTIFY")
    return city, rig, notify


def gc(*args, city=None, rig=None):
    """Run gc and return stripped stdout; raise RuntimeError on failure.

    With city and rig, pin the store explicitly: hooks and the sweep can run
    from a different rig or worktree than the run, so inherited store context
    could select the wrong one. Without them, use the calling session's own
    scope: a mutator acting on the slot bead its session claimed.
    """
    env = os.environ.copy()
    cwd = None
    command = [os.environ.get("FS_GC", "gc"), *args]
    if city is not None:
        env.update(GC_CITY_PATH=str(city), GC_CITY=str(city), GC_RIG=rig)
        for key in ("BEADS_DIR", "GC_RIG_ROOT", "GC_BEADS_SCOPE_ROOT"):
            env.pop(key, None)
        cwd = city
        if args[0] == "bd":
            command += ["--rig", rig]
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"gc {' '.join(args[:3])}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def scoped_gc(city, rig, *args):
    return gc(*args, city=city, rig=rig)


def registry_path(city, run_id):
    if (not run_id or Path(run_id).name != run_id or run_id in (".", "..")
            or any(char in run_id for char in "\n\r\0")):
        raise ValueError("invalid run id")
    return city / ".gc" / "funsearch" / "active" / (run_id + ".json")


def remove_registry(city, root, run_id, run_bead):
    # An explicit id can be reused by a later run after completion. Retrying
    # an old finish hook must never delete that later run's registry entry.
    with lock(city / ".gc" / "funsearch" / "registry.lock"):
        registry = registry_path(city, run_id)
        if registry.exists():
            entry = read_json(registry)
            if entry["run_dir"] == str(root) and entry["run_bead"] == run_bead:
                registry.unlink()


def on_start(root):
    root = root.resolve()
    with lock(root / ".gc-lifecycle.lock"):
        # Different problems may use the same explicit run id. Serialize the
        # city registry check before creating any externally visible beads.
        with lock(city_path() / ".gc" / "funsearch" / "registry.lock"):
            start_locked(root)


def mutator_pool_cap():
    """The pack's mutator max_active_sessions, or None if it cannot be read.

    A city may patch the pool, so this is the pack default, not a guarantee.
    """
    try:
        with MUTATOR_AGENT.open("rb") as handle:
            return int(tomllib.load(handle)["max_active_sessions"])
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError):
        return None


def start_locked(root):
    metadata = read_json(root / "run.json")
    fs = read_lifecycle(root, metadata)
    city, rig, notify = context(fs)
    if fs.get("finished"):
        return
    fs.update(city=str(city), rig=rig, notify=notify)
    cfg = metadata["config"]
    pid = int((root / "engine.pid").read_text())
    registry = registry_path(city, metadata["run_id"])
    if registry.exists() and read_json(registry)["run_dir"] != str(root):
        raise ValueError(f"run id already registered in this city: {metadata['run_id']}")
    if not fs.get("run_bead"):
        fields = {"fs.problem": metadata["problem_dir"], "fs.run_dir": str(root),
                  "fs.instance": metadata["instance"], "fs.pid": str(pid),
                  "fs.status": RUNNING, "fs.notify": notify}
        fs["run_bead"] = scoped_gc(city, rig, "bd", "create",
            f"funsearch run {metadata['run_id']}: {cfg['problem']['name']} {metadata['instance']}",
            "--labels", "funsearch-run", "--metadata", json.dumps(fields),
            "--description", f"FunSearch results: {root}", "--silent")
    write_lifecycle(root, fs)
    entry = {"run_id": metadata["run_id"], "run_dir": str(root),
             "run_bead": fs["run_bead"], "rig": rig, "pid": pid, "notify": notify}
    write_json(registry, entry)
    cap = mutator_pool_cap()
    if cap is not None and cfg["search"]["mutators"] > cap:
        # Slots beyond the pool cap wait for a free session; the cap is also
        # shared by every concurrent run in the rig.
        print(f"warning: search.mutators={cfg['search']['mutators']} exceeds the mutator pool's "
              f"max_active_sessions={cap}, shared by all runs in rig {rig}; at most {cap} "
              "slots run at once", file=sys.stderr)
    slots = fs.setdefault("slots", {})
    routed = fs.setdefault("routed_slots", [])
    for index in range(1, cfg["search"]["mutators"] + 1):
        slot = str(index)
        if slot not in slots:
            fields = {"fs.run_dir": str(root), "fs.slot": slot,
                      "fs.tasks_per_session": str(cfg["search"]["tasks_per_session"]),
                      "fs.run_bead": fs["run_bead"], "opt_model": cfg["mutator"]["model"]}
            slots[slot] = scoped_gc(city, rig, "bd", "create",
                f"funsearch slot {metadata['run_id']}/{slot}", "--labels", "funsearch-slot",
                "--parent", fs["run_bead"], "--metadata", json.dumps(fields),
                "--description", "Follow the FunSearch mutator slot loop.", "--silent")
            write_lifecycle(root, fs)
        if slot not in routed:
            scoped_gc(city, rig, "sling", f"{rig}/funsearch.mutator", slots[slot], "--no-formula")
            routed.append(slot)
            write_lifecycle(root, fs)


def summary_note(root, summary):
    return (f"FunSearch run {summary['run_id']} {summary['status']}: {summary.get('reason', '')}\n"
            f"Best score: {summary.get('best_score')} (seed: {summary.get('seed_score')})\n"
            f"Best candidate: {root / 'best.c'}\n"
            f"Children scored: {summary.get('children_scored', 0)}; "
            f"throughput/hour: {summary.get('throughput_per_hour', 0)}; "
            f"OK rate: {summary.get('ok_rate', 0)}\n"
            f"Summary: {root / 'summary.json'}")


def finish_locked(root, metadata):
    fs = read_lifecycle(root, metadata)
    if not fs.get("run_bead"):
        # An on-start failure before bead creation has no city state to retire.
        return
    city, rig, notify = context(fs)
    if fs.get("finished"):
        remove_registry(city, root, metadata["run_id"], fs["run_bead"])
        return
    summary = read_json(root / "summary.json")
    if not is_terminal(summary["status"]):
        raise ValueError("finish hook needs a terminal summary status")
    note = summary_note(root, summary)
    if fs.get("summary_recorded") != summary:
        args = ["bd", "update", fs["run_bead"], "--append-notes", note]
        for key in ("status", "best_score", "children_scored", "ok_rate"):
            args += ["--set-metadata", f"fs.{key}={summary.get(key)}"]
        scoped_gc(city, rig, *args)
        fs["summary_recorded"] = summary
        write_lifecycle(root, fs)
    # Include blocked/deferred/in-progress slots, and avoid the default 50-row
    # limit. Slot children must close before the parent (bd enforces this).
    rows = json.loads(scoped_gc(city, rig, "bd", "list", "--parent", fs["run_bead"],
                         "--label", "funsearch-slot", "--all", "--limit", "0", "--json"))
    for row in rows:
        if row["status"] != "closed":
            # The engine/sweep owns run shutdown while an active mutator owns
            # each claimed slot. Terminal cleanup must override that claim.
            scoped_gc(city, rig, "bd", "close", row["id"], "--force", "--reason", summary["status"])
    if fs.get("run_closed") != summary["status"]:
        scoped_gc(city, rig, "bd", "close", fs["run_bead"], "--reason", summary["status"])
        fs["run_closed"] = summary["status"]
        write_lifecycle(root, fs)
    if not fs.get("mail_sent"):
        scoped_gc(city, rig, "mail", "send", notify, "-s",
           f"funsearch run {metadata['run_id']} {summary['status']}: best {summary.get('best_score')}",
           "-m", note)
        fs["mail_sent"] = True
    fs["finished"] = True
    write_lifecycle(root, fs)
    remove_registry(city, root, metadata["run_id"], fs["run_bead"])


def on_finish(root):
    root = root.resolve()
    with lock(root / ".gc-lifecycle.lock"):
        finish_locked(root, read_json(root / "run.json"))


def sweep_check():
    state = city_path() / ".gc" / "funsearch"
    active = state / "active"
    if not active.is_dir() or not any(active.glob("*.json")):
        return 1
    timestamp = state / "last-sweep"
    return int(timestamp.exists() and time.time() - timestamp.stat().st_mtime <= 1800)


def failed_summary(root, metadata):
    # The engine recovers authoritative counters and best.c from the live
    # database or its newest readable snapshot. Without either, keep what
    # the previous summary knew and mark the run failed.
    result = subprocess.run([str(PACK / "bin" / "funsearch"), "run", "recover", str(root)],
                            capture_output=True, text=True, timeout=300)
    if result.returncode == 0:
        return
    print(f"cannot recover {root}: {result.stderr.strip()}", file=sys.stderr)
    previous = read_json(root / "summary.json") if (root / "summary.json").exists() else {}
    previous.update(run_id=metadata["run_id"], instance=metadata["instance"],
                    status=FAILED, reason="engine died", ended_at=time.time())
    previous.setdefault("children_scored", 0)
    previous.setdefault("ok_rate", 0)
    previous.setdefault("best_score", None)
    write_json(root / "summary.json", previous)


def sweep():
    state = city_path() / ".gc" / "funsearch"
    errors = []
    with lock(state / "sweep.lock", nonblocking=True) as acquired:
        if not acquired:
            return
        try:
            for registry in sorted((state / "active").glob("*.json")):
                try:
                    entry = read_json(registry)
                    root = Path(entry["run_dir"]).resolve()
                    # The engine's exit record beats a PID a new process may reuse.
                    if engine_alive(root, entry["pid"]):
                        continue
                    with lock(root / ".gc-lifecycle.lock"):
                        metadata = read_json(root / "run.json")
                        summary = read_json(root / "summary.json") if (root / "summary.json").exists() else {}
                        if not is_terminal(summary.get("status")):
                            failed_summary(root, metadata)
                        # A terminal summary with a failed finish hook needs
                        # delivery retried, preserving its actual outcome.
                        finish_locked(root, metadata)
                except Exception as exc:
                    errors.append(f"{registry}: {exc}")
        finally:
            (state / "last-sweep").touch()
    if errors:
        raise RuntimeError("\n".join(errors))


def launch(args):
    # Parse overrides as data; never eval them as shell source.
    city_path()
    if not (os.environ.get("FS_RIG") or os.environ.get("GC_RIG")):
        raise ValueError("launch requires a rig-scoped agent (GC_RIG)")
    if not args.notify:
        raise ValueError("notify is required; Gas City exports no reliable slinger recipient")
    env = os.environ.copy()
    env["FS_NOTIFY"] = args.notify
    command = [str(PACK / "bin" / "funsearch"), "run", "start", args.problem]
    if args.instance:
        command += ["--instance", args.instance]
    for override in shlex.split(args.overrides):
        key = override.partition("=")[0]
        if not key.startswith(LAUNCH_OVERRIDE_SECTIONS):
            raise ValueError(f"launch overrides may set only search.* or stop.*, "
                             f"not {key!r}; edit problem.toml or use --instance instead")
        command += ["--set", override]
    command += ["--on-start", shlex.quote(str(PACK / "scripts" / "on-start.sh")),
                "--on-finish", shlex.quote(str(PACK / "scripts" / "on-finish.sh"))]
    return subprocess.run(command, env=env).returncode


def owned_slot(bead, *, allow_closed=False):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", bead):
        raise UsageError("invalid slot bead id")
    session = os.environ.get("GC_SESSION_ID")
    if not session:
        raise UsageError("slot helpers require GC_SESSION_ID")
    rows = json.loads(gc("bd", "show", bead, "--json"))
    row = rows[0] if isinstance(rows, list) and len(rows) == 1 else rows
    metadata = row.get("metadata", {})
    closed = allow_closed and row.get("status") == "closed"
    if row.get("id") != bead:
        raise UsageError("gc returned the wrong slot")
    if not closed and (row.get("status") != "in_progress"
            or metadata.get("gc.session_id") != session
            or row.get("assignee") not in {value for value in
                (session, os.environ.get("GC_SESSION_NAME"), os.environ.get("GC_ALIAS")) if value}):
        raise UsageError("slot is not claimed by this session")
    if not metadata.get("gc.routed_to"):
        raise UsageError("slot has no routing metadata")
    root = Path(metadata.get("fs.run_dir", ""))
    if not root.is_absolute() or not str(metadata.get("fs.slot", "")):
        raise UsageError("slot requires absolute fs.run_dir and fs.slot")
    try:
        count = int(metadata["fs.tasks_per_session"])
        if count < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise UsageError("fs.tasks_per_session must be a positive integer") from None
    context = {"bead": bead, "session": session, "run_dir": str(root.resolve()),
               "slot": str(metadata["fs.slot"]), "tasks_per_session": count}
    return row, context


def slot_command(command, bead):
    # Slot calls run in the mutator session that claimed the bead, so they use
    # that session's own store scope (gc() without city/rig).
    row, context = owned_slot(bead, allow_closed=command == "close")
    path = Path.cwd() / SLOT_CONTEXT_FILE
    if command == "show":
        database = Path(context["run_dir"]) / "db.sqlite"
        if database.exists():
            with Database(database, readonly=True) as db:
                tasks = db.open_tasks_for_slot(context["slot"])
            if len(tasks) > 1:
                raise UsageError("slot has multiple unfinished tasks")
            if tasks:
                context["pending_task"] = {
                    "id": tasks[0].id, "dir": str(database.parent / "tasks" / str(tasks[0].id)),
                    "trials_used": tasks[0].trials_used}
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
        raise UsageError("unknown slot command")
    # Deferred nudges can reach a provider before the controller finishes its
    # drain. Leave a receipt so the tool guard cannot let that context reclaim
    # another task; a different pool session ignores this old session receipt.
    retired = Path.cwd() / SLOT_RETIRED_FILE
    retired.write_text(json.dumps({"session": context["session"], "bead": bead}) + "\n")
    retired.chmod(0o600)
    path.unlink(missing_ok=True)
    print(f"SLOT_{command.upper()} {bead}")
    return 0


def slot_main(argv):
    """`funsearch slot show|release|close <bead>`, dispatched by bin/funsearch."""
    parser = argparse.ArgumentParser(prog="funsearch slot")
    parser.add_argument("command", choices=("show", "release", "close"))
    parser.add_argument("bead")
    args = parser.parse_args(argv)
    try:
        return slot_command(args.command, args.bead)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("on-start", "on-finish"):
        commands.add_parser(name).add_argument("run_dir", type=Path)
    for name in ("sweep-check", "sweep"):
        commands.add_parser(name)
    start = commands.add_parser("launch")
    start.add_argument("problem")
    start.add_argument("--instance", default="")
    start.add_argument("--notify", required=True)
    start.add_argument("--overrides", default="")
    args = parser.parse_args()
    try:
        if args.command == "on-start":
            on_start(args.run_dir)
        elif args.command == "on-finish":
            on_finish(args.run_dir)
        elif args.command == "sweep-check":
            return sweep_check()
        elif args.command == "sweep":
            sweep()
        else:
            return launch(args)
    except Exception as exc:
        print(f"funsearch {args.command}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
