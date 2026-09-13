#!/usr/bin/env python3
"""Exercise two of repo-config/configure.sh's environment checks by running the script's own lines.

The shell is lifted out of the file rather than restated here, so an edit that removes the
behavior fails these tests instead of leaving a reimplementation to agree with itself. Each
harness lifts the whole region under test, consumer included, since lifting a value's producer
alone would leave a test that passes while the consumer ignores what it produced.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import unittest
from pathlib import Path

CONFIGURE = Path(__file__).resolve().parents[2] / "repo-config" / "configure.sh"


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


REPORTERS = r"(FAILED=0\nnote\(\).*?\nfail\(\) \{.*?\n\})"


class AbsentRegistryEntryCase(unittest.TestCase):
    """A repo the registry does not name declares nothing to check, which is a state rather than a defect."""

    def harness(self, entry_count: str) -> subprocess.CompletedProcess[str]:
        branch = lift(r'(    if \[ "\$entry_count" != 1 \]; then\n.*?\n    fi\n)')
        return run_bash(
            f"{lift(REPORTERS)}\ncheck() {{\n  local name=Fixture entry_count={entry_count}\n"
            f'{branch}  return\n}}\ncheck\necho "FAILED=$FAILED"\n'
        )

    def test_no_entry_reports_without_setting_the_failure_flag(self) -> None:
        result = self.harness("0")
        self.assertIn("nothing is declared to check", result.stdout)
        # `fail` prints a two-space "  FAIL " prefix, which the harness's own "FAILED=0" would satisfy.
        self.assertNotIn("  FAIL ", result.stdout)
        self.assertIn("FAILED=0", result.stdout)

    def test_one_entry_reports_nothing_and_falls_through(self) -> None:
        """The guard is the count test itself, so the ordinary case must reach the checks below it."""
        result = self.harness("1")
        self.assertEqual(result.stdout, "FAILED=0\n")


class EnvironmentNameInAPathCase(unittest.TestCase):
    """GitHub documents no character restriction on an environment name beyond length and uniqueness."""

    def request_paths(self, name: str, gh_status: int = 0) -> list[str]:
        """Every path `configure.sh` asks `gh` for, with `gh` stubbed to report its argument.

        The loop runs the name under test and then a plain `second`, so an arm that ended the run
        rather than reporting shows up as a missing second path rather than as nothing.

        The encoding line and the call that consumes it are lifted together on purpose: lifting
        the encoder alone would pass against a call that still interpolated the raw name.
        """
        region = lift(
            r'(        ename_uri="\$\(jq -rn --arg s "\$ename" \'\$s\|@uri\'\)"\n'
            r"        if ! policies=.*?\n        fi\n)"
        )
        stub = f"gh() {{\n  printf '%s\\n' \"$3\" >&2\n  return {gh_status}\n}}\n"
        script = (
            f"{lift(REPORTERS)}\n{stub}repo=owner/name\n"
            f"for ename in {shlex.quote(name)} second; do\n{region}done\n"
        )
        result = run_bash(script, "jq")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.stdout = result.stdout
        return result.stderr.strip().splitlines()

    def test_a_name_becomes_exactly_one_path_segment(self) -> None:
        """Each name carries a character that breaks a raw path, and each expectation spells out its escape.

        Asserting the whole segment rather than the absence of the raw character is what catches a
        stray `%`, which is the byte that makes an invalid escape and cannot be tested for by
        absence, since every encoded segment contains one.
        """
        for name, segment in (
            ("prod/west", "prod%2Fwest"),
            ("my env", "my%20env"),
            ("a#b", "a%23b"),
            ("a?b", "a%3Fb"),
            ("100%", "100%25"),
            ("a\\b", "a%5Cb"),
            # Written as escapes rather than literals, since the fleet charset gate classifies every character a file carries.
            ("\u751f\u7523", "%E7%94%9F%E7%94%A3"),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    self.request_paths(name)[0],
                    f"repos/owner/name/environments/{segment}/deployment-branch-policies",
                )

    def test_a_failed_read_reports_the_raw_name_and_does_not_abort_the_loop(self) -> None:
        """The arm reports with the raw name, and a failing call does not end the run.

        `pipefail` is what makes the arm reachable at all, since `jq -s` exits 0 on empty input
        and would otherwise mask the failure. The arm's `continue` is not covered: what it skips
        lies below the region lifted here, and reaching it would mean lifting most of the function.
        """
        paths = self.request_paths("my env", gh_status=1)
        self.assertIn("FAIL environment 'my env' - could not read", self.stdout)
        self.assertEqual(
            paths[-1], "repos/owner/name/environments/second/deployment-branch-policies"
        )

    def test_an_ordinary_name_is_unchanged(self) -> None:
        """Encoding every name would be a behavior change on the names every repo actually declares."""
        for name in ("pypi", "production", "staging", "prod-west", "prod_west", "prod.west"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.request_paths(name)[0],
                    f"repos/owner/name/environments/{name}/deployment-branch-policies",
                )


if __name__ == "__main__":
    unittest.main()
