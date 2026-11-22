#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's interaction-limit policy: its registry resolution, apply write, pre-flight, and check.

The shell is lifted out of the file rather than restated here, so an edit that drops a guard fails these tests.
Only `gh`, the boundary the functions call out to, is stubbed, and every case runs offline.
No case reaches a live repository, since the stub answers every call the functions make and refuses any other.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from host_capability import bash_or_skip

ROOT = Path(__file__).resolve().parents[1]
CONFIGURE = ROOT / "repo-config" / "configure.sh"
TEXT = CONFIGURE.read_text(encoding="utf-8")


def lift(pattern: str) -> str:
    """The one region of configure.sh matching `pattern`, or a failure naming what was missing."""
    found = re.findall(pattern, TEXT, re.MULTILINE | re.DOTALL)
    if len(found) != 1:
        raise AssertionError(
            f"expected one match in {CONFIGURE.name} for {pattern!r}, found {len(found)}"
        )
    return found[0]


def function(name: str) -> str:
    """A whole top-level function, from its opening line to the closing brace in column one."""
    return lift(rf"^({re.escape(name)}\(\) \{{[^\n]*\n.*?\n\}}\n)")


OPTIONS = lift(r"^(set -[A-Za-z]+ [a-z]+)$")
JQR = lift(r"^(jqr\(\) \{ jq -r .*?\}\n)")
HELPERS = "".join(
    lift(pattern)
    for pattern in (
        r"^(note\(\) \{.*?\}\n)",
        r"^(fail\(\) \{\n.*?\n\}\n)",
        r"^(pass\(\) \{.*?\}\n)",
        r"^(warn\(\) \{.*?\}\n)",
        r"^(assert\(\) \{\n.*?\n\}\n)",
        r"^(jqr\(\) \{ jq -r .*?\}\n)",
        r"^(jq_has\(\) \{ jq -e .*?\}\n)",
    )
)
RESOLVE = lift(
    r"^(# ----- Resolve the public interaction policy .*?\ninteraction_warn_days=[0-9]+\n)"
)
PREFLIGHT = lift(
    r"^(    # The visibility and any live interaction limit are read here too\.\n.*?\n        fi\n    fi\n)"
)
REPO = "ptr727/Fixture"


def expiry(days: float) -> str:
    """An expires_at stamp this many days from now, in the shape GitHub returns."""
    stamp = datetime.now(UTC) + timedelta(days=days)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def gh_stub(private: str, limit: str | None, capture: Path) -> str:
    """A gh function answering the reads the functions make, recording a PUT, and refusing anything else.

    A None limit stands for GitHub's error on a private repository's interaction-limits read.
    """
    limit_arm = (
        'echo "Interaction limits are not available for private repositories." >&2; return 1'
        if limit is None
        else f"printf '%s' {shlex.quote(limit)}"
    )
    return (
        "gh() {\n"
        '    case "$*" in\n'
        f'    "api repos/{REPO} --jq .private") echo {shlex.quote(private)} ;;\n'
        f'    "api repos/{REPO}/interaction-limits") {limit_arm} ;;\n'
        f'    "api --method PUT repos/{REPO}/interaction-limits --input -") cat >{shlex.quote(str(capture))}; echo "{{}}" ;;\n'
        '    *) echo "unexpected gh call: $*" >&2; return 1 ;;\n'
        "    esac\n"
        "}\n"
    )


