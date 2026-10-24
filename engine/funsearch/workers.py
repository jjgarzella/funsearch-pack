"""Persistent worker processes and a blocking, thread-safe scoring pool."""

import errno
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import select
import shlex
import subprocess
import sys
import threading
import time

from ._process import kill_group, process_resident_bytes, run_command, worker_environment


START_TIMEOUT_S = 60
START_ATTEMPTS = 3
# Candidate output is diagnostics only; keep at most this much per worker.
STDERR_LIMIT_BYTES = 1 << 20
# A deterministic score cap avoids allocator/GC high-water noise and bounds
# retained candidate state on every supported host. Restart before score K+1,
# never while returning score K's result.
RECYCLE_SCORES = 100
# Startup and scoring failures carry this much of the worker's stderr (evaluator output).
STDERR_TAIL_BYTES = 2048
# After a scoring reply, an honest worker answers the SYNC barrier at once; a
# reply that arrives just before the deadline still gets this long for it.
SYNC_TIMEOUT_S = 5
_build_lock = threading.Lock()


def _darwin_memory_limit_bytes(env):
    if sys.platform != "darwin":
        return None
    value = env.get("FS_MEMORY_RSS_MB")
    if value is None:
        value = env.get("FS_MEMORY_MB")
    try:
        memory_mb = int(value)
    except (TypeError, ValueError):
        return None
    if memory_mb < 0:
        return None
    return memory_mb * 1024 * 1024


def start_budget_s():
    """Longest one worker start may take: every attempt times out."""
    return START_ATTEMPTS * START_TIMEOUT_S


def score_budget_s(timeout_s):
    """Longest one Worker.score call may take.

    A recycle may replace the worker before the candidate runs, the SYNC
    barrier may follow the reply, and a timeout or crash replaces the worker
    again before the call returns.
    """
    return timeout_s + SYNC_TIMEOUT_S + 2 * start_budget_s()


class WorkerError(RuntimeError):
    """A worker could not start, or the pool is closed."""


class EvaluatorInitError(WorkerError):
    """The evaluator reported a fatal startup error."""


def worker_binary():
    """Locate this pack's worker, building it if it is missing or stale.

    Several engines (and rigs sharing one pack) can race here, so the build
    holds an flock and the Makefile installs the binary with an atomic rename.
    An up-to-date binary needs neither the lock nor a writable pack.
    """
    root = Path(__file__).resolve().parents[2]
    binary = root / "build" / "funsearch-worker"
    sources = [path for path in (root / "worker" / "funsearch-worker.c", root / "include" / "funsearch.h")
               if path.is_file()]

    def fresh():
        return binary.is_file() and all(binary.stat().st_mtime >= path.stat().st_mtime for path in sources)

    with _build_lock:
        if fresh():
            return binary
        try:
            binary.parent.mkdir(exist_ok=True)
            handle = (binary.parent / ".build.lock").open("a")
        except OSError as exc:
            raise WorkerError(f"cannot build funsearch-worker in {root}: {exc}") from exc
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if not fresh():
                ok, log = run_command(f"make -C {shlex.quote(str(root))} worker", root, 60)
                if not ok or not binary.is_file():
                    raise WorkerError(f"cannot build funsearch-worker: {log}")
    return binary


