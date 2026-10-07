"""Bounded shell commands shared by the compilation pipeline."""

from fnmatch import fnmatchcase
import os
import select
import signal
import subprocess
import threading


LOG_BYTES = 4096


# Candidate and evaluator code runs as the host user, in evaluator workers and
# in the export check. It sees only this allowlist, plus the evaluator.env
# patterns a problem declares, so identity, store scope and credentials added
# to the engine's environment (by Gas City or anything else) never reach it by
# default. worker_environment() is the one place that applies the policy.
CANDIDATE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_*", "TZ",
                 "TMPDIR", "LD_LIBRARY_PATH")


def candidate_environment(extra_env=None, passthrough=()):
    """Allowlisted environment for candidate code; None in extra_env removes."""
    patterns = (*CANDIDATE_ENV, *passthrough)
    result = {key: value for key, value in os.environ.items()
              if any(fnmatchcase(key, pattern) for pattern in patterns)}
    for key, value in (extra_env or {}).items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = str(value)
    return result


def worker_environment(cfg, extra_env=None):
    """Environment for every process that loads evaluator or candidate code.

    Worker spawn and the export checks (candidate and evaluator libraries)
    all use it, so constructors see what the worker will. extra_env is a
    mode overlay such as try_worker_env(cfg); None values remove variables.
    """
    return candidate_environment({"FS_MEMORY_MB": str(cfg.evaluator.memory_mb), **(extra_env or {})},
                                 cfg.evaluator.env)


def kill_group(process):
    """Kill descendants too, including when the group leader already exited."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


class _StderrPrefix:
    """Drain stderr continuously, retaining only the first LOG_BYTES in RAM."""

    def __init__(self, pipe):
        self.pipe = pipe
        self.buffer = bytearray()
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
                    self.buffer.extend(chunk[:max(0, LOG_BYTES - len(self.buffer))])
                if self.stop.is_set():
                    break
        finally:
            self.pipe.close()

    def close(self):
        # A descendant may still hold the pipe after the shell exits. Do not
        # wait for EOF from it; the reader stops within one select interval.
        self.stop.set()
        self.thread.join()
        return self.buffer.decode("utf-8", errors="replace")


def run_command(command, cwd, timeout_s):
    """Return (success, bounded stderr), killing the shell group on timeout."""
    try:
        process = subprocess.Popen(command, shell=True, cwd=cwd,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   start_new_session=True)
    except OSError as exc:
        return False, str(exc)[:LOG_BYTES]
    stderr = _StderrPrefix(process.stderr)
    timed_out = False
    try:
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_group(process)
    finally:
        log = stderr.close()
    if timed_out:
        message = f"command timed out after {timeout_s:g}s\n"
        return False, message + log[:LOG_BYTES - len(message)]
    if process.returncode and not log:
        log = f"command exited with code {process.returncode}"
    return process.returncode == 0, log
