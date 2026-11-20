#!/usr/bin/env python3
"""spec/audit_pins.py's `pins.age` check on constructed fixtures, with every API read stubbed."""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "spec"))
import audit
import audit_pins

HUB = "acme/hub"
NOW = datetime(2030, 6, 1, tzinfo=UTC)
SHA_OLD = "a" * 40
SHA_MID = "b" * 40
SHA_NEW = "c" * 40


def at(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


def caller(*refs: str, owner: str = "acme/hub") -> str:
    jobs = "".join(
        f"  job{i}:\n    uses: {owner}/.github/workflows/validate-task.yml@{ref} # 1.0.{i}\n"
        for i, ref in enumerate(refs)
    )
    return f"name: Test\non: pull_request\njobs:\n{jobs}"


def releases(*rows: tuple[str, float, bool]) -> dict[str, audit_pins.Release]:
    return {tag: (tag, at(days), pre) for tag, days, pre in rows}


RELEASES = releases(("1.0.1", 60, False), ("1.0.2", 40, False), ("1.0.3", 5, False))
SHAS = {SHA_OLD: "1.0.1", SHA_MID: "1.0.2", SHA_NEW: "1.0.3"}


def findings(
    pins: dict[str, dict[str, list[str]]], notes: tuple[str, ...] = ()
) -> list[tuple[str, str]]:
    entry = {"name": "Widget", "driftNotes": list(notes)}
    return audit_pins.pin_findings(entry, pins, RELEASES, SHAS, NOW)


def pin(ref: str) -> dict[str, dict[str, list[str]]]:
    return {".github/workflows/test.yml": {ref: ["validate-task.yml"]}}


class HubPinsCase(unittest.TestCase):
    """Which `uses:` lines count as a pin on the hub's reusable workflows."""

    def test_a_job_level_hub_call_is_a_pin(self) -> None:
        self.assertEqual(
            audit_pins.hub_pins(caller(SHA_OLD), HUB), {SHA_OLD: ["validate-task.yml"]}
        )

    def test_the_owner_and_repo_match_without_case(self) -> None:
        got = audit_pins.hub_pins(caller(SHA_OLD, owner="Acme/Hub"), HUB)
        self.assertEqual(got, {SHA_OLD: ["validate-task.yml"]})

    def test_one_ref_names_each_task_once(self) -> None:
        self.assertEqual(
            audit_pins.hub_pins(caller(SHA_OLD, SHA_OLD), HUB), {SHA_OLD: ["validate-task.yml"]}
        )

    def test_another_repo_a_step_and_an_unparsed_file_are_not_pins(self) -> None:
        for label, text in (
            ("another repository", caller(SHA_OLD, owner="other/hub")),
            (
                "a step-level action",
                (
                    "jobs:\n  a:\n    runs-on: x\n    steps:\n"
                    f"      - uses: acme/hub/.github/workflows/validate-task.yml@{SHA_OLD}\n"
                ),
            ),
            ("no ref", "jobs:\n  a:\n    uses: acme/hub/.github/workflows/validate-task.yml\n"),
            ("a local call", "jobs:\n  a:\n    uses: ./.github/workflows/validate-task.yml\n"),
            ("unparsed YAML", "jobs:\n  - [\n"),
            ("no jobs mapping", "jobs: []\n"),
        ):
            with self.subTest(label):
                self.assertEqual(audit_pins.hub_pins(text, HUB), {})

    def test_only_files_directly_under_workflows_are_read(self) -> None:
        tree = {
            ".github/workflows/a.yml",
            ".github/workflows/b.yaml",
            ".github/workflows/sub/c.yml",
            ".github/workflows/notes.md",
            "catalog/x.yml",
        }
        self.assertEqual(
            audit_pins.workflow_paths(tree),
            [".github/workflows/a.yml", ".github/workflows/b.yaml"],
        )


class PinFindingsCase(unittest.TestCase):
    """The two severities, the hold, and the stale hold."""

    def test_a_pin_on_the_newest_release_reports_nothing(self) -> None:
        self.assertEqual(findings(pin(SHA_NEW)), [])

    def test_a_pin_superseded_within_the_bound_is_an_advisory_with_its_count(self) -> None:
        got = findings(pin(SHA_MID))
        self.assertEqual([k for k, _ in got], ["ADVISORY"])
        self.assertIn("(1.0.2), 1 release(s) behind 1.0.3", got[0][1])

    def test_a_pin_superseded_past_the_bound_is_drift(self) -> None:
        got = findings(pin(SHA_OLD))
        self.assertEqual([k for k, _ in got], ["DRIFT"])
        self.assertIn("2 release(s) behind 1.0.3", got[0][1])
        self.assertIn("superseded 40 days ago", got[0][1])
        self.assertIn("(pins.age)", got[0][1])

    def test_a_tag_ref_resolves_like_its_commit(self) -> None:
        for ref in ("1.0.1", SHA_OLD.upper()):
            with self.subTest(ref):
                got = findings(pin(ref))
                self.assertEqual([k for k, _ in got], ["DRIFT"])
                self.assertIn("(1.0.1), 2 release(s) behind 1.0.3", got[0][1])

    def test_a_ref_naming_no_release_is_an_advisory(self) -> None:
        got = findings(pin("main"))
        self.assertEqual([k for k, _ in got], ["ADVISORY"])
        self.assertIn("no hub release", got[0][1])

    def test_a_held_pin_past_the_bound_is_an_advisory(self) -> None:
        got = findings(pin(SHA_OLD), ("Held at 1.0.1 for a regression (pins.age).",))
        self.assertEqual([k for k, _ in got], ["ADVISORY"])
        self.assertIn("held by a driftNote", got[0][1])

    def test_a_hold_with_no_pin_past_the_bound_is_stale(self) -> None:
        for label, pins in (("a current pin", pin(SHA_MID)), ("no pin at all", {})):
            with self.subTest(label):
                got = findings(pins, ("Held for a regression (pins.age).",))
                self.assertEqual(got[-1][0], "DRIFT")
                self.assertIn("Delete the note", got[-1][1])

    def test_a_hold_is_not_stale_while_a_pin_s_age_is_unread(self) -> None:
        got = findings(pin("main"), ("Held for a regression (pins.age).",))
        self.assertEqual([k for k, _ in got], ["ADVISORY"])

    def test_releases_order_by_version_not_by_publish_time(self) -> None:
        rows = releases(("1.0.9", 50, False), ("1.0.10", 45, False), ("1.0.8", 1, False))
        entry: dict[str, Any] = {"name": "Widget"}
        self.assertEqual(audit_pins.pin_findings(entry, pin("1.0.10"), rows, {}, NOW), [])
        got = audit_pins.pin_findings(entry, pin("1.0.8"), rows, {}, NOW)
        self.assertEqual([k for k, _ in got], ["DRIFT"])
        self.assertIn("2 release(s) behind 1.0.10, superseded 50 days ago", got[0][1])

    def test_a_tag_of_another_shape_is_unread(self) -> None:
        self.assertEqual(audit_pins.version_key("2.0.1509"), (2, 0, 1509))
        self.assertEqual(audit_pins.version_key("1.0.59-gad39ee1061"), (1, 0, 59))
        self.assertIsNone(audit_pins.version_key("v1.0"))
        got = audit_pins.pin_findings({}, pin("v1.0"), releases(("v1.0", 50, False)), {}, NOW)
        self.assertIn("no hub release", got[0][1])

    def test_a_prerelease_is_never_the_release_a_pin_trails(self) -> None:
        rows = releases(("1.0.1", 10, False), ("1.0.0-pre", 50, True), ("2.0.0-pre", 1, True))
        entry: dict[str, Any] = {"name": "Widget"}
        self.assertEqual(audit_pins.pin_findings(entry, pin("1.0.1"), rows, {}, NOW), [])
        got = audit_pins.pin_findings(entry, pin("1.0.0-pre"), rows, {}, NOW)
        self.assertEqual([k for k, _ in got], ["ADVISORY"])
        self.assertIn("1 release(s) behind 1.0.1", got[0][1])


class PinAgeReadsCase(unittest.TestCase):
    """What the check reads, through stubbed `gh` and file reads."""

    @staticmethod
    def reader(pages: list[list[dict[str, Any]]], calls: list[str]) -> Any:
        refs = [
            {"ref": "refs/tags/1.0.1", "object": {"type": "commit", "sha": SHA_OLD}},
            {"ref": "refs/tags/1.0.3", "object": {"type": "tag", "sha": SHA_NEW}},
        ]

        def read(path: str) -> Any:
            calls.append(path)
            if path.startswith(f"repos/{HUB}/releases?"):
                return pages[int(path.rsplit("=", 1)[1]) - 1]
            if path == f"repos/{HUB}/git/matching-refs/tags/":
                return refs
            raise AssertionError(path)

        return read

    @staticmethod
    def release(tag: str, days: float, **extra: Any) -> dict[str, Any]:
        published = at(days).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"tag_name": tag, "published_at": published, **extra}

    def run_check(self, read: Any, tree: set[str] | None, text: str, notes: tuple[str, ...] = ()):
        entry = {"name": "Widget", "driftNotes": list(notes)}
        return audit_pins.pin_age_findings(entry, HUB, tree, lambda _: text, read, NOW)

    def test_releases_page_until_a_short_page_and_drafts_drop_out(self) -> None:
        calls: list[str] = []
        first = [self.release(f"0.0.{i}", 400 - i) for i in range(100)]
        second = [self.release("1.0.1", 60), self.release("1.0.9", 1, draft=True)]
        second.append(self.release("1.0.3", 40))
        read = self.reader([first, second], calls)
        got = self.run_check(read, {".github/workflows/test.yml"}, caller(SHA_OLD))
        self.assertEqual([k for k, _ in got], ["DRIFT"])
        self.assertIn("(1.0.1), 1 release(s) behind 1.0.3", got[0][1])
        self.assertEqual(sum("releases?" in c for c in calls), 2)

    def test_an_annotated_tag_commit_names_no_release(self) -> None:
        read = self.reader([[self.release("1.0.3", 1)]], [])
        got = self.run_check(read, {".github/workflows/test.yml"}, caller(SHA_NEW))
        self.assertEqual([k for k, _ in got], ["ADVISORY"])
        self.assertIn("no hub release", got[0][1])

    def test_no_pin_reads_no_release(self) -> None:
        calls: list[str] = []
        got = self.run_check(self.reader([], calls), {".github/workflows/test.yml"}, "on: push\n")
        self.assertEqual((got, calls), ([], []))

    def test_an_unread_tree_reports_nothing_even_under_a_hold(self) -> None:
        calls: list[str] = []
        got = self.run_check(self.reader([], calls), None, "", ("Held (pins.age).",))
        self.assertEqual((got, calls), ([], []))

    def test_a_failed_read_is_one_error(self) -> None:
        def read(path: str) -> Any:
            raise RuntimeError(f"gh api {path}: HTTP 502")

        got = self.run_check(read, {".github/workflows/test.yml"}, caller(SHA_OLD))
        self.assertEqual([k for k, _ in got], ["ERROR"])
        self.assertIn("HTTP 502", got[0][1])


