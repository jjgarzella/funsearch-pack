"""Run owner: asynchronous scoring, stop conditions, and durable outputs."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import faulthandler
import json
import os
from pathlib import Path
import random
import signal
import sqlite3
import sys
import time
import traceback

from .compile import try_worker_env
from .db import Database
from .evolve import reset_weakest
from .normalize import normalized_hash
from .runtime import (COMPLETED, FAILED, HOOK_TIMEOUT_S, RUNNING, STOPPED, STOPPING,
                      read_run, run_hook, top_programs)
from .workers import WorkerPool, score_budget_s, start_budget_s


# Engine periods. daemonize reads these when it launches an engine and passes
# them to serve() as arguments; a running engine never reads the globals.
SNAPSHOT_PERIOD_S = 600
STOP_GRACE_S = 120
# Abandonment only matters after ~2 trial budgets of inactivity (>= 20 min at
# defaults), so a write transaction every loop tick would be wasted contention.
ABANDON_PERIOD_S = 30
# A database lock held this long without one successful tick fails the run.
BUSY_RETRY_S = 60
# Upper bound on one loop tick's wait. A finished evaluation wakes the loop at
# once; new queue entries and state changes are noticed within this period.
POLL_S = 0.1
# Scheduling slack on top of the scoring budget before a waiting client gives up.
CLAIM_SLACK_S = 60


def log_event(message):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
          f"pid={os.getpid()} {message}", file=sys.stderr, flush=True)


def _busy(exc):
    message = str(exc)
    return "database is locked" in message or "database is busy" in message


def snapshot(db, root):
    directory = root / "snapshots"
    directory.mkdir(exist_ok=True)
    n = db.increment_state("snapshots")
    db.backup(directory / f"db-{n}.sqlite")
    files = sorted(directory.glob("db-*.sqlite"), key=lambda p: int(p.stem[3:]))
    for path in files[:-5]:
        path.unlink()


def stop_reason(db, cfg):
    state = db.all_state()  # One read transaction for every condition.
    if state.get("stop_requested", False):
        return "stop requested"
    if time.time() - state["started_at"] >= cfg.stop.duration_s:
        return "duration_s"
    if state.get("children_scored", 0) >= cfg.stop.max_children:
        return "max_children"
    if cfg.stop.plateau_children and state.get("plateau_count", 0) >= cfg.stop.plateau_children:
        return "plateau_children"
    return None


def discard_candidate(evaluation):
    """Drop a finished request's compiled library; its source stays for audit."""
    try:
        Path(evaluation.so_path).unlink(missing_ok=True)
    except OSError as exc:
        log_event(f"cannot remove {evaluation.so_path}: {exc}")


def store_result(db, evaluation, result):
    """Commit the result, task completion, and counters as one transaction."""
    result = dict(result)
    if evaluation.kind != "try":
        # Read and tokenize before taking the writer lock.
        source = Path(evaluation.src_path).read_text()
        norm_hash = normalized_hash(source)
    with db.transaction():
        if evaluation.kind == "try":
            db.add_trial(evaluation.task_id, evaluation.trial_n,
                         status=result["status"], score=result["score"], msg=result["msg"])
        else:
            task = db.get_task(evaluation.task_id)
            # Every scored submission spends budget, stored or not.
            db.increment_state("children_scored")
            if result["status"] == "OK":
                db.increment_state("children_ok")
            duplicate = db.has_normalized_hash(norm_hash)
            if result["status"] == "OK":
                duplicate = duplicate or db.has_scored_duplicate(result["score"], result["sig"])
            if task.status != "open":
                result["rejected"] = "task is no longer open"
            elif duplicate:
                result["rejected"] = "duplicate candidate"
            else:
                program = db.add_program(task.island, source, parent_ids=task.parent_ids,
                                         status=result["status"], score=result["score"],
                                         sig=result["sig"], msg=result["msg"], norm_hash=norm_hash)
                db.close_task(task.id)
                result["program_id"] = program.id
            # Only a stored program can improve the best score; a rejected
            # child must not leave later children chasing a phantom best.
            if "program_id" in result and result["status"] == "OK" and result["score"] > db.get_state("best_score"):
                db.set_state("best_score", result["score"])
                db.set_state("plateau_count", 0)
            else:
                db.increment_state("plateau_count")
        db.finish_evaluation(evaluation.id, result)


def cancel_queued(db):
    while (evaluation := db.claim_evaluation()) is not None:
        result = {"status": "ERROR", "score": 0, "sig": [], "msg": "RUN_OVER", "run_over": True}
        with db.transaction():
            if evaluation.kind == "try":
                db.add_trial(evaluation.task_id, evaluation.trial_n,
                             status="ERROR", msg="RUN_OVER")
            db.finish_evaluation(evaluation.id, result)
        discard_candidate(evaluation)


