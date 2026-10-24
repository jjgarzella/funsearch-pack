import json
import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.codex_mutator_guard import RECEIPT_TTL, authorize_codex, handle, main, prune_receipts
from scripts.codex_mutator_setup import install_launcher, sandbox_backend, setup
from scripts.codex_mutator_launch import launch, read_effective, validate_effective
from tests.engine import test_mutator_guard as base_guard

PACK = base_guard.PACK


class CodexGuardTests(unittest.TestCase):
    def setUp(self):
        base_guard.MutatorGuardTests.setUp(self)
        locks = tempfile.TemporaryDirectory(prefix="codex locks ", dir="/var/tmp")
        self.addCleanup(locks.cleanup)
        shared = Path(locks.name) / "shared-codex"
        shared.mkdir()
        self.shared_auth = shared / "auth.json"
        self.shared_auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "fake-token"}}))
        policy = patch.dict(os.environ, FS_CODEX_SANDBOX="workspace-write",
                            FS_CODEX_LOCK_DIR=str(Path(locks.name) / "locks"), CODEX_HOME=str(shared))
        policy.start()
        self.addCleanup(policy.stop)

    def receipts(self):
        return list(self.home.glob(".funsearch-output-*"))

    def event(self, command):
        return {"tool_name": "Bash", "tool_input": {"command": command}}

    def assertNoRm(self, command):
        # Codex 0.164 rejects `rm -f` relays after the brokered command ran.
        words = shlex.split(command.replace(";", " ; "))
        self.assertFalse([word for word in words if Path(word).name == "rm"], command)

    def test_codex_prunes_only_receipts_older_than_ttl(self):
        old, fresh = self.home / ".funsearch-output-old", self.home / ".funsearch-output-fresh"
        for receipt in (old, fresh):
            receipt.write_text("output")
        now = fresh.stat().st_mtime
        os.utime(old, (now - RECEIPT_TTL - 1, now - RECEIPT_TTL - 1))
        prune_receipts(self.home, now)
        self.assertEqual(self.receipts(), [fresh])
        # Each hook call prunes before writing its own receipt.
        os.utime(fresh, (now - RECEIPT_TTL - 1, now - RECEIPT_TTL - 1))
        task = self.directory / "TASK.md"
        task.write_text("task data\n")
        result = handle(self.event(shlex.join(["cat", str(task)])), self.home, PACK)
        receipts = self.receipts()
        self.assertEqual(len(receipts), 1)
        self.assertIn(str(receipts[0]), result["hookSpecificOutput"]["updatedInput"]["command"])

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
        self.assertNoRm(output["updatedInput"]["command"])
        self.assertEqual(len(self.receipts()), 1)
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
            self.assertNoRm(result["hookSpecificOutput"]["updatedInput"]["command"])

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
        # The guard (alive while candidate C runs) never holds the model API key.
        prefix = ["/usr/bin/env", "-u", "CODEX_API_KEY", "-u", "CODEX_ACCESS_TOKEN", "-u", "OPENAI_API_KEY"]
        self.assertEqual(argv[:8], prefix + [sys.executable])
        probe = subprocess.run(prefix + [sys.executable, "-c", "import os; print(sorted(os.environ))"],
                               env=dict(os.environ, CODEX_API_KEY="sk-x", CODEX_ACCESS_TOKEN="t",
                                        OPENAI_API_KEY="sk-y"),
                               capture_output=True, text=True, timeout=5)
        self.assertNotIn("CODEX_API_KEY", probe.stdout)
        self.assertNotIn("CODEX_ACCESS_TOKEN", probe.stdout)
        self.assertNotIn("OPENAI_API_KEY", probe.stdout)
        for event in ({}, self.event("cat /etc/passwd")):
            result = subprocess.run(argv, input=json.dumps(event), text=True, capture_output=True,
                                    env=os.environ.copy(), timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_guard_caught_process_errors_return_deny_and_timeouts_report_unknown(self):
        from scripts.codex_mutator_guard import BrokerTimeout
        for error in (OSError("cannot execute broker"), BrokerTimeout("gc exceeded 280 s")):
            output = io.StringIO()
            with self.subTest(error=error), \
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

    def test_setup_preserves_macos_and_linux_native_sandboxes(self):
        import tomllib
        setup(self.home, PACK, backend="seatbelt")
        config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())
        self.assertEqual(config["default_permissions"], "funsearch_native")
        self.assertNotIn("sandbox_mode", config)
        validate_effective(config, self.home, backend="seatbelt")

        linux_home = self.home / "linux"
        setup(linux_home, PACK, backend="linux")
        linux_config = tomllib.loads((linux_home / ".funsearch-codex-home/config.toml").read_text())
        self.assertEqual(linux_config["sandbox_mode"], "workspace-write")
        validate_effective(linux_config, linux_home, backend="linux")

    def test_native_backend_selection_tracks_host_platform(self):
        self.assertEqual(sandbox_backend({}, "Darwin"), "seatbelt")
        self.assertEqual(sandbox_backend({}, "Linux"), "linux")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            sandbox_backend({}, "Windows")
        for override in ("container", "danger-full-access"):
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, "native sandboxing"):
                sandbox_backend({"FS_CODEX_SANDBOX": override}, "Darwin")

    def test_unsupported_sandbox_overrides_are_refused(self):
        for mode in ("container", "danger-full-access", "", "CONTAINER", "container "):
            with self.subTest(mode=mode), patch.dict(os.environ, FS_CODEX_SANDBOX=mode):
                with self.assertRaisesRegex(ValueError, "native sandboxing is required"):
                    sandbox_backend()
                with self.assertRaisesRegex(ValueError, "native sandboxing is required"):
                    setup(self.home, PACK)

    def test_native_profile_preflight_preserves_other_refusals(self):
        import copy
        import tomllib
        from scripts.codex_mutator_launch import DISABLED
        setup(self.home, PACK, backend="seatbelt")
        config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())
        changes = [((), key, value) for key, value in (
            ("approval_policy", "on-request"), ("sandbox_mode", "workspace-write"),
            ("sandbox_mode", "read-only"), ("mcp_servers", {"unwanted": {}}),
            ("plugins", {"unwanted": {}}), ("default_permissions", "broad"),
            ("web_search", "live"), ("allow_login_shell", True),
            ("project_root_markers", [".git"]), ("project_doc_max_bytes", 100))]
        changes += [(("features",), key, True) for key in DISABLED]
        changes += [(("features",), key, False) for key in ("hooks", "shell_tool", "skip_host_skill_discovery")]
        changes += [(("permissions", "funsearch_native"), "extends", ":danger-full-access"),
                    (("permissions", "funsearch_native"), "workspace_roots", ["/"]),
                    (("permissions", "funsearch_native", "filesystem"), "~/.ssh", "read"),
                    (("permissions", "funsearch_native", "filesystem"), ":tmpdir", "write"),
                    (("permissions", "funsearch_native", "network"), "enabled", True)]
        changes += [(("shell_environment_policy",), "inherit", "all"),
                    (("shell_environment_policy",), "set", {"PATH": "/usr/bin:/bin", "EXTRA": "1"})]
        for parents, key, value in changes:
            modified = copy.deepcopy(config)
            target = modified
            for parent in parents:
                target = target[parent]
            target[key] = value
            with self.subTest(parents=parents, key=key, value=value), self.assertRaises(ValueError):
                validate_effective(modified, self.home, backend="seatbelt")

    def test_effective_config_rejects_inherited_capabilities(self):
        import tomllib
        setup(self.home, PACK, backend="seatbelt")
        config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())
        validate_effective(config, self.home, backend="seatbelt")
        for key, value in (("approval_policy", "on-request"), ("sandbox_mode", "danger-full-access"),
                           ("mcp_servers", {"unwanted": {}}), ("plugins", {"unwanted": {}}),
                           ("web_search", "cached"), ("default_permissions", "broad")):
            modified = dict(config, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_effective(modified, self.home, backend="seatbelt")

    def test_launcher_uses_claimed_slot_model_and_fixed_native_options(self):
        self.check_launcher("workspace-write", "seatbelt")

    def test_linux_launcher_keeps_workspace_write_and_fixed_native_options(self):
        self.check_launcher("workspace-write", "linux")

    def check_launcher(self, mode, backend):
        from scripts.codex_mutator_launch import DISABLED
        import tomllib
        policy = patch.dict(os.environ, FS_CODEX_SANDBOX=mode,
                            OPENAI_API_KEY="sk-test-key", OPENAI_BASE_URL="https://elsewhere.invalid",
                            CODEX_API_KEY="stale-key", CODEX_ACCESS_TOKEN="shared-token")
        policy.start()
        self.addCleanup(policy.stop)
        setup(self.home, PACK, backend=backend)
        stale = self.home / ".funsearch-codex-home/auth.json"
        stale.write_text("{}")
        config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())
        hooks = json.loads((self.home / ".funsearch-codex-home/hooks.json").read_text())["hooks"]["PreToolUse"]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["matcher"], "*")
        self.assertIn("scripts/codex_mutator_guard.py", hooks[0]["hooks"][0]["command"])
        with patch("scripts.codex_mutator_launch.sandbox_backend", return_value=backend), \
                patch("scripts.codex_mutator_launch.read_effective", return_value=config) as effective, \
                patch("scripts.codex_mutator_launch.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "codex-cli 0.164.0\n")) as run, \
                patch("scripts.gc_lifecycle.gc") as gc, \
                patch("scripts.codex_mutator_launch.os.execvpe") as execute, \
                patch("scripts.codex_mutator_launch.os.chdir"):
            gc.side_effect = [json.dumps({"action": "work", "bead_id": "fs-slot.1"}),
                              json.dumps({"id": "fs-slot.1", "metadata": {"fs.provider": "codex", "opt_model": "custom-model"}})]
            launch(self.home, PACK)
            self.assertEqual(gc.call_args_list[0].args, ("hook", "--claim", "--drain-ack", "--json"))
            argv, env = execute.call_args.args[1:]
            self.assertEqual(argv[argv.index("-m") + 1], "custom-model")
            if backend == "linux":
                self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
            else:
                self.assertNotIn("-s", argv)
            self.assertNotIn("-a", argv)
            self.assertIn("--no-daemon", argv)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertEqual(run.call_args_list[0].args[0], ["codex", "--version"])
            self.assertEqual(run.call_args_list[1].args[0][-1], "/usr/bin/true")
            if backend == "seatbelt":
                self.assertEqual(run.call_args_list[1].args[0][-3:], ["--permission-profile", "funsearch_native", "/usr/bin/true"])
            self.assertEqual(env["CODEX_HOME"], str(self.home / ".funsearch-codex-home"))
            self.assertEqual({name for name in env if name.startswith(("OPENAI_", "CODEX_"))}, {"CODEX_HOME"})
            self.assertNotIn("FS_CODEX_POLICY_ACK", env)
            self.assertNotIn("FS_CODEX_SANDBOX", env)
            self.assertEqual(effective.call_args.args[1], env)
            self.assertTrue(stale.is_symlink())
            self.assertEqual(stale.readlink(), self.shared_auth.resolve())
            self.assertEqual(config["forced_login_method"], "chatgpt")
            execute.reset_mock()
            gc.side_effect = [json.dumps({"action": "drain"})]
            self.assertEqual(launch(self.home, PACK), 0)
            execute.assert_not_called()
        for feature in DISABLED:
            modified = dict(config, features=dict(config["features"], **{feature: True}))
            with self.subTest(feature=feature), self.assertRaises(ValueError):
                validate_effective(modified, self.home, backend=backend)

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
        private = workspace / ".funsearch-codex-home"
        (private / "auth.json").symlink_to(self.shared_auth.resolve())
        env = {name: value for name, value in os.environ.items()
               if not name.startswith(("OPENAI_", "CODEX_"))}
        env["CODEX_HOME"] = str(private)
        from scripts.codex_mutator_launch import check_layers
        with patch("scripts.codex_mutator_launch.check_layers", wraps=check_layers) as checked:
            validate_effective(read_effective(workspace, env), workspace)
        checked.assert_called_once()
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
        # Codex splits only at LF: any other separator would hide a second path.
        for separator in "\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029\x00":
            for header in ("*** Add File: child.c", "*** Update File: child.c"):
                lines = (f"{header}{separator}+/../.funsearch-slot.json", "+{}")
                with self.subTest(separator=separator, header=header), self.assertRaises(ValueError):
                    authorize_codex(self.patch_event(*lines), self.home, PACK)
            with self.subTest(body=separator), self.assertRaises(ValueError):
                authorize_codex(self.patch_event("*** Add File: child.c", f"+x{separator}y"), self.home, PACK)
        tab = self.patch_event("*** Add File: child.c", "+\tint x;")
        self.assertEqual(authorize_codex(tab, self.home, PACK), ("patch", None))
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
