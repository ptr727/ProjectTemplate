#!/usr/bin/env python3
"""Drive the review gate's title check against constructed titles and payloads.

A wrong answer here holds a merge on a title the contract allows. Or it lets one into history that
a reviewer then raises a round at a time. So each case pins one rule, one exemption, or one
place the hold surfaces.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_review
import pr_review_answers as answers
import pr_review_title as title
import test_pr_review as fixtures

SKILL = (
    Path(__file__).resolve().parent.parent
    / ".agents"
    / "skills"
    / "comment-and-doc-style"
    / "SKILL.md"
)


def titled(name: str | None, login: str = "someone", state: str = "OPEN") -> dict:
    """A pull request payload carrying `name` as its title, or no title at all where None."""
    pr = {**fixtures.payload([]), "author": {"login": login}, "state": state}
    if name is not None:
        pr["title"] = name
    return pr


def skill_section() -> str:
    """The skill's own title section, which the bind words and the examples are read from."""
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("## PR titles and commit messages")
    return text[start : text.index("\n## ", start + 1)]


class TestContractSource(unittest.TestCase):
    """The list and the examples are read from the skill, so neither can drift from it."""

    def test_the_bind_words_are_the_skills_list(self) -> None:
        found = re.search(
            r"The bind-word list is exhaustive: (.+?)\. A hyphenated", skill_section(), re.DOTALL
        )
        assert found is not None
        listed = {w.strip() for w in " ".join(found[1].split()).split(",")}
        self.assertGreaterEqual(len(listed), 19)
        self.assertEqual(listed, set(title.BIND_WORDS))

    def test_every_example_title_in_the_skill_passes(self) -> None:
        block = re.search(r"```text\n(.+?)```", skill_section(), re.DOTALL)
        assert block is not None
        examples = [ln for ln in block[1].splitlines() if ln.strip()]
        self.assertGreaterEqual(len(examples), 5)
        for example in examples:
            with self.subTest(example):
                self.assertEqual([], title.problems(example))

    def test_the_skills_inline_examples_pass(self) -> None:
        """The format line's own example, and a title using each of its hyphenated examples."""
        for example in ("Add 24-Hour PM2.5 Average Sensor", "Add a Built-in EPA-Corrected Value"):
            with self.subTest(example):
                self.assertEqual([], title.problems(example))


class TestLength(unittest.TestCase):
    def test_seventy_two_characters_pass_and_seventy_three_do_not(self) -> None:
        self.assertEqual([], title.problems("Add " + "X" * 68))
        self.assertEqual(
            ["shorten it from 73 to 72 characters or fewer"], title.problems("Add " + "X" * 69)
        )


class TestTrailingPeriod(unittest.TestCase):
    def test_a_trailing_period_is_named(self) -> None:
        self.assertEqual(["drop the trailing period"], title.problems("Add a Sensor."))

    def test_a_period_inside_a_name_is_not_trailing(self) -> None:
        self.assertEqual([], title.problems("Target .NET"))


class TestTitleCase(unittest.TestCase):
    def test_a_capitalized_bind_word_inside_the_title_is_lowered(self) -> None:
        self.assertEqual(
            ["write `From` as `from`, since a bind word stays lower case"],
            title.problems("Read the Value From the Header"),
        )

    def test_a_lower_case_word_is_raised_or_marked_as_a_name(self) -> None:
        (fix,) = title.problems("Read the value")
        self.assertTrue(fix.startswith("write `value` as `Value`, or put it in a code span"))

    def test_the_first_word_is_capitalized_even_as_a_bind_word(self) -> None:
        self.assertEqual([], title.problems("A Sensor for the Hall"))
        self.assertEqual(1, len(title.problems("a Sensor for the Hall")))

    def test_a_bind_word_opening_a_subtitle_takes_either_case(self) -> None:
        self.assertEqual([], title.problems("Promote Develop: A Sensor"))
        self.assertEqual([], title.problems("Promote Develop: a Sensor"))
        self.assertEqual(1, len(title.problems("Promote Develop: sensor Reads")))

    def test_a_hyphenated_word_capitalizes_each_part_but_a_bind_word(self) -> None:
        self.assertEqual([], title.problems("Add Built-in Multi-Targeting"))
        self.assertEqual(
            ["write `Built-In` as `Built-in`, since a bind word stays lower case"],
            title.problems("Add Built-In Support"),
        )
        self.assertEqual([], title.problems("Use an In-Memory Cache and an On-Demand Scan"))
        (fix,) = title.problems("Add Read-only Mounts")
        self.assertTrue(fix.startswith("write `Read-only` as `Read-Only`"))

    def test_a_word_no_pattern_can_case_is_exempt(self) -> None:
        for name in (
            "Pin softprops/action-gh-release to Commit SHA",
            "Drop net8.0 Multi-Targeting",
            "Support macOS Hosts",
            "Read the README",
            "Run `ruff` on the Tests",
            "Run `uv lock --upgrade` on the Tests",
            "Read the Head's Value",
            "Fix `pr_review.py`'s Exit Code",
            "Drop Stale `Item`s",
        ):
            with self.subTest(name):
                self.assertEqual([], title.problems(name))

    def test_each_rule_broken_is_named_once(self) -> None:
        found = title.problems("fix the Value From the header" + " Pad" * 12 + ".")
        self.assertEqual(5, len(found), found)