def finish_unscored(db, reason):
    """Finish every queued or running request; none of them will be scored."""
    cancel_queued(db)
    for evaluation in db.running_evaluations():
        result = {"status": "ERROR", "score": 0, "sig": [], "msg": reason, "run_over": True}
        with db.transaction():
            if evaluation.kind == "try":
                db.add_trial(evaluation.task_id, evaluation.trial_n, status="ERROR", msg=reason)
            db.finish_evaluation(evaluation.id, result)
        discard_candidate(evaluation)


def write_outputs(db, root, metadata, cfg, status, reason):
    ended = time.time()
    started = db.get_state("started_at")
    scored = db.get_state("children_scored", 0)
    ok = db.get_state("children_ok", 0)
    best = top_programs(db)
    summary = {"run_id": metadata["run_id"], "instance": cfg.problem.instance,
               "status": status, "reason": reason, "started_at": started, "ended_at": ended,
               "children_scored": scored, "children_ok": ok,
               "ok_rate": ok / scored if scored else 0,
               "best_score": best[0].score if best else None,
               "seed_score": db.get_state("seed_score"),
               "best_program_id": best[0].id if best else None,
               "throughput_per_hour": scored * 3600 / max(ended - started, 0.001),
               "islands": [{"island": i, "best_program_id": p.id if p else None,
                            "best_score": p.score if p else None}
                           for i in range(cfg.search.islands)
                           for p in [db.best_program(i)]]}
    with db.transaction():
        db.set_state("status", status)
        db.set_state("reason", reason)
        db.set_state("ended_at", ended)
    temporary = root / "summary.json.tmp"
    temporary.write_text(json.dumps(summary, indent=2) + "\n")
    temporary.replace(root / "summary.json")
    if best:
        (root / "best.c").write_text(best[0].source)
    (root / "top").mkdir(exist_ok=True)
    for rank, program in enumerate(best, 1):
        (root / "top" / f"{rank}-{program.score:g}.c").write_text(program.source)


