"""Bounded shell commands shared by the compilation pipeline."""

import os
import signal
import subprocess
import tempfile


LOG_BYTES = 4096


def environment(extra_env=None):
    """Overlay process environment; None explicitly removes a variable."""
    result = os.environ.copy()
    for key, value in (extra_env or {}).items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = str(value)
    return result


def kill_group(process):
    """Kill descendants too, including when the group leader already exited."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_command(command, cwd, timeout_s):
    """Return (success, bounded stderr), killing the shell group on timeout."""
    with tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, shell=True, cwd=cwd,
                                       stdout=subprocess.DEVNULL, stderr=stderr,
                                       start_new_session=True)
        except OSError as exc:
            return False, str(exc)[:LOG_BYTES]
        timed_out = False
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_group(process)
        stderr.seek(0)
        log = stderr.read(LOG_BYTES).decode("utf-8", errors="replace")
    if timed_out:
        message = f"command timed out after {timeout_s:g}s\n"
        return False, message + log[:LOG_BYTES - len(message)]
    if process.returncode and not log:
        log = f"command exited with code {process.returncode}"
    return process.returncode == 0, log
