import json
import os
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest.mock import patch

from scripts.codex_mutator_guard import authorize_codex, handle
from scripts.codex_mutator_setup import ACK, install_launcher, setup
from scripts.codex_mutator_launch import launch, read_effective, validate_effective
from tests.engine import test_mutator_guard as base_guard

PACK = base_guard.PACK


class CodexGuardTests(unittest.TestCase):
    setUp = base_guard.MutatorGuardTests.setUp

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
        for code in (0, 3, 4):
            with patch("scripts.codex_mutator_guard.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], code, "RUN_OVER\n")
                result = handle(self.event("gc hook --claim --drain-ack --json"), self.home, PACK)
                run.assert_called_once()
                self.assertFalse(run.call_args.kwargs.get("shell", False))
                self.assertEqual(run.call_args.args[0], ["gc", "hook", "--claim", "--drain-ack", "--json"])
            reply = subprocess.run(result["hookSpecificOutput"]["updatedInput"]["command"], shell=True,
                                   capture_output=True, text=True, timeout=5)
            self.assertEqual(reply.returncode, code)
            self.assertEqual(reply.stdout, "RUN_OVER\n")

    def test_codex_stages_only_current_child_before_try(self):
        (self.home / "child.c").write_text("// IDEA: change\nint f(void) { return 2; }\n")
        argv = [str(PACK / "bin/funsearch"), "try", str(self.run), str(self.task.id),
                str(self.directory / "child.c")]
        with patch("scripts.codex_mutator_guard.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "RESULT OK 2\n")
            handle(self.event(shlex.join(argv)), self.home, PACK)
        self.assertEqual((self.directory / "child.c").read_bytes(), (self.home / "child.c").read_bytes())
        argv[3] = str(self.other.id)
        with patch("scripts.codex_mutator_guard.subprocess.run") as run, self.assertRaises(ValueError):
            handle(self.event(shlex.join(argv)), self.home, PACK)
        run.assert_not_called()

    def test_codex_patch_is_scoped_and_retirement_blocks_it(self):
        event = {"tool_name": "apply_patch", "tool_input": {"command":
                 "*** Begin Patch\n*** Add File: child.c\n+int f(void) { return 2; }\n*** End Patch"}}
        self.assertEqual(authorize_codex(event, self.home, PACK), ("patch", None))
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

    def test_launch_requires_explicit_policy_acknowledgment(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "sign-off"):
            launch(self.home, PACK)

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
        from scripts.codex_mutator_launch import DISABLED
        import tomllib
        setup(self.home, PACK)
        config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())
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
            self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
            self.assertEqual(argv[argv.index("-a") + 1], "never")
            self.assertIn("--no-daemon", argv)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
            self.assertEqual(env["CODEX_HOME"], str(self.home / ".funsearch-codex-home"))
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
        with patch("scripts.codex_mutator_guard.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "TASK 3 path\n")
            handle(event, self.home, PACK)
        self.assertFalse(source.exists())
        new = self.db.add_task(0, [], slot="1")
        directory = self.run / "tasks" / str(new.id)
        directory.mkdir()
        (directory / "child.c").write_text("pending candidate")
        event = self.event(shlex.join([str(PACK / "bin/funsearch"), "slot", "show", "fs-slot.1"]))
        with patch("scripts.codex_mutator_guard.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "{}\n")
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
