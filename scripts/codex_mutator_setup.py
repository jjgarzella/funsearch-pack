"""Create a private native Codex launch configuration for one pool session."""

import json
from pathlib import Path
import shlex
import sys


ACK = "trusted-local-v1"


def setup(work_dir, pack_dir):
    home, pack = Path(work_dir).resolve(), Path(pack_dir).resolve()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    directory = home / ".funsearch-codex-home"
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
    (home / ".funsearch-codex-root").touch()
    # Keep the user config separate from project config and all shared homes.
    # The wrapper also checks effective config before making a model request.
    guard = shlex.join([sys.executable, str(pack / "scripts/codex_mutator_guard.py"), str(home), str(pack)])
    settings = '''model = "gpt-6-luna"
approval_policy = "never"
sandbox_mode = "workspace-write"
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
'''.replace('[projects.HOME]', f'[projects.{json.dumps(str(home))}]')
    config = directory / "config.toml"
    config.write_text(settings)
    hooks = directory / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": guard, "timeout": 310}]}]}}, indent=2) + "\n")
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
