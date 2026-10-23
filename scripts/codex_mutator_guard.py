"""Codex adapter for the existing task policy; trusted-local, not isolation.

Only the staging child.c may be edited under the task policy. Approved
engine/lifecycle commands run here, outside the native sandbox, because their DB and
Gas City control operations cannot be granted to arbitrary model shell code.
The host-native Seatbelt or Linux workspace-write sandbox remains active.
The shell receives only a trusted receipt-reading command, never the original
command. Codex hook runtime failures can fail open; see docs/codex-mutator.md.
"""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import unicodedata
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.codex_mutator_setup import open_lock
from scripts.mutator_guard import authorize, context, literal_command, open_task, task_file


# The native hook timeout (310 s) exceeds LOCK_WAIT + BROKER_TIMEOUT.
LOCK_WAIT = 5
BROKER_TIMEOUT = 280
OUTCOME_UNKNOWN = 75
# Credentials are for the model process only; the brokered CLI compiles
# candidate C in-process for try/submit.
CREDENTIALS = ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN")


def staging_file(home):
    path = home / "child.c"
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("staging child.c must not be a symlink")
    return path


def current_child(home):
    data = context(home)
    task = open_task(data)
    if task is None:
        raise ValueError("allocate a task first")
    path = Path(data["run_dir"]) / "tasks" / str(task) / "child.c"
    task_file(str(path), home, data, write=True)
    return path


def check_patch(patch, home):
    """Accept exactly one Add/Update of local child.c in the plain patch grammar."""
    if not isinstance(patch, str):
        raise ValueError("expected a patch string")
    # Codex's grammar ends lines at LF only. str.splitlines would also split
    # at CR, form feed and Unicode separators, letting a header Python sees
    # as child.c name another path for Codex. Accept LF (or CRLF) and refuse
    # every other control or separator character.
    lines = patch.split("\n")
    if lines[-1] == "":
        lines.pop()
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    for line in lines:
        if any(char != "\t" and (unicodedata.category(char)[0] == "C" or char in "\u2028\u2029")
               for char in line):
            raise ValueError("patch lines may not contain control or separator characters")
    if len(lines) < 2 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("expected one complete child.c patch")
    child = home / "child.c"
    header, body = lines[1], lines[2:-1]
    if header in ("*** Add File: child.c", f"*** Add File: {child}"):
        update = False
    elif header in ("*** Update File: child.c", f"*** Update File: {child}"):
        update = True
        # End of File is a supported terminator, not another operation.
        if body and body[-1] == "*** End of File":
            body = body[:-1]
    else:
        raise ValueError("patch may only add/update local child.c; no moves/deletes")
    if not body:
        raise ValueError("patch must contain child.c lines")
    for line in body:
        if line.lstrip().startswith("***"):
            raise ValueError("patch may contain only one child.c operation")
        if update:
            if not line.startswith(("@@", " ", "-", "+")):
                raise ValueError("update lines must be @@ hunks or start with space, - or +")
        elif not line.startswith("+"):
            raise ValueError("every added child.c line must start with +")


def authorize_codex(event, home, pack):
    """Return (operation, argv), or raise before doing any work."""
    if "cwd" in event and Path(event["cwd"]).resolve() != home:
        raise ValueError("tool cwd must be the private slot workspace")
    tool, inputs = event["tool_name"], event["tool_input"]
    if tool == "apply_patch":
        # Check retirement/session/current-task via the same Write policy.
        child = current_child(home)
        authorize({"tool_name": "Write", "tool_input": {"file_path": str(child)}}, home, pack)
        staging_file(home)
        check_patch(inputs["command"], home)
        return "patch", None
    if tool != "Bash":
        raise ValueError("only shell and child.c apply_patch are permitted")
    # Do not honor caller-supplied process controls, environments or cwd.
    # Native Bash hook input carries command; future/unknown fields fail closed.
    if set(inputs) - {"command"}:
        raise ValueError("unexpected shell input fields")
    argv = literal_command(inputs["command"])
    if len(argv) == 2 and argv[0] == "cat":
        path = Path(argv[1])
        if not path.is_absolute():
            path = home / path
        if path == home / "child.c":
            child = current_child(home)
            authorize({"tool_name": "Read", "tool_input": {"file_path": str(child)}}, home, pack)
            return "read", ["cat", str(staging_file(home))]
        authorize({"tool_name": "Read", "tool_input": {"file_path": argv[1]}}, home, pack)
        return "read", argv
    authorize(event, home, pack)
    return "command", argv


