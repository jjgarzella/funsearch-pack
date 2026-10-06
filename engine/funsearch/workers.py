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
import tempfile
import threading
import time

from ._process import candidate_environment, kill_group, run_command


START_TIMEOUT_S = 60
START_ATTEMPTS = 3
# Candidate output is diagnostics only; keep at most this much per worker.
STDERR_LIMIT_BYTES = 1 << 20
# Replace a warm worker once candidates have consumed this fraction of its
# memory budget and it is still growing, before leaks from earlier candidates
# fail an innocent one.
RECYCLE_FRACTION = 0.5
# Startup failures carry this much of the worker's stderr (evaluator output).
STDERR_TAIL_BYTES = 2048
_build_lock = threading.Lock()


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


def _memory_kb(pid, field):
    """Return a /proc/<pid>/status size in KiB, or None where unavailable."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


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
    C worker does not send a ready line. Stderr goes to a file, never a pipe
    that evaluator output could fill. Each process owns a new process group.
    """

    def __init__(self, cfg, evaluator_so, instance, extra_env=None):
        self.cfg = cfg
        self.evaluator_so = Path(evaluator_so).resolve()
        self.instance = instance
        self.env = candidate_environment({"FS_MEMORY_MB": str(cfg.evaluator.memory_mb), **(extra_env or {})},
                                         cfg.evaluator.env)
        self.binary = worker_binary()
        self.process = None
        self._stderr = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._failure = None
        self._baseline_kb = None
        self._last_kb = None
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
            self._stderr = None
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

    def _with_startup_stderr(self, message):
        """Append the tail of a starting worker's stderr to message.

        Only the worker and evaluator have run at startup, so this is the
        evaluator author's diagnostic. Stderr written while scoring may come
        from candidate code and is never copied into results the mutator sees.
        """
        if self._stderr is None:
            return message
        try:
            size = os.fstat(self._stderr.fileno()).st_size
            self._stderr.seek(max(0, size - STDERR_TAIL_BYTES))
            tail = self._stderr.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return message
        return f"{message}\nworker stderr:\n{tail}" if tail else message

    def _start(self):
        last_error = None
        for _ in range(START_ATTEMPTS):
            if self._closed:
                self._dispose()
                raise WorkerError("worker is closed")
            try:
                self._stderr = tempfile.TemporaryFile()
                self.process = subprocess.Popen(
                    [str(self.binary), str(self.evaluator_so), self.instance],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                    env=self.env, start_new_session=True, bufsize=0)
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
                self._baseline_kb = _memory_kb(self.process.pid, self._memory_field())
                self._last_kb = None
                return
            except EvaluatorInitError as exc:
                message = self._with_startup_stderr(str(exc))
                self._dispose()
                raise EvaluatorInitError(message) from None
            except (EOFError, OSError, TimeoutError, ValueError, WorkerError) as exc:
                if isinstance(exc, (EOFError, BrokenPipeError)) and self.process is not None:
                    try:
                        last_error = self._crash_message()
                    except EvaluatorInitError as init_error:
                        message = self._with_startup_stderr(str(init_error))
                        self._dispose()
                        raise EvaluatorInitError(message) from None
                elif isinstance(exc, TimeoutError):
                    last_error = f"worker startup timeout after {START_TIMEOUT_S:g}s"
                else:
                    last_error = str(exc)
                last_error = self._with_startup_stderr(last_error)
                self._dispose()
        raise WorkerError(f"worker failed to start after {START_ATTEMPTS} attempts: {last_error}")

    def _memory_field(self):
        # RLIMIT_AS bounds virtual size; sanitizer workers run without it,
        # and their shadow mapping makes resident size the meaningful measure.
        return "VmSize" if "FS_MEMORY_MB" in self.env else "VmRSS"

    def _should_recycle(self):
        """Whether candidates leaked: memory is past the threshold and grew.

        Allocators and GC runtimes (an embedded Julia) keep their peak mapped
        and reuse it, so a high but stable level is not a leak; recycling on it
        would cold-start the worker after every score. Only growth since the
        previous score, beyond the threshold, counts.
        """
        if self._baseline_kb is None or self.process is None:
            return False
        current = _memory_kb(self.process.pid, self._memory_field())
        if current is None:
            return False
        previous, self._last_kb = self._last_kb, current
        budget = self.cfg.evaluator.memory_mb * 1024
        if "FS_MEMORY_MB" in self.env:
            budget -= self._baseline_kb
        return (previous is not None and current > previous
                and current - self._baseline_kb > RECYCLE_FRACTION * max(budget, 0))

    def _trim_stderr(self):
        # The worker shares this file's offset, so rewinding it here bounds
        # the file without the worker reopening anything.
        if self._stderr is not None:
            descriptor = self._stderr.fileno()
            if os.fstat(descriptor).st_size > STDERR_LIMIT_BYTES:
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)

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
                    self._failure = exc
                    self._dispose()
                    raise
            except (OSError, ValueError, WorkerError) as exc:
                result = _error(f"worker protocol error: {exc}")
            else:
                self._trim_stderr()
                # Replace a leaking worker before the next request, not while
                # this caller waits for the reply already in hand.
                self._recycle_due = self._should_recycle()
                return reply
            self._dispose()
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
        """Replace a healthy worker whose candidates leaked too much memory."""
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
    Construct a second pool with try_worker_env() for sanitizer candidates.
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
