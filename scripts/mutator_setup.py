"""Install private per-session Claude settings before Gas City launches it."""

import json
from pathlib import Path
import shlex
import sys


def setup(work_dir, pack_dir):
    home, pack = Path(work_dir).resolve(), Path(pack_dir).resolve()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    directory = home / ".claude"
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
    command = shlex.join(["python3", str(pack / "scripts/mutator_guard.py"), str(home), str(pack)])
    settings = {
        "permissions": {"defaultMode": "dontAsk", "deny": ["WebFetch", "WebSearch"]},
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": command, "timeout": 10}]}]},
    }
    path = directory / "settings.json"
    path.write_text(json.dumps(settings, indent=2) + "\n")
    path.chmod(0o600)


if __name__ == "__main__":
    setup(*sys.argv[1:])
