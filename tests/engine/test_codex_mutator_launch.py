import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from scripts.codex_mutator_launch import check_layers, launch, validate_effective
from scripts.codex_mutator_setup import PERMISSION_PROFILE, setup

PACK = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures/codex-0.164-config-read.json"


class CodexLaunchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="codex launch ", dir="/var/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "slot"
        self.etc = self.root / "etc-codex"
        self.etc.mkdir()
        self.shared = self.root / "shared-codex"
        self.shared.mkdir()
        self.auth = self.shared / "auth.json"
        self.auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "fake-token"}}))
        policy = patch.dict(os.environ, FS_CODEX_SANDBOX="workspace-write",
                            FS_CODEX_LOCK_DIR=str(self.root / "locks"), GC_SESSION_ID="session-1",
                            CODEX_HOME=str(self.shared))
        policy.start()
        self.addCleanup(policy.stop)
        os.environ.pop("CODEX_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        setup(self.home, PACK, backend="seatbelt")
        self.config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())

    def layers(self):
        # A raw 0.164 config/read response from the generated native profile.
        text = FIXTURE.read_text().replace("@HOME@", str(self.home))
        text = text.replace('"@ETC@/config.toml"', json.dumps(str(self.etc / "config.toml")))
        return json.loads(text)["response"]["result"]

    def test_fixture_layers_pass_and_unexpected_layers_are_refused(self):
        result = self.layers()
        check_layers(result, self.home)
        user, system = result["layers"]
        project = {"name": {"type": "project", "dotCodexFolder": str(self.root / ".codex")}, "config": {}}
        cases = {"no layers": [], "null layers": None, "unknown type": [user, {"name": {"type": "future"}, "config": {}}],
                 "missing type": [user, {"name": {}, "config": {}}], "not a layer": [user, "system"],
                 "two user layers": [user, copy.deepcopy(user)], "no user layer": [system],
                 "wrong user file": [dict(user, name=dict(user["name"], file=str(self.root / "config.toml"))), system],
                 "profile": [dict(user, name=dict(user["name"], profile="other")), system],
                 "project layer": [user, system, project],
                 "nonempty system": [user, dict(system, config={"mcp_servers": {"x": {}}})]}
        for label, layers in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                check_layers(dict(result, layers=layers), self.home)
        (self.etc / "hooks.json").write_text('{"hooks": {}}')
        with self.assertRaisesRegex(ValueError, "hooks"):
            check_layers(result, self.home)
        (self.etc / "hooks.json").unlink()
        (self.etc / "hooks.json").symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "hooks"):
            check_layers(result, self.home)
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "hooks.json").write_text("{}")
        layer = {"name": {"type": "legacyManagedConfigTomlFromFile", "file": str(legacy / "managed_config.toml")},
                 "config": {}}
        with self.assertRaisesRegex(ValueError, "hooks"):
            check_layers(dict(result, layers=[user, layer]), self.home)

    def test_fixture_effective_config_passes_with_chatgpt_login(self):
        config = self.layers()["config"]
        self.assertEqual(config["forced_login_method"], "chatgpt")
        self.assertEqual(config["default_permissions"], PERMISSION_PROFILE)
        validate_effective(config, self.home, backend="seatbelt")

    def test_effective_config_refuses_code_running_and_redirecting_settings(self):
        validate_effective(self.config, self.home, backend="seatbelt")
        for key, value in (("forced_login_method", "api"), ("forced_login_method", None),
                           ("sandbox_mode", "workspace-write"),
                           ("default_permissions", "broad"),
                           ("hooks", {"PreToolUse": []}), ("notify", ["sh"]), ("model_provider", "proxy"),
                           ("model_providers", {"proxy": {}}), ("openai_base_url", "https://x.invalid"),
                           ("otel", {"exporter": "otlp"}), ("profile", "other")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_effective(dict(self.config, **{key: value}), self.home, backend="seatbelt")
        # config/read reports these explicitly as null/empty when unset.
        validate_effective(dict(self.config, hooks=None, notify=None, model_provider=None,
                                model_providers={}, openai_base_url=None, otel=None, profile=None),
                           self.home, backend="seatbelt")
        missing = dict(self.config)
        del missing["forced_login_method"]
        with self.assertRaisesRegex(ValueError, "ChatGPT"):
            validate_effective(missing, self.home, backend="seatbelt")

    def test_native_profile_requires_workspace_boundary_ssh_deny_and_no_network(self):
        cases = []
        for key, value in (("~/.ssh", "read"), ("/tmp", "write"), (":slash_tmp", "write")):
            changed = copy.deepcopy(self.config)
            changed["permissions"][PERMISSION_PROFILE]["filesystem"][key] = value
            cases.append((key, changed))
        changed = copy.deepcopy(self.config)
        changed["permissions"][PERMISSION_PROFILE]["network"]["enabled"] = True
        cases.append(("network", changed))
        changed = copy.deepcopy(self.config)
        changed["permissions"][PERMISSION_PROFILE]["extends"] = ":danger-full-access"
        cases.append(("extends", changed))
        changed = copy.deepcopy(self.config)
        changed["permissions"]["extra"] = {"extends": ":workspace"}
        cases.append(("extra profile", changed))
        changed = copy.deepcopy(self.config)
        changed["permissions"][PERMISSION_PROFILE]["tools"] = {"shell": "unrestricted"}
        cases.append(("extra profile capability", changed))
        for label, config in cases:
            with self.subTest(label=label), self.assertRaises(ValueError):
                validate_effective(config, self.home, backend="seatbelt")

    def test_launch_requires_shared_chatgpt_auth_before_claiming(self):
        for label, value in (("missing", None), ("malformed", "{fake-secret"),
                             ("api", '{"auth_mode":"api","tokens":{"access_token":"fake-secret"}}'),
                             ("empty tokens", '{"auth_mode":"chatgpt","tokens":{}}'),
                             ("wrong tokens type", '{"auth_mode":"chatgpt","tokens":["fake-secret"]}'),
                             ("not an object", "[]"), ("no tokens", '{"auth_mode":"chatgpt"}')):
            self.auth.unlink(missing_ok=True)
            if value is not None:
                self.auth.write_text(value)
            with self.subTest(label=label), \
                    patch("scripts.gc_lifecycle.gc") as gc, \
                    patch("scripts.codex_mutator_launch.subprocess.run") as run, \
                    self.assertRaisesRegex(ValueError, "run codex login with the shared account") as caught:
                launch(self.home, PACK)
            self.assertNotIn("fake-secret", str(caught.exception))
            gc.assert_not_called()
            run.assert_not_called()
        self.auth.unlink()
        self.auth.mkdir()
        with patch("scripts.gc_lifecycle.gc") as gc, self.assertRaisesRegex(ValueError, "codex login"):
            launch(self.home, PACK)
        gc.assert_not_called()

    def test_launch_without_api_key_symlinks_resolved_shared_login(self):
        alias = self.root / "shared-alias"
        alias.symlink_to(self.shared, target_is_directory=True)
        private_auth = self.home / ".funsearch-codex-home/auth.json"
        for stale in ("absent", "file", "symlink"):
            private_auth.unlink(missing_ok=True)
            if stale == "file":
                private_auth.write_text("stale")
            elif stale == "symlink":
                private_auth.symlink_to(self.root / "missing-auth")
            with self.subTest(stale=stale), patch.dict(os.environ, CODEX_HOME=str(alias)):
                self.launch_with(json.dumps({"action": "work", "bead_id": "fs-slot.1"}),
                                 self.slot(**{"fs.provider": "codex"}))
            self.execute.assert_called_once()
            self.assertTrue(private_auth.is_symlink())
            self.assertEqual(private_auth.readlink(), self.auth.resolve())
            env = self.execute.call_args.args[2]
            self.assertEqual({key for key in env if key.startswith(("CODEX_", "OPENAI_"))}, {"CODEX_HOME"})
            self.assertEqual(env["CODEX_HOME"], str(private_auth.parent))

    def test_shared_login_defaults_to_user_home_when_codex_home_unset_or_empty(self):
        from scripts.codex_mutator_launch import shared_auth_file
        user_home = self.root / "user"
        user_home.mkdir()
        (user_home / ".codex").symlink_to(self.shared, target_is_directory=True)
        for value in (None, ""):
            with self.subTest(value=value), patch.dict(os.environ), patch("pathlib.Path.home", return_value=user_home):
                if value is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = value
                self.assertEqual(shared_auth_file(), self.auth.resolve())

    def launch_with(self, *responses, effective=None, exec_error=None):
        self.addCleanup(os.chdir, os.getcwd())
        with patch("scripts.codex_mutator_launch.read_effective",
                   side_effect=effective or (lambda home, env: self.config)), \
                patch("scripts.codex_mutator_launch.sandbox_backend", return_value="seatbelt"), \
                patch("scripts.codex_mutator_launch.subprocess.run",
                      return_value=subprocess.CompletedProcess([], 0, "codex-cli 0.164.0\n")), \
                patch("scripts.gc_lifecycle.gc", side_effect=list(responses)) as gc, \
                patch("scripts.codex_mutator_launch.os.execvpe", side_effect=exec_error) as execute:
            try:
                return launch(self.home, PACK)
            finally:
                self.gc_calls = [call.args for call in gc.call_args_list]
                self.execute = execute

    def slot(self, **metadata):
        fields = {"fs.provider": "claude", "gc.session_id": "session-1", "gc.routed_to": "rig/funsearch.mutator-codex",
                  "fs.run_dir": str(self.root / "run"), "fs.slot": "1", "fs.tasks_per_session": "2"}
        fields.update(metadata)
        return json.dumps({"id": "fs-slot.1", "status": "in_progress", "assignee": "session-1", "metadata": fields})

    def test_refusal_after_claim_releases_through_slot_helper(self):
        claim = json.dumps({"action": "work", "bead_id": "fs-slot.1"})
        with self.assertRaisesRegex(ValueError, "non-Codex slot; slot released"):
            self.launch_with(claim, self.slot(), self.slot(), "")
        self.execute.assert_not_called()
        update = self.gc_calls[-1]
        self.assertEqual(update[:4], ("bd", "update", "fs-slot.1", "--status=open"))
        self.assertIn("--if-status=in_progress", update)
        # slot_command ran in the slot workspace (chdir before the claim).
        retired = json.loads((self.home / ".funsearch-retired.json").read_text())
        self.assertEqual(retired, {"session": "session-1", "bead": "fs-slot.1"})

    def test_failed_release_reports_both_errors_and_claim(self):
        claim = json.dumps({"action": "work", "bead_id": "fs-slot.1"})
        malformed = self.slot(**{"gc.routed_to": ""})
        with self.assertRaises(ValueError) as caught:
            self.launch_with(claim, self.slot(), malformed)
        message = str(caught.exception)
        self.assertIn("refused after claiming fs-slot.1: Codex mutator claimed a non-Codex slot", message)
        self.assertIn("release FAILED: slot has no routing metadata", message)
        self.assertIn("slot still claimed by session session-1", message)
        self.assertNotIn("slot released", message)
        self.assertFalse((self.home / ".funsearch-retired.json").exists())

    def test_exec_failure_releases_claim(self):
        claim = json.dumps({"action": "work", "bead_id": "fs-slot.1"})
        with self.assertRaisesRegex(ValueError, "no codex; slot released"):
            self.launch_with(claim, self.slot(**{"fs.provider": "codex"}), self.slot(), "",
                             exec_error=OSError("no codex"))
        self.execute.assert_called_once()

    def test_malformed_work_claim_says_slot_may_be_claimed(self):
        with self.assertRaisesRegex(ValueError, "may still be claimed by session session-1"):
            self.launch_with(json.dumps({"action": "work"}))
        self.assertEqual(len(self.gc_calls), 1)

    def test_hooks_changed_after_setup_refuses_before_claim(self):
        def tamper(home, env):
            (home / ".funsearch-codex-home/hooks.json").write_text('{"hooks": {}}')
            return self.config
        with self.assertRaisesRegex(ValueError, "hooks.json changed"):
            self.launch_with(effective=tamper)
        self.assertEqual(self.gc_calls, [])

    def test_sandbox_probe_failures_refuse_before_claim(self):
        failures = (
            ("spawn", OSError("sandbox unavailable")),
            ("nonzero exit", subprocess.CalledProcessError(
                1, ["codex", "sandbox"], stderr="sandbox denied")),
            ("timeout", subprocess.TimeoutExpired(["codex", "sandbox"], timeout=15)),
        )
        version = subprocess.CompletedProcess(["codex", "--version"], 0, "codex-cli 0.164.0\n")
        for label, failure in failures:
            with self.subTest(label=label), \
                    patch("scripts.codex_mutator_launch.sandbox_backend", return_value="seatbelt"), \
                    patch("scripts.codex_mutator_launch.subprocess.run",
                          side_effect=[version, failure]) as run, \
                    patch("scripts.codex_mutator_launch.read_effective", return_value=self.config), \
                    patch("scripts.gc_lifecycle.gc") as gc, \
                    patch("scripts.codex_mutator_launch.os.chdir") as chdir, \
                    self.assertRaisesRegex(ValueError, "seatbelt sandbox preflight failed"):
                launch(self.home, PACK)
            self.assertEqual(run.call_count, 2)
            gc.assert_not_called()
            chdir.assert_not_called()

    def test_drain_needs_no_release(self):
        self.assertEqual(self.launch_with(json.dumps({"action": "drain"})), 0)
        self.assertEqual(self.gc_calls, [("hook", "--claim", "--drain-ack", "--json")])
        self.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
