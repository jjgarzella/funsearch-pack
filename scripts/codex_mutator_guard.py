"""Codex adapter for the existing task policy; trusted-local, not isolation.

Only the staging child.c may be edited under the task policy. Approved
engine/lifecycle commands run here, outside the native sandbox, because their DB and
Gas City control operations cannot be granted to arbitrary model shell code.
Container mode removes the native sandbox, retaining the same task policy.
The shell receives only a trusted receipt-reading command, never the original
command. Codex hook runtime failures can fail open; see docs/codex-mutator.md.
"""

import json
from pathlib import Path
import shlex
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.mutator_guard import authorize, context, literal_command, open_task, task_file


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
        patch = inputs["command"]
        if not isinstance(patch, str):
            raise ValueError("expected a patch string")
        lines = patch.splitlines()
        if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
            raise ValueError("expected one complete child.c patch")
        headers = [line for line in lines[1:-1] if line.startswith("*** ")]
        permitted = {"*** Add File: child.c", "*** Update File: child.c",
                     f"*** Add File: {home / 'child.c'}", f"*** Update File: {home / 'child.c'}"}
        # End of File is a supported patch terminator, not another operation.
        operations = [line for line in headers if line != "*** End of File"]
        if len(operations) != 1 or operations[0] not in permitted or lines[1] != operations[0]:
            raise ValueError("patch may only add/update local child.c; no moves/deletes")
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


def broker(operation, argv, home, pack):
    if operation == "read":
        path = Path(argv[1])
        if not path.is_absolute():
            path = home / path
        return 0, path.read_text()
    if argv[:1] == [str(pack / "bin/funsearch")] and argv[1] in ("try", "submit"):
        # authorize_codex validated the canonical path and current task first.
        source = staging_file(home)
        child = current_child(home)
        if child != Path(argv[4]):
            raise ValueError("task changed before staging")
        child.write_bytes(source.read_bytes())
    result = subprocess.run(argv, cwd=home, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=300)
    # Each new/resumed task starts from its canonical candidate, if any. Never
    # leak a previous task's local source into the next submission.
    if argv[:1] == [str(pack / "bin/funsearch")] and result.returncode == 0:
        if argv[1] == "next-task" or argv[1:3] == ["slot", "show"]:
            local = staging_file(home)
            local.unlink(missing_ok=True)
            data = context(home)
            if open_task(data) is not None:
                child = current_child(home)
                if child.exists():
                    local.write_bytes(child.read_bytes())
    return result.returncode if 0 <= result.returncode <= 255 else 1, result.stdout


def handle(event, home, pack):
    operation, argv = authorize_codex(event, home, pack)
    output = {"hookEventName": "PreToolUse", "permissionDecision": "allow",
              "permissionDecisionReason": "FunSearch Codex task policy"}
    if operation != "patch":
        code, body = broker(operation, argv, home, pack)
        receipt = home / (".funsearch-output-" + uuid.uuid4().hex)
        receipt.write_text(body)
        receipt.chmod(0o600)
        output["updatedInput"] = {"command": f"/bin/cat {shlex.quote(str(receipt))}; exit {code}"}
    return {"hookSpecificOutput": output}


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
