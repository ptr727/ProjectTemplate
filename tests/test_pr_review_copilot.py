#!/usr/bin/env python3
"""Read constructed reviewer bodies through the adapter and assert the typed result.

Every body here is built from the current format's own markers, with titles, paths, and prose
invented for the case, per GOVERNANCE.md "Representative Data in Agent-Authored Text". A new
spelling the reviewer adopts is a vocabulary edit in `scripts/reviewers/` plus one case here.

Run under `uv run --no-project --with-requirements tests/requirements.txt`, as
`python tests/test_pr_review_copilot.py` or `python -m unittest discover -s tests`.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_review_copilot as adapter

MARKER = "<!-- ccr-overview-v2 -->"
BADGE = (
    '<picture><source media="(prefers-color-scheme: dark)" srcset="https://example.test/m.svg">'
    '<img src="https://example.test/m.png" alt="Medium severity" width="62" height="18"></picture>'
)


def body(
    verdict: str = "### \U0001f7e1 Changes recommended",
    headline: str = "One reader still needs a look.",
    sections: str = "",
    tail: str = "<sub>Review effort: Lite</sub>",
) -> str:
    """A body in the current overview format: marker, verdict, headline, sections, effort."""
    return f"{MARKER}\n\n{verdict}\n\n{headline}\n\n{sections}\n\n{tail}\n"


def open_section(label: str = "2 open findings", ids: tuple[str, ...] = ("101", "102")) -> str:
    """An open section whose entries each link a thread's first comment."""
    entries = "\n".join(
        f"- {BADGE} [Finding {n}](https://example.test/pull/1#discussion_r{n}) \u00b7 New"
        for n in ids
    )
    return f"<details open>\n<summary><strong>{label}</strong></summary>\n\n{entries}\n</details>"


def collapsed(label: str, finding: str = "Validate the input before the write.") -> str:
    """A collapsed finding section holding one finding as a nested block, as the format does."""
    return (
        f"<details>\n<summary><strong>{label}</strong></summary>\n\n"
        "In code that has not changed since last review\n\n"
        f"<details>\n<summary>{BADGE} Guard the write</summary>\n\n"
        f"`src/example.py:12`\n\n[P2] {finding}\n</details>\n</details>"
    )


def what_changed(paths: tuple[str, ...] = ("a.py", "docs/b.md"), extra: str = "") -> str:
    """The narrative section with its change list and file table."""
    rows = "\n".join(f"| `{p}` | Narrows the reader. |" for p in paths)
    return (
        "<details>\n<summary><strong>What changed in this PR</strong></summary>\n\n"
        "This pull request narrows one reader.\n\n**Changes:**\n- Narrow the reader.\n\n"
        f"{extra}| File | Description |\n|---|---|\n{rows}\n</details>"
    )


class TestVocabulary(unittest.TestCase):
    def test_every_file_carries_every_key_the_adapter_reads_in_its_shape(self) -> None:
        for name in adapter.REVIEWERS:
            with self.subTest(name=name):
                data = json.loads((adapter.VOCABULARY / f"{name}.json").read_text(encoding="utf-8"))
                self.assertEqual(set(adapter.KEYS), set(data))
                for key, kind in adapter.KEYS.items():
                    self.assertIsInstance(data[key], kind, key)
                self.assertEqual(set(adapter.KINDS) | {"vetted"}, set(data["sections"]))
                self.assertEqual({"quota", "error", "file_count"}, set(data["refusal_kinds"]))
                self.assertEqual({"clean", "flagging"}, set(data["verdicts"]))

    def test_every_key_the_adapter_source_names_is_declared(self) -> None:
        source = Path(adapter.__file__).read_text(encoding="utf-8")
        named = set(re.findall(r'vocab(?:ulary)?\["([a-z_]+)"\]', source))
        self.assertTrue(named)
        self.assertLessEqual(named, set(adapter.KEYS))

    def test_each_login_maps_to_its_own_vocabulary(self) -> None:
        self.assertEqual("copilot", adapter.LOGINS["copilot-pull-request-reviewer"])
        self.assertEqual("coderabbit", adapter.LOGINS["coderabbitai"])
        self.assertEqual("qodo", adapter.LOGINS["qodo-free-for-open-source-projects"])

    def test_the_vocabulary_files_are_ascii(self) -> None:
        for path in adapter.VOCABULARY.glob("*.json"):
            with self.subTest(path=path.name):
                self.assertTrue(path.read_bytes().isascii())


