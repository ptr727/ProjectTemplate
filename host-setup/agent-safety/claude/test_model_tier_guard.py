#!/usr/bin/env python3
"""Self-test for model-tier-guard.py, running the hook as Claude Code does, over stdin.

Each case copies the hook beside a synthetic model-tiers.json in a throwaway directory. The hub's
real mapping is never read, so a model release does not break the suite.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
HOOK = HERE / "model-tier-guard.py"

TIERS = {
    "maintainerOnly": "highest",
    "defaultTier": "frontier",
    "tools": {
        "claude-code": {
            "models": {"highest": "fable", "frontier": "opus", "standard": "sonnet"},
        }
    },
}


class TestModelTierGuard(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        shutil.copyfile(HOOK, self.dir / "model-tier-guard.py")
        self.write_config(TIERS)

    def write_config(self, cfg):
        (self.dir / "model-tiers.json").write_text(json.dumps(cfg), encoding="utf-8")

    def run_hook(self, tool, tool_input, allow=None, cwd=None):
        env = {k: v for k, v in os.environ.items() if k != "FLEET_ALLOW_HIGHEST"}
        if allow is not None:
            env["FLEET_ALLOW_HIGHEST"] = allow
        done = subprocess.run(
            [sys.executable, str(self.dir / "model-tier-guard.py")],
            input=json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": cwd}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout) if done.stdout.strip() else None

    def assertDenied(self, out, *fragments):
        self.assertIsNotNone(out, "the call was allowed")
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["permissionDecision"], "deny")
        for fragment in fragments:
            self.assertIn(fragment, spec["permissionDecisionReason"])

    def test_the_selftest_passes(self):
        done = subprocess.run(
            [sys.executable, str(HOOK), "--selftest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("SELFTEST PASS", done.stdout)

    def test_an_agent_at_the_blocked_model_is_denied(self):
        out = self.run_hook("Agent", {"model": "fable", "prompt": "x"})
        self.assertDenied(out, "maintainer-only", "`opus`")

    def test_the_comparison_ignores_case(self):
        self.assertDenied(self.run_hook("Agent", {"model": "FABLE"}))

    def test_other_models_and_no_model_are_allowed(self):
        for tool_input in ({"model": "opus"}, {"model": "sonnet"}, {"prompt": "x"}):
            with self.subTest(tool_input=tool_input):
                self.assertIsNone(self.run_hook("Agent", tool_input))

    def test_the_launch_allowance_lets_the_blocked_model_through(self):
        self.assertIsNone(self.run_hook("Agent", {"model": "fable"}, allow="1"))

    def test_only_the_exact_value_one_is_an_allowance(self):
        for value in ("true", "yes", "0", ""):
            with self.subTest(value=value):
                self.assertDenied(self.run_hook("Agent", {"model": "fable"}, allow=value))

    def test_the_deny_text_never_tells_the_agent_to_set_the_allowance(self):
        out = self.run_hook("Agent", {"model": "fable"})
        self.assertNotIn(
            "FLEET_ALLOW_HIGHEST", out["hookSpecificOutput"]["permissionDecisionReason"]
        )

    def test_a_workflow_script_naming_the_blocked_model_is_denied(self):
        for script in (
            "agent('x', { model: 'fable' })",
            'agent("x", {model:"fable"})',
            "agent(`x`, { model : `fable` })",
            'agent("x", {"model": "fable"})',
            "agent('x', {'model': 'fable'})",
            "agent('x', { model: 'claude-fable-5-1' })",
        ):
            with self.subTest(script=script):
                self.assertDenied(self.run_hook("Workflow", {"script": script}))

    def test_a_workflow_script_file_is_read(self):
        path = self.dir / "flow.js"
        path.write_text("agent('x', { model: 'fable' })", encoding="utf-8")
        self.assertDenied(self.run_hook("Workflow", {"scriptPath": str(path)}))

    def test_a_relative_script_path_resolves_against_the_session_cwd(self):
        (self.dir / "flow.js").write_text("agent('x', { model: 'fable' })", encoding="utf-8")
        self.assertDenied(self.run_hook("Workflow", {"scriptPath": "flow.js"}, cwd=str(self.dir)))

    def test_an_unreadable_script_path_is_denied(self):
        missing = str(self.dir / "absent.js")
        self.assertDenied(self.run_hook("Workflow", {"scriptPath": missing}))

    def test_a_script_file_is_read_even_beside_an_inline_script(self):
        path = self.dir / "flow.js"
        path.write_text("agent('x', { model: 'fable' })", encoding="utf-8")
        tool_input = {"script": "agent('x', { model: 'opus' })", "scriptPath": str(path)}
        self.assertDenied(self.run_hook("Workflow", tool_input))

    def test_a_workflow_at_another_model_or_saved_by_name_is_allowed(self):
        self.assertIsNone(self.run_hook("Workflow", {"script": "agent('x', { model: 'opus' })"}))
        self.assertIsNone(self.run_hook("Workflow", {"name": "saved-flow"}))

    def test_another_tool_is_allowed(self):
        self.assertIsNone(
            self.run_hook("Bash", {"command": "echo model: 'fable'", "model": "fable"})
        )

    def test_a_missing_config_denies_a_named_model_and_allows_none(self):
        (self.dir / "model-tiers.json").unlink()
        self.assertDenied(self.run_hook("Agent", {"model": "opus"}), "installer")
        self.assertIsNone(self.run_hook("Agent", {"prompt": "x"}))

    def test_a_config_lacking_the_keys_is_treated_as_missing(self):
        self.write_config({"tools": {}})
        self.assertDenied(self.run_hook("Agent", {"model": "opus"}), "installer")
        self.assertIsNone(self.run_hook("Agent", {}))

    def test_unparseable_input_is_allowed(self):
        done = subprocess.run(
            [sys.executable, str(self.dir / "model-tier-guard.py")],
            input="not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        self.assertEqual((done.returncode, done.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main(verbosity=1)
