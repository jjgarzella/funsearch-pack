"""Create a private native Codex launch configuration for one pool session."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys


ACK = "trusted-local-v1"
LOCK_DIR = "/var/tmp/funsearch-codex-locks"
# Exit status of the probe child when the parent's lock correctly blocks it.
PROBE_BLOCKED = 75


def selected_sandbox(env):
    """Only the acknowledged container policy may remove the native sandbox."""
    mode = env.get("FS_CODEX_SANDBOX", "workspace-write")
    if mode == "workspace-write":
        return "workspace-write"
    if mode != "container":
        raise ValueError("FS_CODEX_SANDBOX must be workspace-write or container")
    if env.get("FS_CODEX_POLICY_ACK") != ACK:
        raise ValueError(f"container mode requires FS_CODEX_POLICY_ACK={ACK}")
    return "danger-full-access"


def lock_directory(env):
    """The private directory holding per-slot hook locks, checked on every use."""
    path = Path(env.get("FS_CODEX_LOCK_DIR", LOCK_DIR))
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError(f"Codex lock directory {path} must be a real 0700 directory owned by this user")
    return path


def lock_path(home):
    digest = hashlib.sha256(str(Path(home).resolve()).encode()).hexdigest()
    return lock_directory(os.environ) / f"{digest}.lock"


def open_lock(home):
    return os.open(lock_path(home), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def probe_lock(home):
    """Fail closed unless flock on the lock file excludes another process.

    Some shared filesystems (for example the virtiofs host share) accept
    flock calls without enforcing them, which would silently disable the
    per-slot serialization in the guard.
    """
    fd = open_lock(home)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Codex slot lock is held; another session uses this slot workspace") from None
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                probe = os.open(lock_path(home), os.O_RDWR | os.O_NOFOLLOW)
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                code = 0
            except BlockingIOError:
                code = PROBE_BLOCKED
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        if os.waitstatus_to_exitcode(status) != PROBE_BLOCKED:
            raise ValueError("Codex lock directory does not enforce flock between processes; "
                             "set FS_CODEX_LOCK_DIR to a local filesystem")
    finally:
        os.close(fd)


def hooks_document(home, pack):
    # The guard waits on try/submit while candidate C runs as this user; keep
    # the API key out of the guard's /proc/<pid>/environ. Codex may run hooks
    # via a login shell whose profile could re-export OPENAI_API_KEY.
    guard = shlex.join(["/usr/bin/env", "-u", "CODEX_API_KEY", "-u", "CODEX_ACCESS_TOKEN", "-u", "OPENAI_API_KEY",
                        sys.executable, str(pack / "scripts/codex_mutator_guard.py"), str(home), str(pack)])
    # The hook timeout exceeds the guard's 5 s lock wait plus its 280 s broker limit.
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": guard, "timeout": 310}]}]}}, indent=2) + "\n"


def setup(work_dir, pack_dir):
    sandbox = selected_sandbox(os.environ)
    home, pack = Path(work_dir).resolve(), Path(pack_dir).resolve()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    probe_lock(home)
    directory = home / ".funsearch-codex-home"
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
    (home / ".funsearch-codex-root").touch()
    for receipt in home.glob(".funsearch-output-*"):
        receipt.unlink()
    # Keep the user config separate from project config and all shared homes.
    # The wrapper also checks effective config before making a model request.
    settings = '''model = "gpt-6-luna"
forced_login_method = "api"
approval_policy = "never"
sandbox_mode = SANDBOX
web_search = "disabled"
allow_login_shell = false
project_root_markers = [".funsearch-codex-root"]
project_doc_max_bytes = 0
[sandbox_workspace_write]
network_access = false
writable_roots = []
exclude_slash_tmp = true
exclude_tmpdir_env_var = true
[shell_environment_policy]
inherit = "none"
[shell_environment_policy.set]
PATH = "/usr/bin:/bin"
[features]
hooks = true
skip_host_skill_discovery = true
shell_tool = true
unified_exec = false
shell_snapshot = false
apps = false
plugins = false
multi_agent = false
browser_use = false
computer_use = false
image_generation = false
view_image = false
skill_search = false
skill_mcp_dependency_install = false
memories = false
code_mode = false
code_mode_host = false
remote_plugin = false
workspace_dependencies = false
tool_suggest = false
recommended_plugins = false
[projects.HOME]
trust_level = "trusted"
'''.replace('SANDBOX', json.dumps(sandbox)).replace('[projects.HOME]', f'[projects.{json.dumps(str(home))}]')
    config = directory / "config.toml"
    config.write_text(settings)
    hooks = directory / "hooks.json"
    hooks.write_text(hooks_document(home, pack))
    for path in (config, hooks):
        path.chmod(0o600)


def install_launcher(work_dir, pack_dir):
    # Gas City can project hooks/MCP/skills into WorkDir after pre_start. A
    # nested root marker prevents those layers entering the model workspace.
    root, pack = Path(work_dir).resolve(), Path(pack_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    home = root / "slot"
    setup(home, pack)
    launch = root / ".funsearch-codex-launch"
    launch.write_text("#!/bin/sh\nexec " + shlex.join([
        sys.executable, str(pack / "scripts/codex_mutator_launch.py"), str(home), str(pack)]) + "\n")
    launch.chmod(0o700)


if __name__ == "__main__":
    install_launcher(*sys.argv[1:])
