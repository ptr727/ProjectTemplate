#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's Actions workflow token defaults: the payload contract, the live read, the apply write, and the check.

The shell is lifted out of the file, through the helpers test_configure_archived.py and test_configure_interaction.py already share, rather than restated here.
An edit that drops a guard therefore fails these tests.
Only `gh`, the boundary the functions call out to, is stubbed, and every case runs offline.
No case reaches a live repository, since the stub answers every call the functions make.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_configure_archived import lift, require, run_bash
from test_configure_interaction import HELPERS, function

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "repo-config" / "workflow-permissions.json"
ENDPOINT = "repos/ptr727/Fixture/actions/permissions/workflow"


CONFIGURE = ROOT / "repo-config" / "configure.sh"
PROBLEM = function("payload_problem") + function("workflow_permissions_problem")
LIVE = function("workflow_permissions_live")
HELD = function("workflow_permissions_held")


def compact(document: dict[str, object]) -> str:
    """The form workflow_permissions_live prints, compact JSON with sorted keys."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


DECLARED = {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}
LOOSE = {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}


def gh_stub(log: Path, reads: list[str], read_status: int = 0, put_status: int = 0) -> str:
    """A `gh` recording each call, answering each read with the next of `reads` and a write with no body.

    The reads are served in order, so a read after the write can answer differently from the one before it.
    That is what lets the read-back confirmation be told apart from the pre-flight read.
    The position is kept in a file, since each read runs in a command substitution's subshell, where a variable set is lost.
    """
    answers = "\n".join(f"answers+=({shlex.quote(answer)})" for answer in reads)
    index = shlex.quote(f"{log}.index")
    return (
        f"answers=()\n{answers}\necho 0 >{index}\n"
        "gh() {\n"
        "  local read_index\n"
        f'  printf "%s\\n" "$*" >>{shlex.quote(str(log))}\n'
        '  case "$*" in\n'
        f"  *--method\\ PUT*) return {put_status} ;;\n"
        "  esac\n"
        f'  read_index="$(cat {index})"\n'
        f'  echo "$((read_index + 1))" >{index}\n'
        '  printf "%s" "${answers[$read_index]}"\n'
        f"  return {read_status}\n"
        "}\n"
    )


class PayloadContractCase(unittest.TestCase):
    """The contract apply pre-flights before any write, so a bad payload never half-applies a repo."""

    def verdict(self, document: str | None) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workflow-permissions.json"
            if document is not None:
                path.write_text(document, encoding="utf-8")
            result = run_bash(
                f"workflow_permissions_file={shlex.quote(str(path))}\n{PROBLEM}"
                'if why="$(workflow_permissions_problem)"; then echo yes; else echo "no: $why"; fi\n',
                "jq",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            out = result.stdout.strip()
            return out == "yes", out

    def test_the_committed_payload_meets_its_own_contract(self) -> None:
        """The file every fleet apply reads is the one case that must never fail."""
        self.assertEqual(self.verdict(PAYLOAD.read_text(encoding="utf-8")), (True, "yes"))

    def test_the_committed_payload_declares_the_decided_values(self) -> None:
        """Read-only tokens and no pull request creation or approval, the values the maintainer decided."""
        self.assertEqual(json.loads(PAYLOAD.read_text(encoding="utf-8")), DECLARED)

    def test_each_defect_is_refused_for_its_own_reason(self) -> None:
        """Each case names the one thing it breaks, so a loosened test fails for a readable reason."""
        for label, document, expected in (
            ("missing file", None, "was not found"),
            ("not JSON", "{", "did not parse"),
            ("empty file", "", "exactly one JSON document"),
            ("two documents", "{} {}", "exactly one JSON document"),
            ("an array", "[]", "not a JSON object"),
            (
                "a missing key",
                '{"default_workflow_permissions": "read"}',
                "exactly the keys",
            ),
            (
                "an extra key",
                '{"default_workflow_permissions": "read", "can_approve_pull_request_reviews": false, "x": 1}',
                "exactly the keys",
            ),
            (
                "a permission the endpoint does not take",
                '{"default_workflow_permissions": "none", "can_approve_pull_request_reviews": false}',
                "a value the endpoint does not take",
            ),
            (
                "approval written as a string",
                '{"default_workflow_permissions": "read", "can_approve_pull_request_reviews": "false"}',
                "a value the endpoint does not take",
            ),
        ):
            with self.subTest(label=label):
                ok, out = self.verdict(document)
                self.assertFalse(ok)
                self.assertIn(expected, out)


class LiveReadCase(unittest.TestCase):
    """The read both modes share, which never hands a caller a partial answer."""

    def read(self, answer: str, status: int = 0) -> tuple[str, str, int]:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "gh-calls.txt"
            result = run_bash(
                f"repo=ptr727/Fixture\n{HELPERS}{gh_stub(log, [answer], read_status=status)}{LIVE}"
                "workflow_permissions_live\n",
                "jq",
                "sed",
            )
            return result.stdout, result.stderr, result.returncode

    def test_the_two_keys_are_printed_in_the_compared_form(self) -> None:
        """A key GitHub adds later is dropped, so it never reads as drift against the payload."""
        answer = json.dumps({**LOOSE, "added_later": 1})
        out, err, status = self.read(answer)
        self.assertEqual(status, 0, err)
        self.assertEqual(out.strip(), compact(LOOSE))

    def test_a_failed_call_fails_with_a_message_naming_the_repository(self) -> None:
        _, err, status = self.read("", status=1)
        self.assertNotEqual(status, 0)
        self.assertIn("Failed to read the Actions workflow token defaults of ptr727/Fixture", err)

    def test_a_body_without_both_keys_fails(self) -> None:
        _, err, status = self.read(json.dumps({"default_workflow_permissions": "read"}))
        self.assertNotEqual(status, 0)
        self.assertIn("did not read as one JSON object holding both keys", err)


class ApplyCase(unittest.TestCase):
    """The write: never repeated, never lowering a write default unconfirmed, and never reported without the read-back."""

    def apply(
        self,
        before: dict[str, object],
        reads: list[str],
        put_status: int = 0,
        lower: bool = True,
    ) -> tuple[str, str, int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "workflow-permissions.json"
            payload.write_text(json.dumps(DECLARED), encoding="utf-8")
            log = Path(tmp) / "gh-calls.txt"
            log.touch()
            result = run_bash(
                f"repo=ptr727/Fixture\nworkflow_permissions_file={shlex.quote(str(payload))}\n"
                f"lower_workflow_token={str(lower).lower()}\n"
                f"{HELPERS}{gh_stub(log, reads, put_status=put_status)}{LIVE}{HELD}"
                f"{function('apply_workflow_permissions')}"
                f"apply_workflow_permissions {shlex.quote(compact(before))}\n",
                "jq",
                "sed",
            )
            return result.stdout, result.stderr, result.returncode, log.read_text(encoding="utf-8")

    def test_matching_defaults_are_left_alone_rather_than_written_again(self) -> None:
        """Idempotence here is the absence of a call, so the call log is what the test reads."""
        out, err, status, calls = self.apply(DECLARED, [])
        self.assertEqual(status, 0, err)
        self.assertIn("already match", out)
        self.assertEqual(calls.strip(), "")

    def test_differing_defaults_are_written_once_and_read_back(self) -> None:
        out, err, status, calls = self.apply(LOOSE, [json.dumps(DECLARED)])
        self.assertEqual(status, 0, err)
        lines = calls.strip().splitlines()
        self.assertEqual(len(lines), 2, calls)
        self.assertIn(f"--method PUT {ENDPOINT} --input ", lines[0])
        self.assertEqual(lines[1], f"api {ENDPOINT}")
        self.assertIn(f"from {compact(LOOSE)} to {compact(DECLARED)}", out)

    def test_lowering_a_write_default_unconfirmed_is_held_back_with_no_call(self) -> None:
        """Lowering write to read can fail a job relying on the implicit write, so it waits for the option."""
        out, err, status, calls = self.apply(LOOSE, [], lower=False)
        self.assertEqual(status, 0, err)
        self.assertIn("Held back the workflow token defaults", out)
        self.assertIn("--lower-workflow-token", out)
        self.assertEqual(calls.strip(), "")

    def test_a_read_default_is_written_without_the_option(self) -> None:
        """Only the approval setting differs here, so nothing is lowered from write and nothing waits."""
        before = {**DECLARED, "can_approve_pull_request_reviews": True}
        out, err, status, calls = self.apply(before, [json.dumps(DECLARED)], lower=False)
        self.assertEqual(status, 0, err)
        self.assertIn(f"--method PUT {ENDPOINT}", calls)
        self.assertIn(f"to {compact(DECLARED)}", out)

    def test_a_refused_write_stops_the_run_naming_both_values(self) -> None:
        out, err, status, calls = self.apply(LOOSE, [], put_status=1)
        self.assertNotEqual(status, 0)
        self.assertIn("Failed to set the Actions workflow token defaults of ptr727/Fixture", err)
        self.assertEqual(len(calls.strip().splitlines()), 1)
        self.assertNotIn("Set the workflow token defaults", out)

    def test_a_write_that_does_not_read_back_is_unconfirmed(self) -> None:
        """A policy above the repository can hold a value, so a success status alone is not proof."""
        out, err, status, _ = self.apply(LOOSE, [json.dumps(LOOSE)])
        self.assertNotEqual(status, 0)
        self.assertIn("unconfirmed", err)
        self.assertNotIn("Set the workflow token defaults", out)


class HeldCase(unittest.TestCase):
    """The one test of whether applying would lower a write default unconfirmed, which the pre-flight and the write share."""

    def held(self, live: dict[str, object], declared: dict[str, object], lower: bool) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "workflow-permissions.json"
            payload.write_text(json.dumps(declared), encoding="utf-8")
            result = run_bash(
                f"workflow_permissions_file={shlex.quote(str(payload))}\n"
                f"lower_workflow_token={str(lower).lower()}\n{HELPERS}{HELD}"
                f"if workflow_permissions_held {shlex.quote(compact(live))}; then echo yes; else echo no; fi\n",
                "jq",
                "sed",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip() == "yes"

    def test_only_an_unconfirmed_lowering_of_write_is_held(self) -> None:
        on_read = {**LOOSE, "default_workflow_permissions": "read"}
        for label, live, declared, lower, expected in (
            ("write lowered unconfirmed", LOOSE, DECLARED, False, True),
            ("write lowered and confirmed", LOOSE, DECLARED, True, False),
            ("already read", on_read, DECLARED, False, False),
            ("declared write", LOOSE, LOOSE, False, False),
        ):
            with self.subTest(label=label):
                self.assertEqual(self.held(live, declared, lower), expected)


class CheckCase(unittest.TestCase):
    """The check group, which reports drift in the form apply compares and never ends the run on a failed read."""

    def check(
        self, answer: str, status: int = 0, document: str | None = None
    ) -> tuple[str, str, int]:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "workflow-permissions.json"
            payload.write_text(
                json.dumps(DECLARED) if document is None else document, encoding="utf-8"
            )
            log = Path(tmp) / "gh-calls.txt"
            result = run_bash(
                f"repo=ptr727/Fixture\nworkflow_permissions_file={shlex.quote(str(payload))}\n"
                f"FAILED=0\n{HELPERS}{PROBLEM}{gh_stub(log, [answer], read_status=status)}"
                f"{LIVE}{function('check_workflow_permissions')}"
                'check_workflow_permissions\necho "FAILED=$FAILED"\n',
                "jq",
                "sed",
            )
            return result.stdout, result.stderr, result.returncode

    def test_matching_defaults_pass(self) -> None:
        out, err, status = self.check(json.dumps(DECLARED))
        self.assertEqual(status, 0, err)
        self.assertIn(f"ok   workflow token defaults = {compact(DECLARED)}", out)
        self.assertIn("FAILED=0", out)

    def test_each_drifted_key_fails_naming_the_live_value(self) -> None:
        for key, value in (
            ("default_workflow_permissions", "write"),
            ("can_approve_pull_request_reviews", True),
        ):
            with self.subTest(key=key):
                live = {**DECLARED, key: value}
                out, err, status = self.check(json.dumps(live))
                self.assertEqual(status, 0, err)
                self.assertIn(
                    f"FAIL workflow token defaults = {compact(DECLARED)}, live {compact(live)}", out
                )
                self.assertIn("FAILED=1", out)

    def test_an_unreadable_endpoint_fails_without_ending_the_run(self) -> None:
        out, err, status = self.check("", status=1)
        self.assertEqual(status, 0, err)
        self.assertIn("FAIL could not read the Actions workflow token defaults", out)
        self.assertIn("FAILED=1", out)

    def test_a_payload_outside_its_contract_fails_naming_why(self) -> None:
        out, err, status = self.check(json.dumps(DECLARED), document="[]")
        self.assertEqual(status, 0, err)
        self.assertIn("is not a JSON object", out)
        self.assertIn("FAILED=1", out)


class WiringCase(unittest.TestCase):
    """The call sites that decide whether any of the above runs at all, and in which order."""

    PREFLIGHT = lift(
        r"(    # The workflow token defaults are validated and read here.*?\n    fi\n.*?\n    fi\n)"
        r"    # The visibility"
    )

    def preflight(self, stubs: str, dry_run: bool = False) -> tuple[str, str, int]:
        if "workflow_permissions_held" not in stubs:
            stubs += "workflow_permissions_held() { return 1; }\n"
        script = (
            f"repo=ptr727/Fixture\ndry_run={str(dry_run).lower()}\n"
            f"workflow_permissions_file=/fixture/workflow-permissions.json\n{stubs}"
            "cmd_apply() {\n    local why workflow_live workflow_held=false held_status=0\n"
            f"{self.PREFLIGHT}"
            '    echo "reached-the-writes with $workflow_live held=$workflow_held status=$held_status"\n}\ncmd_apply\n'
        )
        result = run_bash(script)
        return result.stdout, result.stderr, result.returncode

    def test_a_payload_outside_its_contract_aborts_before_any_write(self) -> None:
        out, err, status = self.preflight(
            'workflow_permissions_problem() { echo "was not found"; return 1; }\n'
            "workflow_permissions_live() { echo live; }\n"
        )
        self.assertNotEqual(status, 0)
        self.assertIn("workflow-permissions.json was not found. Aborting before any write", err)
        self.assertNotIn("reached-the-writes", out)

    def test_an_unreadable_endpoint_aborts_before_any_write(self) -> None:
        out, err, status = self.preflight(
            "workflow_permissions_problem() { :; }\nworkflow_permissions_live() { return 1; }\n"
        )
        self.assertNotEqual(status, 0)
        self.assertIn("Aborting before any write", err)
        self.assertNotIn("reached-the-writes", out)

    def test_the_live_read_is_handed_on_to_the_writes(self) -> None:
        out, err, status = self.preflight(
            "workflow_permissions_problem() { :; }\nworkflow_permissions_live() { echo live; }\n"
        )
        self.assertEqual(status, 0, err)
        self.assertIn("reached-the-writes with live held=false status=0", out)

    def test_a_held_group_is_decided_before_any_write_and_named_in_a_dry_run(self) -> None:
        stubs = (
            "workflow_permissions_problem() { :; }\nworkflow_permissions_live() { echo live; }\n"
            "workflow_permissions_held() { return 0; }\n"
        )
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                out, err, status = self.preflight(stubs, dry_run=dry_run)
                self.assertEqual(status, 0, err)
                self.assertIn("reached-the-writes with live held=true status=1", out)
                self.assertEqual("would be held back" in out, dry_run)

    def test_every_dry_run_exit_and_the_secrets_group_account_for_a_held_group(self) -> None:
        """Each of these exits before the last line of cmd_apply, so each has to carry the held status itself."""
        body = function("cmd_apply")
        self.assertIn(
            '(dry_run_secrets "$HOME/.secrets") || exit\n                exit "$held_status"', body
        )
        self.assertIn('so there is nothing to preview."\n        exit "$held_status"', body)
        note = body.index("the workflow token defaults were held back as noted above")
        self.assertLess(note, body.index("apply_secrets "))

    def test_apply_reads_before_its_first_write_and_writes_after_dependabot(self) -> None:
        body = function("cmd_apply")
        self.assertLess(
            body.index("workflow_permissions_live"), body.index("General repository settings")
        )
        self.assertLess(
            body.index("automated-security-fixes"),
            body.index('apply_workflow_permissions "$workflow_live"'),
        )
        self.assertLess(
            body.index('apply_workflow_permissions "$workflow_live"'),
            body.index("\n    apply_labels\n"),
        )

    def test_a_held_group_ends_apply_non_zero(self) -> None:
        """A run that left a write default in place has not applied the configuration, so it never ends clean."""
        body = function("cmd_apply")
        self.assertIn("workflow_held=false", body)
        guard = body.rindex('if [ "$workflow_held" = true ]; then')
        self.assertIn("exit 1", body[guard : guard + 250])
        self.assertLess(body.index('apply_workflow_permissions "$workflow_live"'), guard)

    def test_the_option_is_refused_outside_apply_before_any_read(self) -> None:
        """The refusal runs before the first read, which a `gh` that fails every call would otherwise expose."""
        bash = require("bash")
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "gh"
            stub.write_text("#!/bin/sh\necho gh-was-called >&2\nexit 1\n", encoding="utf-8")
            stub.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}")
            result = subprocess.run(
                [bash, str(CONFIGURE), "check", "owner/name", "--lower-workflow-token"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
                env=env,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Option '--lower-workflow-token' is valid only with apply", result.stderr)
        self.assertNotIn("gh-was-called", result.stderr)

    def test_check_runs_the_workflow_permissions_group(self) -> None:
        self.assertIn("\n    check_workflow_permissions\n", function("cmd_check"))


if __name__ == "__main__":
    unittest.main()