class BrokerTimeout(Exception):
    pass


def run_command(argv, home, timeout=BROKER_TIMEOUT):
    """Run argv in its own process group; kill the group if it overruns."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("OPENAI_") and key not in CREDENTIALS}
    process = subprocess.Popen(argv, cwd=home, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A descendant in another session still holds the pipe.
            process.stdout.close()
            process.wait()
        raise BrokerTimeout(f"{shlex.join(argv)} exceeded {timeout} s") from None
    code = process.returncode
    return code if 0 <= code <= 255 else 1, output.decode(errors="replace")


def broker(operation, argv, home, pack):
    if operation == "read":
        path = Path(argv[1])
        if not path.is_absolute():
            path = home / path
        return 0, path.read_text(errors="replace")
    if argv[:1] == [str(pack / "bin/funsearch")] and argv[1] in ("try", "submit"):
        # authorize_codex validated the canonical path and current task first.
        source = staging_file(home)
        child = current_child(home)
        if child != Path(argv[4]):
            raise ValueError("task changed before staging")
        child.write_bytes(source.read_bytes())
    try:
        code, output = run_command(argv, home)
    except BrokerTimeout as exc:
        # The command was started, so a denial would misreport its outcome.
        try:
            bead = context(home)["bead"]
        except (OSError, KeyError, ValueError):
            bead = "<bead_id>"
        return OUTCOME_UNKNOWN, (f"OUTCOME UNKNOWN: {exc}; the evaluation may still complete. "
                                 f"Run {pack / 'bin/funsearch'} slot show {bead} before retrying.\n")
    # Each new/resumed task starts from its canonical candidate, if any. Never
    # leak a previous task's local source into the next submission.
    if argv[:1] == [str(pack / "bin/funsearch")] and code == 0:
        if argv[1] == "next-task" or argv[1:3] == ["slot", "show"]:
            local = staging_file(home)
            local.unlink(missing_ok=True)
            data = context(home)
            if open_task(data) is not None:
                child = current_child(home)
                if child.exists():
                    local.write_bytes(child.read_bytes())
    return code, output


@contextmanager
def slot_lock(home, wait=LOCK_WAIT):
    """Serialize this slot's hook calls; the kernel releases it if we die."""
    fd = open_lock(home)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("slot busy: another tool call for this slot is running; retry") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def handle(event, home, pack):
    # The lock covers authorization, the brokered command and its receipt.
    # Codex applies a permitted patch only after this hook returns, so a
    # parallel try/submit can still stage an older child.c (see docs).
    with slot_lock(home):
        operation, argv = authorize_codex(event, home, pack)
        if operation == "patch":
            # INFERENCE from the 0.160 binary: a bare "allow" without
            # updatedInput is reported unsupported, so a permitted patch
            # returns no decision. The smoke test must confirm this.
            return {}
        code, body = broker(operation, argv, home, pack)
        receipt = home / (".funsearch-output-" + uuid.uuid4().hex)
        receipt.write_text(body)
        receipt.chmod(0o600)
    path = shlex.quote(str(receipt))
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "permissionDecisionReason": "FunSearch Codex task policy",
        "updatedInput": {"command": f"/bin/cat {path}; /bin/rm -f {path}; exit {code}"}}}


def main():
    try:
        home, pack = (Path(value).resolve() for value in sys.argv[1:])
        result = handle(json.load(sys.stdin), home, pack)
    except Exception as exc:
        result = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                  "permissionDecision": "deny", "permissionDecisionReason": f"FunSearch Codex: {exc}"}}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