class TestRefusal(unittest.TestCase):
    def test_each_kind_is_named_from_the_vocabulary(self) -> None:
        for text, kind in (
            (
                (
                    "Copilot was unable to review this pull request because the user who "
                    "requested the review has reached their quota limit."
                ),
                "quota",
            ),
            (
                "Copilot encountered an error and was unable to review this pull request.",
                "error",
            ),
            (
                (
                    "Copilot wasn't able to review this pull request because it exceeds the "
                    "maximum number of files (300)."
                ),
                "file_count",
            ),
            ("Copilot wasn\u2019t able to review this pull request.", "unknown"),
        ):
            with self.subTest(kind=kind):
                found = adapter.read(text)
                self.assertEqual(kind, found.refusal)
                self.assertEqual("none", found.coverage)
                self.assertEqual((), found.notes)

    def test_the_wording_quoted_below_an_opening_is_not_a_refusal(self) -> None:
        found = adapter.read(
            body(headline="This change reads a body saying it was unable to review.")
        )
        self.assertEqual("none", found.refusal)

    def test_a_refusal_after_the_format_marker_is_still_read(self) -> None:
        found = adapter.read(f"{MARKER}\n\nCopilot was not able to review this pull request.\n")
        self.assertEqual("unknown", found.refusal)


class TestCoverage(unittest.TestCase):
    def test_the_marker_states_full_partial_or_an_unbelievable_pair(self) -> None:
        for marker, state in (
            ("<!-- fleet-review: reviewed=3 changed=3 findings=0 -->", "full"),
            ("<!-- fleet-review: reviewed=2 changed=3 findings=1 -->", "partial"),
            ("<!-- fleet-review: reviewed=4 changed=3 findings=0 -->", "unknown"),
            ("<!-- fleet-review: reviewed=12345 changed=12345 findings=0 -->", "unknown"),
        ):
            with self.subTest(marker=marker):
                found = adapter.read(body(tail=marker))
                self.assertEqual(state, found.coverage)
                self.assertEqual(marker, found.coverage_line)

    def test_a_marker_ending_a_sentence_is_read(self) -> None:
        found = adapter.read(
            body(headline="Read it all. <!-- fleet-review: reviewed=1 changed=1 findings=0 -->")
        )
        self.assertEqual("full", found.coverage)

    def test_the_worst_marker_wins(self) -> None:
        tail = (
            "<!-- fleet-review: reviewed=3 changed=3 findings=0 -->\n\n"
            "<!-- fleet-review: reviewed=1 changed=3 findings=0 -->"
        )
        self.assertEqual("partial", adapter.read(body(tail=tail)).coverage)

    def test_a_quoted_marker_states_nothing(self) -> None:
        marker = "<!-- fleet-review: reviewed=1 changed=3 findings=0 -->"
        for quoted in (f"`{marker}`", f"```\n{marker}\n```", f"    {marker}"):
            with self.subTest(quoted=quoted):
                found = adapter.read(body(tail=f"Lead.\n\n{quoted}"))
                self.assertEqual("none", found.coverage)

    def test_a_count_stated_in_a_paragraph_of_its_own_is_read(self) -> None:
        for line, state, noted in (
            ("**Files reviewed:** 3/4 changed files", "partial", False),
            ("Copilot reviewed 4 out of 4 changed files.", "full", False),
            ("Copilot reviewed 3 out of\n4 changed files.", "partial", False),
            ("Copilot reviewed 5 out of 4 changed files.", "unknown", True),
        ):
            with self.subTest(line=line):
                found = adapter.read(body(sections=line))
                self.assertEqual(state, found.coverage)
                self.assertEqual(noted, any("cannot believe" in n for n in found.notes))

    def test_a_count_directly_under_the_verdict_is_read_and_is_no_headline(self) -> None:
        for line in (
            "**Files reviewed:** 3/4 changed files",
            "Copilot reviewed 3 out of 4 changed files.",
        ):
            with self.subTest(line=line):
                found = adapter.read(body(headline=f"{line}\n\nOne reader still needs a look."))
                self.assertEqual("partial", found.coverage)
                self.assertEqual("One reader still needs a look.", found.headline)

    def test_a_count_in_the_narrative_section_is_read_beside_its_table(self) -> None:
        extra = "Copilot reviewed 1 out of 2 changed files.\n\n"
        found = adapter.read(body(sections=what_changed(extra=extra)))
        self.assertEqual("partial", found.coverage)
        self.assertEqual(("a.py", "docs/b.md"), found.table)

    def test_a_count_opening_a_list_item_states_nothing(self) -> None:
        count = "Reviewed 3 of 3 changed files in the earlier round."
        resolved = open_section("1 resolved since last review", ("101",)).replace(
            "[Finding", f"{count} [Finding"
        )
        for text in (
            body(sections=resolved),
            body(sections=what_changed(extra=f"**Changes:**\n- {count}\n\n")),
        ):
            with self.subTest(text=text[60:120]):
                self.assertEqual("none", adapter.read(text).coverage)

    def test_a_count_opening_a_nested_resolved_entry_states_nothing(self) -> None:
        entry = collapsed("2 resolved since last review", "Done.").replace(
            "`src/example.py:12`", "Copilot reviewed 4 out of 4 changed files."
        )
        self.assertEqual("none", adapter.read(body(sections=entry)).coverage)

    def test_a_sentence_naming_the_count_midway_states_nothing(self) -> None:
        for text in (
            body(headline="The reader treats Copilot reviewed 1 of 2 changed files as partial."),
            body(sections="This round reads that Copilot reviewed 1 of 2 changed files."),
        ):
            with self.subTest(text=text[40:90]):
                self.assertEqual("none", adapter.read(text).coverage)

    def test_a_partial_wins_over_any_reading_beside_it(self) -> None:
        tail = (
            "<!-- fleet-review: reviewed=4 changed=4 findings=0 -->\n\n"
            "<!-- fleet-review: reviewed=5 changed=4 findings=0 -->\n\n"
            "Copilot reviewed 3 out of 4 changed files."
        )
        self.assertEqual("partial", adapter.read(body(tail=tail)).coverage)

    def test_a_count_quoted_in_code_or_a_finding_states_nothing(self) -> None:
        quoted = "A fixture reads `Copilot reviewed 3 out of 4 changed files` here."
        self.assertEqual("none", adapter.read(body(headline=quoted)).coverage)
        inside = body(
            sections=collapsed(
                "1 previously missed issue", "Copilot reviewed 3 out of 4 changed files."
            )
        )
        self.assertEqual("none", adapter.read(inside).coverage)

    def test_no_marker_states_none(self) -> None:
        self.assertEqual("none", adapter.read(body()).coverage)


