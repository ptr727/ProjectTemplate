#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's interaction-limit policy: its registry resolution, verification record, apply write, pre-flight, and check.

The shell is lifted out of the file, through the lift helper test_configure_archived.py already shares, rather than restated here.
An edit that drops a guard therefore fails these tests.
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

from test_configure_archived import lift, run_bash

ROOT = Path(__file__).resolve().parents[1]


def function(name: str) -> str:
    """A whole top-level function, from its opening line to the closing brace in column one."""
    return lift(rf"^({re.escape(name)}\(\) \{{[^\n]*\n.*?\n\}}\n)")


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
RECORD = lift(r"^(# ----- Resolve the bot verification record .*?\n^fi\n)")
PAYLOAD_PROBLEM = function("payload_problem")
REPO = "ptr727/Fixture"
RECORD_PATH = "/fixture/repo-config/interaction-verification.json"
BOTS = ["Copilot", "CodeRabbit", "Qodo", "Dependabot"]


def expiry(days: float) -> str:
    """An expires_at stamp this many days from now, in the shape GitHub returns.

    A case asserting a day count passes a half day.
    The second the stamp drops and the run's own lag then never cross a day boundary.
    """
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
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.capture = self.tmp / "put.json"

    def run_script(self, body: str) -> subprocess.CompletedProcess[str]:
        return run_bash(body, "jq")

    def resolved(self, interaction: str, verified: bool = True) -> str:
        """The variables the resolution blocks derive, set as they would set them."""
        limit = "existing_users" if interaction == "community" else "collaborators_only"
        discussions = "true" if interaction == "community" else "false"
        return (
            f"interaction={interaction}\ninteraction_limit={limit}\n"
            f"public_discussions={discussions}\ninteraction_warn_days=30\n"
            f"py_cmd=({shlex.quote(Path(sys.executable).as_posix())})\n"
            f"interaction_record={RECORD_PATH}\ninteraction_verified={str(verified).lower()}\n"
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

    def test_a_declared_null_stops_the_run_rather_than_defaulting(self) -> None:
        result = self.resolve({"name": "Fixture", "publicInteraction": None})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unknown publicInteraction 'null'", result.stderr)

    def test_no_matching_entry_is_collaborators(self) -> None:
        result = self.resolve({"name": "Other", "publicInteraction": "community"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resolved collaborators collaborators_only false", result.stdout)


class RecordCase(Harness):
    """The committed verification record decides whether a limit is owed, and a malformed one stops the run."""

    def record(self, content: str | None) -> subprocess.CompletedProcess[str]:
        if content is not None:
            (self.tmp / "interaction-verification.json").write_text(content, encoding="utf-8")
        script = (
            f"script_dir={shlex.quote(self.tmp.as_posix())}\n"
            f"py_cmd=({shlex.quote(Path(sys.executable).as_posix())})\n"
            f'{PAYLOAD_PROBLEM}{RECORD}echo "verified=$interaction_verified"\n'
        )
        return self.run_script(script)

    def entry(self, **fields: object) -> str:
        record: dict[str, object] = {"pilot": REPO, "date": "2026-12-01", "bots": BOTS}
        record.update(fields)
        return json.dumps(record)

    def refused(self, content: str, why: str) -> None:
        result = self.record(content)
        self.assertEqual(result.returncode, 1)
        self.assertIn(why, result.stderr)
        self.assertIn("Stopping before any write", result.stderr)
        self.assertNotIn("verified=", result.stdout)

    def test_an_absent_record_is_unverified(self) -> None:
        result = self.record(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified=false", result.stdout)

    def test_a_complete_record_is_verified(self) -> None:
        result = self.record(self.entry())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified=true", result.stdout)

    def test_the_bots_may_be_listed_in_any_order(self) -> None:
        result = self.record(self.entry(bots=list(reversed(BOTS))))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified=true", result.stdout)

    def test_invalid_json_stops_the_run(self) -> None:
        self.refused('{"pilot": ', "did not parse")

    def test_an_empty_file_stops_the_run(self) -> None:
        self.refused("", "does not hold exactly one JSON document")

    def test_two_values_stop_the_run(self) -> None:
        self.refused(self.entry() + self.entry(), "does not hold exactly one JSON document")

    def test_a_non_object_stops_the_run(self) -> None:
        self.refused(json.dumps([self.entry()]), "is not a JSON object")

    def test_a_missing_key_stops_the_run(self) -> None:
        self.refused(json.dumps({"pilot": REPO, "bots": BOTS}), "exactly the keys")

    def test_an_extra_key_stops_the_run(self) -> None:
        self.refused(self.entry(evidence="a pull request"), "exactly the keys")

    def test_a_pilot_or_date_that_is_not_a_non_empty_string_stops_the_run(self) -> None:
        for field, value in (("pilot", ""), ("pilot", 7), ("date", ""), ("date", None)):
            with self.subTest(field=field, value=value):
                self.refused(self.entry(**{field: value}), "as non-empty strings")

    def test_anything_but_the_four_bots_stops_the_run(self) -> None:
        for bots in (BOTS[:3], [*BOTS, "Renovate"], [*BOTS[:3], BOTS[0]], "Copilot", None):
            with self.subTest(bots=bots):
                self.refused(self.entry(bots=bots), "exactly the four bots")


class ApplyCase(Harness):
    """The write sets the declared limit for six months on a verified public repo and skips any other."""

    def apply(
        self, private: str, interaction: str = "collaborators", verified: bool = True
    ) -> subprocess.CompletedProcess[str]:
        script = (
            f"repo={REPO}\n{self.resolved(interaction, verified)}"
            f"{gh_stub(private, '{}', self.capture)}"
            f"{function('apply_interaction')}apply_interaction {private}\n"
        )
        return self.run_script(script)

    def test_an_unverified_public_repo_is_skipped_with_no_write(self) -> None:
        result = self.apply("false", verified=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertIn("no pilot's bot verification is recorded", result.stdout)

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

    def preflight(
        self, private: str, limit: str | None, verified: bool = True
    ) -> subprocess.CompletedProcess[str]:
        script = (
            f"repo={REPO}\ninteraction_verified={str(verified).lower()}\n"
            f"{HELPERS}{gh_stub(private, limit, self.capture)}"
            f"{function('interaction_limits')}{function('interaction_origin')}"
            f"cmd_apply() {{\n    local private origin\n{PREFLIGHT}    echo reached-the-writes\n}}\ncmd_apply\n"
        )
        return self.run_script(script)

    def test_no_live_limit_reaches_the_writes(self) -> None:
        result = self.preflight("false", "{}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reached-the-writes", result.stdout)

    def test_an_empty_body_reaches_the_writes(self) -> None:
        """GitHub answers an unrestricted repository with an empty body, which means no limit."""
        result = self.preflight("false", "")
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

    def test_an_unverified_repo_never_reads_the_limit(self) -> None:
        """Apply then writes no limit, so neither an unreadable limit nor an account one may stop it."""
        for limit in (None, json.dumps({"limit": "collaborators_only", "origin": "user"})):
            with self.subTest(limit=limit):
                result = self.preflight("false", limit, verified=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("reached-the-writes", result.stdout)


class CheckCase(Harness):
    """The check fails on a missing, wrong, or account-wide limit, and warns inside the threshold."""

    def check(
        self,
        private: str,
        limit: str | None,
        interaction: str = "collaborators",
        verified: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        script = (
            f"repo={REPO}\nFAILED=0\n{self.resolved(interaction, verified)}{HELPERS}"
            f"{gh_stub(private, limit, self.capture)}{function('interaction_limits')}{function('check_interaction')}"
            'check_interaction\necho "FAILED=$FAILED"\n'
        )
        return self.run_script(script)

    def live(
        self, limit: str = "collaborators_only", origin: str = "repository", days: float = 90.5
    ) -> str:
        return json.dumps({"limit": limit, "origin": origin, "expires_at": expiry(days)})

    def test_a_matching_limit_with_time_left_passes(self) -> None:
        result = self.check("false", self.live())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok   interaction limit = collaborators_only", result.stdout)
        self.assertIn("ok   interaction limit has 90 day(s) left", result.stdout)
        self.assertIn("FAILED=0", result.stdout)

    def test_a_limit_inside_the_threshold_warns_without_failing(self) -> None:
        result = self.check("false", self.live(days=10.5))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARN interaction limit expires in 10 day(s)", result.stdout)
        self.assertNotIn("  FAIL ", result.stdout)
        self.assertIn("FAILED=0", result.stdout)

    def test_fractional_seconds_in_the_expiry_still_parse(self) -> None:
        stamp = expiry(90.5).replace("Z", ".123Z")
        live = json.dumps(
            {"limit": "collaborators_only", "origin": "repository", "expires_at": stamp}
        )
        result = self.check("false", live)
        self.assertIn("ok   interaction limit has 90 day(s) left", result.stdout)

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

    def test_an_account_wide_limit_fails_and_names_where_to_remove_it(self) -> None:
        result = self.check("false", self.live(origin="user"))
        self.assertIn(
            "FAIL interaction limit collaborators_only is set on the owning user", result.stdout
        )
        self.assertIn("FAILED=1", result.stdout)

    def test_an_account_wide_limit_of_the_other_group_never_names_apply_alone(self) -> None:
        """Apply refuses a non-repository origin, so the check must name the origin, not a re-apply."""
        result = self.check("false", self.live(limit="existing_users", origin="organization"))
        self.assertIn("is set on the owning organization", result.stdout)
        self.assertNotIn("live existing_users", result.stdout)
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
            "FAIL live interaction limit for 'ptr727/Fixture' could not be read as one JSON object",
            result.stdout,
        )
        self.assertIn("FAILED=1", result.stdout)

    def test_a_private_repo_is_a_note_and_never_reads_the_limit(self) -> None:
        result = self.check("true", None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is private, so none is owed", result.stdout)
        self.assertIn("FAILED=0", result.stdout)

    def test_an_unverified_public_repo_with_no_limit_is_a_note(self) -> None:
        """Apply sets no limit before the record, so a check failing on its absence would name an unfollowable fix."""
        result = self.check("false", "{}", verified=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"interaction limit: none is owed until a pilot's bot verification is recorded at {RECORD_PATH}",
            result.stdout,
        )
        self.assertIn("FAILED=0", result.stdout)

    def test_an_unverified_public_repo_names_a_limit_in_force_and_its_expiry(self) -> None:
        """Nothing renews such a limit, the pilot's hand-set one included, so the check names when it lapses."""
        live = self.live(limit="existing_users")
        result = self.check("false", live, verified=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        stamp = json.loads(live)["expires_at"]
        self.assertIn(
            f"interaction limit: existing_users is in force on the repository until {stamp}",
            result.stdout,
        )
        self.assertIn("FAILED=0", result.stdout)

    def test_an_unverified_public_repo_with_an_unreadable_limit_fails(self) -> None:
        result = self.check("false", "[]", verified=False)
        self.assertIn("FAIL live interaction limit for 'ptr727/Fixture'", result.stdout)
        self.assertIn("FAILED=1", result.stdout)


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

    def test_the_record_is_resolved_before_either_command_runs(self) -> None:
        """A malformed record exits the shared block, which only stops every write if it precedes the dispatch."""
        text = (ROOT / "repo-config" / "configure.sh").read_text(encoding="utf-8")
        self.assertLess(text.index(RECORD), text.index('\ncase "$cmd" in\napply) cmd_apply ;;'))

    def test_check_runs_the_interaction_group(self) -> None:
        body = function("cmd_check")
        self.assertIn("\n    check_interaction\n", body)


if __name__ == "__main__":
    unittest.main()
