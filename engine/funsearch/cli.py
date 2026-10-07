"""Command-line clients. The engine communicates exclusively through SQLite."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

from .compile import compile_candidate
from .config import ConfigError, load_config
from .daemon import daemonize, recover_outputs
from .db import (BEST_SCORE, CHILDREN_OK, CHILDREN_SCORED, CLAIM_TIMEOUT_S, Database, END_BY,
                 PLATEAU_COUNT, SEED_SCORE, STARTED_AT, STATUS, STOP_REQUESTED)
from .evaluator import build_evaluator, evaluator_digest, snapshot_evaluator
from .evolve import seed_islands
from .normalize import normalized_hash
from .runtime import (STARTING, Rejected, RunOver, engine_alive, is_terminal, read_run,
                      require_running, top_programs)
from .tasks import create_task
from .workers import WorkerPool, worker_binary


# A waiting client polls the database at this period.
POLL_S = 0.1


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
    runs.add_parser("recover", help="write failed outputs for a run whose engine died").add_argument("run_dir")
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


def preflight(problem, cfg, library):
    """Score the seed with an evaluator library; return (source, result)."""
    worker_binary()  # Build/verify once before run setup and pool construction.
    source = (problem / "seed.c").read_text()
    with tempfile.TemporaryDirectory(prefix="funsearch-check-") as directory:
        seed = Path(directory) / "seed.c"
        seed.write_text(source)
        (Path(directory) / "candidate.h").write_bytes((problem / "candidate.h").read_bytes())
        ok, candidate, log = compile_candidate(cfg, seed, directory, "final")
        if not ok:
            return source, {"status": "ERROR", "score": 0, "sig": [], "msg": log}
        with WorkerPool(cfg, library, cfg.problem.instance, 1) as pool:
            result = pool.score(candidate, cfg.evaluator.timeout_s)
    return source, result


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
    run_id = (args.run_id if args.run_id is not None else
              datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2))
    if (not run_id or run_id in (".", "..") or Path(run_id).name != run_id
            or any(char in run_id for char in "\n\r\0")):
        raise ConfigError("run id must be a single directory name")
    root = problem / "runs" / run_id
    if root.exists():
        raise ConfigError(f"run already exists: {root}")
    built = build_evaluator(cfg, problem)
    # The run owns its evaluator like its other inputs. The seed is scored by
    # a staged snapshot that then moves into the run unchanged, so the seed
    # and the run's workers load the same files whatever happens to the
    # problem directory meanwhile.
    with tempfile.TemporaryDirectory(prefix="funsearch-start-") as staging:
        staged = snapshot_evaluator(built, staging)
        source, result = preflight(problem, cfg, staged)
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
        shutil.move(staged.parent, root / "evaluator")
    evaluator = root / "evaluator" / staged.name
    (root / "seed.c").write_text(source)
    for name in ("candidate.h", "problem.md"):
        (root / name).write_bytes((problem / name).read_bytes())
    # run.json is the engine's write-once manifest; integrations keep their
    # own state in separate files.
    metadata = {"run_id": run_id, "problem_dir": str(problem), "instance": cfg.problem.instance,
                "config": cfg.to_dict(), "pack_version": pack_version(),
                "evaluator_library": str(evaluator), "evaluator_source": str(built),
                "evaluator_sha256": evaluator_digest(evaluator.parent), "on_start": args.on_start,
                "on_finish": args.on_finish}
    (root / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")
    with Database(root / "db.sqlite") as db:
        with db.transaction():
            seed_islands(db, cfg, source, score=result["score"], sig=result["sig"], msg=result["msg"])
            for key, value in {STATUS: STARTING, STARTED_AT: time.time(),
                               SEED_SCORE: result["score"], BEST_SCORE: result["score"],
                               CHILDREN_SCORED: 0, CHILDREN_OK: 0, PLATEAU_COUNT: 0,
                               STOP_REQUESTED: False}.items():
                db.set_state(key, value)
    # Returns once the daemon has started its workers and run the on-start hook.
    daemonize(root)
    print(f"{run_id} {root}")
    return 0


def open_task(db, task_id):
    task = db.get_task(task_id)
    if task is None or task.status != "open":
        raise Rejected(f"task {task_id} is not open")
    return task


def wait_result(db, evaluation):
    """Wait while the engine lives, within the deadlines the engine published.

    Queue time is not charged to the request: the engine finishes every queued
    request by end_by. A claimed evaluation finishes within claim_timeout_s,
    which covers worker replacement around the candidate's own timeout.
    The engine publishes both keys together with status running.
    """
    claim_timeout_s = db.get_state(CLAIM_TIMEOUT_S)
    end_by = db.get_state(END_BY)
    if claim_timeout_s is None or end_by is None:
        raise RuntimeError("the engine published no client deadlines (claim_timeout_s, end_by); "
                           "it predates this funsearch, so restart the run")
    while True:
        current = db.get_evaluation(evaluation.id)
        if current.state == "done":
            if current.result.get("run_over"):
                raise RunOver("RUN_OVER")
            return current.result
        if is_terminal(db.get_state(STATUS)):
            raise RunOver("RUN_OVER")
        if not engine_alive(db.path.parent):
            raise RuntimeError("engine process is not alive")
        now = time.time()
        if (current.state == "running" and now - current.started_at > claim_timeout_s) or now > end_by:
            raise RuntimeError(f"timed out waiting for evaluation {evaluation.id}")
        time.sleep(POLL_S)


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
        (request / "candidate.so").unlink(missing_ok=True)
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
                if db.has_pending_submission(task.id):
                    raise Rejected("task already has a pending submission")
            evaluation = db.enqueue(kind, source_path, candidate, task_id=task.id, trial_n=trial_n)
    except BaseException as exc:
        # Never queued, so the engine will not discard it after scoring.
        candidate.unlink(missing_ok=True)
        if trial_n is not None:
            db.add_trial(task.id, trial_n, status="ERROR", msg=str(exc))
        raise
    result = wait_result(db, evaluation)
    if result.get("rejected"):
        raise Rejected(result["rejected"])
    # A non-OK score carries no meaning and may be null; keep the field numeric.
    score = 0 if result["score"] is None else result["score"]
    if kind == "try":
        print(f"RESULT {result['status']} {score} {result['msg']}")
    else:
        print(f"ACCEPTED {result['program_id']} {result['status']} {score}")
    return 0


def recover_run(args):
    root, metadata, cfg = read_run(args.run_dir, require_db=False)
    if engine_alive(root):
        raise ConfigError(f"engine is still running: {root}")
    # A cleanly finished run's engine is gone too; never relabel its outcome.
    try:
        status = json.loads((root / "summary.json").read_text()).get("status")
    except (OSError, ValueError, AttributeError):
        status = None
    if is_terminal(status):
        raise ConfigError(f"run already finished with status {status}; nothing to recover: {root}")
    database = recover_outputs(root, metadata, cfg)
    print(json.dumps({"run_dir": str(root), "recovered_from": str(database)}))
    return 0


def dispatch(args):
    if args.command == "check":
        problem = Path(args.problem).resolve()
        cfg = load_config(problem, instance=args.instance)
        _, result = preflight(problem, cfg, build_evaluator(cfg, problem))
        print(json.dumps(result))
        return 0 if result["status"] == "OK" else 1
    if args.command == "run" and args.run_command == "start":
        return start_run(args)
    if args.command == "run" and args.run_command == "recover":
        return recover_run(args)
    root, metadata, cfg = read_run(args.run_dir)
    readonly = args.command in ("run", "best", "rescore")
    with Database(root / "db.sqlite", readonly=readonly) as db:
        if args.command == "next-task":
            with db.transaction():
                require_running(db)
                task_id = create_task(db, cfg, root, root, args.slot)
            print(f"TASK {task_id} {root / 'tasks' / str(task_id)}")
        elif args.command in ("try", "submit"):
            return evaluate(args, root, cfg, db)
        elif args.command == "stop":
            db.set_state(STOP_REQUESTED, True)
            print("STOP_REQUESTED")
        elif args.command == "run":
            status = db.all_state()
            status.update(run_id=metadata["run_id"], engine_alive=engine_alive(root))
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


def report_unexpected(exc):
    """Name the exception: str() alone is empty or cryptic for many types."""
    if os.environ.get("FUNSEARCH_DEBUG"):
        traceback.print_exception(exc)
    detail = str(exc)
    print(type(exc).__name__ + (f": {detail}" if detail else ""), file=sys.stderr)


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
        report_unexpected(exc)
        return 1
