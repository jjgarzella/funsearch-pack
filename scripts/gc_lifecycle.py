#!/usr/bin/env python3
"""Gas City integration; the standalone engine has no dependency on this module."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK / "engine"))
from funsearch.runtime import pid_alive  # noqa: E402


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


def context(metadata):
    saved = metadata.get("fs", {})
    city = Path(saved["city"]) if saved.get("city") else city_path()
    rig = saved.get("rig") or os.environ.get("FS_RIG") or os.environ.get("GC_RIG")
    notify = saved.get("notify") or os.environ.get("FS_NOTIFY")
    if not rig or not notify:
        raise ValueError("run hooks require a rig (FS_RIG/GC_RIG) and FS_NOTIFY")
    return city, rig, notify


def gc(city, rig, *args):
    env = os.environ.copy()
    # The sweep can run in a different rig from the run. Explicit bd scope and
    # a city cwd prevent inherited worktree/store context from selecting it.
    env.update(GC_CITY_PATH=str(city), GC_CITY=str(city), GC_RIG=rig)
    for key in ("BEADS_DIR", "GC_RIG_ROOT", "GC_BEADS_SCOPE_ROOT"):
        env.pop(key, None)
    command = [os.environ.get("FS_GC", "gc"), *args]
    if args[0] == "bd":
        command += ["--rig", rig]
    result = subprocess.run(command, cwd=city, env=env, capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"gc {' '.join(args[:3])}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def registry_path(city, run_id):
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("invalid run id")
    return city / ".gc" / "funsearch" / "active" / (run_id + ".json")


def remove_registry(city, root, metadata):
    # An explicit id can be reused by a later run after completion. Retrying
    # an old finish hook must never delete that later run's registry entry.
    with lock(city / ".gc" / "funsearch" / "registry.lock"):
        registry = registry_path(city, metadata["run_id"])
        if registry.exists():
            entry = read_json(registry)
            if entry["run_dir"] == str(root) and entry["run_bead"] == metadata["fs"]["run_bead"]:
                registry.unlink()


def on_start(root):
    root = root.resolve()
    with lock(root / ".gc-lifecycle.lock"):
        # Different problems may use the same explicit run id. Serialize the
        # city registry check before creating any externally visible beads.
        with lock(city_path() / ".gc" / "funsearch" / "registry.lock"):
            start_locked(root)


def start_locked(root):
    metadata = read_json(root / "run.json")
    city, rig, notify = context(metadata)
    fs = metadata.setdefault("fs", {})
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
                  "fs.status": "running", "fs.notify": notify}
        fs["run_bead"] = gc(city, rig, "bd", "create",
            f"funsearch run {metadata['run_id']}: {cfg['problem']['name']} {metadata['instance']}",
            "--labels", "funsearch-run", "--metadata", json.dumps(fields),
            "--description", f"FunSearch results: {root}", "--silent")
    metadata["run_bead"] = fs["run_bead"]
    write_json(root / "run.json", metadata)
    entry = {"run_id": metadata["run_id"], "run_dir": str(root),
             "run_bead": fs["run_bead"], "rig": rig, "pid": pid, "notify": notify}
    write_json(registry, entry)
    slots = fs.setdefault("slots", {})
    routed = fs.setdefault("routed_slots", [])
    for index in range(1, cfg["search"]["mutators"] + 1):
        slot = str(index)
        if slot not in slots:
            fields = {"fs.run_dir": str(root), "fs.slot": slot,
                      "fs.tasks_per_session": str(cfg["search"]["tasks_per_session"]),
                      "fs.run_bead": fs["run_bead"], "opt_model": cfg["mutator"]["model"]}
            slots[slot] = gc(city, rig, "bd", "create",
                f"funsearch slot {metadata['run_id']}/{slot}", "--labels", "funsearch-slot",
                "--parent", fs["run_bead"], "--metadata", json.dumps(fields),
                "--description", "Follow the FunSearch mutator slot loop.", "--silent")
            write_json(root / "run.json", metadata)
        if slot not in routed:
            gc(city, rig, "sling", f"{rig}/funsearch.mutator", slots[slot], "--no-formula")
            routed.append(slot)
            write_json(root / "run.json", metadata)


def summary_note(root, summary):
    return (f"FunSearch run {summary['run_id']} {summary['status']}: {summary.get('reason', '')}\n"
            f"Best score: {summary.get('best_score')} (seed: {summary.get('seed_score')})\n"
            f"Best candidate: {root / 'best.c'}\n"
            f"Children scored: {summary.get('children_scored', 0)}; "
            f"throughput/hour: {summary.get('throughput_per_hour', 0)}; "
            f"OK rate: {summary.get('ok_rate', 0)}\n"
            f"Summary: {root / 'summary.json'}")


def finish_locked(root, metadata):
    fs = metadata.get("fs", {})
    if not fs.get("run_bead"):
        # An on-start failure before bead creation has no city state to retire.
        return
    city, rig, notify = context(metadata)
    if fs.get("finished"):
        remove_registry(city, root, metadata)
        return
    summary = read_json(root / "summary.json")
    if summary["status"] not in ("completed", "stopped", "failed"):
        raise ValueError("finish hook needs a terminal summary status")
    note = summary_note(root, summary)
    if fs.get("summary_recorded") != summary:
        args = ["bd", "update", fs["run_bead"], "--append-notes", note]
        for key in ("status", "best_score", "children_scored", "ok_rate"):
            args += ["--set-metadata", f"fs.{key}={summary.get(key)}"]
        gc(city, rig, *args)
        fs["summary_recorded"] = summary
        write_json(root / "run.json", metadata)
    # Include blocked/deferred/in-progress slots, and avoid the default 50-row
    # limit. Slot children must close before the parent (bd enforces this).
    rows = json.loads(gc(city, rig, "bd", "list", "--parent", fs["run_bead"],
                         "--label", "funsearch-slot", "--all", "--limit", "0", "--json"))
    for row in rows:
        if row["status"] != "closed":
            # The engine/sweep owns run shutdown while an active mutator owns
            # each claimed slot. Terminal cleanup must override that claim.
            gc(city, rig, "bd", "close", row["id"], "--force", "--reason", summary["status"])
    if fs.get("run_closed") != summary["status"]:
        gc(city, rig, "bd", "close", fs["run_bead"], "--reason", summary["status"])
        fs["run_closed"] = summary["status"]
        write_json(root / "run.json", metadata)
    if not fs.get("mail_sent"):
        gc(city, rig, "mail", "send", notify, "-s",
           f"funsearch run {metadata['run_id']} {summary['status']}: best {summary.get('best_score')}",
           "-m", note)
        fs["mail_sent"] = True
    fs["finished"] = True
    write_json(root / "run.json", metadata)
    remove_registry(city, root, metadata)


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
    # Recover authoritative counters and best.c from the live database, falling
    # back to the latest readable backup if the daemon died during a write.
    from funsearch.daemon import write_outputs
    from funsearch.db import Database
    from funsearch.runtime import read_run
    previous = read_json(root / "summary.json") if (root / "summary.json").exists() else {}
    backups = sorted((root / "snapshots").glob("db-*.sqlite"),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    for database in [root / "db.sqlite", *backups]:
        if not database.exists():
            continue
        try:
            _, _, cfg = read_run(root)
            with Database(database) as db:
                write_outputs(db, root, metadata, cfg, "failed", "engine died")
            return
        except Exception as exc:
            print(f"cannot recover {database}: {exc}", file=sys.stderr)
    previous.update(run_id=metadata["run_id"], instance=metadata["instance"],
                    status="failed", reason="engine died", ended_at=time.time())
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
                    if pid_alive(entry["pid"]):
                        continue
                    root = Path(entry["run_dir"]).resolve()
                    with lock(root / ".gc-lifecycle.lock"):
                        metadata = read_json(root / "run.json")
                        summary = read_json(root / "summary.json") if (root / "summary.json").exists() else {}
                        if summary.get("status") not in ("completed", "stopped", "failed"):
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
        command += ["--set", override]
    command += ["--on-start", shlex.quote(str(PACK / "scripts" / "on-start.sh")),
                "--on-finish", shlex.quote(str(PACK / "scripts" / "on-finish.sh"))]
    return subprocess.run(command, env=env).returncode


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