class AuditWiringCase(unittest.TestCase):
    """How spec/audit.py runs the check and treats its ADVISORY kind."""

    def test_the_check_reads_the_hub_under_the_audited_repository_s_owner(self) -> None:
        run = audit.Run({"name": "Widget"}, {}, "acme/Widget", "main", {}, None, None)
        run.__dict__["tree"] = {".github/workflows/test.yml"}
        seen: list[Any] = []

        def fake(entry: Any, hub: str, tree: Any, read_text: Any, read: Any) -> list[Any]:
            seen.append((hub, tree, read is audit.gh))
            return []

        with (
            mock.patch.object(audit, "hub_repo_name", lambda: "hub"),
            mock.patch.object(audit.audit_pins, "pin_age_findings", fake),
        ):
            self.assertEqual(audit.check_pins_age(run), [])
        self.assertEqual(seen, [(HUB, {".github/workflows/test.yml"}, True)])

    def test_a_hold_is_a_suppression_on_every_repository(self) -> None:
        self.assertTrue(audit.is_suppression_id({"types": ["docs"]}, "pins.age"))
        self.assertTrue(audit.is_suppression_id({"types": ["python"]}, "pins.age"))

    def test_an_advisory_is_not_an_open_finding_for_the_driftnote_check(self) -> None:
        entry = {"name": "Widget", "driftNotes": ["Rollout still owed."]}
        run = audit.Run(entry, {"types": {"types": {}}}, "o/r", "main", {}, None, None)
        run.findings = [("ADVISORY", "pins: behind")]
        got = audit.check_registry_driftnotes(run)
        self.assertEqual(len(got), 1)
        self.assertIn("but the audit is clean", got[0][1])

    def test_the_issue_lists_advisories_in_their_own_section(self) -> None:
        found = [("DRIFT", "a drift"), ("ADVISORY", "pins: an advisory")]
        title, body = audit.render_issue({"name": "Widget"}, found, "main", "c0ffee", "now", "hub")
        self.assertIn("(1 detected finding)", title)
        advisory = body.index("## Advisory")
        self.assertGreater(advisory, body.index("## Converge"))
        self.assertIn("- pins: an advisory\n", body[advisory:])
        self.assertNotIn("an advisory", body[:advisory])

    def test_a_repository_with_only_advisories_reads_clean(self) -> None:
        found = [("ADVISORY", "pins: an advisory")]
        title, body = audit.render_issue({"name": "Widget"}, found, "main", "c0ffee", "now", "hub")
        self.assertIn("(0 detected findings)", title)
        self.assertIn("The deterministic checks are clean", body)


if __name__ == "__main__":
    unittest.main()