def recover_outputs(root, metadata, cfg, reason="engine died"):
    """Rewrite failed outputs after the engine died; return the database used.

    Prefer the live database and fall back to the newest readable snapshot if
    the engine died mid-write and left it unreadable.
    """
    backups = sorted((root / "snapshots").glob("db-*.sqlite"),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    errors = []
    for database in [root / "db.sqlite", *backups]:
        if not database.exists():
            continue
        try:
            with Database(database, migrate=True) as db:
                try:
                    # Clients may still wait on requests the dead engine held.
                    finish_unscored(db, reason)
                except Exception:
                    traceback.print_exc()  # Outputs matter more than queue rows.
                write_outputs(db, root, metadata, cfg, FAILED, reason)
            return database
        except Exception as exc:
            errors.append(f"{database}: {exc}")
    raise RuntimeError("no readable run database" + "".join(f"\n{e}" for e in errors))


def serve(run_dir, ready_fd, *, snapshot_period_s, stop_grace_s, abandon_period_s, busy_retry_s):
    root, metadata, cfg = read_run(run_dir)
    pools, executors, pending = {}, {}, {}
    db = Database(root / "db.sqlite", migrate=True)
    status, reason = FAILED, "daemon startup failed"
    notified = False
    stop_deadline = None
    signal_stop = False

    def request_stop(signum, _frame):
        nonlocal signal_stop
        log_event(f"received {signal.Signals(signum).name}; requesting shutdown")
        signal_stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGHUP, request_stop)
    log_event(f"starting run={metadata['run_id']} workers_per_pool={cfg.search.workers}")
    try:
        evaluator = metadata["evaluator_library"]
        for kind, env in (("submit", None), ("try", try_worker_env(cfg))):
            pools[kind] = WorkerPool(cfg, evaluator, cfg.problem.instance,
                                     cfg.search.workers, env)
            executors[kind] = ThreadPoolExecutor(max_workers=cfg.search.workers)
        # Publish how long a waiting client may expect: a claimed evaluation
        # finishes within claim_timeout_s, and every request by end_by.
        claim_timeout_s = score_budget_s(cfg.evaluator.timeout_s) + CLAIM_SLACK_S
        with db.transaction():
            db.set_state("claim_timeout_s", claim_timeout_s)
            db.set_state("end_by", db.get_state("started_at") + cfg.stop.duration_s
                         + stop_grace_s + claim_timeout_s)
            db.set_state("status", RUNNING)
            db.set_state("pid", os.getpid())
        (root / "engine.pid").write_text(str(os.getpid()) + "\n")
        log_event("worker pools ready")
        # The engine owns startup as it owns shutdown: the on-start hook runs
        # here, before readiness and before any evaluation is dispatched, so
        # the run never depends on its launcher surviving. Requests that
        # mutators enqueue meanwhile wait in the queue.
        try:
            run_hook(metadata.get("on_start"), root)
        except Exception as exc:
            raise RuntimeError(f"on-start hook: {exc}") from None
        notified = True
        try:
            os.write(ready_fd, b"READY\n")
        except BrokenPipeError:
            log_event("launcher exited before readiness; the run continues")
        os.close(ready_fd)
        last_reset = last_snapshot = last_abandon = time.monotonic()
        rng = random.Random()
        busy_since = None
        while True:
            try:
                if signal_stop:
                    db.set_state("stop_requested", True)
                for future, evaluation in list(pending.items()):
                    if future.done():
                        try:
                            result = future.result()
                        except Exception:
                            if stop_deadline is None:
                                raise
                            result = {"status": "ERROR", "score": 0, "sig": [],
                                      "msg": "evaluation interrupted at shutdown"}
                        store_result(db, evaluation, result)
                        discard_candidate(evaluation)
                        del pending[future]
                # Checked once per tick, after every finished result is stored.
                if stop_deadline is None:
                    found = stop_reason(db, cfg)
                    if found:
                        reason = found
                        status = STOPPED if found == "stop requested" else COMPLETED
                        db.set_state("status", STOPPING)
                        stop_deadline = time.monotonic() + stop_grace_s
                if stop_deadline is not None:
                    cancel_queued(db)
                    if not pending:
                        break
                    if time.monotonic() >= stop_deadline:
                        for pool in pools.values():
                            pool.abort()
                else:
                    for kind in ("submit", "try"):
                        occupied = sum(e.kind == kind for e in pending.values())
                        capacity = cfg.search.workers - occupied
                        if kind == "submit":
                            capacity = min(capacity, cfg.stop.max_children -
                                           db.get_state("children_scored", 0) - occupied)
                        for _ in range(capacity):
                            evaluation = db.claim_evaluation(kind)
                            if evaluation is None:
                                break
                            future = executors[kind].submit(pools[kind].score, evaluation.so_path,
                                                             cfg.evaluator.timeout_s)
                            pending[future] = evaluation
                now = time.monotonic()
                if stop_deadline is None and now - last_reset >= cfg.search.reset_period_s:
                    reset_weakest(db, rng)
                    last_reset = now
                if now - last_snapshot >= snapshot_period_s:
                    snapshot(db, root)
                    last_snapshot = now
                if now - last_abandon >= abandon_period_s:
                    age = 2 * (cfg.search.trial_budget * cfg.evaluator.timeout_s + 600)
                    db.abandon_stale_tasks(time.time() - age)
                    last_abandon = now
                if pending:
                    wait(pending, timeout=POLL_S, return_when=FIRST_COMPLETED)
                else:
                    time.sleep(POLL_S)
            except sqlite3.OperationalError as exc:
                # Rollback journaling makes clients and the engine exclude
                # each other; a lock held past busy_timeout is transient, so
                # retry the tick. Every tick step commits atomically or not at
                # all, and an unstored result stays pending for the retry.
                if not _busy(exc):
                    raise
                busy_since = busy_since or time.monotonic()
                if time.monotonic() - busy_since >= busy_retry_s:
                    raise RuntimeError(f"database busy for {busy_retry_s:g}s: {exc}") from None
                log_event(f"database busy, retrying: {exc}")
                time.sleep(POLL_S)
            else:
                busy_since = None
    except BaseException as exc:
        traceback.print_exc()
        sys.stderr.flush()
        status, reason = FAILED, str(exc)
        # Interrupt workers even if persisting the failed state encounters an
        # error. Pool cleanup must not prevent outputs or the finish hook.
        for pool in pools.values():
            try:
                pool.abort()
            except BaseException:
                traceback.print_exc()
        try:
            db.set_state("status", STOPPING)
        except BaseException:
            traceback.print_exc()
    finally:
        # Abort makes active calls return before waiting for executor shutdown.
        cleanups = [lambda executor=executor: executor.shutdown(wait=True, cancel_futures=True)
                    for executor in executors.values()]
        cleanups += [pool.close for pool in pools.values()]
        for cleanup in cleanups:
            try:
                cleanup()
            except BaseException as exc:
                traceback.print_exc()
                status, reason = FAILED, f"worker cleanup: {exc}"
        try:
            finish_unscored(db, reason)
        except BaseException as exc:
            traceback.print_exc()
            status, reason = FAILED, f"queue cleanup: {exc}"
        try:
            write_outputs(db, root, metadata, cfg, status, reason)
            snapshot(db, root)
        except BaseException as exc:
            traceback.print_exc()
            status, reason = FAILED, f"final outputs: {exc}"
            try:
                write_outputs(db, root, metadata, cfg, status, reason)
            except BaseException:
                traceback.print_exc()
        try:
            run_hook(metadata.get("on_finish"), root)
        except BaseException as exc:
            traceback.print_exc()
            try:
                write_outputs(db, root, metadata, cfg, FAILED, f"on-finish hook: {exc}")
                snapshot(db, root)
            except BaseException:
                traceback.print_exc()
        finally:
            log_event(f"shutdown status={status} reason={reason}")
            try:
                (root / "engine.pid").unlink(missing_ok=True)
            finally:
                db.close()
                if not notified:
                    try:
                        os.write(ready_fd, ("FAILED " + reason.replace("\n", " ") + "\n").encode())
                    except BrokenPipeError:
                        pass  # The launcher is gone; engine.log has the reason.
                    finally:
                        os.close(ready_fd)