class TestHold(unittest.TestCase):
    def test_a_conventional_title_reads_ok(self) -> None:
        pr = titled("Add a Sensor")
        self.assertEqual([], title.title_hold(pr))
        self.assertEqual(title.OK, title.title_field(pr))
        self.assertEqual("", title.title_status(pr))
        self.assertEqual([], title.title_lines(pr))

    def test_a_broken_title_holds_and_names_each_fix(self) -> None:
        pr = titled("add a sensor.")
        self.assertEqual(title.HELD, title.title_field(pr))
        status = title.title_status(pr)
        self.assertTrue(status.startswith(f"status={title.STATUS} "), status)
        self.assertIn("Drop the trailing period", status)
        self.assertIn("Write `add` as `Add`", status)
        lines = title.title_lines(pr)
        self.assertTrue(lines[0].startswith("  TITLE NOT CONVENTIONAL:"))
        self.assertIn("    title: add a sensor.", lines)

    def test_dependabot_is_exempt(self) -> None:
        pr = titled(
            "Bump ruff from 0.1.0 to 0.2.0 in /a/b in the group across 1 directory now",
            "dependabot",
        )
        self.assertIsNone(title.title_hold(pr))
        self.assertEqual(title.EXEMPT, title.title_field(pr))

    def test_a_closed_pull_request_holds_nothing(self) -> None:
        for state in ("MERGED", "CLOSED"):
            with self.subTest(state):
                for login in ("someone", "dependabot"):
                    pr = titled("add a sensor.", login, state)
                    self.assertIsNone(title.title_hold(pr))
                    self.assertIsNone(title.title_field(pr))

    def test_a_payload_that_never_asked_reads_nothing(self) -> None:
        pr = titled(None)
        self.assertIsNone(title.title_hold(pr))
        self.assertIsNone(title.title_field(pr))

    def test_every_gate_query_asks_for_the_title_and_author(self) -> None:
        for name in ("Q_HELD", "Q_FULL"):
            with self.subTest(name):
                query = getattr(pr_review, name)
                self.assertIn(" title ", query)
                self.assertIn(" author{login}", query)


class TestGate(fixtures.GqlCase):
    """The hold reaches the digest, the verdict's exit, `merge`'s items, and its route."""

    def verdict(self, pr: dict) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = answers.report_verdict(pr)
        return code, out.getvalue()

    def test_a_held_title_before_any_review_prints_its_line_and_keeps_the_exit(self) -> None:
        """Exit 0 there says no review covers the head, which a refusal reading relies on."""
        code, out = self.verdict(titled("add a sensor"))
        self.assertEqual(0, code)
        self.assertIn(f"status={title.STATUS} ", out)

    def test_a_conventional_title_before_any_review_exits_zero(self) -> None:
        self.assertEqual((0, ""), self.verdict(titled("Add a Sensor")))

    def test_a_held_title_exits_fifty_over_a_clean_head_review(self) -> None:
        clean = {**titled("Add a Sensor"), **fixtures.payload([fixtures.review()])}
        self.assertEqual(0, self.verdict({**clean, "title": "Add a Sensor"})[0])
        self.assertEqual(50, self.verdict({**clean, "title": "add a sensor"})[0])

    def test_the_digest_carries_the_field_and_the_fixes(self) -> None:
        out, _ = pr_review.digest("o", "r", 7, pr=titled("add a sensor"))
        self.assertIn(" title=HELD ", out)
        self.assertIn("TITLE NOT CONVENTIONAL", out)
        out, _ = pr_review.digest("o", "r", 7, pr=titled("Add a Sensor"))
        self.assertIn(" title=ok ", out)
        self.assertNotIn("TITLE NOT CONVENTIONAL", out)

    def test_merge_names_the_hold_and_redrives(self) -> None:
        held = answers.unmet(titled("add a sensor"), {}, [])
        self.assertIn(title.STATUS, [status for status, _ in held])
        clear = answers.unmet(titled("Add a Sensor"), {}, [])
        self.assertNotIn(title.STATUS, [status for status, _ in clear])
        self.assertEqual("redrive", answers.route(1, False, f"status={title.STATUS} why\n"))


if __name__ == "__main__":
    unittest.main()
