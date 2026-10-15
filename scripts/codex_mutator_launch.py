"""Native Codex-only launch with no shared daemon/config and fixed policy.

No model request is made during config preflight. The operator acknowledgment
is required because Codex's native hooks are not a fail-closed boundary.
"""

import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.codex_mutator_setup import ACK, hooks_document, selected_sandbox, setup


TESTED_CODEX_VERSION = "codex-cli 0.160.0"


DISABLED = ("unified_exec", "shell_snapshot", "apps", "plugins", "multi_agent",
            "browser_use", "computer_use", "image_generation", "view_image",
            "skill_search", "skill_mcp_dependency_install", "memories",
            "code_mode", "code_mode_host", "remote_plugin", "workspace_dependencies",
            "tool_suggest", "recommended_plugins")


# ConfigLayerSource variants in the 0.160 app-server schema.
LAYER_TYPES = ("packagedDefaults", "mdm", "system", "enterpriseManaged", "user", "project",
               "sessionFlags", "legacyManagedConfigTomlFromFile", "legacyManagedConfigTomlFromMdm")


def validate_effective(config, home):
    """Reject inherited capability/sandbox overrides before a model is loaded."""
    expected_sandbox = selected_sandbox(os.environ)
    if config.get("approval_policy") != "never" or config.get("sandbox_mode") != expected_sandbox:
        raise ValueError(f"Codex must use {expected_sandbox} and approval never")
    sandbox = config.get("sandbox_workspace_write", {})
    if (sandbox.get("network_access") is not False or sandbox.get("writable_roots")
            or sandbox.get("exclude_slash_tmp") is not True
            or sandbox.get("exclude_tmpdir_env_var") is not True):
        # In container mode these settings are retained but inactive; they do
        # not provide filesystem/network enforcement with danger-full-access.
        raise ValueError("Codex workspace-write settings must remain slot-only with no network")
    features = config.get("features", {})
    if (features.get("hooks") is not True or features.get("shell_tool") is not True
            or features.get("skip_host_skill_discovery") is not True
            or any(features.get(key) is not False for key in DISABLED)):
        raise ValueError("Codex capability overrides are not permitted")
    if config.get("mcp_servers") or config.get("plugins") or config.get("default_permissions"):
        raise ValueError("Codex MCP servers, plugins and permission profiles are forbidden")
    if config.get("web_search") != "disabled" or config.get("allow_login_shell") is not False:
        raise ValueError("Codex web search and login shells are forbidden")
    if (config.get("project_root_markers") != [".funsearch-codex-root"]
            or config.get("project_doc_max_bytes") != 0):
        raise ValueError("Codex project discovery must stop at the private slot")
    policy = config.get("shell_environment_policy", {})
    if policy.get("inherit") != "none" or policy.get("set") != {"PATH": "/usr/bin:/bin"}:
        raise ValueError("Codex shell environment must be private")
    if config.get("forced_login_method") != "chatgpt":
        raise ValueError("Codex must use ChatGPT login")
    # Hook JSON does not appear here; inline hooks, notifiers, telemetry and
    # provider/endpoint overrides could run code or redirect credentials.
    if (any(config.get(key) is not None for key in
            ("hooks", "notify", "model_provider", "openai_base_url", "otel", "profile"))
            or config.get("model_providers")):
        raise ValueError("Codex hooks, notify, telemetry, profiles and provider overrides are forbidden")


def check_layers(result, home):
    """Accept only our private user layer plus empty non-project layers."""
    private = home / ".funsearch-codex-home"
    layers = result.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ValueError("Codex config/read returned no layers")
    users = []
    for layer in layers:
        source = layer.get("name") if isinstance(layer, dict) else None
        kind = source.get("type") if isinstance(source, dict) else None
        if kind not in LAYER_TYPES:
            raise ValueError(f"unknown Codex config layer type {kind!r}")
        if kind == "project":
            raise ValueError("Codex project config layers are forbidden")
        if kind == "user":
            users.append(source)
            continue
        if layer.get("config") not in ({}, None):
            raise ValueError(f"inherited Codex {kind} configuration is forbidden")
        # Codex loads hooks.json beside each layer's config.toml.
        for key in ("file", "dotCodexFolder"):
            if isinstance(source.get(key), str):
                folder = Path(source[key])
                folder = folder.parent if key == "file" else folder
                if os.path.lexists(folder / "hooks.json"):
                    raise ValueError(f"inherited Codex hooks are forbidden: {folder / 'hooks.json'}")
    if (len(users) != 1 or users[0].get("file") != str(private / "config.toml")
            or users[0].get("profile") is not None):
        raise ValueError("Codex must load exactly the private user configuration")


