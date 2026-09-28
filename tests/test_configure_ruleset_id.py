#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's ruleset_id() by running its own lines.

The shell is lifted out of the file rather than restated here, so an edit that removes the
behavior fails these tests instead of leaving a reimplementation to agree with itself. Each
harness lifts the whole function under test and stubs only `gh`, the boundary it calls out to.

Every case runs offline: `gh` is a shell function the harness defines, which is what lets a
2xx-but-unreadable response be exercised at all without firing a real call to GitHub.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIGURE = ROOT / "repo-config" / "configure.sh"


def lift(pattern: str) -> str:
    """The one region of configure.sh matching `pattern`, or a failure naming what was missing."""
    text = CONFIGURE.read_text(encoding="utf-8")
    found = re.findall(pattern, text, re.MULTILINE | re.DOTALL)
    if len(found) != 1:
        raise AssertionError(
            f"expected one match in {CONFIGURE.name} for {pattern!r}, found {len(found)}"
        )
    return found[0]


def require(*tools: str) -> str:
    """The bash path, skipping instead of failing where a tool the harness shells out to is absent."""
    for tool in tools:
        if shutil.which(tool) is None:
            raise unittest.SkipTest(f"no {tool} on PATH, so the script's own lines cannot be run")
    return str(shutil.which("bash"))


def run_bash(script: str, *tools: str) -> subprocess.CompletedProcess[str]:
    """The script under the same shell options configure.sh sets, with a bounded wait.

    The options are lifted rather than typed, because a harness running without them is blind to
    exactly the error handling the lines under test rely on.
    """
    bash = require("bash", *tools)
    options = lift(r"^(set -[A-Za-z]+ [a-z]+)$")
    return subprocess.run(
        [bash, "-c", f"{options}\n{script}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


JQR = lift(r"^(jqr\(\) \{ jq -r .*?\}\n)")
JQ_HAS = lift(r"^(jq_has\(\) \{ jq -e .*?\}\n)")
RULESET_ID = lift(r"^(ruleset_id\(\) \{\n.*?\n\}\n)")


def gh_stub(stdout: str, status: int = 0) -> str:
    """A `gh` that answers with one canned document."""
    return f"gh() {{\n  printf '%s' {json.dumps(stdout)}\n  return {status}\n}}\n"


class RulesetIdCase(unittest.TestCase):
    """ruleset_id() maps a name to an id, and aborts rather than reading "not found" on a
    response it cannot trust."""

    def lookup(
        self, response: str, status: int = 0, name: str = "fixture"
    ) -> subprocess.CompletedProcess[str]:
        # Invoked the way both real call sites invoke it, `id="$(ruleset_id ...)"`, rather than as a bare top-level statement.
        # The defect this guards is specific to running inside that command-substitution subshell.
        # `set -e` does not apply there without `shopt -s inherit_errexit`, which this script does not set.
        # So an unguarded failure inside the function falls through to the next line instead of aborting it.
        script = (
            f"repo=ptr727/fixture\n{JQ_HAS}{JQR}{gh_stub(response, status)}{RULESET_ID}"
            f'id="$(ruleset_id {shlex.quote(name)})"\nstatus=$?\nprintf \'%s\\n\' "$id"\nexit "$status"\n'
        )
        return run_bash(script, "jq", "sed", "grep")

    def test_a_matching_name_resolves_to_its_id(self) -> None:
        result = self.lookup(json.dumps([{"name": "fixture", "id": 42}]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "42")

    def test_no_match_returns_empty_without_failing(self) -> None:
        result = self.lookup(json.dumps([{"name": "other", "id": 1}]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_a_non_array_2xx_body_aborts_rather_than_reading_as_not_found(self) -> None:
        """The defect this guards: an unreadable-but-0-status response used to fall through to
        the empty-ids case (returncode 0, no output), reading as "not found" instead of aborting."""
        for label, response in (
            ("an object", json.dumps({"message": "not found"})),
            ("a scalar", "null"),
            ("not JSON at all", "<html>not json</html>"),
            ("truncated JSON", '[{"name": "fixture"'),
        ):
            with self.subTest(label=label):
                result = self.lookup(response)
                self.assertNotEqual(
                    result.returncode,
                    0,
                    f"ruleset_id must abort on {label}, not read it as an empty match "
                    f"(stdout={result.stdout!r})",
                )
                self.assertIn("could not read live ruleset state", result.stderr)

    def test_a_gh_api_failure_still_aborts(self) -> None:
        """The pre-existing guard this fix sits beside: an outright gh failure already aborts."""
        result = self.lookup("", status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Failed to list rulesets", result.stderr)

    def test_the_per_page_cap_still_aborts(self) -> None:
        """The guard this fix sits ahead of: 100 rulesets (a valid array) still trips the cap."""
        result = self.lookup(json.dumps([{"name": f"r{i}", "id": i} for i in range(100)]))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the per_page cap", result.stderr)

    def test_a_duplicate_name_uses_the_first_and_warns(self) -> None:
        result = self.lookup(
            json.dumps(
                [
                    {"name": "fixture", "id": 1},
                    {"name": "fixture", "id": 2},
                ]
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assertIn("Using the first", result.stderr)


if __name__ == "__main__":
    unittest.main()
