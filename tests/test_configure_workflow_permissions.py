#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's Actions workflow token defaults: the payload contract, the live read, the apply write, and the check.

The shell is lifted out of the file, through the lift helper test_configure_archived.py already shares, rather than restated here.
An edit that drops a guard therefore fails these tests.
Only `gh`, the boundary the functions call out to, is stubbed, and every case runs offline.
No case reaches a live repository, since the stub answers every call the functions make.
"""

from __future__ import annotations

import json
import re
import shlex
import tempfile
import unittest
from pathlib import Path

from test_configure_archived import lift, run_bash

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "repo-config" / "workflow-permissions.json"
ENDPOINT = "repos/ptr727/Fixture/actions/permissions/workflow"


def function(name: str) -> str:
    """A whole top-level function, from its opening line to the closing brace in column one."""
    return lift(rf"^({re.escape(name)}\(\) \{{[^\n]*\n.*?\n\}}\n)")


JQR = lift(r"^(jqr\(\) \{ jq -r .*?\}\n)")
JQ_HAS = lift(r"^(jq_has\(\) \{ jq -e .*?\}\n)")
REPORTERS = lift(r"(FAILED=0\nnote\(\).*?\nfail\(\) \{.*?\n\})") + "\n"
ASSERT = function("assert")
PROBLEM = function("payload_problem") + function("workflow_permissions_problem")
LIVE = function("workflow_permissions_live")


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
                f"repo=ptr727/Fixture\n{JQR}{JQ_HAS}{gh_stub(log, [answer], read_status=status)}{LIVE}"
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
    """The write: never repeated, and never reported without the read-back confirming it."""

    def apply(
        self, before: dict[str, object], reads: list[str], put_status: int = 0
    ) -> tuple[str, str, int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "workflow-permissions.json"
            payload.write_text(json.dumps(DECLARED), encoding="utf-8")
            log = Path(tmp) / "gh-calls.txt"
            log.touch()
            result = run_bash(
                f"repo=ptr727/Fixture\nworkflow_permissions_file={shlex.quote(str(payload))}\n"
                f"{JQR}{JQ_HAS}{gh_stub(log, reads, put_status=put_status)}{LIVE}"
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


class CheckCase(unittest.TestCase):
    """The check group, which reports drift per key and never ends the run on a failed read."""

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
                f"{JQR}{REPORTERS}{JQ_HAS}{ASSERT}{PROBLEM}{gh_stub(log, [answer], read_status=status)}"
                f"{LIVE}{function('check_workflow_permissions')}"
                'check_workflow_permissions\necho "FAILED=$FAILED"\n',
                "jq",
                "sed",
            )
            return result.stdout, result.stderr, result.returncode

    def test_matching_defaults_pass(self) -> None:
        out, err, status = self.check(json.dumps(DECLARED))
        self.assertEqual(status, 0, err)
        self.assertIn("ok   workflow token default default_workflow_permissions = read", out)
        self.assertIn("ok   workflow token default can_approve_pull_request_reviews = false", out)
        self.assertIn("FAILED=0", out)

    def test_each_drifted_key_fails_on_its_own_line(self) -> None:
        for key, value, line in (
            (
                "default_workflow_permissions",
                "write",
                "FAIL workflow token default default_workflow_permissions = read",
            ),
            (
                "can_approve_pull_request_reviews",
                True,
                "FAIL workflow token default can_approve_pull_request_reviews = false",
            ),
        ):
            with self.subTest(key=key):
                out, err, status = self.check(json.dumps({**DECLARED, key: value}))
                self.assertEqual(status, 0, err)
                self.assertIn(line, out)
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

    def preflight(self, stubs: str) -> tuple[str, str, int]:
        script = (
            f"workflow_permissions_file=/fixture/workflow-permissions.json\n{stubs}"
            f"cmd_apply() {{\n    local why workflow_live\n{self.PREFLIGHT}"
            '    echo "reached-the-writes with $workflow_live"\n}\ncmd_apply\n'
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
        self.assertIn("reached-the-writes with live", out)

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

    def test_check_runs_the_workflow_permissions_group(self) -> None:
        self.assertIn("\n    check_workflow_permissions\n", function("cmd_check"))


if __name__ == "__main__":
    unittest.main()