class TestVerdictAndHeadline(unittest.TestCase):
    def test_each_verdict_class_and_its_headline(self) -> None:
        for heading, kind in (
            ("### \U0001f7e2 Approval recommended", "clean"),
            ("### Ready to approve", "clean"),
            ("### \U0001f7e1 Changes recommended", "flagging"),
            ("### \U0001f535 Needs a closer look", "flagging"),
        ):
            with self.subTest(heading=heading):
                found = adapter.read(body(verdict=heading))
                self.assertEqual(kind, found.verdict)
                self.assertEqual("One reader still needs a look.", found.headline)

    def test_the_headline_stops_at_the_bold_count_line(self) -> None:
        found = adapter.read(body(headline="A caution.\n\n**No open issues** \U0001f389"))
        self.assertEqual("A caution.", found.headline)
        self.assertEqual(0, found.open)

    def test_a_headline_keeps_its_code_spans(self) -> None:
        found = adapter.read(body(headline="The `<summary>` reader needs a look."))
        self.assertEqual("The `<summary>` reader needs a look.", found.headline)

    def test_no_verdict_heading_reads_unknown(self) -> None:
        self.assertEqual("unknown", adapter.read(f"{MARKER}\n\nProse only.\n").verdict)


class TestOpenFindings(unittest.TestCase):
    def test_each_open_spelling_states_its_count(self) -> None:
        for label, ids, count in (
            ("2 open findings", ("101", "102"), 2),
            ("1 open issue", ("101",), 1),
            ("3 open issues", ("101", "102", "103"), 3),
            ("Open (2)", ("101", "102"), 2),
        ):
            with self.subTest(label=label):
                found = adapter.read(body(sections=open_section(label, ids)))
                self.assertEqual(count, found.open)
                self.assertEqual(len(ids), found.entries)
                self.assertEqual(0, found.unthreaded)
                self.assertEqual((), found.notes)

    def test_a_bold_count_line_states_zero(self) -> None:
        for line in ("**0 open findings**", "**No open issues** \U0001f389"):
            with self.subTest(line=line):
                found = adapter.read(body(sections=line))
                self.assertEqual(0, found.open)
                self.assertEqual(
                    {"threaded": 0, "suppressed": 0, "previously_missed": 0}, found.counts
                )

    def test_no_count_reads_as_none_rather_than_zero(self) -> None:
        self.assertIsNone(adapter.read(body()).open)

    def test_an_entry_linking_no_thread_on_the_pull_request_is_unthreaded(self) -> None:
        found = adapter.read(body(sections=open_section()), threads=("101",))
        self.assertEqual(1, found.unthreaded)
        self.assertEqual(
            0, adapter.read(body(sections=open_section()), threads=("101", "102")).unthreaded
        )

    def test_a_count_beyond_the_linked_entries_is_unthreaded(self) -> None:
        found = adapter.read(body(sections=open_section("3 open findings")), threads=("101", "102"))
        self.assertEqual(1, found.unthreaded)

    def test_a_findings_list_longer_than_the_threads_is_unthreaded(self) -> None:
        listed = "**Review findings:**\n- P1 moderate (2 votes): one.\n- P2 nit (1 vote): two.\n- P2 nit (1 vote): three.\n\n"
        found = adapter.read(
            body(sections=open_section() + "\n\n" + what_changed(extra=listed)),
            threads=("101", "102"),
        )
        self.assertEqual(1, found.unthreaded)

    def test_a_nested_back_reference_is_not_an_entry(self) -> None:
        nested = open_section().replace(
            "\u00b7 New\n- ", "\u00b7 New\n  - see [earlier](#discussion_r999)\n- ", 1
        )
        found = adapter.read(body(sections=nested), threads=("101", "102"))
        self.assertEqual(2, found.entries)


