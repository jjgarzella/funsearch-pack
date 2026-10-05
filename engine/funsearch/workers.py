"""Persistent worker processes and a blocking, thread-safe scoring pool."""

import json
import math
import os
from pathlib import Path
import select
import subprocess
import tempfile
import threading
import time

from ._process import environment, kill_group, run_command


START_TIMEOUT_S = 60
START_ATTEMPTS = 3
_build_lock = threading.Lock()


class WorkerError(RuntimeError):
    """A worker could not start, or the pool is closed."""


class EvaluatorInitError(WorkerError):
    """The evaluator reported a fatal startup error."""


def worker_binary():
    """Locate this pack's worker and build it once if missing."""
    root = Path(__file__).resolve().parents[2]
    binary = root / "build" / "funsearch-worker"
    with _build_lock:
        if not binary.is_file():
            # Use argv, not a shell interpolation of the installation path.
            import shlex
            ok, log = run_command(f"make -C {shlex.quote(str(root))} worker", root, 60)
            if not ok or not binary.is_file():
                raise WorkerError(f"cannot build funsearch-worker: {log}")
    return binary


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
        self.env = environment({"FS_MEMORY_MB": str(cfg.evaluator.memory_mb), **(extra_env or {})})
        self.binary = worker_binary()
        self.process = None
        self._stderr = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._failure = None
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

    def _start(self):
        last_error = None
        for _ in range(START_ATTEMPTS):
            try:
                self._stderr = tempfile.TemporaryFile()
                self.process = subprocess.Popen(
                    [str(self.binary), str(self.evaluator_so), self.instance],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                    env=self.env, start_new_session=True, bufsize=0)
                self._send("")
                reply = json.loads(self._line(time.monotonic() + START_TIMEOUT_S))
                if not isinstance(reply, dict):
                    raise WorkerError("invalid worker startup response")
                if "fatal" in reply:
                    raise EvaluatorInitError(str(reply["fatal"]))
                if reply != _error("bad request"):
                    raise WorkerError(f"unexpected worker startup response: {reply!r}")
                return
            except EvaluatorInitError:
                self._dispose()
                raise
            except (EOFError, OSError, TimeoutError, ValueError, WorkerError) as exc:
                if isinstance(exc, (EOFError, BrokenPipeError)) and self.process is not None:
                    try:
                        last_error = self._crash_message()
                    except EvaluatorInitError:
                        self._dispose()
                        raise
                elif isinstance(exc, TimeoutError):
                    last_error = f"worker startup timeout after {START_TIMEOUT_S:g}s"
                else:
                    last_error = str(exc)
                self._dispose()
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
            deadline = time.monotonic() + timeout_s
            try:
                self._send("SCORE " + path)
                reply = json.loads(self._line(deadline))
                if not isinstance(reply, dict):
                    raise WorkerError("invalid worker response")
                if "fatal" in reply:
                    raise EvaluatorInitError(str(reply["fatal"]))
                if (reply.get("status") not in {"OK", "INVALID", "ERROR"}
                        or not {"score", "sig", "msg"} <= reply.keys()):
                    raise WorkerError("invalid worker response")
                return reply
            except TimeoutError:
                result = _error(f"timeout after {timeout_s:g}s")
            except (EOFError, BrokenPipeError):
                try:
                    result = _error(self._crash_message())
                except EvaluatorInitError as exc:
                    self._failure = exc
                    self._dispose()
                    raise
            except EvaluatorInitError as exc:
                self._failure = exc
                self._dispose()
                raise
            except (OSError, ValueError, WorkerError) as exc:
                result = _error(f"worker protocol error: {exc}")
            self._dispose()
            try:
                self._start()
            except WorkerError as exc:
                self._failure = exc
                raise
            return result

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
