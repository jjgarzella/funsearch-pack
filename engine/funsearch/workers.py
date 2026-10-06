"""Persistent worker processes and a blocking, thread-safe scoring pool."""

import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import select
import shlex
import subprocess
import threading
import time

from ._process import kill_group, run_command, worker_environment


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
_build_lock = threading.Lock()


def start_budget_s():
    """Longest one worker start may take: every attempt times out."""
    return START_ATTEMPTS * START_TIMEOUT_S


def score_budget_s(timeout_s):
    """Longest one Worker.score call may take.

    A recycle may replace the worker before the candidate runs, and a timeout
    or crash replaces it again before the call returns.
    """
    return timeout_s + 2 * start_budget_s()


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
        self._start()

    def _line(self, deadline):
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                return line.decode("utf-8")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            readable, _, _ = select.select([self.process.stdout], [], [], min(remaining, 0.1))
            if readable:
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    raise EOFError
                self._buffer.extend(chunk)
                if len(self._buffer) > 65536:
                    raise WorkerError("worker response exceeds 64 KiB")
            elif self.process.poll() is not None:
                # Descendants may keep stdout open after the worker crashes.
                raise EOFError

    def _send(self, request):
        self.process.stdin.write((request + "\n").encode("utf-8"))
        self.process.stdin.flush()

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

    def _crash_message(self):
        try:
            code = self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return "worker crashed: closed stdout"
        if code == 3:
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
                        last_error = self._crash_message()
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
            if self._recycle_due:
                # Outside the deadline: the replacement's fs_init is not
                # charged to this candidate's timeout.
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
            except TimeoutError:
                result = _error(f"timeout after {timeout_s:g}s")
            except (EOFError, BrokenPipeError):
                try:
                    result = _error(self._crash_message())
                except EvaluatorInitError as exc:
                    self._dispose()
                    self._failure = EvaluatorInitError(self._with_stderr(str(exc)))
                    raise self._failure from None
            except (OSError, ValueError, WorkerError) as exc:
                result = _error(f"worker protocol error: {exc}")
            else:
                self._scores += 1
                # Replace a leaking worker before the next request, not while
                # this caller waits for the reply already in hand.
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
            self._dispose(graceful=True)

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
