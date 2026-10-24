"""Bounded shell commands shared by the compilation pipeline."""

import ctypes
from fnmatch import fnmatchcase
import os
import select
import signal
import subprocess
import sys
import threading


LOG_BYTES = 4096
_PROC_PIDTASKINFO = 4


class _DarwinProcTaskInfo(ctypes.Structure):
    """Public libproc task counters; the first two fields include resident bytes."""

    _fields_ = [
        ("pti_virtual_size", ctypes.c_uint64),
        ("pti_resident_size", ctypes.c_uint64),
        ("pti_total_user", ctypes.c_uint64),
        ("pti_total_system", ctypes.c_uint64),
        ("pti_threads_user", ctypes.c_uint64),
        ("pti_threads_system", ctypes.c_uint64),
        ("pti_policy", ctypes.c_int32),
        ("pti_faults", ctypes.c_int32),
        ("pti_pageins", ctypes.c_int32),
        ("pti_cow_faults", ctypes.c_int32),
        ("pti_messages_sent", ctypes.c_int32),
        ("pti_messages_received", ctypes.c_int32),
        ("pti_syscalls_mach", ctypes.c_int32),
        ("pti_syscalls_unix", ctypes.c_int32),
        ("pti_csw", ctypes.c_int32),
        ("pti_threadnum", ctypes.c_int32),
        ("pti_numrunning", ctypes.c_int32),
        ("pti_priority", ctypes.c_int32),
    ]


_proc_pidinfo = None
_proc_pidinfo_library = None
_proc_pidinfo_error = None
_proc_pidinfo_lock = threading.Lock()


def process_resident_bytes(pid):
    """Return a Darwin process's resident size, including after it execs."""
    global _proc_pidinfo, _proc_pidinfo_library, _proc_pidinfo_error
    if sys.platform != "darwin":
        raise OSError("process resident-size queries are only available on macOS")
    if _proc_pidinfo is None and _proc_pidinfo_error is None:
        with _proc_pidinfo_lock:
            if _proc_pidinfo is None and _proc_pidinfo_error is None:
                try:
                    _proc_pidinfo_library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
                    _proc_pidinfo = _proc_pidinfo_library.proc_pidinfo
                    _proc_pidinfo.argtypes = (
                        ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                        ctypes.POINTER(_DarwinProcTaskInfo), ctypes.c_int,
                    )
                    _proc_pidinfo.restype = ctypes.c_int
                except (OSError, AttributeError) as exc:
                    _proc_pidinfo_error = str(exc)
    if _proc_pidinfo is None:
        raise OSError(f"cannot load libproc proc_pidinfo: {_proc_pidinfo_error}")

    info = _DarwinProcTaskInfo()
    ctypes.set_errno(0)
    written = _proc_pidinfo(
        pid, _PROC_PIDTASKINFO, 0, ctypes.byref(info), ctypes.sizeof(_DarwinProcTaskInfo))
    if written != ctypes.sizeof(_DarwinProcTaskInfo):
        error = ctypes.get_errno()
        detail = os.strerror(error) if error else f"returned {written} bytes"
        raise OSError(error, f"proc_pidinfo(PROC_PIDTASKINFO) failed: {detail}")
    return int(info.pti_resident_size)


# Candidate and evaluator code runs as the host user, in evaluator workers and
# in the export check. It sees only this allowlist, plus the evaluator.env
# patterns a problem declares, so identity, store scope and credentials added
# to the engine's environment (by Gas City or anything else) never reach it by
# default. worker_environment() is the one place that applies the policy.
CANDIDATE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_*", "TZ",
                 "TMPDIR", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")


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


def _darwin_group_has_live_members(pgid):
    """Return whether ``ps`` sees a non-zombie process in a Darwin group.

    macOS can report EPERM for a process group containing only zombies. A
    failed query is unknown rather than evidence that the group is gone.
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "pgid=,stat="], capture_output=True, text=True,
            timeout=1, check=True)
    except (OSError, subprocess.SubprocessError):
        return None

    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            return None
        try:
            group_id = int(fields[0])
        except ValueError:
            return None
        if group_id == pgid and not fields[1].startswith("Z"):
            return True
    return False


def kill_group(process):
    """Kill descendants too, including when the group leader already exited."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        # Reap the group leader before checking again. On Darwin, killpg may
        # return EPERM when the only remaining group members are zombies.
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()
        if sys.platform == "darwin":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            except PermissionError:
                if _darwin_group_has_live_members(process.pid) is False:
                    return
            else:
                return
        raise PermissionError(
            f"could not signal process group {process.pid}; descendants may remain"
        ) from exc
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
