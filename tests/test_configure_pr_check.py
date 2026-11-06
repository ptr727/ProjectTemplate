#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's pullRequestCheck read by running its own lines.

The region is lifted out of the file rather than reimplemented.
So a change to the jq expression or the refusal is what these cases run.
"""

from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path

from test_configure_archived import lift, run_bash

PR_CHECK_READ = lift(r"(pr_check=true\n.*?\nesac\n)(?=compose_dir=)")


class PullRequestCheckReadCase(unittest.TestCase):
    """The registry's pullRequestCheck decides the fact, an absent field reading as true."""

    def run_region(self, registry_text: str | None, name: str = "Fixture") -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "repos.json"
            if registry_text is not None:
                registry.write_text(registry_text, encoding="utf-8")
            script = (
                f"registry={shlex.quote(str(registry))}\nname={shlex.quote(name)}\n"
                f'{PR_CHECK_READ}echo "$pr_check"\n'
            )
            result = run_bash(script, "jq", "sed")
        return result.returncode, result.stdout, result.stderr

    @staticmethod
    def registry(**fields: object) -> str:
        return json.dumps({"repos": [{"name": "Fixture", **fields}, {"name": "Other"}]})

    def test_an_absent_field_keeps_the_rule(self) -> None:
        code, out, err = self.run_region(self.registry())
        self.assertEqual((code, out), (0, "true\n"), err)

    def test_a_declared_false_drops_the_rule(self) -> None:
        code, out, err = self.run_region(self.registry(pullRequestCheck=False))
        self.assertEqual((code, out), (0, "false\n"), err)

    def test_a_declared_true_keeps_the_rule(self) -> None:
        code, out, err = self.run_region(self.registry(pullRequestCheck=True))
        self.assertEqual((code, out), (0, "true\n"), err)

    def test_another_entrys_false_does_not_leak(self) -> None:
        data = {"repos": [{"name": "Fixture"}, {"name": "Other", "pullRequestCheck": False}]}
        code, out, err = self.run_region(json.dumps(data))
        self.assertEqual((code, out), (0, "true\n"), err)

    def test_a_repo_outside_the_registry_keeps_the_rule(self) -> None:
        code, out, err = self.run_region(self.registry(pullRequestCheck=False), name="Absent")
        self.assertEqual((code, out), (0, "true\n"), err)

    def test_no_registry_file_keeps_the_rule(self) -> None:
        code, out, err = self.run_region(None)
        self.assertEqual((code, out), (0, "true\n"), err)

    def test_a_string_false_is_refused_rather_than_read_as_the_boolean(self) -> None:
        code, out, err = self.run_region(self.registry(pullRequestCheck="false"))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("not true or false", err)

    def test_a_registry_that_will_not_parse_fails_with_the_guards_own_message(self) -> None:
        code, _, err = self.run_region("not json")
        self.assertEqual(code, 1)
        self.assertIn("Failed to read pullRequestCheck", err)


if __name__ == "__main__":
    unittest.main()
