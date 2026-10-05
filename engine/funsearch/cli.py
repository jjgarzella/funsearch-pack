"""Command-line clients. The engine communicates exclusively through SQLite."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid

from .compile import compile_candidate
from .config import ConfigError, load_config
from .daemon import daemonize
from .db import Database
from .evaluator import build_evaluator
from .evolve import seed_islands
from .normalize import normalized_hash
from .runtime import Rejected, RunOver, pid_alive, read_run, require_running, run_hook, top_programs
from .tasks import create_task
from .workers import WorkerPool


def parser():
    cli = argparse.ArgumentParser(prog="funsearch")
    commands = cli.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate and score the seed")
    check.add_argument("problem")
    check.add_argument("--instance")
    run = commands.add_parser("run", help="start or inspect a run")
    runs = run.add_subparsers(dest="run_command", required=True)
    start = runs.add_parser("start")
    start.add_argument("problem")
    start.add_argument("--instance")
    start.add_argument("--run-id")
    start.add_argument("--set", action="append", default=[], metavar="key=val")
    start.add_argument("--on-start")
    start.add_argument("--on-finish")
    runs.add_parser("status").add_argument("run_dir")
    best = commands.add_parser("best", help="list highest scoring programs")
    best.add_argument("run_dir")
    best.add_argument("-k", type=int, default=10)
    commands.add_parser("stop", help="request a graceful stop").add_argument("run_dir")
    task = commands.add_parser("next-task", help="create a mutator task")
    task.add_argument("run_dir")
    task.add_argument("--slot", default="")
    for name in ("try", "submit"):
        command = commands.add_parser(name)
        command.add_argument("run_dir")
        command.add_argument("task", type=int)
        command.add_argument("source")
    rescore = commands.add_parser("rescore", help="score a stored program on another instance")
    rescore.add_argument("run_dir")
    rescore.add_argument("program", metavar="id|best")
    rescore.add_argument("--instance", required=True)
    return cli


def preflight(problem, cfg):
    library = build_evaluator(cfg, problem)
    source = (problem / "seed.c").read_text()
    with tempfile.TemporaryDirectory(prefix="funsearch-check-") as directory:
        seed = Path(directory) / "seed.c"
        seed.write_text(source)
        (Path(directory) / "candidate.h").write_bytes((problem / "candidate.h").read_bytes())
        ok, candidate, log = compile_candidate(cfg, seed, directory, "final")
        if not ok:
            return library, source, {"status": "ERROR", "score": 0, "sig": [], "msg": log}
        with WorkerPool(cfg, library, cfg.problem.instance, 1) as pool:
            result = pool.score(candidate, cfg.evaluator.timeout_s)
    return library, source, result


def pack_version():
    try:
        result = subprocess.run(["git", "describe", "--always", "--dirty"],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                                text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def start_run(args):
    problem = Path(args.problem).resolve()
    cfg = load_config(problem, args.set, instance=args.instance)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    if not run_id or run_id in (".", "..") or Path(run_id).name != run_id or "\n" in run_id:
        raise ConfigError("run id must be a single directory name")
    root = problem / "runs" / run_id
    if root.exists():
        raise ConfigError(f"run already exists: {root}")
    library, source, result = preflight(problem, cfg)
    if result["status"] != "OK":
        print(json.dumps(result))
        return 1
    root.parent.mkdir(exist_ok=True)
    ignore = root.parent / ".gitignore"
    try:
        with ignore.open("x") as file:
            file.write("*\n")
    except FileExistsError:
        pass
    root.mkdir()
    (root / "seed.c").write_text(source)
    for name in ("candidate.h", "problem.md"):
        (root / name).write_bytes((problem / name).read_bytes())
    metadata = {"run_id": run_id, "problem_dir": str(problem), "instance": cfg.problem.instance,
                "config": cfg.to_dict(), "pack_version": pack_version(),
                "evaluator_library": str(library), "on_start": args.on_start,
                "on_finish": args.on_finish}
    (root / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")
    with Database(root / "db.sqlite") as db:
        with db.transaction():
            seed_islands(db, cfg, source, score=result["score"], sig=result["sig"], msg=result["msg"])
            for key, value in {"status": "starting", "started_at": time.time(),
                               "seed_score": result["score"], "best_score": result["score"],
                               "children_scored": 0, "children_ok": 0, "plateau_count": 0,
                               "stop_requested": False, "start_hook_done": False}.items():
                db.set_state(key, value)
    daemonize(root)
    with Database(root / "db.sqlite") as db:
        try:
            run_hook(args.on_start, root)
        except BaseException as exc:
            db.set_state("start_error", f"on-start hook: {exc}")
            raise
        finally:
            db.set_state("start_hook_done", True)
    print(f"{run_id} {root}")
    return 0


def open_task(db, task_id):
    task = db.get_task(task_id)
    if task is None or task.status != "open":
        raise Rejected(f"task {task_id} is not open")
    return task


def wait_result(db, evaluation, cfg):
    deadline = time.monotonic() + cfg.evaluator.timeout_s + 60
    while time.monotonic() < deadline:
        current = db.get_evaluation(evaluation.id)
        if current.state == "done":
            if current.result.get("run_over"):
                raise RunOver("RUN_OVER")
            return current.result
        if db.get_state("status") in ("completed", "stopped", "failed"):
            raise RunOver("RUN_OVER")
        if not pid_alive(db.get_state("pid")):
            raise RuntimeError("engine process is not alive")
        time.sleep(0.02)
    raise RuntimeError(f"timed out waiting for evaluation {evaluation.id}")


def evaluate(args, root, cfg, db):
    kind = args.command
    trial_n = None
    with db.transaction():
        require_running(db)
        task = open_task(db, args.task)
        if kind == "try":
            try:
                trial_n = db.reserve_trial(task.id, cfg.search.trial_budget)
            except ValueError as exc:
                raise Rejected(str(exc)) from exc
    # Each request owns immutable source and output paths, so later edits to
    # child.c cannot replace a queued candidate or its recorded source.
    request = root / "requests" / uuid.uuid4().hex
    request.mkdir(parents=True)
    (request / "candidate.h").write_bytes((root / "candidate.h").read_bytes())
    source_path = request / "candidate.c"
    try:
        source = Path(args.source).read_text()
        source_path.write_text(source)
        if kind == "submit" and db.has_normalized_hash(normalized_hash(source)):
            raise Rejected("duplicate candidate")
        ok, candidate, log = compile_candidate(cfg, source_path, request, "try" if kind == "try" else "final")
    except BaseException as exc:
        if trial_n is not None:
            db.add_trial(task.id, trial_n, status="ERROR", msg=str(exc))
        raise
    if not ok:
        if trial_n is not None:
            db.add_trial(task.id, trial_n, status="ERROR", score=0, msg=log)
            print(f"RESULT ERROR 0 {log}")
            return 0
        raise Rejected(log)
    try:
        with db.transaction():
            require_running(db)
            open_task(db, task.id)
            if kind == "submit":
                if db.has_normalized_hash(normalized_hash(source)):
                    raise Rejected("duplicate candidate")
                pending = db.connection.execute(
                    "SELECT 1 FROM evalq WHERE task_id=? AND kind='submit' AND state!='done'",
                    (task.id,)).fetchone()
                if pending:
                    raise Rejected("task already has a pending submission")
            evaluation = db.enqueue(kind, source_path, candidate, task_id=task.id, trial_n=trial_n)
    except BaseException as exc:
        if trial_n is not None:
            db.add_trial(task.id, trial_n, status="ERROR", msg=str(exc))
        raise
    result = wait_result(db, evaluation, cfg)
    if result.get("rejected"):
        raise Rejected(result["rejected"])
    if kind == "try":
        print(f"RESULT {result['status']} {result['score']} {result['msg']}")
    else:
        print(f"ACCEPTED {result['program_id']} {result['status']} {result['score']}")
    return 0


def dispatch(args):
    if args.command == "check":
        problem = Path(args.problem).resolve()
        cfg = load_config(problem, instance=args.instance)
        _, _, result = preflight(problem, cfg)
        print(json.dumps(result))
        return 0 if result["status"] == "OK" else 1
    if args.command == "run" and args.run_command == "start":
        return start_run(args)
    root, metadata, cfg = read_run(args.run_dir)
    with Database(root / "db.sqlite") as db:
        if args.command == "next-task":
            with db.transaction():
                require_running(db)
                task_id = create_task(db, cfg, root, root, args.slot)
            print(f"TASK {task_id} {root / 'tasks' / str(task_id)}")
        elif args.command in ("try", "submit"):
            return evaluate(args, root, cfg, db)
        elif args.command == "stop":
            db.set_state("stop_requested", True)
            print("STOP_REQUESTED")
        elif args.command == "run":
            status = {row["key"]: json.loads(row["value"]) for row in
                      db.connection.execute("SELECT * FROM state")}
            status.update(run_id=metadata["run_id"], pid_alive=pid_alive(status.get("pid")))
            print(json.dumps(status))
        elif args.command == "best":
            if args.k <= 0:
                raise ConfigError("-k must be positive")
            print(json.dumps([{"id": p.id, "island": p.island, "score": p.score,
                               "sig": p.sig, "status": p.status, "msg": p.msg}
                              for p in top_programs(db, args.k)]))
        elif args.command == "rescore":
            if args.program == "best":
                best = top_programs(db, 1)
                program = best[0] if best else None
            else:
                try:
                    program = db.get_program(int(args.program))
                except ValueError:
                    raise ConfigError("program must be an integer id or best") from None
            if program is None:
                raise ConfigError("program does not exist")
            with tempfile.TemporaryDirectory(prefix="funsearch-rescore-") as directory:
                source = Path(directory) / "candidate.c"
                source.write_text(program.source)
                (Path(directory) / "candidate.h").write_bytes((root / "candidate.h").read_bytes())
                ok, candidate, log = compile_candidate(cfg, source, directory, "final")
                if not ok:
                    raise RuntimeError(log)
                with WorkerPool(cfg, metadata["evaluator_library"], args.instance, 1) as pool:
                    print(json.dumps(pool.score(candidate, cfg.evaluator.timeout_s)))
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return dispatch(args)
    except RunOver:
        print("RUN_OVER")
        return 3
    except Rejected as exc:
        print(f"REJECTED {exc}")
        return 4
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (Exception, KeyboardInterrupt) as exc:
        print(str(exc), file=sys.stderr)
        return 1
