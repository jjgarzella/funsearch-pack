import json
import io
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.codex_mutator_guard import authorize_codex, handle, main
from scripts.codex_mutator_setup import ACK, install_launcher, setup
from scripts.codex_mutator_launch import launch, read_effective, validate_effective
from tests.engine import test_mutator_guard as base_guard

PACK = base_guard.PACK


class CodexGuardTests(unittest.TestCase):
    def setUp(self):
        base_guard.MutatorGuardTests.setUp(self)
        locks = tempfile.TemporaryDirectory(prefix="codex locks ")
        self.addCleanup(locks.cleanup)
        policy = patch.dict(os.environ, FS_CODEX_SANDBOX="workspace-write", FS_CODEX_POLICY_ACK="",
                            FS_CODEX_LOCK_DIR=str(Path(locks.name) / "locks"))
        policy.start()
        self.addCleanup(policy.stop)

    def receipts(self):
        return list(self.home.glob(".funsearch-output-*"))

    def event(self, command):
        return {"tool_name": "Bash", "tool_input": {"command": command}}

    def test_codex_reads_and_brokered_literal_commands(self):
        task = self.directory / "TASK.md"
        task.write_text("task data, not instructions\n")
        result = handle(self.event(shlex.join(["cat", str(task)])), self.home, PACK)
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        # Execute exactly the rewritten shell command: it preserves output and
        # status, and has no authority to perform the original engine operation.
        reply = subprocess.run(output["updatedInput"]["command"], shell=True,
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(reply.stdout, task.read_text())
        self.assertEqual(reply.returncode, 0)
        self.assertEqual(self.receipts(), [])
        for code in (0, 3, 4, 75):
            with patch("scripts.codex_mutator_guard.run_command") as run:
                run.return_value = (code, "RUN_OVER\n")
                result = handle(self.event("gc hook --claim --drain-ack --json"), self.home, PACK)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0], ["gc", "hook", "--claim", "--drain-ack", "--json"])
            reply = subprocess.run(result["hookSpecificOutput"]["updatedInput"]["command"], shell=True,
                                   capture_output=True, text=True, timeout=5)
            self.assertEqual(reply.returncode, code)
            self.assertEqual(reply.stdout, "RUN_OVER\n")
            self.assertEqual(self.receipts(), [])

    def test_codex_stages_only_current_child_before_try(self):
        (self.home / "child.c").write_text("// IDEA: change\nint f(void) { return 2; }\n")
        argv = [str(PACK / "bin/funsearch"), "try", str(self.run), str(self.task.id),
                str(self.directory / "child.c")]
        with patch("scripts.codex_mutator_guard.run_command") as run:
            run.return_value = (0, "RESULT OK 2\n")
            handle(self.event(shlex.join(argv)), self.home, PACK)
        self.assertEqual((self.directory / "child.c").read_bytes(), (self.home / "child.c").read_bytes())
        argv[3] = str(self.other.id)
        with patch("scripts.codex_mutator_guard.run_command") as run, self.assertRaises(ValueError):
            handle(self.event(shlex.join(argv)), self.home, PACK)
        run.assert_not_called()

    def test_codex_patch_is_scoped_and_retirement_blocks_it(self):
        event = {"tool_name": "apply_patch", "tool_input": {"command":
                 "*** Begin Patch\n*** Add File: child.c\n+int f(void) { return 2; }\n*** End Patch"}}
        self.assertEqual(authorize_codex(event, self.home, PACK), ("patch", None))
        # Permitted patches return no decision (see docs: inferred 0.160 contract).
        self.assertEqual(handle(event, self.home, PACK), {})
        for header in ("*** Add File: ../secret", "*** Update File: .funsearch-slot.json",
                       "*** Delete File: child.c", "*** Update File: child.c\n*** Move to: secret",
                       "*** Add File: child.c\n+x\n*** Add File: secret"):
            event["tool_input"]["command"] = f"*** Begin Patch\n{header}\n+x\n*** End Patch"
            with self.subTest(header=header), self.assertRaises(ValueError):
                authorize_codex(event, self.home, PACK)
        event["tool_input"]["command"] = "*** Begin Patch\n*** Update File: child.c\n@@\n+x\n*** End Patch"
        (self.home / ".funsearch-retired.json").write_text(json.dumps({"session": "session-1"}))
        with self.assertRaisesRegex(ValueError, "released/closed"):
            authorize_codex(event, self.home, PACK)

    def test_codex_denies_extra_tools_shell_fields_and_staging_symlink(self):
        events = [self.event("cat /etc/passwd"), self.event("cat x; id"), self.event("python3 -c 'pass'")]
        events += [{"tool_name": name, "tool_input": {}} for name in
                   ("mcp__kaimon__ex", "spawn_agent", "write_stdin", "view_image", "WebSearch")]
        events += [{"tool_name": "Bash", "tool_input": {"command": "gc runtime drain-ack", key: value}}
                   for key, value in (("cwd", "/"), ("shell", "python3"), ("env", {}))]
        for event in events:
            with self.subTest(event=event), self.assertRaises((ValueError, KeyError)):
                authorize_codex(event, self.home, PACK)
        (self.home / "child.c").symlink_to(self.home / "secret")
        event = {"tool_name": "apply_patch", "tool_input": {"command":
                 "*** Begin Patch\n*** Add File: child.c\n+x\n*** End Patch"}}
        with self.assertRaisesRegex(ValueError, "symlink"):
            authorize_codex(event, self.home, PACK)

    def test_codex_native_hook_contract_and_malformed_input_denial(self):
        setup(self.home, PACK)
        hooks = json.loads((self.home / ".funsearch-codex-home/hooks.json").read_text())
        argv = shlex.split(hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        for event in ({}, self.event("cat /etc/passwd")):
            result = subprocess.run(argv, input=json.dumps(event), text=True, capture_output=True,
                                    env=os.environ.copy(), timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_guard_caught_process_errors_return_deny_and_timeouts_report_unknown(self):
        from scripts.codex_mutator_guard import BrokerTimeout
        for mode in ("workspace-write", "container"):
            for error in (OSError("cannot execute broker"), BrokerTimeout("gc exceeded 280 s")):
                output = io.StringIO()
                with self.subTest(mode=mode, error=error), \
                        patch.dict(os.environ, FS_CODEX_SANDBOX=mode, FS_CODEX_POLICY_ACK=ACK), \
                        patch("sys.argv", ["guard", str(self.home), str(PACK)]), \
                        patch("sys.stdin", io.StringIO(json.dumps(self.event("gc hook --claim --drain-ack --json")))), \
                        patch("sys.stdout", output), \
                        patch("scripts.codex_mutator_guard.run_command", side_effect=error):
                    main()
                result = json.loads(output.getvalue())["hookSpecificOutput"]
                if isinstance(error, OSError):
                    self.assertEqual(result["permissionDecision"], "deny")
                    self.assertNotIn("updatedInput", result)
                    continue
                # The command started, so its outcome is unknown rather than denied.
                reply = subprocess.run(result["updatedInput"]["command"], shell=True,
                                       capture_output=True, text=True, timeout=5)
                self.assertEqual(reply.returncode, 75)
                self.assertIn("OUTCOME UNKNOWN", reply.stdout)
                self.assertIn(f"{PACK / 'bin/funsearch'} slot show fs-slot.1", reply.stdout)

    def test_launch_requires_explicit_policy_acknowledgment(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "sign-off"):
            launch(self.home, PACK)

    def test_default_setup_retains_native_sandbox(self):
        import tomllib
        with patch.dict(os.environ, {}, clear=True):
            setup(self.home, PACK)
        config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())
        self.assertEqual(config["sandbox_mode"], "workspace-write")
        validate_effective(config, self.home)

    def test_container_requires_exact_mode_and_ack_before_config_or_launch(self):
        import tomllib
        with patch.dict(os.environ, FS_CODEX_SANDBOX="container", FS_CODEX_POLICY_ACK=ACK):
            setup(self.home, PACK)
            config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())
            self.assertEqual(config["sandbox_mode"], "danger-full-access")
            validate_effective(config, self.home)
        for mode in ("workspace-write", "container", "danger-full-access", "", "CONTAINER", "container "):
            for ack in (None, "", "trusted-local-v2", ACK + " ", ACK):
                if mode == "container" and ack == ACK:
                    continue
                env = {"FS_CODEX_SANDBOX": mode}
                if ack is not None:
                    env["FS_CODEX_POLICY_ACK"] = ack
                with self.subTest(mode=mode, ack=ack), patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(ValueError):
                        validate_effective(config, self.home)
                    if mode != "workspace-write" or ack != ACK:
                        with patch("scripts.codex_mutator_launch.subprocess.run") as run, \
                                patch("scripts.codex_mutator_launch.setup") as configure, \
                                self.assertRaises(ValueError):
                            launch(self.home, PACK)
                        run.assert_not_called()
                        configure.assert_not_called()
                    if mode != "workspace-write":
                        with self.assertRaises(ValueError):
                            setup(self.home, PACK)

    def test_container_preflight_preserves_all_other_refusals(self):
        import copy
        import tomllib
        from scripts.codex_mutator_launch import DISABLED
        with patch.dict(os.environ, FS_CODEX_SANDBOX="container", FS_CODEX_POLICY_ACK=ACK):
            setup(self.home, PACK)
            config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())
            changes = [((), key, value) for key, value in (
                ("approval_policy", "on-request"), ("sandbox_mode", "workspace-write"),
                ("sandbox_mode", "read-only"), ("mcp_servers", {"unwanted": {}}),
                ("plugins", {"unwanted": {}}), ("default_permissions", "broad"),
                ("web_search", "live"), ("allow_login_shell", True),
                ("project_root_markers", [".git"]), ("project_doc_max_bytes", 100))]
            changes += [(("features",), key, True) for key in DISABLED]
            changes += [(("features",), key, False) for key in ("hooks", "shell_tool", "skip_host_skill_discovery")]
            changes += [(("sandbox_workspace_write",), key, value) for key, value in (
                ("network_access", True), ("writable_roots", ["/"]),
                ("exclude_slash_tmp", False), ("exclude_tmpdir_env_var", False))]
            changes += [(("shell_environment_policy",), "inherit", "all"),
                        (("shell_environment_policy",), "set", {"PATH": "/usr/bin:/bin", "EXTRA": "1"})]
            for parents, key, value in changes:
                modified = copy.deepcopy(config)
                target = modified
                for parent in parents:
                    target = target[parent]
                target[key] = value
                with self.subTest(parents=parents, key=key, value=value), self.assertRaises(ValueError):
                    validate_effective(modified, self.home)

    def test_effective_config_rejects_inherited_capabilities(self):
        import tomllib
        setup(self.home, PACK)
        config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())
        validate_effective(config, self.home)
        for key, value in (("approval_policy", "on-request"), ("sandbox_mode", "danger-full-access"),
                           ("mcp_servers", {"unwanted": {}}), ("plugins", {"unwanted": {}}),
                           ("web_search", "cached"), ("default_permissions", "broad")):
            modified = dict(config, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_effective(modified, self.home)
        for key, value in (("network_access", True), ("writable_roots", ["/tmp"]),
                           ("exclude_slash_tmp", False), ("exclude_tmpdir_env_var", False)):
            modified = dict(config, sandbox_workspace_write=dict(config["sandbox_workspace_write"], **{key: value}))
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_effective(modified, self.home)

    def test_launcher_uses_claimed_slot_model_and_fixed_native_options(self):
        self.check_launcher("workspace-write", "workspace-write")

    def test_container_launcher_uses_full_access_and_keeps_guard(self):
        self.check_launcher("container", "danger-full-access")

    def check_launcher(self, mode, sandbox):
        from scripts.codex_mutator_launch import DISABLED
        import tomllib
        policy = patch.dict(os.environ, FS_CODEX_SANDBOX=mode, FS_CODEX_POLICY_ACK=ACK,
                            OPENAI_API_KEY="sk-test-key", OPENAI_BASE_URL="https://elsewhere.invalid",
                            CODEX_ACCESS_TOKEN="shared-token")
        policy.start()
        self.addCleanup(policy.stop)
        setup(self.home, PACK)
        stale = self.home / ".funsearch-codex-home/auth.json"
        stale.write_text("{}")
        config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())
        hooks = json.loads((self.home / ".funsearch-codex-home/hooks.json").read_text())["hooks"]["PreToolUse"]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["matcher"], "*")
        self.assertIn("scripts/codex_mutator_guard.py", hooks[0]["hooks"][0]["command"])
        with patch.dict(os.environ, FS_CODEX_POLICY_ACK=ACK), \
                patch("scripts.codex_mutator_launch.read_effective", return_value=config), \
                patch("scripts.codex_mutator_launch.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "codex-cli 0.160.0\n")), \
                patch("scripts.gc_lifecycle.gc") as gc, \
                patch("scripts.codex_mutator_launch.os.execvpe") as execute, \
                patch("scripts.codex_mutator_launch.os.chdir"):
            gc.side_effect = [json.dumps({"action": "work", "bead_id": "fs-slot.1"}),
                              json.dumps({"id": "fs-slot.1", "metadata": {"fs.provider": "codex", "opt_model": "custom-model"}})]
            launch(self.home, PACK)
            self.assertEqual(gc.call_args_list[0].args, ("hook", "--claim", "--drain-ack", "--json"))
            argv, env = execute.call_args.args[1:]
            self.assertEqual(argv[argv.index("-m") + 1], "custom-model")
            self.assertEqual(argv[argv.index("-s") + 1], sandbox)
            self.assertEqual(argv[argv.index("-a") + 1], "never")
            self.assertIn("--no-daemon", argv)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertEqual(env["CODEX_HOME"], str(self.home / ".funsearch-codex-home"))
            # API key from the environment only; no shared login or endpoint.
            self.assertEqual(env["CODEX_API_KEY"], "sk-test-key")
            self.assertFalse([name for name in env if name.startswith("OPENAI_")])
            self.assertNotIn("CODEX_ACCESS_TOKEN", env)
            self.assertFalse(stale.exists())
            self.assertEqual(config["forced_login_method"], "api")
            execute.reset_mock()
            gc.side_effect = [json.dumps({"action": "drain"})]
            self.assertEqual(launch(self.home, PACK), 0)
            execute.assert_not_called()
        for feature in DISABLED:
            modified = dict(config, features=dict(config["features"], **{feature: True}))
            with self.subTest(feature=feature), self.assertRaises(ValueError):
                validate_effective(modified, self.home)

    def test_next_task_clears_old_staging_and_show_restores_pending_child(self):
        source = self.home / "child.c"
        source.write_text("old task")
        self.db.close_task(self.task.id)
        event = self.event(shlex.join([str(PACK / "bin/funsearch"), "next-task", str(self.run), "--slot", "1"]))
        with patch("scripts.codex_mutator_guard.run_command") as run:
            run.return_value = (0, "TASK 3 path\n")
            handle(event, self.home, PACK)
        self.assertFalse(source.exists())
        new = self.db.add_task(0, [], slot="1")
        directory = self.run / "tasks" / str(new.id)
        directory.mkdir()
        (directory / "child.c").write_text("pending candidate")
        event = self.event(shlex.join([str(PACK / "bin/funsearch"), "slot", "show", "fs-slot.1"]))
        with patch("scripts.codex_mutator_guard.run_command") as run:
            run.return_value = (0, "{}\n")
            handle(event, self.home, PACK)
        self.assertEqual(source.read_text(), "pending candidate")

    def test_native_config_excludes_projected_parent_mcp(self):
        import shutil
        if shutil.which("codex") is None:
            self.skipTest("native Codex CLI is not installed")
        # Only config/read; never starts a model thread or mutator.
        (self.home / ".codex").mkdir()
        (self.home / ".codex/config.toml").write_text('[mcp_servers.unwanted]\ncommand="false"\n')
        (self.home / ".codex/hooks.json").write_text('{"hooks":{"PreToolUse":[]}}')
        install_launcher(self.home, PACK)
        workspace = self.home / "slot"
        env = dict(os.environ, CODEX_HOME=str(workspace / ".funsearch-codex-home"))
        validate_effective(read_effective(workspace, env), workspace)
        self.assertIn(str(workspace), (self.home / ".funsearch-codex-launch").read_text())

    def test_local_staging_read_requires_current_task_and_real_file(self):
        (self.home / "child.c").write_text("new staging candidate")
        event = self.event("cat child.c")
        output = handle(event, self.home, PACK)["hookSpecificOutput"]
        reply = subprocess.run(output["updatedInput"]["command"], shell=True,
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(reply.stdout, "new staging candidate")
        self.db.close_task(self.task.id)
        with self.assertRaisesRegex(ValueError, "allocate a task"):
            authorize_codex(event, self.home, PACK)
        self.db.add_task(0, [], slot="1")
        (self.home / "child.c").unlink()
        (self.home / "child.c").symlink_to(self.home / "secret")
        with self.assertRaisesRegex(ValueError, "symlink"):
            authorize_codex(event, self.home, PACK)

    def patch_event(self, *lines):
        return {"tool_name": "apply_patch", "tool_input": {"command": "\n".join(
            ["*** Begin Patch", *lines, "*** End Patch"])}}

    def test_patch_grammar_is_strict(self):
        child = self.home / "child.c"
        valid = [("*** Add File: child.c", "+// IDEA: x", "+int f(void) { return 2; }"),
                 (f"*** Add File: {child}", "+x"),
                 ("*** Update File: child.c", "@@", " int f(void) {", "-  return 1;", "+  return 2;", " }"),
                 (f"*** Update File: {child}", "@@ int f(void)", "-a", "+b", "*** End of File"),
                 ("*** Add File: child.c", "+*** not a header once prefixed")]
        for lines in valid:
            with self.subTest(lines=lines):
                self.assertEqual(authorize_codex(self.patch_event(*lines), self.home, PACK), ("patch", None))
        crlf = self.patch_event("*** Add File: child.c", "+x")
        crlf["tool_input"]["command"] = crlf["tool_input"]["command"].replace("\n", "\r\n")
        self.assertEqual(authorize_codex(crlf, self.home, PACK), ("patch", None))
        invalid = [("*** Add File: child.c", "+x", "  *** Add File: secret", "+y"),
                   ("***  Add File: child.c", "+x"), ("*** add file: child.c", "+x"),
                   ("*** Update File: child.c", "*** Move to: secret", "@@", "+x"),
                   ("*** Delete File: child.c",), ("*** Add File: child.c", "+x", "*** Add File: secret", "+y"),
                   ("*** Add File: child.c",), ("*** Update File: child.c",),
                   ("*** Update File: child.c", "*** End of File"),
                   ("*** Add File: child.c", "x"), ("*** Add File: child.c", "+x", "*** End of File"),
                   ("*** Update File: child.c", "@@", "bare context"), ("*** Update File: child.c", "@@", ""),
                   ("*** Update File: child.c", "@@", "+x", "*** end patch", "+y"),
                   ("*** Update File: child.c", "@@", "+x", "*** End of File", "+y"),
                   ("*** Add File: child.c ", "+x"), ()]
        for lines in invalid:
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                authorize_codex(self.patch_event(*lines), self.home, PACK)
        for command in (None, "", "*** Begin Patch", "*** Begin Patch\n*** Add File: child.c\n+x\n*** End Patch\n\n"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                authorize_codex({"tool_name": "apply_patch", "tool_input": {"command": command}}, self.home, PACK)

    def test_broker_drops_credentials_replaces_bad_bytes_and_kills_overrun_group(self):
        from scripts.codex_mutator_guard import BrokerTimeout, run_command
        secrets = {"OPENAI_API_KEY": "sk-secret", "OPENAI_BASE_URL": "https://x.invalid",
                   "CODEX_API_KEY": "sk-secret", "CODEX_ACCESS_TOKEN": "token"}
        with patch.dict(os.environ, secrets):
            code, output = run_command(["sh", "-c", "env; printf '\\377'; exit 3"], self.home)
        self.assertEqual(code, 3)
        self.assertNotIn("sk-secret", output)
        self.assertNotIn("x.invalid", output)
        self.assertNotIn("CODEX_ACCESS_TOKEN", output)
        self.assertIn("GC_SESSION_ID=session-1", output)
        self.assertTrue(output.endswith("\ufffd"))
        marker = self.home / "background.pid"
        with self.assertRaisesRegex(BrokerTimeout, "exceeded 0.5 s"):
            run_command(["sh", "-c", f"sleep 30 & echo $! > {shlex.quote(str(marker))}; wait"],
                        self.home, timeout=0.5)
        status = Path(f"/proc/{int(marker.read_text())}/stat")
        for _ in range(100):
            if not status.exists() or status.read_text().split(") ")[1].startswith("Z"):
                break
            import time
            time.sleep(0.02)
        else:
            self.fail("the overrunning process group was not killed")

    def test_concurrent_next_task_allocates_once(self):
        import threading
        import time
        from engine.funsearch.db import Database
        self.db.close_task(self.task.id)
        calls, errors = [], []

        def allocate(argv, home):
            calls.append(argv)
            time.sleep(0.3)
            with Database(self.run / "db.sqlite") as db:
                db.add_task(0, [], slot="1")
            return 0, "TASK\n"

        event = self.event(shlex.join([str(PACK / "bin/funsearch"), "next-task", str(self.run), "--slot", "1"]))

        def call():
            try:
                handle(event, self.home, PACK)
            except ValueError as exc:
                errors.append(str(exc))

        with patch("scripts.codex_mutator_guard.run_command", side_effect=allocate):
            threads = [threading.Thread(target=call) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(errors), 1)
        self.assertRegex(errors[0], "finish the current task|slot busy")

    def test_busy_slot_lock_times_out(self):
        import fcntl
        from scripts.codex_mutator_guard import slot_lock
        from scripts.codex_mutator_setup import open_lock
        holder = open_lock(self.home)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        with self.assertRaisesRegex(ValueError, "slot busy"):
            with slot_lock(self.home, wait=0.2):
                pass
        fcntl.flock(holder, fcntl.LOCK_UN)
        with slot_lock(self.home, wait=0.2):
            pass

    def test_setup_requires_private_coherent_lock_directory_and_clears_receipts(self):
        from scripts.codex_mutator_setup import lock_directory
        receipt = self.home / ".funsearch-output-old"
        receipt.write_text("stale")
        setup(self.home, PACK)
        self.assertFalse(receipt.exists())
        directory = lock_directory(os.environ)
        directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            setup(self.home, PACK)
        directory.chmod(0o700)
        link = directory.parent / "link"
        link.symlink_to(directory)
        with patch.dict(os.environ, FS_CODEX_LOCK_DIR=str(link)), self.assertRaisesRegex(ValueError, "0700"):
            setup(self.home, PACK)
        # A filesystem that accepts but ignores flock lets the probe child lock too.
        with patch("scripts.codex_mutator_setup.fcntl.flock"), \
                self.assertRaisesRegex(ValueError, "does not enforce flock"):
            setup(self.home, PACK)