class TestCollapsedSections(unittest.TestCase):
    def test_previously_missed_in_both_spellings(self) -> None:
        for label, count in (("1 previously missed issue", 1), ("Previously missed (2)", 2)):
            with self.subTest(label=label):
                found = adapter.read(body(sections=collapsed(label)))
                self.assertEqual(count, found.counts["previously_missed"])
                section = found.sections[0]
                self.assertEqual("previously_missed", section.kind)
                self.assertIn("Validate the input before the write.", section.text)
                self.assertIn("[Medium severity] Guard the write", section.text)
                self.assertEqual((), found.notes)

    def test_suppressed_in_both_spellings(self) -> None:
        for label in ("Suppressed comments (2)", "Comments suppressed due to low confidence (2)"):
            with self.subTest(label=label):
                self.assertEqual(
                    2, adapter.read(body(sections=collapsed(label))).counts["suppressed"]
                )

    def test_a_resolved_section_counts_nothing(self) -> None:
        resolved = open_section("3 resolved since last review", ("1", "2", "3"))
        found = adapter.read(body(sections=resolved))
        self.assertEqual((), found.sections)
        self.assertIsNone(found.open)
        self.assertEqual((), found.notes)

    def test_a_section_quoted_in_code_is_not_read(self) -> None:
        for quoted in (
            "The `<details><summary>Previously missed (1)</summary>` shape is read.",
            "```\n<details>\n<summary>Previously missed (1)</summary>\n</details>\n```",
        ):
            with self.subTest(quoted=quoted[:20]):
                found = adapter.read(body(headline=quoted))
                self.assertEqual((), found.sections)
                self.assertEqual((), found.notes)