def observe_engine(run_dir, writer, snapshot_period_s, stop_grace_s, abandon_period_s,
                   busy_retry_s):
    """Wait for a detached engine with no launching-agent process identity."""
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
    engine = os.fork()
    if engine != 0:
        os.close(writer)
        _, wait_status = os.waitpid(engine, 0)
        exitcode = os.waitstatus_to_exitcode(wait_status)
        evidence = {"pid": engine, "ended_at": time.time(),
                    "exitcode": exitcode,
                    "signal": -exitcode if exitcode < 0 else None}
        # Readers poll for this file, so it must appear complete.
        Path("engine-exit.json.tmp").write_text(json.dumps(evidence, indent=2) + "\n")
        Path("engine-exit.json.tmp").replace("engine-exit.json")
        log_event(f"engine pid={engine} exited exitcode={exitcode} signal={evidence['signal']}")
        os._exit(0)
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
        serve(run_dir, writer, snapshot_period_s=snapshot_period_s,
              stop_grace_s=stop_grace_s, abandon_period_s=abandon_period_s,
              busy_retry_s=busy_retry_s)
    except BaseException:
        traceback.print_exc()
        try:
            os.write(writer, b"FAILED daemon startup\n")
        except OSError:
            pass
    finally:
        os._exit(0)


def daemonize(run_dir):
    """Detach an engine and a small exit observer; wait only for readiness."""
    import select
    reader, writer = os.pipe()
    first = os.fork()
    if first == 0:
        os.close(reader)
        try:
            os.setsid()
            if os.fork() != 0:
                os._exit(0)
            os.chdir(run_dir)
            with open("/dev/null", "rb") as null, open("engine.log", "ab", buffering=0) as log:
                os.dup2(null.fileno(), 0)
                os.dup2(log.fileno(), 1)
                os.dup2(log.fileno(), 2)
            # A double fork/setsid detaches process groups, but Gas City's
            # orphan sweep also finds processes by inherited GC_SESSION_ID.
            # Exec with a clean identity: unsetting os.environ after fork alone
            # leaves the initial /proc/<pid>/environ bytes on Linux unchanged.
            env = os.environ.copy()
            for key in ("GC_SESSION_ID", "GC_SESSION_NAME", "GC_AGENT", "GC_AGENT_NAME", "GC_ALIAS"):
                env.pop(key, None)
            os.set_inheritable(writer, True)
            bootstrap = ("import sys; sys.path.insert(0, sys.argv[1]); "
                         "from funsearch.daemon import observe_engine; "
                         "observe_engine(sys.argv[2], int(sys.argv[3]), "
                         "float(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6]), "
                         "float(sys.argv[7]))")
            os.execve(sys.executable, [sys.executable, "-c", bootstrap,
                      str(Path(__file__).resolve().parents[1]), str(run_dir), str(writer),
                      str(SNAPSHOT_PERIOD_S), str(STOP_GRACE_S), str(ABANDON_PERIOD_S),
                      str(BUSY_RETRY_S)], env)
        except BaseException:
            traceback.print_exc()
            try:
                os.write(writer, b"FAILED daemon startup\n")
            except OSError:
                pass
        finally:
            os._exit(0)
    os.close(writer)
    os.waitpid(first, 0)
    try:
        _, _, cfg = read_run(run_dir)
        # Two pools of workers, each with bounded start attempts, then the hook.
        deadline = time.monotonic() + 60 + 2 * cfg.search.workers * start_budget_s() + HOOK_TIMEOUT_S
        message = b""
        while b"\n" not in message:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([reader], [], [], remaining)[0]:
                raise RuntimeError("daemon startup timed out")
            chunk = os.read(reader, 4096)
            if not chunk:
                raise RuntimeError("daemon exited during startup")
            message += chunk
        if message != b"READY\n":
            raise RuntimeError(f"{message.decode().strip()} (see {Path(run_dir) / 'engine.log'})")
    finally:
        os.close(reader)