class _StderrTail:
    """Continuously drain a pipe while retaining at most STDERR_LIMIT_BYTES.

    Native output never creates a growing temporary file or fills an unread
    pipe. The reader owns its descriptor, and disposal stops it even if a
    descendant escaped the worker process group with the pipe still open.
    """

    def __init__(self, pipe):
        self.pipe = pipe
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while True:
                readable, _, _ = select.select([self.pipe], [], [], 0.1)
                if readable:
                    chunk = os.read(self.pipe.fileno(), 65536)
                    if not chunk:
                        break
                    with self.lock:
                        del self.buffer[:max(0, len(self.buffer) + len(chunk) - STDERR_LIMIT_BYTES)]
                        self.buffer.extend(chunk[-STDERR_LIMIT_BYTES:])
                if self.stop.is_set():
                    break
        finally:
            self.pipe.close()

    def tail(self):
        with self.lock:
            return bytes(self.buffer[-STDERR_TAIL_BYTES:]).decode('utf-8', errors='replace').strip()

    def close(self):
        # Disposal normally kills the worker process group first, so the
        # reader can drain through EOF and retain the final diagnostics. An
        # escaped descendant may keep the pipe open; bound that wait, then
        # stop the reader (which checks the event at most one select interval
        # after it is set).
        self.thread.join(timeout=0.25)
        if self.thread.is_alive():
            self.stop.set()
            self.thread.join()


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _checked_reply(reply, nonce):
    """Validate one scoring reply; raise WorkerError for protocol violations."""
    # Candidate code shares the worker process and can write to its protocol
    # fd. A reply must echo this request's random token to count.
    if not isinstance(reply, dict) or reply.pop("nonce", None) != nonce:
        raise WorkerError("reply does not answer this request")
    if (reply.get("status") not in {"OK", "INVALID", "ERROR"}
            or not {"score", "sig", "msg"} <= reply.keys()
            or not isinstance(reply["sig"], list) or len(reply["sig"]) > 8):
        raise WorkerError("invalid worker response")
    score = reply["score"]
    if (not all(map(_finite_number, reply["sig"]))
            or not (_finite_number(score) or (score is None and reply["status"] != "OK"))):
        return _error("non-finite score or signature")
    return reply


def _error(message):
    return {"status": "ERROR", "score": 0, "sig": [], "msg": message}