class TestTableAndEffort(unittest.TestCase):
    def test_the_table_names_its_paths_with_format_characters_dropped(self) -> None:
        found = adapter.read(body(sections=what_changed(("a.py", "docs/\u200bb.md"))))
        self.assertEqual(("a.py", "docs/b.md"), found.table)

    def test_a_table_quoted_inside_a_finding_is_not_the_file_table(self) -> None:
        quoted = "Quoted below.\n\n| File | Note |\n|---|---|\n| `x.py` | y |"
        nested = "- One finding quotes a table.\n\n  | File | Note |\n  |---|---|\n  | `x.py` | y |"
        for sections in (
            collapsed("1 previously missed issue", quoted) + "\n\n" + what_changed(),
            "**Review findings:**\n\n" + nested + "\n\n" + what_changed(),
        ):
            with self.subTest(sections=sections[:30]):
                self.assertEqual(("a.py", "docs/b.md"), adapter.read(body(sections=sections)).table)

    def test_a_table_under_another_header_is_not_the_file_table(self) -> None:
        found = adapter.read(body(sections="| Name | Note |\n|---|---|\n| x | y |"))
        self.assertIsNone(found.table)

    def test_effort_in_each_spelling(self) -> None:
        for tail, want in (
            ("<sub>Review effort: Lite</sub>", ("lite", "explicit")),
            ("\U0001f9e0 **Review effort:** Balanced", ("balanced", "explicit")),
            ("<sub>Review effort: Default (Max)</sub>", ("max", "default")),
            ("", ("unknown", "unknown")),
        ):
            with self.subTest(tail=tail):
                found = adapter.read(body(tail=tail))
                self.assertEqual(want, (found.effort, found.effort_source))


class TestNotes(unittest.TestCase):
    def test_a_copilot_body_without_the_marker_is_noted(self) -> None:
        found = adapter.read("## Pull request overview\n\nThe change is narrow.\n")
        self.assertEqual(1, len(found.notes))
        self.assertIn("ccr-overview-v2", found.notes[0])

    def test_an_empty_copilot_body_is_noted(self) -> None:
        self.assertEqual(("the body is empty",), adapter.read("").notes)

    def test_an_unknown_section_heading_and_label_are_each_noted(self) -> None:
        odd = (
            "<details>\n<summary>Confidence notes (2)</summary>\n\nText.\n</details>\n\n"
            "### Confidence assessment\n\n**Risk level:**\n\nLow."
        )
        notes = adapter.read(body(sections=odd)).notes
        self.assertEqual(3, len(notes))
        self.assertTrue(any("confidence notes (2)" in n for n in notes))
        self.assertTrue(any("confidence assessment" in n for n in notes))
        self.assertTrue(any("risk level:" in n for n in notes))

    def test_a_live_shaped_body_carries_no_note(self) -> None:
        sections = "\n\n".join(
            (open_section(), collapsed("1 previously missed issue"), what_changed())
        )
        self.assertEqual((), adapter.read(body(sections=sections)).notes)

    def test_the_reader_never_raises(self) -> None:
        with mock.patch.object(adapter, "_read", side_effect=ValueError("boom")):
            found = adapter.read("anything")
        self.assertEqual("unknown", found.coverage)
        self.assertEqual("none", found.refusal)
        self.assertEqual(("the reader failed on this body (ValueError)",), found.notes)
        self.assertEqual(("anything",), tuple(p.text for p in found.unplaced))