def read_effective(home, env):
    # Use the installed CLI's own resolver instead of assuming TOML merge
    # semantics. Read config only: this does not start a thread or a mutator.
    process = subprocess.Popen(["codex", "--no-daemon", "--strict-config", "app-server"],
                               cwd=home, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = b""
    def request(identity, method, params):
        nonlocal pending
        process.stdin.write((json.dumps({"id": identity, "method": method, "params": params}) + "\n").encode())
        process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                row = json.loads(line)
                if row.get("id") == identity:
                    if "error" in row:
                        raise ValueError(f"Codex {method} failed")
                    return row["result"]
            if not selector.select(max(0, deadline - time.monotonic())):
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise ValueError("Codex config preflight exited; CLI 0.160.0 is required")
            pending += chunk
        raise ValueError("Codex config preflight timed out")
    try:
        request(1, "initialize", {"clientInfo": {"name": "funsearch-mutator", "version": "1"}})
        result = request(2, "config/read", {"cwd": str(home), "includeLayers": True})
        check_layers(result, home)
        return result["config"]
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def release_after_refusal(bead, error):
    """Return a claimed slot through the supported helper, then re-raise."""
    from scripts.gc_lifecycle import slot_command
    try:
        code = slot_command("release", bead)
        if code != 0:
            raise ValueError(f"slot release exited {code}")
    except Exception as failure:
        raise ValueError(f"refused after claiming {bead}: {error}; release FAILED: {failure}; "
                         f"slot still claimed by session {os.environ.get('GC_SESSION_ID')}") from error
    raise ValueError(f"refused after claiming {bead}: {error}; slot released") from error


def shared_auth_file():
    """Validate the shared login without exposing credentials in errors."""
    try:
        path = (Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json").resolve()
        if not path.is_file():
            raise ValueError("not a regular file")
        auth = json.loads(path.read_text())
        if (not isinstance(auth, dict) or auth.get("auth_mode") != "chatgpt"
                or not isinstance(auth.get("tokens"), dict) or not auth["tokens"]):
            raise ValueError("not a ChatGPT login")
    except (OSError, ValueError, RuntimeError):
        raise ValueError("Codex mutators require a shared ChatGPT login; "
                         "run codex login with the shared account") from None
    return path


def launch(home, pack):
    if os.environ.get("FS_CODEX_POLICY_ACK") != ACK:
        raise ValueError("Codex mutators require overseer sign-off on docs/codex-mutator.md; "
                         f"then set FS_CODEX_POLICY_ACK={ACK} in the agent environment")
    sandbox = selected_sandbox(os.environ)
    # Resolve the shared login before replacing CODEX_HOME with our private home.
    auth = shared_auth_file()
    version = subprocess.run(["codex", "--version"], capture_output=True, text=True,
                             check=True, timeout=10).stdout.strip()
    if version != TESTED_CODEX_VERSION:
        raise ValueError(f"Codex task policy requires {TESTED_CODEX_VERSION}; review other versions before use")
    # Only our own isolated user config/hooks may be active. Do not load any
    # project-local hook file (hook JSON does not appear in config/read).
    if (home / ".codex").exists() or (home / ".agents").exists():
        raise ValueError("Codex slot workspace must not contain project config, hooks or skills")
    setup(home, pack)
    private = home / ".funsearch-codex-home"
    # Share refreshes with the existing login; a copied cache can diverge.
    (private / "auth.json").unlink(missing_ok=True)
    (private / "auth.json").symlink_to(auth)
    # Drop OPENAI_BASE_URL and every other inherited OPENAI_*/CODEX_* setting.
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("OPENAI_", "CODEX_"))}
    env["CODEX_HOME"] = str(private)
    validate_effective(read_effective(home, env), home)
    if (private / "hooks.json").read_text() != hooks_document(home, pack):
        raise ValueError("private Codex hooks.json changed after setup")
    # slot helpers write their context/retirement files in the working directory.
    os.chdir(home)
    # Claim via the required protocol and read opt_model from that exact bead.
    # The model's first hook call resumes this same assigned slot.
    from scripts.gc_lifecycle import gc
    claim = json.loads(gc("hook", "--claim", "--drain-ack", "--json"))
    if claim.get("action") == "drain":
        return 0
    bead = claim.get("bead_id")
    if claim.get("action") != "work" or not isinstance(bead, str) or not bead:
        raise ValueError(f"unexpected gc hook result {claim!r}; a slot may still be claimed by "
                         f"session {os.environ.get('GC_SESSION_ID')}")
    try:
        rows = json.loads(gc("bd", "show", bead, "--json"))
        row = rows[0] if isinstance(rows, list) and len(rows) == 1 else rows
        if row.get("id") != bead:
            raise ValueError("gc returned the wrong claimed slot")
        metadata = row.get("metadata", {})
        if metadata.get("fs.provider") != "codex":
            raise ValueError("Codex mutator claimed a non-Codex slot")
        model = metadata.get("opt_model", "gpt-6-luna")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("slot opt_model must be a nonempty string")
        prompt = (pack / "agents/mutator-codex/prompt.template.md").read_text().replace("{{.ConfigDir}}", str(pack))
        command = ["codex", "--no-daemon", "--strict-config", "--dangerously-bypass-hook-trust",
                   "-a", "never", "-s", sandbox, "-C", str(home),
                   "exec", "--skip-git-repo-check", "-m", model, prompt]
        os.execvpe(command[0], command, env)
    except Exception as error:
        release_after_refusal(bead, error)


if __name__ == "__main__":
    try:
        sys.exit(launch(*(Path(value).resolve() for value in sys.argv[1:])))
    except Exception as exc:
        print(f"FunSearch Codex launch refused: {exc}", file=sys.stderr)
        sys.exit(2)