class Worker:
    """One serial worker; score/close are safe to call from different threads.

    A blank protocol request acknowledges completed initialization, since the
    C worker does not send a ready line. A dedicated reader continuously drains
    stderr into a bounded tail. Each process owns a new process group.
    """

    def __init__(self, cfg, evaluator_so, instance, extra_env=None):
        self.cfg = cfg
        self.evaluator_so = Path(evaluator_so).resolve()
        self.instance = instance
        self.env = worker_environment(cfg, extra_env)
        self.binary = worker_binary()
        self.process = None
        self._stderr = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._failure = None
        self._scores = 0
        self._recycle_due = False
        self._rss_limit_bytes = _darwin_memory_limit_bytes(self.env)
        self._rss_watchdog_condition = threading.Condition()
        self._rss_watchdog_active = self._rss_limit_bytes is not None
        self._rss_watchdog_stop = False
        self._rss_watchdog_error = None
        self._pool_termination_lock = threading.Lock()
        self._pool_termination_reason = None
        self._rss_watchdog_thread = None
        if self._rss_limit_bytes is not None:
            try:
                process_resident_bytes(os.getpid())
            except OSError as exc:
                raise WorkerError(f"cannot inspect worker resident set: {exc}") from exc
            self._rss_watchdog_thread = threading.Thread(
                target=self._watch_worker_rss, daemon=True)
            self._rss_watchdog_thread.start()
        try:
            self._start()
        except BaseException:
            self._stop_rss_watchdog()
            raise
        self._set_rss_watchdog_active(False)

    def _set_rss_watchdog_active(self, active):
        if self._rss_watchdog_thread is None:
            return
        with self._rss_watchdog_condition:
            self._rss_watchdog_active = active
            self._rss_watchdog_condition.notify_all()

    def _pool_termination_guard(self):
        lock = getattr(self, "_pool_termination_lock", None)
        if lock is None:  # Worker.__new__ test doubles need no shared state.
            lock = threading.Lock()
            self._pool_termination_lock = lock
        return lock

    def _record_pool_termination(self, reason):
        """Keep the first reason the pool chose to terminate this worker."""
        with self._pool_termination_guard():
            if self._pool_termination_reason is None:
                self._pool_termination_reason = reason
            return self._pool_termination_reason

    def _stop_rss_watchdog(self):
        thread = self._rss_watchdog_thread
        if thread is None:
            return
        with self._rss_watchdog_condition:
            self._rss_watchdog_stop = True
            self._rss_watchdog_condition.notify_all()
        if thread is not threading.current_thread():
            thread.join()
        self._rss_watchdog_thread = None

    def _watch_worker_rss(self):
        while True:
            with self._rss_watchdog_condition:
                if self._rss_watchdog_stop:
                    return
                interval = 0.02 if self._rss_watchdog_active else 1.0
                self._rss_watchdog_condition.wait(timeout=interval)
                if self._rss_watchdog_stop:
                    return
                if not self._rss_watchdog_active:
                    continue

            process = self.process
            if process is None or process.poll() is not None:
                continue
            try:
                resident_bytes = process_resident_bytes(process.pid)
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    # proc_pidinfo reports ESRCH after the worker exits. Stop
                    # watching this process and let the scoring path reap it
                    # and classify its exit or signal.
                    if self.process is process:
                        self._set_rss_watchdog_active(False)
                    continue
                if process.poll() is not None:
                    continue
                failure = f"worker resident-set query failed: {exc}"
            else:
                if resident_bytes <= self._rss_limit_bytes:
                    continue
                failure = (
                    "worker resident-set limit exceeded "
                    f"({resident_bytes} > {self._rss_limit_bytes} bytes)"
                )

            if self.process is not process or process.poll() is not None:
                continue
            with self._rss_watchdog_condition:
                if self._rss_watchdog_stop:
                    return
                self._rss_watchdog_error = failure
                self._record_pool_termination(failure)
            try:
                kill_group(process)
            except OSError:
                # kill_group already falls back to killing the leader when a
                # process-group signal is denied; keep the monitor alive if a
                # platform-specific error still escapes that path.
                try:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                except OSError:
                    pass

    def _pop_line(self):
        newline = self._buffer.find(b"\n")
        if newline < 0:
            return None
        line = bytes(self._buffer[:newline])
        del self._buffer[:newline + 1]
        return line.decode("utf-8")

    def _drain_protocol_after_exit(self):
        """Read protocol bytes already pending after the worker exits.

        Do not wait for EOF: a descendant may have escaped the worker group
        and still hold the pipe open. The response buffer remains capped at
        the same 64 KiB limit as the normal line reader.
        """
        while True:
            if len(self._buffer) > 65536:
                raise WorkerError("worker response exceeds 64 KiB")
            readable, _, _ = select.select([self.process.stdout], [], [], 0)
            if not readable:
                return
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                return
            self._buffer.extend(chunk)
            if len(self._buffer) > 65536:
                raise WorkerError("worker response exceeds 64 KiB")

    def _line_after_eof(self):
        if self.process.poll() is not None:
            # poll() observed the exit; wait() collects the status before we
            # interpret any bytes that were still queued in the pipe.
            self.process.wait()
            self._drain_protocol_after_exit()
        line = self._pop_line()
        if line is not None:
            return line
        if self._buffer:
            raise WorkerError("worker response ended before newline")
        raise EOFError

    def _exit_snapshot(self):
        """Reap an exited worker and collect its currently pending protocol bytes."""
        process = getattr(self, "process", None)
        if process is None:
            return None, False
        code = process.poll()
        if code is None:
            return None, False
        code = process.wait()
        self._drain_protocol_after_exit()
        return code, bool(self._buffer)

    def _exit_message(self, code):
        termination_reason = getattr(self, "_pool_termination_reason", None)
        if termination_reason is not None:
            return termination_reason
        watchdog_error = self._rss_watchdog_error
        if watchdog_error and watchdog_error.startswith("worker resident-set limit exceeded"):
            return watchdog_error
        if code < 0:
            return f"worker crashed: signal {-code}"
        if code > 0:
            return f"worker crashed: exit code {code}"
        return watchdog_error or "worker crashed: exit code 0"

    def _line(self, deadline):
        while True:
            line = self._pop_line()
            if line is not None:
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self.process.poll() is not None:
                    return self._line_after_eof()
                raise TimeoutError
            readable, _, _ = select.select([self.process.stdout], [], [], min(remaining, 0.1))
            if readable:
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    return self._line_after_eof()
                self._buffer.extend(chunk)
                if len(self._buffer) > 65536:
                    raise WorkerError("worker response exceeds 64 KiB")
            elif self.process.poll() is not None:
                # select() can observe an empty pipe just before the writer
                # exits. Reap it and make one bounded pass for its last bytes.
                return self._line_after_eof()

    def _send(self, request):
        self.process.stdin.write((request + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def _sync(self, deadline):
        """Require the accepted reply to be the only line before a barrier.

        Candidate code shares the worker process. If it wrote a reply carrying
        this request's token, the worker's own reply is a second line here.
        """
        token = secrets.token_hex(16)
        self._send(f"SYNC #{token}")
        line = self._line(deadline)
        try:
            synced = json.loads(line) == {"sync": token}
        except ValueError:
            synced = False
        if not synced:
            raise WorkerError("extra output on the protocol fd during scoring")

    def _stale_output(self):
        """Whether protocol output arrived after the last completed request."""
        if self.process is None or self._buffer:
            return True
        readable, _, _ = select.select([self.process.stdout], [], [], 0)
        return bool(readable)

    def _dispose(self, graceful=False):
        if self.process is not None:
            try:
                if graceful and self.process.poll() is None:
                    try:
                        self._send("QUIT")
                        self.process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            finally:
                kill_group(self.process)
                self.process.stdin.close()
                self.process.stdout.close()
                self.process = None
        if self._stderr is not None:
            self._stderr.close()
        self._buffer.clear()

    def _crash_message(self, *, startup=False):
        try:
            code = self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return "worker crashed: closed stdout"
        if startup and code == 3:
            raise EvaluatorInitError("worker exited with code 3")
        detail = f"signal {-code}" if code < 0 else f"exit code {code}"
        return "worker crashed: " + detail

    def _with_stderr(self, message):
        """Include bounded native diagnostics in trusted-local v1 failures."""
        tail = self._stderr.tail() if self._stderr is not None else ""
        return f"{message}\nworker stderr:\n{tail}" if tail else message

    def _start(self):
        last_error = None
        for _ in range(START_ATTEMPTS):
            if self._closed:
                self._dispose()
                raise WorkerError("worker is closed")
            try:
                self._stderr = None
                self.process = subprocess.Popen(
                    [str(self.binary), str(self.evaluator_so), self.instance],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=self.env, start_new_session=True, bufsize=0)
                self._stderr = _StderrTail(self.process.stderr)
                if self._closed:
                    self._dispose()
                    raise WorkerError("worker is closed")
                self._send("")
                reply = json.loads(self._line(time.monotonic() + START_TIMEOUT_S))
                if not isinstance(reply, dict):
                    raise WorkerError("invalid worker startup response")
                if "fatal" in reply:
                    raise EvaluatorInitError(str(reply["fatal"]))
                if reply != _error("bad request"):
                    raise WorkerError(f"unexpected worker startup response: {reply!r}")
                self._scores = 0
                self._recycle_due = False
                return
            except EvaluatorInitError as exc:
                self._dispose()
                message = self._with_stderr(str(exc))
                raise EvaluatorInitError(message) from None
            except (EOFError, OSError, TimeoutError, ValueError, WorkerError) as exc:
                if isinstance(exc, (EOFError, BrokenPipeError)) and self.process is not None:
                    try:
                        last_error = self._crash_message(startup=True)
                    except EvaluatorInitError as init_error:
                        self._dispose()
                        message = self._with_stderr(str(init_error))
                        raise EvaluatorInitError(message) from None
                elif isinstance(exc, TimeoutError):
                    last_error = f"worker startup timeout after {START_TIMEOUT_S:g}s"
                else:
                    last_error = str(exc)
                self._dispose()
                last_error = self._with_stderr(last_error)
        raise WorkerError(f"worker failed to start after {START_ATTEMPTS} attempts: {last_error}")

    def score(self, so_path, timeout_s):
        """Return a protocol result; replace a timed-out or crashed worker."""
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive and finite")
        path = str(Path(so_path).resolve())
        if "\n" in path or "\r" in path:
            raise ValueError("candidate path cannot contain a newline")
        with self._lock:
            if self._closed:
                raise WorkerError("worker is closed")
            if self._failure is not None:
                raise self._failure
            # Clear the previous request's watchdog result before waking the
            # monitor. Otherwise a fast query failure can set this field just
            # before _score_locked clears it, losing the failure and timing out.
            self._rss_watchdog_error = None
            with self._pool_termination_guard():
                self._pool_termination_reason = None
            self._set_rss_watchdog_active(True)
            try:
                return self._score_locked(path, timeout_s)
            finally:
                self._set_rss_watchdog_active(False)

    def _score_locked(self, path, timeout_s):
        if self._recycle_due or self._stale_output():
            # Outside the deadline: the replacement's fs_init is not
            # charged to this candidate's timeout. Late output (an earlier
            # candidate's leftover thread or child, or a dead worker's EOF)
            # is never read as this request's reply.
            self._recycle()
            if self._failure is not None:
                raise self._failure
            if self.process is None:
                raise WorkerError("worker is closed")
        deadline = time.monotonic() + timeout_s
        nonce = secrets.token_hex(16)
        try:
            self._send(f"SCORE #{nonce} {path}")
            reply = _checked_reply(json.loads(self._line(deadline)), nonce)
            self._sync(max(deadline, time.monotonic() + SYNC_TIMEOUT_S))
        except TimeoutError:
            try:
                exit_code, pending = self._exit_snapshot()
            except (OSError, WorkerError) as exc:
                result = _error(f"worker protocol error: {exc}")
            else:
                if pending:
                    result = _error("worker protocol error: unexpected output after worker exit")
                elif exit_code is not None:
                    result = _error(self._exit_message(exit_code))
                else:
                    reason = self._pool_termination_reason or self._rss_watchdog_error
                    if reason is None:
                        reason = f"timeout after {timeout_s:g}s"
                    reason = self._record_pool_termination(reason)
                    result = _error(reason)
        except (EOFError, BrokenPipeError):
            try:
                exit_code, pending = self._exit_snapshot()
            except (OSError, WorkerError) as exc:
                result = _error(f"worker protocol error: {exc}")
            else:
                if pending:
                    result = _error("worker protocol error: unexpected output after worker exit")
                elif exit_code is not None:
                    result = _error(self._exit_message(exit_code))
                else:
                    result = _error(self._rss_watchdog_error or self._crash_message())
        except (OSError, ValueError, WorkerError) as exc:
            # If the protocol violation coincided with worker exit, collect
            # the final bytes and reap before reporting it. The violation
            # remains the primary classification.
            try:
                self._exit_snapshot()
            except (OSError, WorkerError):
                pass
            result = _error(f"worker protocol error: {exc}")
        else:
            try:
                exit_code, pending = self._exit_snapshot()
            except (OSError, WorkerError) as exc:
                result = _error(f"worker protocol error: {exc}")
            else:
                if pending:
                    result = _error("worker protocol error: unexpected output after worker exit")
                elif exit_code is not None:
                    result = _error(self._exit_message(exit_code))
                else:
                    self._scores += 1
                    # Replace a leaking worker before the next request, not
                    # while this caller waits for the reply already in hand.
                    self._recycle_due = self._scores >= RECYCLE_SCORES
                    return reply
        self._dispose()
        result["msg"] = self._with_stderr(result["msg"])
        if self._closed:
            return _error("evaluation interrupted at shutdown")
        try:
            self._start()
        except WorkerError as exc:
            if self._closed:
                return _error("evaluation interrupted at shutdown")
            self._failure = exc
            raise
        return result

    def _recycle(self):
        """Replace a healthy worker after its fixed score budget."""
        self._recycle_due = False
        self._dispose(graceful=True)
        try:
            self._start()
        except WorkerError as exc:
            if not self._closed:
                self._failure = exc

    def abort(self):
        """Interrupt an active score without waiting for its scoring lock."""
        self._closed = True
        process = self.process
        if process is not None:
            kill_group(process)

    def close(self):
        with self._lock:
            self._closed = True
            try:
                self._dispose(graceful=True)
            finally:
                self._stop_rss_watchdog()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class WorkerPool:
    """Blocking scoring API: call pool.score from a ThreadPoolExecutor.

    At most size requests execute concurrently. Others wait for an idle worker.
    Construct a second pool with try_worker_env(cfg) for sanitizer candidates.
    close() rejects new/waiting calls, waits for active calls, then sends QUIT.
    """

    def __init__(self, cfg, evaluator_so, instance, size, extra_env=None):
        if type(size) is not int or size <= 0:
            raise ValueError("worker pool size must be a positive integer")
        self._condition = threading.Condition()
        self._closed = False
        self.workers = []
        try:
            for _ in range(size):
                self.workers.append(Worker(cfg, evaluator_so, instance, extra_env))
        except BaseException:
            for worker in self.workers:
                worker.close()
            raise
        self._idle = self.workers.copy()

    def score(self, so_path, timeout_s):
        with self._condition:
            while not self._idle and not self._closed:
                self._condition.wait()
            if self._closed:
                raise WorkerError("worker pool is closed")
            worker = self._idle.pop()
        try:
            return worker.score(so_path, timeout_s)
        finally:
            with self._condition:
                self._idle.append(worker)
                self._condition.notify_all()

    def abort(self):
        """Reject new calls and interrupt workers after the stop grace period."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        for worker in self.workers:
            worker.abort()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            while len(self._idle) < len(self.workers):
                self._condition.wait()
        for worker in self.workers:
            worker.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