class TestUnplaced(unittest.TestCase):
    def test_each_unplaced_part_carries_its_own_text(self) -> None:
        odd = (
            "<details>\n<summary>Confidence notes (2)</summary>\n\nHidden finding.\n</details>\n\n"
            "### Confidence assessment\n\nThe loop may not end.\n\n**Risk level:**\n\nLow."
        )
        parts = {p.note: p.text for p in adapter.read(body(sections=odd)).unplaced}
        self.assertEqual(3, len(parts))
        texts = list(parts.values())
        self.assertTrue(any("Hidden finding." in t and "<summary>" in t for t in texts))
        self.assertTrue(any(t.startswith("### Confidence assessment") for t in texts))
        self.assertTrue(any("The loop may not end." in t for t in texts))

    def test_a_body_in_another_layout_is_unplaced_whole(self) -> None:
        text = "## Pull request overview\n\nThe change is narrow."
        self.assertEqual((text,), tuple(p.text for p in adapter.read(text).unplaced))

    def test_an_empty_body_and_a_placed_body_leave_nothing_unplaced(self) -> None:
        self.assertEqual((), adapter.read("").unplaced)
        self.assertEqual((), adapter.read(body(sections=open_section())).unplaced)

    def test_the_fingerprint_is_short_stable_and_named_by_the_text(self) -> None:
        a, b = adapter.Unplaced("one", "first part"), adapter.Unplaced("two", "second part")
        fp = adapter.fingerprint([a, b])
        self.assertRegex(fp, r"^[0-9a-f]{12}$")
        self.assertEqual(fp, adapter.fingerprint([b, a]))
        self.assertNotEqual(fp, adapter.fingerprint([a, b._replace(text="second part.")]))

    def test_an_excerpt_is_bounded_in_lines_and_width(self) -> None:
        text = "\n".join(["x" * 500] + [f"line {n}" for n in range(40)])
        shown = adapter.excerpt(text)
        self.assertEqual(adapter.EXCERPT_LINES + 1, len(shown))
        self.assertTrue(shown[0].endswith("x..."))
        self.assertLessEqual(len(shown[0]), adapter.EXCERPT_WIDTH + 10)
        self.assertIn("29 more lines in the body", shown[-1])

    def test_an_unplaced_label_stops_before_what_the_reader_places(self) -> None:
        odd = "**Reviewer musings:**\n\nThe loop may not end.\n\n" + what_changed()
        (part,) = adapter.read(body(sections=odd)).unplaced
        self.assertIn("The loop may not end.", part.text)
        self.assertNotIn("What changed", part.text)

    def test_a_label_inside_a_list_item_is_not_a_part(self) -> None:
        nested = "**Changes:**\n\n- Test updates:\n\n  - Cover the empty case.\n"
        self.assertEqual((), adapter.read(body(sections=nested)).unplaced)

    def test_a_pathological_body_reads_in_bounded_time(self) -> None:
        for text in ("<details>" * 5000, "> " * 20000 + "x", "`" * 30000, "\u00a0" * 30000):
            with self.subTest(text=text[:10]):
                adapter.read(text)


class TestOtherReviewers(unittest.TestCase):
    def test_coderabbit_outside_diff_counts_its_heading(self) -> None:
        text = (
            "Walkthrough prose.\n\n> <details>\n> <summary>\u26a0\ufe0f Outside diff range "
            "comments (3)</summary><blockquote>\n> \n> <details>\n> <summary>a.py (3)</summary>"
            "<blockquote>\n> \n> `12-14`: **Off by one.**\n> \n> </blockquote></details>\n> \n"
            "> </blockquote></details>\n"
        )
        found = adapter.read(text, "coderabbit")
        self.assertEqual(3, found.counts["previously_missed"])
        self.assertIn("Off by one.", found.sections[0].text)
        self.assertEqual((), found.notes)

    def test_coderabbit_rate_limit_in_both_shapes(self) -> None:
        marker = (
            "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n\nText."
        )
        reply = (
            "<!-- This is an auto-generated reply by CodeRabbit -->\n<details>\n"
            "<summary>\u26a0\ufe0f Action not completed</summary>\n\nReview rate limited.\n"
            "</details>\n"
        )
        self.assertEqual("coderabbit.ai", adapter.read(marker, "coderabbit").rate_limited)
        self.assertEqual("coderabbit.ai", adapter.read(reply, "coderabbit").rate_limited)
        self.assertIsNone(adapter.read("Walkthrough prose.", "coderabbit").rate_limited)

    def test_qodo_open_findings_skip_closed_badges_and_nested_blocks(self) -> None:
        def finding(n: int, title: str, badge: str = "") -> str:
            return (
                f"<details>\n<summary>  {n}.  {title}{badge} <code>Bug</code></summary>\n\n"
                "> <details>\n><summary>Description</summary>\n>\n>The name misleads.\n"
                "></details>\n\n</details>\n"
            )

        text = (
            "<h3>Code Review by Qodo</h3>\n\n"
            + finding(1, "Bad naming")
            + finding(2, "<s>Old</s>", " <code>\u2713 Resolved</code>")
        )
        found = adapter.read(text, "qodo")
        self.assertEqual(1, found.open)
        self.assertEqual(["1. Bad naming Bug"], [s.title.strip() for s in found.sections])


if __name__ == "__main__":
    unittest.main(verbosity=2)