class Harness(unittest.TestCase):
    """Runs a lifted region under the script's own shell options."""

    def setUp(self) -> None:
        self.bash = bash_or_skip()
        if shutil.which("jq") is None:
            raise unittest.SkipTest("no jq on PATH")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.capture = self.tmp / "put.json"

    def run_script(self, body: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.bash, "-c", f"{OPTIONS}\n{body}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    def resolved(self, interaction: str) -> str:
        """The variables the resolution block derives, set as it would set them."""
        limit = "existing_users" if interaction == "community" else "collaborators_only"
        discussions = "true" if interaction == "community" else "false"
        return (
            f"interaction={interaction}\ninteraction_limit={limit}\n"
            f"public_discussions={discussions}\ninteraction_warn_days=30\n"
        )


class ResolveCase(Harness):
    """The registry field resolves to a limit and a Discussions setting, and a bad value stops the run."""

    def resolve(self, entry: dict[str, object] | None) -> subprocess.CompletedProcess[str]:
        registry = self.tmp / "repos.json"
        if entry is not None:
            registry.write_text(json.dumps({"repos": [entry]}), encoding="utf-8")
        script = (
            f"registry={shlex.quote(str(registry))}\nname=Fixture\n"
            f"{JQR}{RESOLVE}"
            'echo "resolved $interaction $interaction_limit $public_discussions"\n'
        )
        return self.run_script(script)

    def test_an_absent_field_is_collaborators(self) -> None:
        result = self.resolve({"name": "Fixture"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resolved collaborators collaborators_only false", result.stdout)

    def test_a_missing_registry_is_collaborators(self) -> None:
        result = self.resolve(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resolved collaborators collaborators_only false", result.stdout)

    def test_community_keeps_discussions_and_limits_new_accounts(self) -> None:
        result = self.resolve({"name": "Fixture", "publicInteraction": "community"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resolved community existing_users true", result.stdout)

    def test_an_unknown_value_stops_the_run(self) -> None:
        result = self.resolve({"name": "Fixture", "publicInteraction": "everyone"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unknown publicInteraction 'everyone'", result.stderr)
        self.assertNotIn("resolved", result.stdout)


class ApplyCase(Harness):
    """The write sets the declared limit for six months on a public repo and skips a private one."""

    def apply(
        self, private: str, interaction: str = "collaborators"
    ) -> subprocess.CompletedProcess[str]:
        script = (
            f"repo={REPO}\n{self.resolved(interaction)}{gh_stub(private, '{}', self.capture)}"
            f"{function('apply_interaction')}apply_interaction {private}\n"
        )
        return self.run_script(script)

    def test_a_public_collaborators_repo_gets_collaborators_only(self) -> None:
        result = self.apply("false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.capture.read_text(encoding="utf-8")),
            {"limit": "collaborators_only", "expiry": "six_months"},
        )
        self.assertIn("Set the interaction limit to collaborators_only", result.stdout)

    def test_a_public_community_repo_gets_existing_users(self) -> None:
        result = self.apply("false", "community")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.capture.read_text(encoding="utf-8")),
            {"limit": "existing_users", "expiry": "six_months"},
        )

    def test_a_private_repo_is_skipped_with_no_write(self) -> None:
        result = self.apply("true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertIn("is private", result.stdout)


class PreflightCase(Harness):
    """The pre-flight reads the visibility and the live limit's origin before any write."""

    def preflight(self, private: str, limit: str | None) -> subprocess.CompletedProcess[str]:
        script = (
            f"repo={REPO}\n{HELPERS}{gh_stub(private, limit, self.capture)}"
            f"{function('interaction_origin')}"
            f"cmd_apply() {{\n    local private origin\n{PREFLIGHT}    echo reached-the-writes\n}}\ncmd_apply\n"
        )
        return self.run_script(script)

    def test_no_live_limit_reaches_the_writes(self) -> None:
        result = self.preflight("false", "{}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reached-the-writes", result.stdout)

    def test_a_repository_limit_reaches_the_writes(self) -> None:
        live = {"limit": "collaborators_only", "origin": "repository", "expires_at": expiry(90)}
        result = self.preflight("false", json.dumps(live))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reached-the-writes", result.stdout)

    def test_an_account_limit_stops_before_any_write(self) -> None:
        live = {"limit": "collaborators_only", "origin": "user", "expires_at": expiry(90)}
        result = self.preflight("false", json.dumps(live))
        self.assertEqual(result.returncode, 1)
        self.assertIn("set on its user", result.stderr)
        self.assertNotIn("reached-the-writes", result.stdout)

    def test_an_unreadable_limit_stops_before_any_write(self) -> None:
        result = self.preflight("false", "<html>proxy</html>")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not read the interaction limit", result.stderr)
        self.assertNotIn("reached-the-writes", result.stdout)

    def test_a_private_repo_never_reads_the_limit(self) -> None:
        """GitHub answers a private repo's read with an error, so reading it would stop every private apply."""
        result = self.preflight("true", None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reached-the-writes", result.stdout)


class CheckCase(Harness):
    """The check fails on a missing, wrong, or account-wide limit, and warns inside the threshold."""

    def check(
        self, private: str, limit: str | None, interaction: str = "collaborators"
    ) -> subprocess.CompletedProcess[str]:
        script = (
            f"repo={REPO}\nFAILED=0\n{self.resolved(interaction)}{HELPERS}"
            f"{gh_stub(private, limit, self.capture)}{function('check_interaction')}"
            'check_interaction\necho "FAILED=$FAILED"\n'
        )
        return self.run_script(script)

    def live(
        self, limit: str = "collaborators_only", origin: str = "repository", days: float = 90
    ) -> str:
        return json.dumps({"limit": limit, "origin": origin, "expires_at": expiry(days)})

    def test_a_matching_limit_with_time_left_passes(self) -> None:
        result = self.check("false", self.live())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok   interaction limit = collaborators_only", result.stdout)
        self.assertIn("ok   interaction limit has 89 day(s) left", result.stdout)
        self.assertIn("FAILED=0", result.stdout)

    def test_a_limit_inside_the_threshold_warns_without_failing(self) -> None:
        result = self.check("false", self.live(days=10.5))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARN interaction limit expires in 10 day(s)", result.stdout)
        self.assertNotIn("  FAIL ", result.stdout)
        self.assertIn("FAILED=0", result.stdout)

    def test_fractional_seconds_in_the_expiry_still_parse(self) -> None:
        stamp = expiry(90).replace("Z", ".123Z")
        live = json.dumps(
            {"limit": "collaborators_only", "origin": "repository", "expires_at": stamp}
        )
        result = self.check("false", live)
        self.assertIn("ok   interaction limit has 89 day(s) left", result.stdout)

    def test_no_limit_fails_and_names_apply(self) -> None:
        result = self.check("false", "{}")
        self.assertIn("FAIL interaction limit = collaborators_only", result.stdout)
        self.assertIn("live none", result.stdout)
        self.assertIn("FAILED=1", result.stdout)

    def test_the_other_groups_limit_fails(self) -> None:
        result = self.check("false", self.live(limit="existing_users"))
        self.assertIn("FAIL interaction limit = collaborators_only", result.stdout)
        self.assertIn("FAILED=1", result.stdout)

    def test_a_community_repo_wants_existing_users(self) -> None:
        result = self.check("false", self.live(limit="existing_users"), "community")
        self.assertIn("ok   interaction limit = existing_users", result.stdout)
        self.assertIn("FAILED=0", result.stdout)

    def test_an_account_wide_limit_fails(self) -> None:
        result = self.check("false", self.live(origin="user"))
        self.assertIn(
            "FAIL interaction limit = collaborators_only, set on the repository (origin user)",
            result.stdout,
        )
        self.assertIn("FAILED=1", result.stdout)

    def test_an_unreadable_expiry_fails(self) -> None:
        live = json.dumps(
            {"limit": "collaborators_only", "origin": "repository", "expires_at": None}
        )
        result = self.check("false", live)
        self.assertIn("FAIL interaction limit expiry 'none' could not be read", result.stdout)
        self.assertIn("FAILED=1", result.stdout)

    def test_a_non_object_body_fails_and_the_run_continues(self) -> None:
        result = self.check("false", "[]")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "FAIL live interaction limit for 'ptr727/Fixture' was not one JSON object",
            result.stdout,
        )
        self.assertIn("FAILED=1", result.stdout)

    def test_a_private_repo_is_a_note_and_never_reads_the_limit(self) -> None:
        result = self.check("true", None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is private, so none is owed", result.stdout)
        self.assertIn("FAILED=0", result.stdout)


class DiscussionsCase(Harness):
    """check_settings wants Discussions on only for a public community repo."""

    def check(
        self, private: str, discussions: bool, interaction: str
    ) -> subprocess.CompletedProcess[str]:
        settings = self.tmp / "settings.json"
        settings.write_text(json.dumps({"has_wiki": False}), encoding="utf-8")
        live = json.dumps(
            {"has_wiki": False, "private": private == "true", "has_discussions": discussions}
        )
        script = (
            f"repo={REPO}\nname=Fixture\nFAILED=0\ndescription=''\nregistry=/nonexistent\n"
            f"settings_file={shlex.quote(str(settings))}\n{self.resolved(interaction)}{HELPERS}"
            f'gh() {{ if [ "$2" = repos/{REPO} ]; then printf "%s" {shlex.quote(live)}; else return 1; fi; }}\n'
            f"{function('check_settings')}check_settings\n"
        )
        return self.run_script(script)

    def test_a_public_collaborators_repo_wants_discussions_off(self) -> None:
        self.assertIn(
            "ok   has_discussions = false", self.check("false", False, "collaborators").stdout
        )
        self.assertIn(
            "FAIL has_discussions = false", self.check("false", True, "collaborators").stdout
        )

    def test_a_public_community_repo_wants_discussions_on(self) -> None:
        self.assertIn("ok   has_discussions = true", self.check("false", True, "community").stdout)

    def test_a_private_repo_wants_discussions_off_whatever_the_policy(self) -> None:
        self.assertIn("ok   has_discussions = false", self.check("true", False, "community").stdout)


class AuditDiscussionsCase(unittest.TestCase):
    """spec/audit.py expects the Discussions setting configure.sh applies, so the two never disagree."""

    def findings(self, private: bool, discussions: bool, entry: dict[str, str]) -> list[str]:
        sys.path.insert(0, str(ROOT / "spec"))
        self.addCleanup(sys.path.remove, str(ROOT / "spec"))
        import audit  # spec/ joins the path only for this case

        run = SimpleNamespace(
            spec={"settings": {}},
            live={"private": private, "has_discussions": discussions},
            entry=entry,
            main=None,
        )
        return [text for _, text in audit.check_settings_general(run)]

    def test_a_public_collaborators_repo_expects_discussions_off(self) -> None:
        self.assertEqual(self.findings(False, False, {"name": "Fixture"}), [])
        self.assertTrue(self.findings(False, True, {"name": "Fixture"}))

    def test_a_public_community_repo_expects_discussions_on(self) -> None:
        entry = {"name": "Fixture", "publicInteraction": "community"}
        self.assertEqual(self.findings(False, True, entry), [])
        self.assertTrue(self.findings(False, False, entry))

    def test_a_private_repo_expects_discussions_off(self) -> None:
        entry = {"name": "Fixture", "publicInteraction": "community"}
        self.assertEqual(self.findings(True, False, entry), [])


class WiringCase(unittest.TestCase):
    """The group runs only because cmd_apply and cmd_check name it, in the order that keeps the pre-flight first."""

    def test_apply_reads_before_its_first_write_and_writes_with_the_settings(self) -> None:
        body = function("cmd_apply")
        self.assertLess(body.index("interaction_origin"), body.index("General repository settings"))
        self.assertLess(body.index("--method PATCH"), body.index('apply_interaction "$private"'))
        self.assertLess(
            body.index('apply_interaction "$private"'), body.index("vulnerability-alerts")
        )

    def test_check_runs_the_interaction_group(self) -> None:
        body = function("cmd_check")
        self.assertIn("\n    check_interaction\n", body)


if __name__ == "__main__":
    unittest.main()
