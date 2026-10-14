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
from scripts.codex_mutator_setup import ACK, setup

PACK = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures/codex-0.160-config-read.json"


class CodexLaunchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="codex launch ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "slot"
        self.etc = self.root / "etc-codex"
        self.etc.mkdir()
        policy = patch.dict(os.environ, FS_CODEX_SANDBOX="workspace-write", FS_CODEX_POLICY_ACK=ACK,
                            FS_CODEX_LOCK_DIR=str(self.root / "locks"), GC_SESSION_ID="session-1",
                            OPENAI_API_KEY="sk-test-key")
        policy.start()
        self.addCleanup(policy.stop)
        os.environ.pop("CODEX_API_KEY", None)
        setup(self.home, PACK)
        self.config = tomllib.loads((self.home / ".funsearch-codex-home/config.toml").read_text())

    def layers(self):
        # A real 0.160 config/read response for a pack-generated private home.
        text = FIXTURE.read_text().replace("@HOME@", str(self.home))
        text = text.replace('"/etc/codex/config.toml"', json.dumps(str(self.etc / "config.toml")))
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

    def test_effective_config_refuses_code_running_and_redirecting_settings(self):
        validate_effective(self.config, self.home)
        for key, value in (("forced_login_method", "chatgpt"), ("forced_login_method", None),
                           ("hooks", {"PreToolUse": []}), ("notify", ["sh"]), ("model_provider", "proxy"),
                           ("model_providers", {"proxy": {}}), ("openai_base_url", "https://x.invalid"),
                           ("otel", {"exporter": "otlp"}), ("profile", "other")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_effective(dict(self.config, **{key: value}), self.home)
        # config/read reports these explicitly as null/empty when unset.
        validate_effective(dict(self.config, hooks=None, notify=None, model_provider=None,
                                model_providers={}, openai_base_url=None, otel=None, profile=None), self.home)

    def test_launch_requires_api_key_before_claiming(self):
        for value in (None, "", "  "):
            env = {"OPENAI_API_KEY": value} if value is not None else {}
            with self.subTest(value=value), patch.dict(os.environ, env), \
                    patch("scripts.gc_lifecycle.gc") as gc, \
                    patch("scripts.codex_mutator_launch.subprocess.run") as run, \
                    self.assertRaisesRegex(ValueError, "secrets.env"):
                if value is None:
                    os.environ.pop("OPENAI_API_KEY")
                launch(self.home, PACK)
            gc.assert_not_called()
            run.assert_not_called()

    def launch_with(self, *responses):
        self.addCleanup(os.chdir, os.getcwd())
        with patch("scripts.codex_mutator_launch.read_effective", return_value=self.config), \
                patch("scripts.codex_mutator_launch.subprocess.run",
                      return_value=subprocess.CompletedProcess([], 0, "codex-cli 0.160.0\n")), \
                patch("scripts.gc_lifecycle.gc", side_effect=list(responses)) as gc, \
                patch("scripts.codex_mutator_launch.os.execvpe") as execute:
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

    def test_drain_needs_no_release(self):
        self.assertEqual(self.launch_with(json.dumps({"action": "drain"})), 0)
        self.assertEqual(self.gc_calls, [("hook", "--claim", "--drain-ack", "--json")])
        self.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
