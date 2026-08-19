#!/usr/bin/env python3
"""Drive every prose_lint gate against input it must reject.

Each case reintroduces a fault and asserts the gate objects to it, because a gate nobody has
watched fail is a gate nobody knows works. Where a case covers a table, it reads the live table
rather than restating it: a proof that restates the gated data proves only that the function runs.

Run as `python3 scripts/tests/test_prose_lint.py`, or under `python3 -m unittest discover -s scripts/tests`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".github/actions/prose-gate"))
import prose_lint

REPO = Path(__file__).resolve().parent.parent.parent
COMMENT_AND_DOC_STYLE_SKILL = REPO / ".agents" / "skills" / "comment-and-doc-style" / "SKILL.md"
PROSE_GATE_ACTION = REPO / ".github" / "actions" / "prose-gate" / "action.yml"

# Bait assembled from two literals, so this module never holds the pattern it feeds the gate.
# A file full of rejected input would otherwise report itself.
DUP = "the " + "the"
SPLICE_BAIT = "It runs on push; " + "it gates the merge"
# Attribute values whose repetition is correct authoring, assembled for the same reason.
DUP_CLASS = "gallery " + "gallery-cols-1"
DUP_REL = "nofollow " + "nofollow-ugc"


class BaitCase(unittest.TestCase):
    """Base for cases that write a crafted file and read back what the gate says about it."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def kinds(self, text: str, rules: set[str], name: str = "bait.md") -> list[str]:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return [kind for _, kind, _ in prose_lint.check_file(path, rules)]


class TestTierTables(BaitCase):
    def test_every_tier_one_character_is_always_caught(self) -> None:
        """Tier 1 carries no meaning its ASCII form loses, so context never excuses it."""
        for ch, fix in prose_lint.TIER1.items():
            with self.subTest(codepoint=f"U+{ord(ch):04X}", fix=fix):
                self.assertEqual(["charset"], self.kinds(f"left {ch} right\n", {"charset"}))
                self.assertEqual(["charset"], self.kinds(f"8 {ch} 9\n", {"charset"}))

    def test_a_tab_separates_an_operator_from_its_neighbor(self) -> None:
        """Any whitespace, not only a literal space, sits between an operator and its figure."""
        for gap in (" ", "\t", "  "):
            with self.subTest(gap=repr(gap)):
                self.assertEqual(
                    [], self.kinds(f"threshold{gap}{chr(0x2264)}{gap}35\n", {"charset"})
                )

    def test_every_tier_two_character_turns_on_its_neighbors(self) -> None:
        """The same operator is the range it describes next to a figure, and prose between words."""
        for ch in prose_lint.TIER2:
            with self.subTest(codepoint=f"U+{ord(ch):04X}"):
                self.assertEqual([], self.kinds(f"threshold {ch} 35 units\n", {"charset"}))
                self.assertEqual([], self.kinds(f"0 {ch} x\n", {"charset"}))
                self.assertEqual(
                    ["charset"], self.kinds(f"the check {ch} the threshold\n", {"charset"})
                )

    def test_every_tier_three_character_is_left_alone(self) -> None:
        """A unit symbol whose ASCII form would be a lie is kept in prose and in a table alike."""
        for ch in prose_lint.TIER3:
            with self.subTest(codepoint=f"U+{ord(ch):04X}"):
                self.assertEqual([], self.kinds(f"reads 35 {ch} today\n", {"charset"}))
                self.assertEqual([], self.kinds(f"the {ch} value\n", {"charset"}))

    def test_an_unclassified_character_is_reported_not_passed(self) -> None:
        """A gate that allows whatever it does not recognize stops gating as the set grows."""
        unknown = chr(0x2603)
        self.assertNotIn(unknown, prose_lint.TIER1)
        self.assertNotIn(unknown, prose_lint.TIER2)
        self.assertNotIn(unknown, prose_lint.TIER3)
        self.assertEqual(
            ["charset-unknown"], self.kinds(f"a {unknown} here\n", {"charset-unknown"})
        )

    def test_keys_are_single_non_ascii_characters(self) -> None:
        """A key is a substitution target, so an ASCII key would flag text that is already fine."""
        for label, table in (
            ("1", prose_lint.TIER1),
            ("2", prose_lint.TIER2),
            ("3", prose_lint.TIER3),
        ):
            for ch in table:
                with self.subTest(tier=label, codepoint=f"U+{ord(ch):04X}"):
                    self.assertEqual(1, len(ch))
                    self.assertFalse(ch.isascii())

    def test_replacements_are_printable_ascii(self) -> None:
        """The suggested form has to be typeable.

        `isprintable` rather than a codepoint floor: DEL and the Unicode format characters sit
        above 32 and are just as invisible in a diff. Applied to the replacements only, since the
        keys are non-ASCII by construction and two of them (U+00A0, U+2011) are not printable.
        """
        for label, table in (("1", prose_lint.TIER1), ("2", prose_lint.TIER2)):
            for ch, fix in table.items():
                with self.subTest(tier=label, codepoint=f"U+{ord(ch):04X}", fix=fix):
                    self.assertTrue(fix.isascii())
                    self.assertTrue(fix.isprintable())

    def test_the_source_carries_no_literal_non_ascii(self) -> None:
        """The table is written as escapes.

        A literal curly quote or U+00A0 in this module's own source is invisible in a diff, and
        the file is scanned by the rule it implements, so a literal would make it self-flagging.
        """
        for path in (REPO / "scripts" / "prose_lint.py", Path(__file__)):
            with self.subTest(source=path.name):
                bad = [
                    f"U+{ord(c):04X}" for c in path.read_text(encoding="utf-8") if not c.isascii()
                ]
                self.assertEqual([], bad)

    def test_each_tier_covers_a_plausible_number_of_characters(self) -> None:
        """A table that shrank to nothing would satisfy every case above by having no entries."""
        for label, table, floor in (
            ("1", prose_lint.TIER1, 12),
            ("2", prose_lint.TIER2, 8),
            ("3", prose_lint.TIER3, 7),
        ):
            with self.subTest(tier=label):
                self.assertGreaterEqual(len(table), floor)


class TestCommentAndDocStyleSkillCoupling(unittest.TestCase):
    """The rule text drives the tables, rather than a copy of the rule driving them.

    The Character Set rule text lives in the comment-and-doc-style Skill.
    GOVERNANCE.md's own section is a pointer at it, per the "skill becomes sole canonical
    content" decision. These are the cases that catch an incomplete or mis-tiered table, which
    no bait built from the tables themselves can do: bait proves the matching works, not that
    the data is right.
    """

    def setUp(self) -> None:
        self.doc = COMMENT_AND_DOC_STYLE_SKILL.read_text(encoding="utf-8")
        section = re.search(r"^## Character set$(.*?)^## ", self.doc, re.MULTILINE | re.DOTALL)
        if section is None:
            self.fail("the Character set heading moved, so the parse is blind")
        self.section = section.group(1)

    def tier_codepoints(self, label: str) -> set[int]:
        """Codepoints named in one tier's bullet, read out of the rule text itself."""
        m = re.search(
            rf"^- \*\*Tier {label},(.*?)(?=^- \*\*)", self.section, re.MULTILINE | re.DOTALL
        )
        if m is None:
            self.fail(f"the Tier {label} bullet moved, so the parse is blind")
        return {int(h, 16) for h in re.findall(r"U\+([0-9A-Fa-f]{4})", m.group(1))}

    def test_every_tier_names_a_plausible_number_of_characters(self) -> None:
        """A tier bullet that stopped parsing would make every case below it pass vacuously."""
        for label, floor in (("1", 10), ("2", 8), ("3", 7)):
            with self.subTest(tier=label):
                self.assertGreaterEqual(len(self.tier_codepoints(label)), floor)

    def test_tier_one_and_two_are_in_the_gate_tables(self) -> None:
        for label, table in (("1", prose_lint.TIER1), ("2", prose_lint.TIER2)):
            for cp in sorted(self.tier_codepoints(label)):
                with self.subTest(tier=label, codepoint=f"U+{cp:04X}"):
                    self.assertIn(chr(cp), table)

    def test_tier_three_is_allowed_rather_than_replaced(self) -> None:
        """A tier-3 symbol in a replacement table would flag the character the rule protects."""
        for cp in sorted(self.tier_codepoints("3")):
            with self.subTest(codepoint=f"U+{cp:04X}"):
                self.assertIn(chr(cp), prose_lint.TIER3)
                self.assertNotIn(chr(cp), prose_lint.TIER1)
                self.assertNotIn(chr(cp), prose_lint.TIER2)

    def test_the_tiers_do_not_overlap(self) -> None:
        t1, t2, t3 = set(prose_lint.TIER1), set(prose_lint.TIER2), set(prose_lint.TIER3)
        self.assertEqual(set(), t1 & t2)
        self.assertEqual(set(), t1 & t3)
        self.assertEqual(set(), t2 & t3)


class TestDupword(BaitCase):
    """A doubled word, read from Markdown prose and from the comments of every other syntax.

    The scope matters more here than for the other prose rules, because this one gates CI. Outside
    Markdown a repeated token is far more often correct code than a typo: `class="gallery
    gallery-cols-1"` is the ordinary HTML idiom, and no edit fixes it without changing the page.
    """

    def test_every_allowlist_entry_is_permitted(self) -> None:
        for phrase in prose_lint.DUP_ALLOW:
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    [], self.kinds(f"A case where {phrase} reads correctly.\n", {"dupword"})
                )

    def test_a_repetition_outside_the_allowlist_is_flagged(self) -> None:
        """`the the` was in the allowlist while a comment claimed it was flagged."""
        self.assertEqual(["dupword"], self.kinds(f"{DUP} thing\n", {"dupword"}))

    def test_a_word_joining_character_does_not_start_a_repetition(self) -> None:
        """ "either/or or" is a phrase followed by a conjunction, not a doubled word."""
        self.assertEqual([], self.kinds("either/or or must-pair inputs\n", {"dupword"}))
        self.assertEqual([], self.kinds("a must-pair pair of inputs\n", {"dupword"}))

    def test_a_repetition_in_a_comment_is_flagged_in_every_syntax(self) -> None:
        """Narrowing the rule to comments has to leave it enforcing in all of them."""
        for name, comment in (
            ("bait.py", f"# {DUP.capitalize()} thing."),
            ("bait.sh", f"# {DUP.capitalize()} thing."),
            ("bait.yml", f"# {DUP.capitalize()} thing."),
            ("bait.cs", f"// {DUP.capitalize()} thing."),
            ("bait.cs", f"/* {DUP.capitalize()} thing. */"),
            ("bait.html", f"<!-- {DUP.capitalize()} thing. -->"),
        ):
            with self.subTest(file=name, comment=comment):
                self.assertEqual(["dupword"], self.kinds(f"{comment}\n", {"dupword"}, name=name))

    def test_a_trailing_comment_is_read_and_the_code_before_it_is_not(self) -> None:
        """The comment is prose wherever it sits on the line, and the statement stays code."""
        self.assertEqual(
            ["dupword"],
            self.kinds(f"x = 1  # {DUP.capitalize()} thing.\n", {"dupword"}, name="bait.py"),
        )

    def test_code_is_not_prose_outside_markdown(self) -> None:
        """A repeated token in code is the author's, and often the only spelling that works.

        The class attribute is the reported case: two class names sharing a prefix is how CSS is
        written, and `rel`, `srcset` and `data-*` all take value lists with the same shape.
        """
        for name, line in (
            ("bait.html", f'<div class="{DUP_CLASS}">'),
            ("bait.html", f'<a rel="{DUP_REL}" href="#">x</a>'),
            ("bait.html", f"<p>{DUP} thing</p>"),
            ("bait.py", f'x = "{DUP}"'),
            ("bait.yml", f"key: {DUP}"),
            ("bait.json", f'{{ "a": "{DUP}" }}'),
        ):
            with self.subTest(file=name, line=line):
                self.assertEqual([], self.kinds(f"{line}\n", {"dupword"}, name=name))

    def test_two_comments_on_one_line_are_judged_separately(self) -> None:
        """Joining them would read the second comment's opening word as a repeat of the first's."""
        word = DUP.split()[0]
        pair = f"<!-- Ends with {word} --><!-- {word.capitalize()} opens -->\n"
        self.assertEqual([], self.kinds(pair, {"dupword"}, name="bait.html"))

    def test_inline_code_is_not_prose(self) -> None:
        """A backticked token is quoted, the same exemption every other prose rule takes."""
        self.assertEqual([], self.kinds(f"The `{DUP}` field.\n", {"dupword"}))
        self.assertEqual([], self.kinds(f"# The `{DUP}` field.\n", {"dupword"}, name="bait.py"))

    def test_the_repo_is_clean_of_duplicated_words(self) -> None:
        """The rule gates in CI, so the tree it gates has to pass it today and not eventually."""
        found = [
            f"{prose_lint.rel(p)}:{ln}"
            for p in prose_lint.discover(["."])
            for ln, kind, _ in prose_lint.check_file(p, {"dupword"})
            if kind == "dupword"
        ]
        self.assertEqual([], found)


class TestSemicolon(BaitCase):
    def test_a_splice_is_flagged(self) -> None:
        self.assertEqual(["semicolon"], self.kinds(f"{SPLICE_BAIT}.\n", {"semicolon"}))

    def test_a_quoted_counter_example_is_exempt_in_markdown(self) -> None:
        """A rule that states its counter-example quotes the construction it bans."""
        self.assertEqual([], self.kinds(f'Recast "{SPLICE_BAIT}" as two.\n', {"semicolon"}))

    def test_a_list_semicolon_is_not_a_splice(self) -> None:
        self.assertEqual([], self.kinds("Inputs: a, b, and c; outputs: d and e.\n", {"semicolon"}))

    def test_a_fenced_block_is_skipped(self) -> None:
        self.assertEqual([], self.kinds("```sh\nrun; it exits\n```\n", {"semicolon"}))


class TestDash(BaitCase):
    def test_a_clause_break_is_flagged(self) -> None:
        self.assertEqual(
            ["dash"], self.kinds("It is capability, not permission - a token is not.\n", {"dash"})
        )

    def test_a_paired_aside_is_flagged_at_both_ends(self) -> None:
        self.assertEqual(
            ["dash", "dash"], self.kinds("The router - a thin file - holds the map.\n", {"dash"})
        )

    def test_a_label_separator_is_exempt(self) -> None:
        """`- **Label** - explanation` is structurally a colon and the shape every bullet uses."""
        self.assertEqual([], self.kinds("- **Bug** - wrong behavior, missing coverage\n", {"dash"}))

    def test_an_ordered_label_separator_is_exempt(self) -> None:
        """A numbered definition list is the same construct as a bulleted one."""
        self.assertEqual([], self.kinds("1. **Audit** - check it statically\n", {"dash"}))
        self.assertEqual(["dash"], self.kinds("2. **Test** - trace it - and probe it\n", {"dash"}))

    def test_a_later_dash_on_a_label_line_still_counts(self) -> None:
        """Exempting the separator must not exempt the rest of the line."""
        self.assertEqual(
            ["dash"], self.kinds("- **Bug** - wrong behavior - and worse besides\n", {"dash"})
        )

    def test_compound_words_and_ranges_are_left_alone(self) -> None:
        for text in (
            "A well-named must-pair input.\n",
            "Sections D1 - D9 apply.\n",
            "- a plain list item\n",
        ):
            with self.subTest(text=text.strip()):
                self.assertEqual([], self.kinds(text, {"dash"}))


class TestSemicolon2(BaitCase):
    def test_any_prose_semicolon_is_flagged(self) -> None:
        """The rule bans the construction, so the default is to flag rather than to detect a subset."""
        self.assertEqual(["semicolon"], self.kinds(f"{SPLICE_BAIT}.\n", {"semicolon"}))

    def test_the_imperative_splice_the_old_pattern_missed_is_caught(self) -> None:
        """A pronoun-keyed pattern found 170 of 493, and this shape was the documented gap."""
        self.assertEqual(
            ["semicolon"], self.kinds("Delegate exploration; keep synthesis.\n", {"semicolon"})
        )

    def test_a_list_that_already_carries_commas_keeps_its_semicolon(self) -> None:
        self.assertEqual([], self.kinds("Inputs: a, b, and c; outputs: d and e.\n", {"semicolon"}))

    def test_a_splice_whose_clause_carries_a_comma_is_still_a_splice(self) -> None:
        """A comma earlier on the line is not a list, so it cannot excuse the semicolon."""
        self.assertEqual(
            ["semicolon"],
            self.kinds("It runs on push, always; it gates the merge.\n", {"semicolon"}),
        )

    def test_a_list_whose_commas_fall_in_a_later_item_keeps_its_semicolons(self) -> None:
        """The comma qualifies the list, so reading it positionally split one series in two."""
        self.assertEqual(
            [],
            self.kinds(
                "It exists; it covers each target, and excludes the rest; it runs.\n", {"semicolon"}
            ),
        )

    def test_a_table_row_judges_each_cell_alone(self) -> None:
        """A row is a record of fields, so one column's comma cannot excuse another's semicolon."""
        self.assertEqual(
            ["semicolon"], self.kinds("| S1 | it runs; it gates | D1, D2 |\n", {"semicolon"})
        )

    def test_a_bullet_label_colon_does_not_announce_a_list(self) -> None:
        """`- **Label**:` opens the bullet, the same construct the label dash is exempted for."""
        self.assertEqual(
            ["semicolon"],
            self.kinds("- **Async**: avoid blocking calls; use await, always\n", {"semicolon"}),
        )

    def test_a_colon_in_an_earlier_sentence_does_not_exempt_a_later_splice(self) -> None:
        """The exemption belongs to the sentence, not the bullet, and the two were unrelated.

        Read over the whole bullet, an enumeration in one sentence excused every semicolon after
        it, so the gate went silent across 120 semicolons in the docs it exists to check.
        """
        self.assertEqual(
            ["semicolon"],
            self.kinds(
                "- **A rule.** The evidence is three things, and each matters: the first, the second, "
                f"and the third. {SPLICE_BAIT}.\n",
                {"semicolon"},
            ),
        )

    def test_a_sentence_closing_inside_emphasis_or_a_bracket_still_ends(self) -> None:
        """`.**` and `.)` end a sentence, and reading a bare `. ` joined a whole bullet into one."""
        for opener in ("- **A label: with a list, of two.**", "A label (with a list, of two.)"):
            with self.subTest(opener=opener):
                self.assertEqual(
                    ["semicolon"], self.kinds(f"{opener} {SPLICE_BAIT}.\n", {"semicolon"})
                )

    def test_a_series_in_one_sentence_does_not_exempt_the_next(self) -> None:
        """The second-separator arm is scoped the same way, a series belonging to its sentence."""
        self.assertEqual(
            ["semicolon"],
            self.kinds(
                "It covers each target, and excludes the rest; it runs on push; it gates. "
                f"{SPLICE_BAIT}.\n",
                {"semicolon"},
            ),
        )

    def test_a_colon_introduced_list_whose_items_carry_commas_keeps_its_semicolon(self) -> None:
        """The colon arm earns its place: dropping it flagged this, the use the rule names.

        Measured over the tree, dropping it reported 14 further lines, and the shapes below are
        what they were, so the arm is scoped rather than removed.
        """
        for text in (
            (
                "Match the heading style: title case with short bind words (a, an, the, of); "
                "hyphenated compounds capitalize both parts.\n"
            ),
            (
                "- **Python** (the script profile): lint, format, and type check; "
                "format-on-save and import organization via the formatter.\n"
            ),
        ):
            with self.subTest(text=text.split(":")[0]):
                self.assertEqual([], self.kinds(text, {"semicolon"}))

    def test_a_bullet_label_colon_inside_the_emphasis_is_the_same_opener(self) -> None:
        """`- **D3:**` and `- **D3**:` are one construct, and only one spelling was stripped."""
        self.assertEqual(
            ["semicolon"],
            self.kinds(
                "- **D3:** each run builds one branch, so it classifies the version directly; the "
                "gate literal, the expression, and the config all name the same branch.\n",
                {"semicolon"},
            ),
        )

    def test_an_abbreviation_does_not_end_a_sentence(self) -> None:
        """Splitting at `e.g.` cuts a list in half and flags the separator the exemption protects."""
        self.assertEqual(
            [],
            self.kinds(
                "Pinned by path: a script, a hook (e.g. a shebang); vanilla files stay as they are.\n",
                {"semicolon"},
            ),
        )

    def test_prose_rules_do_not_reach_code_files(self) -> None:
        """A shell script carries statement separators, not prose, until comments can be extracted."""
        for name in ("bait.sh", "bait.py", "bait.yml"):
            with self.subTest(name=name):
                self.assertEqual(
                    [], self.kinds("a=1; b=2; it runs\n", {"semicolon", "dash"}, name=name)
                )


class TestCommentWrap(BaitCase):
    """The comment rule reaches every syntax the fleet's project types carry, not only the hash ones."""

    RUN_ON = "One thing happens. Another thing happens."

    def flag(self, name: str, text: str) -> list[str]:
        """Both comment kinds, so a case cannot pass by asking for the rule it does not test."""
        return self.kinds(text, {"comment-wrap", "comment-case"}, name=name)

    def test_a_run_on_is_caught_in_every_comment_syntax(self) -> None:
        """One case per syntax, so a failure names the language whose extractor broke."""
        for name, text in (
            ("a.cs", f"// {self.RUN_ON}\n"),
            ("a.cs", f"/* {self.RUN_ON} */\n"),
            ("a.cpp", f"// {self.RUN_ON}\n"),
            ("a.c", f"/* {self.RUN_ON} */\n"),
            ("a.py", f"x = 1  # {self.RUN_ON}\n"),
            ("a.sh", f"# {self.RUN_ON}\n"),
            ("a.ps1", f"<# {self.RUN_ON} #>\n"),
            ("a.yml", f"# {self.RUN_ON}\n"),
            ("a.toml", f"# {self.RUN_ON}\n"),
            ("a.ini", f"; {self.RUN_ON}\n"),
            ("a.jsonc", f"// {self.RUN_ON}\n"),
            ("a.json", f"// {self.RUN_ON}\n"),
            ("a.code-workspace", f"// {self.RUN_ON}\n"),
            ("a.xml", f"<!-- {self.RUN_ON} -->\n"),
            ("a.csproj", f"<!-- {self.RUN_ON} -->\n"),
        ):
            with self.subTest(file=name, syntax=text.strip()[:12]):
                self.assertEqual(["comment-wrap"], self.flag(name, text))

    def test_a_step_marker_is_not_a_sentence_terminator(self) -> None:
        """A numbered step opening a comment is a label on the sentence after it, not a sentence.

        Reading the marker's dot as a terminator made `# 1. Deploy the hook.` two sentences, so
        every numbered step in the fleet's scripts reported as a run-on. The marker is stripped
        before the sentence checks, which also lets comment-case see the real opening word.
        """
        for marker in ("1.", "2.", "10.", "1)", "3)"):
            with self.subTest(marker=marker):
                self.assertEqual([], self.flag("a.sh", f"# {marker} Deploy the hook.\n"))

    def test_a_step_marker_does_not_hide_a_real_run_on(self) -> None:
        """Stripping the marker must not stop the rule seeing what follows it."""
        self.assertEqual(["comment-wrap"], self.flag("a.sh", f"# 1. {self.RUN_ON}\n"))

    def test_an_ellipsis_is_not_a_sentence_terminator(self) -> None:
        """An ellipsis marks an elision inside one sentence, so its closing dot does not end one.

        Reading it as a terminator made a schematic comment two sentences, and the split the rule
        then asked for would have broken the fragment the line exists to show.
        """
        self.assertEqual([], self.flag("a.py", "# .editorconfig: [glob] ... end_of_line = lf\n"))

    def test_an_ellipsis_does_not_hide_a_real_run_on(self) -> None:
        """The guard is one dot wide, so a terminator later on the line is still caught."""
        self.assertEqual(
            ["comment-wrap"],
            self.flag("a.py", "# Take the first ... and the rest. Another sentence.\n"),
        )

    def test_an_ellipsis_before_a_question_or_bang_still_terminates(self) -> None:
        """The guard covers the dot alternative only, since `?` and `!` after an ellipsis do end a sentence.

        Guarding the whole terminator class would have read these as one sentence, because the `?` and
        the `!` are each preceded by the ellipsis' closing dot.
        """
        for terminator in ("?", "!"):
            with self.subTest(terminator=terminator):
                self.assertEqual(
                    ["comment-wrap"], self.flag("a.py", f"# Really...{terminator} Yes it does.\n")
                )

    def test_a_step_marker_does_not_hide_a_lowercase_opening(self) -> None:
        """Before the strip, the digit read as the opening character, so comment-case never fired."""
        self.assertEqual(["comment-case"], self.flag("a.sh", "# 1. deploy the hook.\n"))

    def test_a_decimal_is_not_a_step_marker(self) -> None:
        """The marker pattern requires trailing whitespace, so a version or decimal is untouched."""
        self.assertEqual([], self.flag("a.sh", "# 3.13 is the pinned interpreter.\n"))

    def test_json_carries_comments_because_jsonc_is_what_ships(self) -> None:
        """VS Code tasks, devcontainer, and workspace files ship comments under a plain .json name."""
        for name in ("a.json", "a.code-workspace", "a.jsonc"):
            with self.subTest(file=name):
                self.assertEqual(["comment-wrap"], self.flag(name, f"// {self.RUN_ON}\n"))

    def test_a_marker_inside_a_string_is_not_a_comment(self) -> None:
        for name, text in (
            ("a.cs", 'var s = "// no. Really.";\n'),
            ("a.sh", 'echo "# no. Really."\n'),
            ("a.json", '{"url": "https://x/y. Z"}\n'),
            ("a.py", 'u = "http://x/#f. G"\n'),
        ):
            with self.subTest(file=name):
                self.assertEqual([], self.flag(name, text))

    def test_a_comment_that_is_only_a_uri_is_a_reference_not_a_sentence(self) -> None:
        """It cannot be capitalized or restructured without corrupting the address it carries.

        A reference block opening a config file is the ordinary shape, so before this exemption
        every repo carrying one inherited a finding no edit could answer.
        """
        for name, text in (
            ("a.yml", "# https://docs.github.com/en/code-security/dependabot\n"),
            (".editorconfig", "; https://editorconfig.org\n"),
            ("a.cs", "// http://example.com/a_b.c\n"),
            ("a.xml", "<!-- https://example.com/schema -->\n"),
            ("a.yml", "# <https://example.com/bracketed>\n"),
            ("a.sh", "# ftp://example.com/pub\n"),
        ):
            with self.subTest(file=name, comment=text.strip()):
                self.assertEqual([], self.flag(name, text))

    def test_a_uri_block_does_not_make_the_next_line_a_continuation(self) -> None:
        """Consecutive reference lines are separate addresses, not one sentence wrapping."""
        self.assertEqual(
            [], self.flag("a.yml", "# https://example.com/one\n# https://example.com/two\n")
        )

    def test_the_scheme_is_case_insensitive(self) -> None:
        """RFC 3986 makes the scheme case-insensitive, so an uppercase one is the same reference.

        The continuation case is the one that matters: a scheme the exemption misses puts the
        reference line back in the wrap logic, which is the false positive this exemption removes.
        """
        self.assertEqual(
            [], self.flag("a.yml", "# HTTPS://example.com/one\n# Describes the format.\n")
        )
        self.assertEqual([], self.flag("a.yml", "# Https://example.com/one\n"))

    def test_an_unbalanced_angle_bracket_is_not_a_delimited_uri(self) -> None:
        """One bracket is a typo rather than a delimiter, so it is reported instead of exempted."""
        for body in ("<https://example.com/one", "https://example.com/one>"):
            with self.subTest(body=body):
                self.assertFalse(prose_lint.BARE_URI.match(body))
        self.assertTrue(prose_lint.BARE_URI.match("<https://example.com/one>"))

    def test_a_uri_inside_a_sentence_is_still_prose(self) -> None:
        """The whole body has to be the address, or the exemption would swallow real prose."""
        self.assertEqual(
            ["comment-wrap"], self.flag("a.yml", f"# See https://example.com. {self.RUN_ON}\n")
        )
        self.assertEqual(
            ["comment-case"], self.flag("a.yml", "# see https://example.com for the options\n")
        )

    def test_a_documentation_comment_is_left_to_codestyle(self) -> None:
        """An XML doc comment and a docstring may run to paragraphs, which CODESTYLE governs."""
        self.assertEqual([], self.flag("a.cs", f"/// <summary>{self.RUN_ON}</summary>\n"))
        self.assertEqual([], self.flag("a.py", f'"""{self.RUN_ON}"""\n'))

    def test_a_closed_block_doc_gives_the_rest_of_the_line_back(self) -> None:
        """CODESTYLE owns the documentation comment, not the line it happens to sit on.

        A line doc comment does run to end of line, so only the block form gives anything back.
        """
        self.assertEqual([], self.flag("a.cs", f"/** {self.RUN_ON} */\n"))
        self.assertEqual([], self.flag("a.cs", f"/// {self.RUN_ON} // and more\n"))
        self.assertEqual(["comment-wrap"], self.flag("a.cs", "/** Docs. */ // Two things. Here.\n"))

    def test_a_multi_line_doc_block_owns_every_line_until_it_closes(self) -> None:
        """A marker in documentation text is prose, so scanning those lines invents comments.

        The closing line still gives back what follows the closer, which is the one finding here.
        """
        self.assertEqual(
            ["comment-wrap"],
            self.flag(
                "a.cs",
                "/** Docs start\n"
                " * // Two things. Here.\n"
                " * /* not an opener\n"
                " */ // Two things. Here.\n",
            ),
        )

    def test_verbatim_rules_apply_to_the_double_quoted_form_only(self) -> None:
        """C# spells a verbatim string with double quotes, so `@` on a char literal is ordinary.

        Under verbatim rules the doubled quote is one escaped character and both are blanked,
        so counting what survives tells the two readings apart.
        """
        masked, _ = prose_lint.strip_strings("var c = @'a''b'; // t", "\"'", True)
        self.assertEqual(4, masked.count("'"))

    def test_a_verbatim_string_spans_lines(self) -> None:
        """It is the one string form here that carries, so masking per line invents comments.

        The line that closes it still gives back what follows the quote.
        """
        # The marker on the second line is string content, so nothing is reported.
        self.assertEqual(
            [], self.flag("a.cs", 'var s = @"line one\n// Two things. Here.\nline three";\n')
        )
        # The line that closes it still gives back the comment after the quote.
        self.assertEqual(
            ["comment-wrap"],
            self.flag("a.cs", 'var s = @"line one\nline two"; // Two things. Here.\n'),
        )
        # A plain string ends on its own line, so the next line is ordinary code.
        self.assertEqual(
            ["comment-wrap"], self.flag("a.cs", 'var s = "line one";\n// Two things. Here.\n')
        )
        # Closing one and opening another leaves real code between them, which is not string content.
        self.assertEqual(
            ["comment-wrap"],
            self.flag(
                "a.cs",
                'var s = @"start\n'
                'end"; /* Two things. Here. */ var t = @"open again\n'
                'still string";\n',
            ),
        )

    def test_a_quote_in_comment_text_is_prose_rather_than_a_string(self) -> None:
        """Masking the comment too lets its quote open a string that blanks the markers after it.

        Within the line that costs the block its closer, and across lines the state carries and
        blanks every marker below until something closes it.
        """
        self.assertEqual(
            ["comment-wrap"],
            self.flag("a.cs", 'code(); /* note @"x */ code2(); // Two things. Here.\n'),
        )
        # Each block line is its own sentence, so the two findings are the recovered comments.
        self.assertEqual(
            ["comment-wrap", "comment-wrap"],
            self.flag(
                "a.cs",
                '/* A note about @"paths.\n'
                "   And more. */ // Two things. Here.\n"
                "var x = 1; // Two things. Here.\n",
            ),
        )

    def test_only_a_c_style_continuation_loses_its_leading_asterisk(self) -> None:
        """The `*` continuing a `/* */` line is punctuation, and anywhere else it is prose.

        Taking it off an emphasis marker leaves a lowercase opening that the case rule reports,
        which is the rule judging text the extractor damaged.
        """
        for name, text in (
            ("a.md", "<!-- *emphasis* leads here -->\n"),
            ("a.ps1", "<# *emphasis* leads here #>\n"),
            ("a.cs", "/* *emphasis* leads here */\n"),
        ):
            with self.subTest(file=name):
                self.assertEqual([], self.flag(name, text))
        # The convention still holds on the lines it was written for.
        self.assertEqual(
            [(1, "Start here.", True), (2, "Still going.", True)],
            prose_lint.extracted_comments(Path("a.cs"), ["/* Start here.", " * Still going. */"]),
        )
        # The marker is one `*` against whitespace, so a continuation keeps its own emphasis.
        for text, body in (
            (" * **bold** here */", "**bold** here"),
            (" **bold** here */", "**bold** here"),
            (" *emphasis* here */", "*emphasis* here"),
        ):
            with self.subTest(line=text):
                self.assertEqual(
                    [(1, "Start.", True), (2, body, True)],
                    prose_lint.extracted_comments(Path("a.cs"), ["/* Start.", text]),
                )

    def test_a_format_with_no_comment_syntax_is_skipped(self) -> None:
        for name in ("a.lock", "a.csv", "a.txt"):
            with self.subTest(file=name):
                self.assertEqual([], self.flag(name, f"// {self.RUN_ON}\n"))

    def test_a_wrapped_sentence_is_caught_and_adjacency_is_required(self) -> None:
        """Two comments with code between them are separate, not one wrapped sentence."""
        self.assertEqual(
            ["comment-wrap"],
            self.flag("a.py", "# A sentence that keeps\n# going onto the next line.\n"),
        )
        self.assertEqual([], self.flag("a.py", "# A label here\nx = 1\n# Another label\n"))

    def test_machinery_and_abbreviations_are_not_prose(self) -> None:
        for text in (
            "#!/usr/bin/env python3\n",
            "# ------------\n",
            "# noqa: S603 - fixed argv\n",
            "# Uses e.g. Docker and i.e. Podman here.\n",
            "# Bump to 3.13 for the runner.\n",
            "# See audit.py and validate.py for this.\n",
        ):
            with self.subTest(text=text.strip()[:30]):
                self.assertEqual([], self.flag("a.py", text))

    def test_a_sentence_opening_in_lowercase_is_flagged(self) -> None:
        """A lowercase opening reads as the continuation of the line above it."""
        self.assertEqual(["comment-case"], self.flag("a.py", "# details are allowed here.\n"))

    def test_a_genuine_continuation_is_not_a_case_error(self) -> None:
        """A wrapped sentence is one finding, not two: the lowercase start is expected there."""
        self.assertEqual(
            ["comment-wrap"],
            self.flag("a.py", "# A sentence that keeps\n# going onto the next line.\n"),
        )

    def test_a_commented_out_key_is_not_a_sentence(self) -> None:
        """`# ignore:` heading a disabled block is configuration, and capitalizing it breaks the key.

        The rule pointed at a codecov snippet whose commented-out `ignore:` block a repo uncomments,
        so following the finding would have corrupted the file the exemption exists to protect.
        """
        for text in ('# ignore:\n#   - "Sandbox/**"\n', "# Outputs:\n"):
            with self.subTest(text=text.splitlines()[0]):
                self.assertEqual([], self.flag("a.yml", text))

    def test_a_colon_ending_real_prose_is_still_judged(self) -> None:
        """The exemption is one token wide, since prose closing on a colon has words before it."""
        self.assertEqual(["comment-case"], self.flag("a.yml", "# the outputs are these:\n"))

    def test_a_label_opening_a_definition_keeps_its_name(self) -> None:
        """`publish` names the output being documented, so capitalizing it renames what ships."""
        for text in (
            "#   publish - 'true' when this run should publish.\n",
            "#   stable  - 'true' when the target branch is main.\n",
            "# payload-file - create-or-update the ruleset by name.\n",
        ):
            with self.subTest(text=text.strip()[:30]):
                self.assertEqual([], self.flag("a.yml", text))

    def test_a_continuation_dash_is_not_read_as_a_label(self) -> None:
        """A wrapped line whose first word takes a spaced dash is a parenthetical, not a definition.

        Two live instances in the tree have this shape, and exempting them would have hidden the
        very construction the dash rule exists to catch. The label test is scoped to a line that
        opens a definition, so a continuation never reaches it.
        """
        self.assertEqual(
            ["comment-wrap"],
            self.flag(
                "a.yml",
                "# It needs every other\n# build - a target disabled on a smoke PR - does not.\n",
            ),
        )

    def test_a_label_does_not_hide_a_run_on(self) -> None:
        """The exemption answers the opening word only, so the sentence checks still see the prose."""
        self.assertEqual(["comment-wrap"], self.flag("a.yml", f"# publish - {self.RUN_ON}\n"))

    def test_a_capitalized_opening_and_a_code_token_are_both_accepted(self) -> None:
        """A backticked identifier does not open in lowercase, so it needs no restructuring."""
        for text in ("# The details element is allowed.\n", "# `ruff format` runs first.\n"):
            with self.subTest(text=text.strip()):
                self.assertEqual([], self.flag("a.py", text))

    def test_a_sentence_ending_in_an_acronym_is_still_two_sentences(self) -> None:
        """The initial guard anchored on any capital, and this codebase ends sentences in acronyms."""
        for text in (
            "# The check runs in CI. Another thing happens.\n",
            "# Pinned by SHA. Dependabot still bumps it.\n",
        ):
            with self.subTest(text=text.strip()[:40]):
                self.assertEqual(["comment-wrap"], self.flag("a.py", text))

    def test_a_second_sentence_may_open_in_either_case(self) -> None:
        """A lowercase opening is still a second sentence on the line."""
        self.assertEqual(
            ["comment-wrap"], self.flag("a.py", "# One thing happens. another thing happens.\n")
        )

    def test_an_initial_is_one_name_rather_than_two_sentences(self) -> None:
        """`J. Smith` is the case the guard exists for, and it must survive the widening."""
        self.assertEqual([], self.flag("a.py", "# Reviewed by J. Smith today.\n"))

    def test_a_trailing_comment_can_start_a_wrapped_sentence(self) -> None:
        """Clearing the predecessor on a trailing comment reported the wrong rule, not merely fewer.

        The pair below is a wrapped sentence, and it was reported as a capitalization error, whose
        advice would have been to capitalize the continuation rather than to un-wrap it.
        """
        self.assertEqual(
            ["comment-wrap"],
            self.flag("a.py", "x = 1  # a sentence that keeps\n# going onto the next line.\n"),
        )

    def test_a_trailing_annotation_does_not_continue_the_line_above(self) -> None:
        """A trailing comment annotates its own line, so it cannot be a continuation."""
        self.assertEqual([], self.flag("a.py", "x = 1  # a thing that\ny = 2  # continues\n"))
        self.assertEqual(
            [], self.flag("a.py", "x = 1  # count of items\n# Another thing entirely.\n")
        )

    def test_a_comment_inside_a_fenced_block_is_skipped(self) -> None:
        """A fenced example is quoted code, so its comments belong to whatever is being shown."""
        self.assertEqual(
            [], self.flag("a.md", "Prose.\n\n```html\n<!-- One thing. Another thing. -->\n```\n")
        )
        self.assertEqual(
            ["comment-wrap"], self.flag("a.md", "Prose.\n\n<!-- One thing. Another thing. -->\n")
        )

    def test_an_unpunctuated_markdown_marker_is_a_label_not_a_sentence(self) -> None:
        """A tool matches these verbatim, so a capital or a split would break what reads them.

        The reference-link group headers, the ToC-omit directive, and the `agent-safety` install
        markers all open lowercase or sit adjacent, which reads as a sentence that failed to
        start or as one wrapping into the next.
        """
        for marker in (
            "<!-- omit from toc -->",
            "<!-- agent-safety v1 start -->",
            "<!-- Shields -->",
        ):
            with self.subTest(marker=marker):
                self.assertEqual([], self.flag("a.md", f"Prose.\n\n{marker}\n"))
        # Adjacent markers must not read as one sentence wrapping into the next.
        self.assertEqual(
            [], self.flag("a.md", "Prose.\n\n<!-- Shields -->\n<!-- agent-safety v1 start -->\n")
        )
        # A punctuated HTML comment is commentary, so it stays judged as prose.
        self.assertEqual(
            ["comment-wrap"], self.flag("a.md", "Prose.\n\n<!-- One thing. Another thing. -->\n")
        )
        # Outside Markdown the carve-out does not apply, since there the marker case does not arise.
        self.assertEqual(["comment-case"], self.flag("a.py", "# lowercase opening\n"))

    def test_a_block_opener_inside_a_line_comment_is_text(self) -> None:
        """Read as a real opener it opens a block, and the code lines below are linted as prose.

        The documentation form is the same case: exempting it from linting must not leave the
        ceiling unbounded, or the syntax whose doc marker is a line comment reopens the defect.
        """
        for name, text in (
            ("a.cs", "// Match a /* opener here\nvar x = 1; // Two things. Here.\n"),
            ("a.cs", "/// See a /* opener here\nvar x = 1; // Two things. Here.\n"),
            ("a.ps1", "# Match a <# opener here\n$x = 1 # Two things. Here.\n"),
        ):
            with self.subTest(file=name, line=text.split("\n")[0]):
                self.assertEqual(["comment-wrap"], self.flag(name, text))

    def test_every_comment_on_a_line_is_read_not_just_the_first(self) -> None:
        """A ceiling can only describe the first comment, so a later one was unreachable.

        Each case puts the offending sentence in the second comment, which a scan that stops at
        the first reports as clean.
        """
        for name, text in (
            ("a.cs", "var x = 1; /* Note. */ // Two things. Here.\n"),
            ("a.cs", "/* Note. */ /* Two things. Here. */\n"),
            ("a.cs", "/* Start here.\n   Still going. */ // Two things. Here.\n"),
        ):
            with self.subTest(line=text.split("\n")[0]):
                self.assertEqual(["comment-wrap"], self.flag(name, text))

    def test_a_verbatim_string_keeps_its_own_closing_quote(self) -> None:
        """A backslash is ordinary inside one and a doubled quote is the escape.

        Read with C escape rules the string never closes, so the masker blanks the rest of the
        line and the trailing comment goes unseen.
        """
        # Ending in a backslash, the string swallows its closing quote and hides a real comment.
        self.assertEqual(
            ["comment-wrap"], self.flag("a.cs", 'var p = @"C:\\tmp\\"; // Two things. Here.\n')
        )
        # Reading a doubled quote as a close then a reopen puts string content outside the string.
        self.assertEqual(
            [], self.flag("a.cs", 'var s = @"a""// One thing. Another thing.""b"; // ok\n')
        )
        # An interpolated one is spelled either way round, and only one of them abuts the quote.
        for text in (
            'var s = $@"C:\\tmp\\"; // Two things. Here.\n',
            'var s = @$"C:\\tmp\\"; // Two things. Here.\n',
        ):
            with self.subTest(line=text.strip()):
                self.assertEqual(["comment-wrap"], self.flag("a.cs", text))

    def test_only_the_syntax_that_has_verbatim_strings_gets_them(self) -> None:
        """C shares the C-like spec without the form, so `@` there is an ordinary character."""
        self.assertTrue(prose_lint.SYNTAX[".cs"]["verbatim"])
        self.assertFalse(prose_lint.SYNTAX[".c"]["verbatim"])
        self.assertFalse(prose_lint.SYNTAX[".json"]["verbatim"])
        # The C escape still hides a marker, which is what the verbatim rule must not undo.
        self.assertEqual(
            ["comment-wrap"], self.flag("a.cs", 'var s = "a\\"b"; // Two things. Here.\n')
        )

    def test_css_has_block_comments_only(self) -> None:
        """A `//` in CSS is the scheme separator of a URL, not a comment marker."""
        self.assertEqual([], self.flag("a.css", "a { background: url(http://x/y. Z); }\n"))
        self.assertEqual(["comment-wrap"], self.flag("a.css", "/* One thing. Another thing. */\n"))

    def test_a_version_pin_is_machinery_rather_than_prose(self) -> None:
        """The action-pinning rule requires a trailing `# vX.Y.Z`, which is a label, not a sentence."""
        self.assertEqual([], self.flag("a.yml", "  uses: x@sha # v7.0.0\n"))
        self.assertEqual([], self.flag("a.yml", "  uses: x@sha # v3\n"))

    def test_the_syntax_table_covers_a_plausible_number_of_extensions(self) -> None:
        """A table that shrank would make every case above pass by having nothing to dispatch on."""
        self.assertGreaterEqual(len(prose_lint.SYNTAX), 25)
        for label in (
            ".cs",
            ".cpp",
            ".py",
            ".sh",
            ".yml",
            ".json",
            ".jsonc",
            ".xml",
            ".ps1",
            ".ini",
        ):
            with self.subTest(ext=label):
                self.assertIsNotNone(prose_lint.syntax_for(Path(f"x{label}")))


class TestMultiLineStrings(BaitCase):
    """A string that spans lines hides its markers on every line it covers, not only the first.

    Masking a line at a time leaves the lines below readable, so the comment rules report on string
    content and ask a reader to edit text that is data. Each case below puts the bait sentence
    inside a spanning string and asserts nothing is reported, then puts a real comment after the
    line that closes it and asserts that one still is - a carry that never releases would swallow
    the rest of the file, which reads exactly like a clean pass.
    """

    BAIT = "Two things. Here."

    def flag(self, name: str, text: str) -> list[str]:
        return self.kinds(text, {"comment-wrap", "comment-case"}, name=name)

    def test_a_shell_quote_that_does_not_close_spans_lines(self) -> None:
        """Either quote form carries, and the line that closes it gives back what follows."""
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                self.assertEqual(
                    [], self.flag("a.sh", f"s={quote}line one\n# {self.BAIT}\nline three{quote}\n")
                )
                self.assertEqual(
                    ["comment-wrap"],
                    self.flag("a.sh", f"s={quote}line one\nline two{quote}  # {self.BAIT}\n"),
                )

    def test_a_shell_single_quoted_string_takes_no_backslash_escape(self) -> None:
        """Read with C escape rules the trailing backslash eats the closing quote.

        The string would then carry into every line below it, which is the failure the C# verbatim
        form already had within a line, one newline further on.
        """
        self.assertEqual(["comment-wrap"], self.flag("a.sh", f"p='C:\\tmp\\'\n# {self.BAIT}\n"))

    def test_a_shell_single_quoted_string_is_neither_doubling_nor_escaped(self) -> None:
        """It takes no escape at all and cannot embed its own delimiter, so `'a''b'` is two strings.

        Declaring it doubling would state something false about the language. It happens to mask
        the same either way, since doubling and plain toggling agree on whether a run of quotes
        leaves a string open, so counting the quotes that survive is what tells the readings apart.
        """
        masked, carry = prose_lint.strip_strings(
            "echo 'a''b'",
            "\"'",
            False,
            prose_lint.CLEAR,
            prose_lint.SHELL["raw"],
            prose_lint.SHELL["escape"],
            prose_lint.SHELL["escape_in"],
            prose_lint.SHELL["escape_out"],
        )
        self.assertEqual(4, masked.count("'"))
        self.assertEqual("", carry.kind)
        self.assertEqual("", prose_lint.SHELL["raw"])

    def test_a_shell_backslash_outside_a_string_escapes_the_next_character(self) -> None:
        r"""`'\''` is how shell embeds a quote in a single-quoted string, and it balances.

        Read without the outside-string escape it leaves one quote open, which then carries and
        blanks every line below it - a rule that reads nothing reports nothing.
        """
        self.assertEqual(["comment-wrap"], self.flag("a.sh", f"echo 'don'\\''t'\n# {self.BAIT}\n"))

    def test_a_heredoc_runs_from_its_label_to_the_line_that_repeats_it(self) -> None:
        """Bare, quoted, and tab-stripping openers all name the same label."""
        for opener in ("<<EOF", "<<'EOF'", '<<"EOF"', "<<-EOF", "<< EOF"):
            with self.subTest(opener=opener):
                self.assertEqual(
                    ["comment-wrap"],
                    self.flag("a.sh", f"cat {opener}\n# {self.BAIT}\nEOF\n# {self.BAIT}\n"),
                )

    def test_only_the_exact_terminator_ends_a_heredoc(self) -> None:
        """An indented line is body content, and ending there resumes scanning inside the string.

        `<<-` strips leading tabs and nothing else, so a space-indented line is body under either
        form. The bait sits on the line after the near-miss, which a premature end reports.
        """
        for opener, near_miss in (
            ("<<EOF", "  EOF"),
            ("<<EOF", "\tEOF"),
            ("<<-EOF", "  EOF"),
            ("<<EOF", "EOF_NOT"),
        ):
            with self.subTest(opener=opener, near_miss=repr(near_miss)):
                self.assertEqual(
                    [], self.flag("a.sh", f"cat {opener}\n{near_miss}\n# {self.BAIT}\n")
                )

    def test_a_tab_indented_terminator_ends_a_dash_heredoc(self) -> None:
        """That is the whole point of `<<-`, so refusing it would run the heredoc to end of file."""
        self.assertEqual(
            ["comment-wrap"], self.flag("a.sh", f"cat <<-EOF\n\tbody\n\tEOF\n# {self.BAIT}\n")
        )

    def test_heredocs_stacked_on_one_line_are_read_in_order(self) -> None:
        """Each body belongs to its own label, so clearing the first must open the second."""
        self.assertEqual(
            ["comment-wrap"],
            self.flag("a.sh", f"cat <<A <<B\n# {self.BAIT}\nA\n# {self.BAIT}\nB\n# {self.BAIT}\n"),
        )

    def test_a_here_string_and_a_quoted_marker_do_not_open_a_heredoc(self) -> None:
        """`<<<` is one line, and a `<<` inside a string or a comment is text."""
        for line in ('jq -r ".x" <<<"$out"', 'echo "cat <<EOF"', "# Match a <<EOF here"):
            with self.subTest(line=line):
                self.assertEqual(["comment-wrap"], self.flag("a.sh", f"{line}\n# {self.BAIT}\n"))

    def test_a_powershell_here_string_spans_lines_in_both_quote_forms(self) -> None:
        """It opens on `@"` or `@'` at the end of a line and closes on the matching token."""
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                self.assertEqual(
                    [], self.flag("a.ps1", f"$s = @{quote}\n# {self.BAIT}\n{quote}@\n")
                )
                # The closing line gives back what follows the token, as a closing quote does.
                self.assertEqual(
                    ["comment-wrap"],
                    self.flag("a.ps1", f"$s = @{quote}\nbody\n{quote}@  # {self.BAIT}\n"),
                )

    def test_a_powershell_double_quoted_string_is_escaped_and_doubling_at_once(self) -> None:
        """Its escape is a backtick, and it also embeds the delimiter by doubling it.

        Neither property implies the other, so both are read per string. Missing the backtick ends
        the string on the escaped quote, and reading a backslash as an escape consumes the closing
        one. The single-quoted form takes no backtick escape, so its escape is doubling only.
        """
        for code in ('$s = "a`"b"', '$s = "a""b"', "$s = 'a`'", "$s = 'a''b'", '$p = "C:\\tmp\\"'):
            with self.subTest(code=code):
                self.assertEqual(["comment-wrap"], self.flag("a.ps1", f"{code}  # {self.BAIT}\n"))

    def test_a_here_string_closes_only_on_a_token_at_the_start_of_the_line(self) -> None:
        """PowerShell requires the closer at column 0, so an indented one is here-string content."""
        self.assertEqual(
            [], self.flag("a.ps1", f'$s = @"\n  "@ is not the closer\n# {self.BAIT}\n"@\n')
        )

    def test_a_powershell_quote_carries_because_the_escape_is_a_backtick(self) -> None:
        """A backslash is an ordinary character there, so it cannot consume the closing quote."""
        self.assertEqual([], self.flag("a.ps1", f'$s = "line one\n# {self.BAIT}\nline three"\n'))
        self.assertEqual(
            ["comment-wrap"], self.flag("a.ps1", '$p = "C:\\tmp\\"  # ' + self.BAIT + "\n")
        )

    def test_a_yaml_block_scalar_is_data_until_the_indentation_drops(self) -> None:
        """Every header form opens one, and the line that dedents is code again."""
        for header in ("key: |", "key: >", "key: |-", "key: >-", "key: |2", "  - |"):
            with self.subTest(header=header):
                self.assertEqual(
                    ["comment-wrap"],
                    self.flag(
                        "a.yml",
                        f"{header}\n    # {self.BAIT}\n\n    still data\nnext: 1  # {self.BAIT}\n",
                    ),
                )

    def test_a_run_scalar_stays_a_script_the_comment_rules_govern(self) -> None:
        """Its `#` lines are shell comments, so treating the block as data would stop linting them.

        A data key holds text the reader cannot edit, which is the case the block rule is for.
        The chomping and indent indicators ride along on the header, so every form of it counts.
        """
        for header in (
            "run: |",
            "run: |-",
            "run: |+",
            "run: |2",
            "run: >",
            "run: >-",
            "      run: |",
            "      - run: |",
        ):
            with self.subTest(header=header):
                self.assertEqual(
                    ["comment-wrap"], self.flag("a.yml", f"{header}\n        # {self.BAIT}\n")
                )
        # The exemption is anchored to the key, so a key merely ending in the word is still data.
        for header in (
            "files: |",
            "files: |-",
            "tags: >-",
            "dry run: |",
            "first run: |-",
            "post-run: |",
        ):
            with self.subTest(header=header):
                self.assertEqual([], self.flag("a.yml", f"{header}\n  # {self.BAIT}\n"))

    def test_a_pipe_that_is_not_a_block_header_opens_nothing(self) -> None:
        """A plain scalar ending in a pipe is a value, not a block indicator."""
        self.assertEqual(["comment-wrap"], self.flag("a.yml", f"key: a | b\n# {self.BAIT}\n"))

    def test_a_yaml_quote_delimits_only_at_the_start_of_a_value(self) -> None:
        """A plain scalar's apostrophe is text, so reading it as an opener hides a real comment.

        The masked span would run from the apostrophe to the end of the line, taking the trailing
        `#` with it, and this repo's own workflow YAML carries the pattern (`the merge-bot's ...`).
        A quote that does open one is preceded by the `:`, the sequence dash, or a flow indicator.
        """
        for code in (
            "key: don't",
            "name: Don't do this",
            "key: [don't, 'x']",
            "key: 'quoted'",
            "  - 'item'",
            'key: "don\'t"',
            "key: {k: 'v'}",
        ):
            with self.subTest(code=code):
                self.assertEqual(["comment-wrap"], self.flag("a.yml", f"{code}  # {self.BAIT}\n"))

    def test_a_quote_still_opens_anywhere_in_a_syntax_with_no_bare_words(self) -> None:
        """The restriction is scoped to YAML, the one syntax here whose values can go unquoted.

        A C# string opens after `=`, `(`, or `,`, none of which YAML's set contains, so applying the
        restriction everywhere would stop masking C# strings entirely. The masked span is what shows
        it, since no C# comment marker fits inside a char literal to hide.
        """
        self.assertEqual("", prose_lint.SYNTAX[".cs"]["quote_after"])
        self.assertTrue(prose_lint.SYNTAX[".yml"]["quote_after"])
        masked, _ = prose_lint.strip_strings('var s = "// no";', "\"'")
        self.assertEqual('var s = "     ";', masked)
        self.assertEqual(["comment-wrap"], self.flag("a.cs", f"var s = 'a';  // {self.BAIT}\n"))
        self.assertEqual([], self.flag("a.cs", f'var s = "// {self.BAIT}";\n'))

    def test_a_single_quoted_yaml_or_toml_scalar_takes_no_backslash_escape(self) -> None:
        r"""A trailing backslash would otherwise consume the closing quote and hide the comment.

        Both languages spell the escape as a doubled quote in the single-quoted form, and both
        keep the backslash escape in the double-quoted one, so the two forms are read differently
        in the same file. The bait is a trailing comment, which only survives a correct read.
        """
        for name, code in (
            ("a.yml", "key: 'C:\\tmp\\'"),
            ("a.yml", "key: 'it''s'"),
            ("a.toml", "s = 'C:\\tmp\\'"),
            ("a.toml", "s = 'it''s'"),
        ):
            with self.subTest(file=name, code=code):
                self.assertEqual(["comment-wrap"], self.flag(name, f"{code}  # {self.BAIT}\n"))
        # The double-quoted form keeps it, so an escaped quote does not close the string early.
        for name, code in (("a.yml", 'key: "a\\"b"'), ("a.toml", 's = "a\\"b"')):
            with self.subTest(file=name, code=code):
                self.assertEqual(["comment-wrap"], self.flag(name, f"{code}  # {self.BAIT}\n"))

    def test_a_form_carries_only_in_the_syntax_that_has_it(self) -> None:
        """A YAML plain scalar's apostrophe is not a string, and a TOML file has no heredoc.

        Carrying either would blank every line below it, and a rule that reads nothing reports
        nothing, so the file would go quiet rather than fail.
        """
        for name, opener in (
            ("a.yml", "key: don't"),
            ("a.yml", 'key: "unclosed'),
            ("a.toml", "s = 'unclosed"),
            ("a.toml", "cat <<EOF"),
            ("a.ini", "k = don't"),
            ("a.json", '{"a": "unclosed'),
        ):
            with self.subTest(file=name, opener=opener):
                self.assertEqual(
                    ["comment-wrap"], self.flag(name, f"{opener}\n# {self.BAIT}\n// {self.BAIT}\n")
                )

    def test_every_declared_carry_kind_is_one_the_extractor_implements(self) -> None:
        """A typo in a `carry` set would silently disable the form it was meant to turn on."""
        implemented = {"quote", "verbatim", "here", "label", "block"}
        for label, spec in sorted(prose_lint.SYNTAX.items()):
            with self.subTest(ext=label):
                self.assertLessEqual(set(spec["carry"]), implemented)
                # A raw quote has to be one the syntax reads as a quote in the first place.
                self.assertLessEqual(set(spec["raw"]), set(spec["quotes"]))
                # An escape character that is also a quote would never reach the escape branch.
                self.assertEqual(set(), set(spec["escape"]) & set(spec["quotes"]))
                # The escape works inside quotes the syntax actually has, and is one character.
                self.assertLessEqual(set(spec["escape_in"]), set(spec["quotes"]) | {'"', "'"})
                self.assertEqual(1, len(spec["escape"]))
                # A syntax with nowhere for its escape to apply would carry a dead field.
                self.assertTrue(spec["escape_in"] or spec["escape_out"])

    def test_the_carrying_syntaxes_are_still_wired_to_their_extensions(self) -> None:
        """Every case above dispatches on a suffix, so a rewired table would pass them vacuously."""
        for label, kind in (
            (".sh", "label"),
            (".bash", "quote"),
            (".yml", "block"),
            (".yaml", "block"),
            (".ps1", "here"),
            (".cs", "verbatim"),
        ):
            with self.subTest(ext=label):
                self.assertIn(kind, prose_lint.SYNTAX[label]["carry"])
        self.assertIn("label", prose_lint.BY_NAME["dockerfile"]["carry"])

    def test_a_string_opens_from_code_after_a_closed_block_comment(self) -> None:
        """The code a line holds is every span outside its comments, not only the first one.

        Reading only the code before the first marker misses an opener that sits after a block
        comment that closed on the same line. The here-string then falls back to an ordinary
        carried quote, which the first quote in its body closes, and scanning resumes inside it.
        """
        for opener in ('<# Note. #> $s = @"', '$s = @"'):
            with self.subTest(opener=opener):
                self.assertEqual(
                    [], self.flag("a.ps1", f'{opener}\nhe said "hi\n# {self.BAIT}\n"@\n')
                )

    def test_a_string_does_not_open_under_a_block_comment(self) -> None:
        """The block comment owns the lines below, so its text cannot open one."""
        self.assertEqual(
            ["comment-wrap", "comment-wrap"],
            self.flag("a.ps1", f'<# Note @"\n   {self.BAIT} #>\n$x = 1 # {self.BAIT}\n'),
        )


class TestDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_sweep_reaches_a_plausible_share_of_the_repo(self) -> None:
        """A sweep that quietly stops finding files satisfies every rule by having nothing to read."""
        found = prose_lint.discover([str(REPO)])
        self.assertGreaterEqual(len(found), prose_lint.LEAST_PLAUSIBLE)

    def test_no_discovered_path_is_git_ignored(self) -> None:
        """Scoping by `git ls-files` is what keeps generated trees out of the sweep."""
        found = prose_lint.discover([str(REPO)])
        r = subprocess.run(
            ["git", "-C", str(REPO), "check-ignore", "--stdin"],
            input="\n".join(str(p) for p in found),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual("", r.stdout.strip())

    def test_the_fallback_skips_generated_roots(self) -> None:
        """`git check-ignore` fails on exactly the machine with no git, so this path names them."""
        (self.tmp / ".mypy_cache").mkdir()
        (self.tmp / ".mypy_cache" / "cached.md").write_text(f"{DUP}\n", encoding="utf-8")
        (self.tmp / "authored.md").write_text("fine\n", encoding="utf-8")
        found = prose_lint.walk_paths(self.tmp)
        self.assertEqual(["authored.md"], sorted(p.relative_to(self.tmp).as_posix() for p in found))

    def test_empty_git_output_falls_back_rather_than_scanning_nothing(self) -> None:
        """An initialized but empty checkout exits 0 with no output.

        Reading that as an empty file set would scan nothing and report success, which is the
        shape of a gate that has silently stopped gating.
        """
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch.object(prose_lint.subprocess, "run", return_value=done):
            self.assertIsNone(prose_lint.tracked_paths(self.tmp))

    def test_a_dot_prefixed_path_keeps_its_prefix(self) -> None:
        """`lstrip` took a character set and ate the leading dot, so --diff never matched .github."""
        self.assertEqual(".github/workflows/x.yml", prose_lint.rel(Path(".github/workflows/x.yml")))
        self.assertEqual(".editorconfig", prose_lint.rel(Path(".editorconfig")))
        self.assertEqual("GOVERNANCE.md", prose_lint.rel(Path("./GOVERNANCE.md")))

    def test_a_subdirectory_root_yields_paths_that_exist(self) -> None:
        """`git ls-files` prints paths relative to its `-C` directory, not the repo top level.

        Review read it the other way round, which would make the join in tracked_paths produce
        broken paths. It does not, and the invariant is pinned here because passing `--full-name`
        would flip the behavior with nothing else to notice.
        """
        for root in (REPO / "spec", REPO / "scripts"):
            with self.subTest(root=root.name):
                found = prose_lint.discover([str(root)])
                self.assertGreaterEqual(len(found), 5)
                self.assertEqual([], [str(p) for p in found if not p.exists()])

    def test_an_explicit_file_argument_bypasses_discovery(self) -> None:
        """A single file has to be checkable directly, including one git does not track."""
        loose = self.tmp / "loose.md"
        loose.write_text("fine\n", encoding="utf-8")
        self.assertEqual([loose], prose_lint.discover([str(loose)]))

    def test_a_directory_git_cannot_describe_warns_and_walks_it(self) -> None:
        """Silently scanning nothing there would report a clean run over an unread tree."""
        (self.tmp / "authored.md").write_text("fine\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "tracked_paths", return_value=None),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            found = prose_lint.discover([str(self.tmp)])
        self.assertEqual(["authored.md"], [p.name for p in found])
        self.assertIn("falling back to a filesystem walk", err.getvalue())

    def test_an_excluded_path_is_dropped(self) -> None:
        (self.tmp / "keep.md").write_text("fine\n", encoding="utf-8")
        (self.tmp / "drop.md").write_text("fine\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "tracked_paths", return_value=None),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            found = prose_lint.discover([str(self.tmp)], ("drop.md",))
        self.assertEqual(["keep.md"], [p.name for p in found])

    def test_a_generated_tree_is_skipped_when_a_wider_scan_expands_into_it(self) -> None:
        """Its prose is the generator's, so a finding there names no edit an author can make."""
        (self.tmp / "authored.md").write_text("fine\n", encoding="utf-8")
        generated = self.tmp / "reports"
        generated.mkdir()
        (generated / "audit.md").write_text("fine\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "tracked_paths", return_value=None),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            found = prose_lint.discover([str(self.tmp)])
        self.assertEqual(["authored.md"], [p.name for p in found])

    def test_a_parent_directory_above_the_checkout_does_not_decide_generated(self) -> None:
        """The decision is repository-relative, so the filesystem path above it cannot leak in.

        Judged on the absolute path, a checkout under a parent named `reports` carried that
        parent into every file's parts, which read the whole scan as deliberately requested and
        put the repository's own generated tree back into an ordinary sweep.
        """
        repo = self.tmp / "reports" / "checkout"
        (repo / "reports").mkdir(parents=True)
        (repo / "authored.md").write_text("fine\n", encoding="utf-8")
        (repo / "reports" / "audit.md").write_text("fine\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "tracked_paths", return_value=None),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            found = prose_lint.discover([str(repo)])
        self.assertEqual(["authored.md"], [p.name for p in found])

    def test_naming_a_generated_tree_directly_still_reads_it(self) -> None:
        """The skip keeps a wide scan honest, and must not make the tree uncheckable."""
        generated = self.tmp / "reports"
        generated.mkdir()
        (generated / "audit.md").write_text("fine\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "tracked_paths", return_value=None),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            found = prose_lint.discover([str(generated)])
        self.assertEqual(["audit.md"], [p.name for p in found])
        loose = generated / "audit.md"
        self.assertEqual([loose], prose_lint.discover([str(loose)]))

    def test_an_unreadable_root_is_not_a_file_set(self) -> None:
        """`tracked_paths` answers None on the error paths, never an empty list read as clean."""
        with mock.patch.object(prose_lint.subprocess, "run", side_effect=OSError):
            self.assertIsNone(prose_lint.tracked_paths(self.tmp))
        failed = subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="")
        with mock.patch.object(prose_lint.subprocess, "run", return_value=failed):
            self.assertIsNone(prose_lint.tracked_paths(self.tmp))

    def test_an_unopenable_path_is_not_text(self) -> None:
        self.assertFalse(prose_lint.is_text(self.tmp / "absent.md"))

    def test_a_binary_file_is_not_scanned(self) -> None:
        blob = self.tmp / "payload.md"
        blob.write_bytes(DUP.encode() + b"\x00binary\n")
        self.assertFalse(prose_lint.is_text(blob))


class TestSentenceSplit(BaitCase):
    """One sentence per line, the Markdown counterpart of the comment-wrap rule."""

    def test_a_sentence_continuing_onto_the_next_line_is_flagged(self) -> None:
        self.assertEqual(
            ["sentence-split"],
            self.kinds("A sentence that keeps\ngoing onto the next line.\n", {"sentence-split"}),
        )

    def test_a_finished_sentence_does_not_continue(self) -> None:
        for text in (
            "One sentence.\nAnother sentence.\n",
            "A question?\nan answer follows.\n",
            "One sentence.\nA capital opens the next.\n",
        ):
            with self.subTest(text=text.split("\n")[1]):
                self.assertEqual([], self.kinds(text, {"sentence-split"}))

    def test_structure_is_not_a_wrapped_sentence(self) -> None:
        """A table row, a quote, a heading, and a link definition are not prose lines.

        A colon, a dash, or a pipe at the end of the previous line introduces what follows it,
        so the next line starts a new construct rather than continuing a sentence.
        """
        for text in (
            "| a | b |\n| c | d |\n",
            "> quoted line\n> continues here\n",
            "# Heading\nthe text below it.\n",
            "[ref]: ./a.md\n[other]: ./b.md\n",
            "The inputs are:\nthe first one.\n",
            "A line ending in a dash -\nthe continuation.\n",
        ):
            with self.subTest(text=text.split("\n")[0]):
                self.assertEqual([], self.kinds(text, {"sentence-split"}))

    def test_the_rule_is_markdown_only(self) -> None:
        """A source file's wrapped lines are code, which comment-wrap judges instead."""
        self.assertEqual(
            [],
            self.kinds(
                "a sentence that keeps\ngoing onto the next line.\n",
                {"sentence-split"},
                name="bait.py",
            ),
        )


class TestSentenceLength(BaitCase):
    """The word cap on one Markdown sentence, the first structural house-style check."""

    def sentence_of(self, words: int) -> str:
        return " ".join(["word"] * words) + "."

    def test_a_sentence_over_the_cap_is_flagged(self) -> None:
        over = self.sentence_of(prose_lint.SENTENCE_WORD_CAP + 1)
        self.assertEqual(["sentence-length"], self.kinds(over + "\n", {"sentence-length"}))

    def test_a_sentence_at_the_cap_passes(self) -> None:
        at_cap = self.sentence_of(prose_lint.SENTENCE_WORD_CAP)
        self.assertEqual([], self.kinds(at_cap + "\n", {"sentence-length"}))

    def test_each_sentence_on_a_line_is_judged_alone(self) -> None:
        """Two sentences sharing a line are two counts, never one joined span."""
        at_cap = self.sentence_of(prose_lint.SENTENCE_WORD_CAP)
        self.assertEqual([], self.kinds(f"{at_cap} {at_cap}\n", {"sentence-length"}))

    def test_an_inline_code_span_is_one_word(self) -> None:
        """Naming a symbol costs the sentence one word however long the symbol's name is."""
        span = "`" + " ".join(["token"] * prose_lint.SENTENCE_WORD_CAP) + "`"
        self.assertEqual([], self.kinds(f"The value {span} is read.\n", {"sentence-length"}))

    def test_a_quotation_is_one_word(self) -> None:
        """Quoted text is someone else's prose, so its length is not the author's finding."""
        quoted = '"' + " ".join(["word"] * prose_lint.SENTENCE_WORD_CAP) + '"'
        self.assertEqual([], self.kinds(f"The doc says {quoted} here.\n", {"sentence-length"}))

    def test_a_quotation_keeps_its_sentence_boundary(self) -> None:
        """A terminator closing a quotation still ends the sentence, so neighbors do not merge."""
        lead = " ".join(["word"] * (prose_lint.SENTENCE_WORD_CAP - 5))
        text = f'{lead} said "stop right now." {lead} said nothing.\n'
        self.assertEqual([], self.kinds(text, {"sentence-length"}))

    def test_structure_is_not_a_sentence(self) -> None:
        """A table row, a heading, a link definition, and a blockquote hold no prose sentence."""
        run = " ".join(["word"] * (prose_lint.SENTENCE_WORD_CAP + 1))
        for text in (f"| {run} |\n", f"# {run}\n", f"[ref]: ./{run}.md\n", f"> {run}\n"):
            with self.subTest(text=text[:20]):
                self.assertEqual([], self.kinds(text, {"sentence-length"}))

    def test_the_rule_is_markdown_only(self) -> None:
        """A source file's long lines are code, which no sentence rule judges."""
        over = self.sentence_of(prose_lint.SENTENCE_WORD_CAP + 1)
        self.assertEqual([], self.kinds(f"# {over}\n", {"sentence-length"}, name="bait.py"))

    def test_the_rule_is_opt_in(self) -> None:
        """Adoption is incremental, so the default set stays unchanged until a promotion."""
        self.assertNotIn("sentence-length", prose_lint.DEFAULT_RULES)
        self.assertIn("sentence-length", prose_lint.RULES)


class TestSpelling(BaitCase):
    """US English, read from Markdown prose and from the comments of every other syntax.

    The rule runs on whatever file it is handed, README and HISTORY included. It complements the
    cspell gate rather than dividing the tree with it: cspell reads those two files and this reads
    the prose in all of them, so what it adds is coverage of everywhere cspell was never pointed.
    """

    def messages(self, text: str, name: str = "bait.md") -> list[str]:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return [msg for _, _, msg in prose_lint.check_file(path, {"spelling"})]

    def test_every_banned_spelling_is_caught_and_its_us_form_is_not(self) -> None:
        """The live table drives the case, so a word added to it arrives already proven."""
        for british, us in prose_lint.BRITISH.items():
            with self.subTest(word=british):
                self.assertEqual(["spelling"], self.kinds(f"One {british} here.\n", {"spelling"}))
                self.assertEqual([], self.kinds(f"One {us} here.\n", {"spelling"}))

    def test_the_finding_names_the_replacement(self) -> None:
        word = "behavi" + "our"
        self.assertEqual(
            [f"British spelling '{word}' -> 'behavior'"], self.messages(f"The {word} of it.\n")
        )

    def test_a_match_keeps_the_case_the_source_wrote(self) -> None:
        """A capitalized or shouted word gets a replacement it can be swapped in for."""
        word = "colo" + "ur"
        for written, offered in ((word.capitalize(), "Color"), (word.upper(), "COLOR")):
            with self.subTest(written=written):
                self.assertEqual(
                    [f"British spelling '{written}' -> '{offered}'"],
                    self.messages(f"{written} of the box.\n"),
                )

    def test_a_word_that_is_also_correct_us_english_is_not_banned(self) -> None:
        """`analyses` is the plural of `analysis`, and `cancelled` is an Actions job status."""
        for word in ("analyses", "cancelled", "analysis", "advertise", "surprise", "exercise"):
            with self.subTest(word=word):
                self.assertEqual([], self.kinds(f"One {word} here.\n", {"spelling"}))

    def test_a_banned_spelling_inside_a_word_is_not_a_match(self) -> None:
        """The pattern is word-anchored, so a longer word that contains one is left alone."""
        for word in ("parameter", "collaborate", "metering"):
            with self.subTest(word=word):
                self.assertEqual([], self.kinds(f"One {word} here.\n", {"spelling"}))

    def test_code_is_not_prose_outside_markdown(self) -> None:
        """A source file is read through its comments, so an identifier or a table is not bait.

        This is what keeps prose_lint.py from reporting its own lookup table.
        """
        word = "organis" + "ation"
        self.assertEqual([], self.kinds(f"x = '{word}'\n", {"spelling"}, name="bait.py"))
        self.assertEqual(
            ["spelling"], self.kinds(f"# The {word} of it.\n", {"spelling"}, name="bait.py")
        )

    def test_inline_code_is_not_prose(self) -> None:
        """A backticked word is a quoted token, the same exemption every other prose rule takes."""
        word = "licen" + "ce"
        self.assertEqual([], self.kinds(f"The `{word}` field.\n", {"spelling"}))
        self.assertEqual([], self.kinds(f"# The `{word}` field.\n", {"spelling"}, name="bait.py"))

    def test_the_repo_is_clean_of_british_spellings(self) -> None:
        """The rule gates in CI, so the tree it gates has to pass it today and not eventually."""
        found = [
            f"{prose_lint.rel(p)}:{ln}"
            for p in prose_lint.discover(["."])
            for ln, kind, _ in prose_lint.check_file(p, {"spelling"})
            if kind == "spelling"
        ]
        self.assertEqual([], found)


class TestCarriedContent(unittest.TestCase):
    """Content the fleet copies byte-matched, which only the hub can ever correct.

    The comment-and-doc-style Skill's "Character set" section states the obligation:
    correct-as-you-next-edit assumes someone able to edit the file, and a downstream repo cannot
    edit a verbatim one, since its copy is byte-matched against the hub's.
    So the hub sweeps the class and re-vendors.
    That makes a finding in a verbatim file different in kind from the tree-wide backlog: it is
    not a correction owed by whoever next edits the file, it is one no downstream repo can make
    at all.
    """

    def verbatim_paths(self) -> list[Path]:
        """Every `verbatim` entry in the carry manifest, read live rather than restated here."""

        def entries(node: object):
            if isinstance(node, dict):
                if "path" in node:
                    yield node
                for value in node.values():
                    yield from entries(value)
            elif isinstance(node, list):
                for value in node:
                    yield from entries(value)

        manifest = json.loads((REPO / "spec" / "files.json").read_text(encoding="utf-8"))
        seen = {e["path"] for e in entries(manifest) if e.get("fidelity") == "verbatim"}
        return sorted(REPO / p for p in seen)

    def test_the_manifest_still_declares_verbatim_content(self) -> None:
        """A manifest that stopped declaring any would make the sweep below vacuously pass."""
        self.assertNotEqual([], self.verbatim_paths())

    def test_every_verbatim_carried_file_is_comment_clean(self) -> None:
        """A downstream repo cannot fix one of these, so the hub may not leave one behind."""
        found = [
            f"{prose_lint.rel(p.relative_to(REPO))}:{ln}: {kind}"
            for p in self.verbatim_paths()
            if p.exists()
            for ln, kind, _ in prose_lint.check_file(p, {"comment-wrap", "comment-case"})
        ]
        self.assertEqual([], found)

    def test_every_declared_verbatim_file_exists(self) -> None:
        """A manifest naming a file the hub does not carry would exempt it by absence."""
        self.assertEqual(
            [], [str(p.relative_to(REPO)) for p in self.verbatim_paths() if not p.exists()]
        )


class TestSyntaxDispatch(unittest.TestCase):
    def test_an_extensionless_file_is_read_as_hash_commented(self) -> None:
        """A shebang script or a config with no suffix is far more often `#` than nothing."""
        self.assertEqual(prose_lint.HASH, prose_lint.syntax_for(Path("somescript")))

    def test_a_format_with_no_comments_and_an_unknown_suffix_are_both_skipped(self) -> None:
        for name in ("data.lock", "data.csv", "image.png", "archive.7z"):
            with self.subTest(file=name):
                self.assertIsNone(prose_lint.syntax_for(Path(name)))

    def test_a_name_match_beats_the_suffix_table(self) -> None:
        self.assertEqual(prose_lint.INI, prose_lint.syntax_for(Path(".editorconfig")))

    def test_python_that_will_not_tokenize_falls_back_to_the_line_scan(self) -> None:
        """`tokenize` raises on a half-written file, and the rule still has to read its comments."""
        self.assertIsNone(prose_lint.python_comments("def f(:\n"))
        self.assertEqual(["comment-wrap"], self.flag("def f(:\n# Two things. Here.\n"))

    def flag(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bait.py"
            path.write_text(text, encoding="utf-8")
            return [
                kind for _, kind, _ in prose_lint.check_file(path, {"comment-wrap", "comment-case"})
            ]


class TestChangedLines(unittest.TestCase):
    """The `--diff` scope, which decides which findings a CI run is allowed to report.

    The repo policy is that existing prose is corrected as each file is next edited rather than
    swept, and this parse is the whole mechanism behind it. A parse that returns too little makes
    a diff-scoped run silently stop reporting, and one that returns too much reports the backlog
    as if the change introduced it.
    """

    DIFF = (
        "diff --git a/a.md b/a.md\n"
        "--- a/a.md\n"
        "+++ b/a.md\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "@@ -10,0 +11,3 @@\n"
        "+one\n+two\n+three\n"
        "diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n"
        "--- a/.github/workflows/x.yml\n"
        "+++ b/.github/workflows/x.yml\n"
        "@@ -5,2 +5,0 @@\n"
        "-gone\n-also gone\n"
    )

    def run_diff(self, stdout: str = "", returncode: int = 0):
        # Untracked files are a second source for the same map and are asserted separately.
        # The parse is read here alone rather than through whatever the tree happens to hold.
        done = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")
        with (
            mock.patch.object(prose_lint.subprocess, "run", return_value=done),
            mock.patch.object(prose_lint, "untracked_paths", return_value=[]),
        ):
            return prose_lint.changed_lines("origin/develop", Path("."))

    def test_each_hunk_maps_to_the_lines_it_adds(self) -> None:
        """A single-line hunk carries no count, and a deletion-only hunk adds nothing."""
        got = self.run_diff(self.DIFF)
        self.assertEqual({1, 11, 12, 13}, got["a.md"])
        self.assertEqual(set(), got[".github/workflows/x.yml"])

    def test_a_dot_prefixed_path_survives_the_parse(self) -> None:
        """The key has to match `rel()`, or a finding under a dot directory is never in scope."""
        self.assertIn(".github/workflows/x.yml", self.run_diff(self.DIFF))

    def test_a_hunk_before_its_file_header_is_not_attributed_to_the_previous_file(self) -> None:
        """Reading a stray hunk against whichever file came last invents a scope."""
        self.assertEqual({}, self.run_diff("@@ -1 +1 @@\n+orphan\n"))

    def test_a_git_failure_is_none_rather_than_an_empty_scope(self) -> None:
        """An empty scope filters every file out and reports a clean run, which is a false pass."""
        with mock.patch.object(
            prose_lint.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "git")
        ):
            self.assertIsNone(prose_lint.changed_lines("origin/develop", Path(".")))
        with mock.patch.object(prose_lint.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(prose_lint.changed_lines("origin/develop", Path(".")))


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # The main() call prints findings, which would read as real ones in a CI log.
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def test_main_exits_nonzero_on_a_bait_tree(self) -> None:
        """The CLI path, not only check_file, reports the finding and exits 1."""
        bait = self.tmp / "bait.md"
        bait.write_text(f"{DUP} thing\n", encoding="utf-8")
        with mock.patch.object(prose_lint, "discover", return_value=[bait]):
            self.assertEqual(1, prose_lint.main(["--check", "dupword"]))

    def test_main_exits_zero_on_a_clean_tree(self) -> None:
        clean = self.tmp / "clean.md"
        clean.write_text("Nothing here breaks a rule.\n", encoding="utf-8")
        with mock.patch.object(prose_lint, "discover", return_value=[clean]):
            self.assertEqual(0, prose_lint.main(["--check", "dupword"]))

    def test_every_rule_name_is_offered_by_the_cli(self) -> None:
        """RULES is the single source, so a rule cannot exist in check_file and not in --check."""
        for name in prose_lint.RULES:
            with (
                self.subTest(rule=name),
                mock.patch.object(prose_lint, "discover", return_value=[]),
            ):
                self.assertEqual(0, prose_lint.main(["--check", name]))

    def test_default_rules_are_a_subset_of_the_declared_rules(self) -> None:
        self.assertLessEqual(set(prose_lint.DEFAULT_RULES), set(prose_lint.RULES))

    def test_a_bare_run_checks_comment_shape(self) -> None:
        """Comment shape is the most regressed rule, so a run nobody parameterized must catch it.

        It sat outside DEFAULT_RULES, so `prose_lint.py .` reported clean on a wrapped comment
        and the rule read as enforced while nothing ran it.
        """
        for rule in ("comment-wrap", "comment-case"):
            with self.subTest(rule=rule):
                self.assertIn(rule, prose_lint.DEFAULT_RULES)
        bait = self.tmp / "bait.py"
        bait.write_text("# A sentence that wraps\n# across two comment lines.\n", encoding="utf-8")
        with mock.patch.object(prose_lint, "discover", return_value=[bait]):
            self.assertEqual(1, prose_lint.main([]))

    def test_a_path_under_no_repository_is_refused_rather_than_scoped_to_nothing(self) -> None:
        """A diff needs a repository, and having none is an error rather than an empty scope.

        Discovery falls back to a filesystem walk, and an empty scope would drop every file it
        found and report a clean run. The refusal now comes from the diff itself, which is where
        the impossibility actually is: a guard comparing the scan root against the process's own
        root could not see this at all, because there is no root to differ from.
        """
        # A real directory, since a path that does not exist is refused earlier for another reason.
        # That would pass this assertion without ever reaching the diff.
        loose = self.tmp / "loose"
        loose.mkdir()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(2, prose_lint.main(["--diff", "HEAD", str(loose)]))
        self.assertIn("cannot diff against", err.getvalue())

    def test_list_files_still_reports_scope_across_repositories(self) -> None:
        """It reports the scan scope and never consults the diff, so no diff failure reaches it."""
        clean = self.tmp / "clean.md"
        clean.write_text("fine\n", encoding="utf-8")
        other = self.tmp / "other"
        other.mkdir()
        with (
            mock.patch.object(
                prose_lint, "repo_root", side_effect=lambda p: "/hub" if str(p) == "." else "/other"
            ),
            mock.patch.object(prose_lint, "discover", return_value=[clean]),
        ):
            self.assertEqual(0, prose_lint.main(["--list-files", "--diff", "HEAD", str(other)]))

    def test_a_matching_repository_is_not_refused(self) -> None:
        """The ordinary case, kept as the floor under every narrowing rule above it."""
        clean = self.tmp / "clean.md"
        clean.write_text("Nothing here breaks a rule.\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value="/hub"),
            mock.patch.object(prose_lint, "discover", return_value=[clean]),
            mock.patch.object(prose_lint, "changed_lines", return_value={}),
        ):
            self.assertEqual(0, prose_lint.main(["--check", "dupword", "--diff", "HEAD"]))

    def test_diff_scope_reports_only_the_changed_lines(self) -> None:
        """A finding on an untouched line is the backlog, which the diff run must not attribute."""
        bait = self.tmp / "bait.md"
        bait.write_text(f"{DUP} thing\nA clean line.\n{DUP} again\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "discover", return_value=[bait]),
            mock.patch.object(
                prose_lint, "changed_lines", return_value={prose_lint.rel(bait): {3}}
            ),
        ):
            self.assertEqual(1, prose_lint.main(["--check", "dupword", "--diff", "HEAD"]))
        with (
            mock.patch.object(prose_lint, "discover", return_value=[bait]),
            mock.patch.object(
                prose_lint, "changed_lines", return_value={prose_lint.rel(bait): {2}}
            ),
        ):
            self.assertEqual(0, prose_lint.main(["--check", "dupword", "--diff", "HEAD"]))

    def test_a_file_outside_the_diff_is_dropped_entirely(self) -> None:
        bait = self.tmp / "bait.md"
        bait.write_text(f"{DUP} thing\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "discover", return_value=[bait]),
            mock.patch.object(prose_lint, "changed_lines", return_value={"other.md": {1}}),
        ):
            self.assertEqual(0, prose_lint.main(["--check", "dupword", "--diff", "HEAD"]))

    def test_a_failed_diff_is_an_error_rather_than_a_wider_or_narrower_scan(self) -> None:
        """An unusable diff has three answers, and only one of them is honest.

        Scoping to nothing reports a clean run, which is a false pass. Widening to the whole tree
        reports the existing backlog as though this change introduced it, which is what a CI
        adoption hits first: an unresolvable base turned PhotoCleaner's first run into 420
        findings its branch never touched. Failing names the cause and asserts neither.

        The exit code is distinct from a findings exit, so a caller can tell "the gate could not
        run" from "the gate ran and found something".
        """
        bait = self.tmp / "bait.md"
        bait.write_text(f"{DUP} thing\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "discover", return_value=[bait]),
            mock.patch.object(prose_lint, "changed_lines", return_value=None),
        ):
            self.assertEqual(2, prose_lint.main(["--check", "dupword", "--diff", "HEAD"]))

    def test_list_files_prints_the_scope_and_reports_nothing(self) -> None:
        """The audit path for the sweep scope exits 0 even on a tree full of findings."""
        bait = self.tmp / "bait.md"
        bait.write_text(f"{DUP} thing\n", encoding="utf-8")
        with mock.patch.object(prose_lint, "discover", return_value=[bait]):
            self.assertEqual(0, prose_lint.main(["--check", "dupword", "--list-files"]))

    def test_summary_mode_reports_the_totals_without_the_per_finding_lines(self) -> None:
        bait = self.tmp / "bait.md"
        bait.write_text(f"{DUP} thing\n", encoding="utf-8")
        with mock.patch.object(prose_lint, "discover", return_value=[bait]):
            self.assertEqual(1, prose_lint.main(["--check", "dupword", "--summary"]))

    def test_an_unreadable_file_is_skipped_rather_than_raising(self) -> None:
        """A sweep is scoped by what git tracks, which includes files this process cannot decode."""
        blob = self.tmp / "payload.md"
        blob.write_bytes(b"\xff\xfe not utf-8\n")
        self.assertEqual([], prose_lint.check_file(blob, {"dupword"}))
        self.assertEqual([], prose_lint.check_file(self.tmp / "absent.md", {"dupword"}))


# The gate reads this file, so a bait path is assembled rather than written.
# A literal would be a finding in the very file that defines the rule.
# This file already takes that approach for non-ASCII, which it writes as escapes.
# `adalovelace` is a constructed account name, never an account on any machine here.
BAIT_USER = "adalovelace"
NIX_HOME = f"/home/{BAIT_USER}"
MAC_HOME = f"/Users/{BAIT_USER}"
WIN_HOME = f"C:\\Users\\{BAIT_USER}"


class TestHomePath(BaitCase):
    """The pattern-detectable half of the representative-data rule, and only that half.

    Every fixture below uses a constructed account name. Writing the maintainer's own path into
    a committed test would be the exact exposure the rule exists to prevent, in the file that
    implements the rule.

    The shapes were chosen against the corpus rather than from the list the backlog recorded. A
    bare drive letter matched 11 files here and named a path in none of them, because an escaped
    newline after any word ending in a letter and a colon reads as one, so `jobs:\\n` inside a
    fixture is a drive letter. The shape kept is a drive letter followed by `Users`.
    """

    def test_a_home_path_naming_a_person_is_flagged(self) -> None:
        for text in (
            f"See {NIX_HOME}/notes.txt for the log.\n",
            f"See {MAC_HOME}/notes.txt for the log.\n",
            f"See {WIN_HOME}\\notes.txt for the log.\n",
        ):
            with self.subTest(text=text.strip()):
                self.assertIn("home-path", self.kinds(text, {"home-path"}))

    def test_the_documentation_placeholder_is_not_a_finding(self) -> None:
        """The rule's own wording quotes these shapes to describe them, and must survive its gate.

        A real user segment is required, so the placeholder form does not match and no exemption
        list has to carry the files that describe the rule. A stale exemption list is what turns
        a gate into a work list that damages correct documents.
        """
        for text in (
            "The shapes are `/home/<name>` and `C:\\Users\\<name>`.\n",
            "A path under `/Users/<name>` is the macOS form.\n",
        ):
            with self.subTest(text=text.strip()):
                self.assertNotIn("home-path", self.kinds(text, {"home-path"}))

    def test_a_container_account_is_not_a_personal_home(self) -> None:
        """`/home/vscode` is a fixed name a devcontainer image ships, so it names no environment.

        Every one of this repository's 15 home-path hits is this shape, from the devcontainer
        snippets and the doc describing them, so a rule without this exemption would open with a
        work list of 15 findings and no true positive among them.
        """
        for account in ("vscode", "runner", "root", "ubuntu", "node"):
            with self.subTest(account=account):
                self.assertNotIn(
                    "home-path",
                    self.kinds(f"Mounted at /home/{account}/.ssh here.\n", {"home-path"}),
                )

    def test_a_path_inside_a_fenced_block_is_still_a_finding(self) -> None:
        """A transcript pasted from a terminal is the exposure, and it arrives fenced.

        Every other prose rule skips a fenced block because it holds code rather than prose. This
        one reads it, since the rule gates a literal path rather than a sentence.
        """
        self.assertIn(
            "home-path", self.kinds(f"```text\n$ ls {NIX_HOME}/keys\n```\n", {"home-path"})
        )

    def test_a_path_in_a_config_value_is_still_a_finding(self) -> None:
        """A bind mount naming a real home is the exposure in its most consequential form."""
        self.assertIn(
            "home-path",
            self.kinds(
                f'{{ "target": "{NIX_HOME}/.ssh/id_ed25519.pub" }}\n', {"home-path"}, "a.json"
            ),
        )

    def test_a_relative_or_tilde_path_is_not_a_finding(self) -> None:
        """`~/.ssh` names no account, which is the form the docs are supposed to use."""
        for text in (
            "Copy `~/.ssh/id_ed25519.pub` into place.\n",
            f"The path `home/{BAIT_USER}` is relative.\n",
        ):
            with self.subTest(text=text.strip()):
                self.assertNotIn("home-path", self.kinds(text, {"home-path"}))

    def test_the_windows_branch_is_case_insensitive(self) -> None:
        """Windows filesystems are case-insensitive, so a pasted path may be any casing."""
        for form in (f"C:\\users\\{BAIT_USER}", f"c:\\USERS\\{BAIT_USER}"):
            with self.subTest(form=form):
                self.assertIn("home-path", self.kinds(f"See {form} here.\n", {"home-path"}))

    def test_the_posix_branch_stays_case_sensitive(self) -> None:
        """A lowercase `/users/` is the commonest REST path there is, and names no home.

        This is why the case-insensitive fix is scoped to the drive-letter branch rather than
        applied to the whole pattern: widening it would flag every API route in every doc.
        """
        for text in (
            f"GET https://api.example.com/users/{BAIT_USER} returns the record.\n",
            f"The route is `/users/{BAIT_USER}` in the API.\n",
        ):
            with self.subTest(text=text.strip()):
                self.assertNotIn("home-path", self.kinds(text, {"home-path"}))

    def test_a_bare_drive_letter_is_not_a_shape(self) -> None:
        """Kept as a case because the backlog recorded it as one and the corpus rejected it.

        These are the two forms that made it unworkable: an escaped newline in a fixture, and a
        temporary directory in a string literal.
        """
        for text in ("key: 'jobs:\\n  a:'\n", 'var p = "C:\\tmp\\out";\n'):
            with self.subTest(text=text.strip()):
                self.assertNotIn("home-path", self.kinds(text, {"home-path"}))

    def test_the_rule_runs_by_default(self) -> None:
        """A rule outside DEFAULT_RULES reads as enforced while nothing runs it."""
        self.assertIn("home-path", prose_lint.DEFAULT_RULES)
        self.assertIn("home-path", prose_lint.RULES)

    def test_neither_source_file_carries_a_literal_home_path(self) -> None:
        """Both files are read by the rule they implement, so the bait is assembled.

        This is the same guarantee the tier tables carry for non-ASCII, and it is the reason the
        constants above exist. Written as literals, the gate would report its own definition and
        its own cases, and the only repair would be an exemption naming these files, which is the
        stale-exemption shape that hands out a work list damaging correct documents.
        """
        for path in (REPO / "scripts" / "prose_lint.py", Path(__file__)):
            with self.subTest(source=path.name):
                found = prose_lint.check_file(path, {"home-path"})
                self.assertEqual([], found)


class TestOperationalExemption(unittest.TestCase):
    """An operational repository's runbook carries the path an operator types.

    That is the repository's own content rather than an agent quoting an environment it observed,
    which is the distinction the rule is about.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def _payload(self, *parts: str) -> None:
        target = self.tmp.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")

    def test_an_operational_checkout_is_read_from_what_it_carries(self) -> None:
        """`spec/files.json` declares the payload per model, so the repository states its own."""
        self._payload("repo-config", "operational", "develop.json")
        self.assertTrue(prose_lint.operational_checkout(self.tmp))

    def test_a_release_checkout_is_not_operational(self) -> None:
        self._payload("repo-config", "develop.json")
        self.assertFalse(prose_lint.operational_checkout(self.tmp))

    def test_the_hub_carrying_both_payloads_is_not_operational(self) -> None:
        """The hub is the template for each model, so carrying the release payload decides it.

        Read as operational, the hub would exempt itself from a rule it authors, which is the
        one repository where that matters most.
        """
        self._payload("repo-config", "develop.json")
        self._payload("repo-config", "operational", "develop.json")
        self.assertFalse(prose_lint.operational_checkout(self.tmp))

    def test_a_checkout_carrying_neither_payload_is_not_operational(self) -> None:
        """An unknown model is gated rather than exempted, since exempting on doubt is the risk."""
        self.assertFalse(prose_lint.operational_checkout(self.tmp))

    def test_the_skip_is_announced_rather_than_silent(self) -> None:
        """A rule that stops running without saying so reads as a rule that passed."""
        self._payload("repo-config", "operational", "develop.json")
        bait = self.tmp / "runbook.md"
        bait.write_text(f"Deploy into {NIX_HOME}/stack here.\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=str(self.tmp)),
            mock.patch.object(prose_lint, "discover", return_value=[bait]),
        ):
            self.assertEqual(0, prose_lint.main(["--check", "home-path"]))
        self.assertIn("operational repository", self.err.getvalue())

    def test_a_release_repository_still_reports_the_finding(self) -> None:
        """The exemption must not be the whole rule."""
        self._payload("repo-config", "develop.json")
        bait = self.tmp / "runbook.md"
        bait.write_text(f"Deploy into {NIX_HOME}/stack here.\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=str(self.tmp)),
            mock.patch.object(prose_lint, "discover", return_value=[bait]),
        ):
            self.assertEqual(1, prose_lint.main(["--check", "home-path"]))


class TestScanRootDecidesTheRuleSet(unittest.TestCase):
    """The rule set follows the repository being scanned, never the directory the caller stands in.

    Read from `.`, the exemption discarded home-path on a release repository whenever the caller
    happened to stand in an operational one, announced the skip on stderr, and exited 0. That
    silences the rule that exists because real paths reached a public comment.

    Every case here gives the two roots *different* models. `TestOperationalExemption` above mocks
    `repo_root` to one value for every argument, which cannot tell the caller's repository from the
    scanned one, and is why this defect survived it.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.operational = self._repo("operational", ("repo-config", "operational", "develop.json"))
        self.release = self._repo("release", ("repo-config", "develop.json"))
        self.bait = self.release / "runbook.md"
        self.bait.write_text(f"Deploy into {NIX_HOME}/stack here.\n", encoding="utf-8")

    def _repo(self, name: str, payload: tuple[str, ...]) -> Path:
        root = self.tmp / name
        target = root.joinpath(*payload)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        return root

    def _roots(self, cwd: Path, scanned: Path):
        """Resolve `.` to one repository and every other path to another."""
        return lambda p: str(cwd) if str(p) == "." else str(scanned)

    def test_standing_in_an_operational_repo_does_not_exempt_a_release_repo(self) -> None:
        """The defect: the caller's directory turned the rule off on the repository being scanned."""
        with (
            mock.patch.object(
                prose_lint, "repo_root", side_effect=self._roots(self.operational, self.release)
            ),
            mock.patch.object(prose_lint, "discover", return_value=[self.bait]),
        ):
            self.assertEqual(1, prose_lint.main([str(self.release), "--check", "home-path"]))
        self.assertNotIn("operational repository", self.err.getvalue())

    def test_standing_in_a_release_repo_does_not_un_exempt_an_operational_repo(self) -> None:
        """The inverse error, which reports a finding the exemption exists to suppress."""
        runbook = self.operational / "runbook.md"
        runbook.write_text(f"Deploy into {NIX_HOME}/stack here.\n", encoding="utf-8")
        with (
            mock.patch.object(
                prose_lint, "repo_root", side_effect=self._roots(self.release, self.operational)
            ),
            mock.patch.object(prose_lint, "discover", return_value=[runbook]),
        ):
            self.assertEqual(0, prose_lint.main([str(self.operational), "--check", "home-path"]))
        self.assertIn("operational repository", self.err.getvalue())

    def test_paths_spanning_two_repositories_refuse_rather_than_pick_one(self) -> None:
        """Two repositories declare two models, and one rule set cannot be correct for both."""
        with mock.patch.object(prose_lint, "repo_root", side_effect=lambda p: str(Path(p))):
            self.assertEqual(
                2,
                prose_lint.main([str(self.release), str(self.operational), "--check", "home-path"]),
            )
        self.assertIn("more than one repository", self.err.getvalue())

    def test_a_path_that_does_not_exist_is_refused_rather_than_absorbed(self) -> None:
        """`discover` reads a non-file, non-directory argument as `.`.

        A typo therefore scanned the caller's directory while the rule set anchored on the missing
        path's parent, so the run described one tree and judged it by another.
        """
        with mock.patch.object(prose_lint, "repo_root", return_value=""):
            self.assertEqual(
                2, prose_lint.main([str(self.tmp / "no-such-dir"), "--check", "home-path"])
            )
        self.assertIn("not a file or a directory", self.err.getvalue())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "mkfifo is POSIX only")
    def test_a_path_that_exists_but_is_neither_a_file_nor_a_directory_is_refused(self) -> None:
        """A FIFO, a socket, and a device all exist, and `discover` reads each of them as `.`.

        Testing for existence therefore left the hole open on everything that is not a typo.
        """
        fifo = self.tmp / "a-fifo"
        os.mkfifo(fifo)
        self.assertTrue(fifo.exists())
        with mock.patch.object(prose_lint, "repo_root", return_value=""):
            self.assertEqual(2, prose_lint.main([str(fifo), "--check", "home-path"]))
        self.assertIn("not a file or a directory", self.err.getvalue())

    def test_a_path_holding_a_space_or_comma_is_quoted_in_the_refusal(self) -> None:
        """Joined bare, one path with a comma in it reads as two paths and the message misleads."""
        awkward = self.tmp / "a dir, with comma"
        with mock.patch.object(prose_lint, "repo_root", return_value=""):
            self.assertEqual(2, prose_lint.main([str(awkward), "--check", "home-path"]))
        self.assertIn(repr(str(awkward)), self.err.getvalue())

    def test_repository_roots_are_quoted_in_the_span_refusal(self) -> None:
        spaced = self.tmp / "root with space"
        spaced.mkdir()
        with mock.patch.object(prose_lint, "repo_root", side_effect=lambda p: str(Path(p))):
            self.assertEqual(
                2, prose_lint.main([str(self.release), str(spaced), "--check", "home-path"])
            )
        self.assertIn(repr(str(spaced)), self.err.getvalue())

    def test_an_existing_path_is_not_refused_by_that_check(self) -> None:
        """The guard must not reject the ordinary case it sits in front of."""
        good = self.tmp / "good.md"
        good.write_text("Nothing here breaks a rule.\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=""),
            mock.patch.object(prose_lint, "discover", return_value=[good]),
        ):
            self.assertEqual(0, prose_lint.main([str(good), "--check", "home-path"]))
        self.assertNotIn("do not exist", self.err.getvalue())

    def test_several_paths_under_no_repository_are_not_a_conflict(self) -> None:
        """Only a repository declares a model, so two loose paths are not ambiguous.

        Refusing on distinct filesystem paths broke the ordinary multi-file invocation outside a
        checkout, where every argument resolves somewhere different.
        """
        loose = self.tmp / "loose"
        (loose / "docs").mkdir(parents=True)
        one, two = loose / "a.md", loose / "docs" / "b.md"
        for target in (one, two):
            target.write_text("Nothing to find here.\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=""),
            mock.patch.object(prose_lint, "discover", return_value=[one, two]),
        ):
            self.assertEqual(0, prose_lint.main([str(one), str(two), "--check", "home-path"]))
        self.assertNotIn("more than one repository", self.err.getvalue())

    def test_one_repository_plus_a_loose_path_is_not_a_conflict(self) -> None:
        """One model is in play, so there is nothing to disambiguate."""
        loose = self.tmp / "loose"
        loose.mkdir()
        bait = loose / "notes.md"
        bait.write_text(f"Deploy into {NIX_HOME}/stack here.\n", encoding="utf-8")
        with (
            mock.patch.object(
                prose_lint,
                "repo_root",
                side_effect=lambda p: str(self.release) if Path(p) == self.release else "",
            ),
            mock.patch.object(prose_lint, "discover", return_value=[bait]),
        ):
            # The one repository in play is a release repo, so home-path stays on and finds it.
            self.assertEqual(
                1, prose_lint.main([str(self.release), str(loose), "--check", "home-path"])
            )
        self.assertNotIn("more than one repository", self.err.getvalue())

    def test_a_loose_file_anchors_on_its_own_parent_rather_than_on_the_caller(self) -> None:
        """The fallback must not reintroduce the dependency the fix removes.

        A *file* argument is the case that matters. Anchoring it on `.` puts the caller's directory
        back in charge, so scanning a loose file from inside an operational repository exempts it.
        Passing a directory here would not exercise that branch at all, which is how the earlier
        version of this test let the regression through.
        """
        loose = self.tmp / "loose"
        loose.mkdir()
        bait = loose / "notes.md"
        bait.write_text(f"Deploy into {NIX_HOME}/stack here.\n", encoding="utf-8")
        # The caller stands somewhere operational; the scanned file's own directory does not.
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=""),
            mock.patch.object(
                prose_lint, "operational_checkout", side_effect=lambda root: Path(root) == Path(".")
            ),
            mock.patch.object(prose_lint, "discover", return_value=[bait]),
        ):
            self.assertEqual(1, prose_lint.main([str(bait), "--check", "home-path"]))
        self.assertNotIn("operational repository", self.err.getvalue())

    def test_a_loose_directory_anchors_on_itself(self) -> None:
        loose = self.tmp / "loose"
        loose.mkdir()
        bait = loose / "notes.md"
        bait.write_text(f"Deploy into {NIX_HOME}/stack here.\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=""),
            mock.patch.object(
                prose_lint, "operational_checkout", side_effect=lambda root: Path(root) == Path(".")
            ),
            mock.patch.object(prose_lint, "discover", return_value=[bait]),
        ):
            self.assertEqual(1, prose_lint.main([str(loose), "--check", "home-path"]))
        self.assertNotIn("operational repository", self.err.getvalue())


class TestDiffScopeFloor(unittest.TestCase):
    """The assertion every `--diff` verdict rests on, that the run matched something it was given.

    Four routes to the same false clean are on record, each closed by a guard written after a
    reviewer noticed it: an unresolvable base widening to a whole-tree scan, a multi-line `paths`
    input read only to its first newline, a diff taken in one repository while scanning another,
    and a path under no repository at all. Per-route guards are the wrong shape for a fifth,
    because the fifth is found by a reviewer or not at all. This asserts the floor instead.

    Zero is not the test on its own. A change touching only files the rules do not read matches
    nothing and is honestly clean, so the comparison is against the diff's own list of files this
    run could have read.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def test_a_diff_naming_readable_files_that_match_nothing_is_refused(self) -> None:
        """The floor itself: a non-empty diff, a readable file in it, and nothing scanned."""
        named = self.tmp / "named.md"
        named.write_text("A clean line.\n", encoding="utf-8")
        elsewhere = self.tmp / "elsewhere.md"
        elsewhere.write_text("A clean line.\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=str(self.tmp)),
            mock.patch.object(prose_lint, "discover", return_value=[elsewhere]),
            mock.patch.object(prose_lint, "changed_lines", return_value={"named.md": {1}}),
        ):
            self.assertEqual(2, prose_lint.main(["--check", "dupword", "--diff", "HEAD"]))
        self.assertIn("named.md", self.err.getvalue())

    def test_a_diff_of_only_unreadable_files_is_a_clean_run(self) -> None:
        """The honest limit, and the reason zero alone cannot be the test.

        An image or a lock file is a real change the rules do not read, so it matches nothing and
        the run is clean rather than broken. Refusing here would make the gate cry wolf on a
        commit that adds a logo, which is how a safety check stops being read.
        """
        blob = self.tmp / "logo.png"
        blob.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=str(self.tmp)),
            mock.patch.object(prose_lint, "discover", return_value=[]),
            mock.patch.object(prose_lint, "changed_lines", return_value={"logo.png": {1}}),
        ):
            self.assertEqual(0, prose_lint.main(["--check", "dupword", "--diff", "HEAD"]))

    def test_a_diff_naming_a_file_that_no_longer_exists_is_a_clean_run(self) -> None:
        """A deletion names a path with nothing behind it, which this run cannot have read."""
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=str(self.tmp)),
            mock.patch.object(prose_lint, "discover", return_value=[]),
            mock.patch.object(prose_lint, "changed_lines", return_value={"gone.md": {1}}),
        ):
            self.assertEqual(0, prose_lint.main(["--check", "dupword", "--diff", "HEAD"]))

    def test_an_empty_diff_is_not_a_scoping_failure(self) -> None:
        """A branch level with its base names no files, which is a result rather than a fault."""
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=str(self.tmp)),
            mock.patch.object(prose_lint, "discover", return_value=[]),
            mock.patch.object(prose_lint, "changed_lines", return_value={}),
        ):
            self.assertEqual(0, prose_lint.main(["--check", "dupword", "--diff", "HEAD"]))

    def test_a_run_that_matched_a_file_asserts_nothing_further(self) -> None:
        """The floor is a floor. One match clears it, and the findings decide the exit code."""
        bait = self.tmp / "bait.md"
        bait.write_text(f"{DUP} thing\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=str(self.tmp)),
            mock.patch.object(prose_lint, "discover", return_value=[bait]),
            mock.patch.object(prose_lint, "changed_lines", return_value={"bait.md": {1}}),
        ):
            self.assertEqual(1, prose_lint.main(["--check", "dupword", "--diff", "HEAD"]))

    def test_a_deliberately_narrowed_scan_is_not_told_the_narrowing_is_a_defect(self) -> None:
        """Asking about one subtree while the change sits in another is a request, not a failure."""
        (self.tmp / "scripts").mkdir()
        changed = self.tmp / "scripts" / "tool.py"
        changed.write_text("# A clean comment.\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=str(self.tmp)),
            mock.patch.object(prose_lint, "discover", return_value=[]),
            mock.patch.object(prose_lint, "changed_lines", return_value={"scripts/tool.py": {1}}),
        ):
            self.assertEqual(
                0, prose_lint.main(["--check", "dupword", "--diff", "HEAD", "catalog"])
            )

    def test_an_excluded_file_is_not_counted_as_one_the_run_should_have_read(self) -> None:
        """`--exclude` removes a file from the scan, so it cannot also be evidence of a failure."""
        skipped = self.tmp / "vendor.md"
        skipped.write_text("A clean line.\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=str(self.tmp)),
            mock.patch.object(prose_lint, "discover", return_value=[]),
            mock.patch.object(prose_lint, "changed_lines", return_value={"vendor.md": {1}}),
        ):
            self.assertEqual(
                0, prose_lint.main(["--check", "dupword", "--diff", "HEAD", "--exclude", "vendor"])
            )

    def test_a_generated_tree_is_not_counted_as_one_the_run_should_have_read(self) -> None:
        """Discovery drops `reports/`, so a change confined to it matches nothing by design."""
        (self.tmp / "reports").mkdir()
        generated = self.tmp / "reports" / "divergences.md"
        generated.write_text("A clean line.\n", encoding="utf-8")
        with (
            mock.patch.object(prose_lint, "repo_root", return_value=str(self.tmp)),
            mock.patch.object(prose_lint, "discover", return_value=[]),
            mock.patch.object(
                prose_lint, "changed_lines", return_value={"reports/divergences.md": {1}}
            ),
        ):
            self.assertEqual(0, prose_lint.main(["--check", "dupword", "--diff", "HEAD"]))

    def test_diff_keys_resolve_against_the_repository_rather_than_the_working_directory(
        self,
    ) -> None:
        """The fifth route, which no per-route guard covers and this one found.

        Run from a subdirectory, `git diff` reports repository-relative keys while discovery keys
        off the directory the run started in, so the intersection is empty and the run reports
        clean. Reading the diff's paths against the working directory would leave this list empty
        in exactly that case, making the floor silent where it is most needed.
        """
        (self.tmp / "scripts").mkdir()
        nested = self.tmp / "scripts" / "tool.py"
        nested.write_text("# A clean comment.\n", encoding="utf-8")
        found = prose_lint.unread_diff_files({"scripts/tool.py": {1}}, ["."], (), self.tmp)
        self.assertEqual(["scripts/tool.py"], found)

    def test_asked_about_reads_a_prefix_as_a_directory_boundary(self) -> None:
        """`catalog` must not claim `catalogue/x.md`, which shares its first seven characters."""
        self.assertTrue(prose_lint.asked_about("catalog/x.md", ["catalog"]))
        self.assertTrue(prose_lint.asked_about("catalog/x.md", ["catalog/"]))
        self.assertTrue(prose_lint.asked_about("README.md", ["README.md"]))
        self.assertTrue(prose_lint.asked_about("anything/at/all.md", ["."]))
        self.assertFalse(prose_lint.asked_about("catalogue/x.md", ["catalog"]))
        self.assertFalse(prose_lint.asked_about("docs/x.md", ["catalog"]))


class TestReusableGateExclusions(unittest.TestCase):
    """The composite action keeps vendored content out without narrowing the authored scan."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".github").mkdir()
        (self.root / ".github" / "prose-gate-excludes").write_bytes(
            b"# Pinned upstream file.\r\nvendor/upstream.md\r\n"
        )
        vendor = self.root / "vendor" / "upstream.md"
        vendor.parent.mkdir()
        vendor.write_text("Clean vendored prose.\n", encoding="utf-8")
        (self.root / "authored.md").write_text("Clean authored prose.\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")

    def git(self, *args: str) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.email=gate@example.invalid",
                "-c",
                "user.name=gate test",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    def run_action(self) -> subprocess.CompletedProcess[str]:
        action = PROSE_GATE_ACTION.read_text(encoding="utf-8")
        _, block = action.split("      run: |\n", 1)
        script_lines = []
        for line in block.splitlines():
            if line.startswith("        "):
                script_lines.append(line.removeprefix("        "))
            elif not line:
                script_lines.append(line)
            else:
                break
        script = "\n".join(script_lines)
        env = os.environ | {
            "BASE": "HEAD",
            "GITHUB_ACTION_PATH": str(PROSE_GATE_ACTION.parent),
            "PATHS": ".",
        }
        return subprocess.run(
            ["bash", "-c", script],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_a_declared_vendored_path_does_not_block_the_gate(self) -> None:
        (self.root / "vendor" / "upstream.md").write_text(
            f"Vendored {DUP} words.\n", encoding="utf-8"
        )
        result = self.run_action()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Excluding 1 path(s)", result.stdout)

    def test_an_ordinary_authored_file_remains_in_scope(self) -> None:
        (self.root / "vendor" / "upstream.md").write_text(
            f"Vendored {DUP} words.\n", encoding="utf-8"
        )
        (self.root / "authored.md").write_text(f"Authored {DUP} words.\n", encoding="utf-8")
        result = self.run_action()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("authored.md:1: dupword", result.stdout)
        self.assertNotIn("vendor/upstream.md:1", result.stdout)


class TestTheScanScopeIsTheScopeReported(unittest.TestCase):
    """What the run read, against real repositories rather than a mocked answer about them.

    Every false clean on record lives in the join between two coordinate systems: a diff names
    files relative to the repository top level, and a path argument arrives in whatever form the
    caller typed. Mocking `repo_root` or `discover` supplies that join already made, so the cases
    below build repositories and let git answer. Both defects were reported clean by the 210-case
    suite at `e2a99f1`, which was green in full, and both are here.

    The invariant: the rule set, the file set, the diff, and the keys that join the last two are
    all read from the repository being scanned, and none of them from the directory the process
    happens to stand in. A verdict states the scope it covered, since reading nothing prints what
    finding nothing prints otherwise.
    """

    BAIT = "# New\n\nA line with a repeated the the word in it.\n"

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def git(self, root: Path, *args: str) -> None:
        # Signing is disabled explicitly, since a host that signs by default cannot commit here.
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.email=gate@example.invalid",
                "-c",
                "user.name=gate test",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    def repo(self, name: str = "repo") -> Path:
        """A real repository holding one committed clean file, which is the base every case diffs."""
        root = self.tmp / name
        root.mkdir()
        self.git(root, "init", "-q")
        (root / "DOC.md").write_text("# Doc\n\nA clean line.\n", encoding="utf-8")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "base")
        return root

    def run_in(self, cwd: Path, *argv: str) -> int:
        with contextlib.chdir(cwd):
            return prose_lint.main(["--check", "dupword", *argv])

    def test_an_absolute_path_argument_reads_what_the_relative_one_reads(self) -> None:
        """The reported defect: same directory, same repository, same ref, and a clean verdict.

        `prose_lint.py . --diff BASE` reported the finding while `prose_lint.py /abs/path --diff
        BASE` reported nothing and exited 0. Discovery returned absolute paths and the diff named
        repository-relative ones, so the intersection was empty and an empty intersection is what
        a clean tree looks like. The same-root guard could not fire, because the run genuinely was
        in the right repository.
        """
        root = self.repo()
        (root / "DOC.md").write_text(self.BAIT, encoding="utf-8")
        self.git(root, "commit", "-qam", "change")
        self.assertEqual(1, self.run_in(root, ".", "--diff", "HEAD~1"))
        self.assertEqual(1, self.run_in(root, str(root), "--diff", "HEAD~1"))

    def test_an_untracked_file_is_in_the_scope_of_the_change_that_adds_it(self) -> None:
        """The second reported defect, on identical bytes either side of a `git add`.

        A new file read clean while unstaged and reported its findings the moment it was
        committed. `git diff` never names an untracked file, so a change whose whole point is
        adding one scoped to nothing, and a change is exactly when a new file is most worth
        reading.
        """
        root = self.repo()
        (root / "NEW.md").write_text(self.BAIT, encoding="utf-8")
        self.assertEqual(1, self.run_in(root, ".", "--diff", "HEAD"))
        self.git(root, "add", "NEW.md")
        self.assertEqual(1, self.run_in(root, ".", "--diff", "HEAD"))

    def test_an_untracked_file_is_read_by_a_whole_tree_sweep_too(self) -> None:
        """`git ls-files` omits it as well, so the hole was never particular to `--diff`.

        A sweep was the documented way to check what a diff-scoped run might have missed, and it
        passed over the same file for its own reason.
        """
        root = self.repo()
        (root / "NEW.md").write_text(self.BAIT, encoding="utf-8")
        self.assertEqual(1, self.run_in(root, "."))

    def test_an_ignored_file_is_read_by_neither(self) -> None:
        """A build output is not authored text, so the widening stops where git's own rule does."""
        root = self.repo()
        (root / ".gitignore").write_text("generated/\n", encoding="utf-8")
        self.git(root, "add", ".gitignore")
        self.git(root, "commit", "-qm", "ignore")
        (root / "generated").mkdir()
        (root / "generated" / "out.md").write_text(self.BAIT, encoding="utf-8")
        self.assertEqual(0, self.run_in(root, "."))
        self.assertEqual(0, self.run_in(root, ".", "--diff", "HEAD"))

    def test_a_generated_tree_stays_out_when_the_file_in_it_is_untracked(self) -> None:
        """The generated-tree rule is about who wrote the prose, so tracking does not change it."""
        root = self.repo()
        (root / "reports").mkdir()
        (root / "reports" / "audit.md").write_text(self.BAIT, encoding="utf-8")
        self.assertEqual(0, self.run_in(root, "."))

    def test_a_binary_file_is_not_read_because_it_is_untracked(self) -> None:
        """The text test decides what the rules can read, and it is applied to both sources."""
        root = self.repo()
        (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00 the the")
        self.assertEqual(0, self.run_in(root, ".", "--diff", "HEAD"))

    def test_a_run_from_a_subdirectory_keys_on_the_repository(self) -> None:
        """It used to be refused with advice to run from the top level, which was the guard talking.

        The keys differed rather than the repositories, so anchoring both sides on the scanned
        repository answers the case instead of turning it away.
        """
        root = self.repo()
        (root / "sub").mkdir()
        (root / "sub" / "note.md").write_text(self.BAIT, encoding="utf-8")
        self.assertEqual(1, self.run_in(root / "sub", ".", "--diff", "HEAD"))

    def test_scanning_one_repository_while_standing_in_another_diffs_the_one_scanned(self) -> None:
        """A refusal was standing in for this, because the diff was taken where the process stood.

        The rule set already came from the scanned repository. The diff and the keys now come from
        there too, which is the whole of what the earlier guard was approximating.
        """
        here = self.repo("here")
        there = self.repo("there")
        (there / "DOC.md").write_text(self.BAIT, encoding="utf-8")
        self.git(there, "commit", "-qam", "change")
        self.assertEqual(1, self.run_in(here, str(there), "--diff", "HEAD~1"))

    def test_a_subtree_argument_reads_the_untracked_files_inside_it(self) -> None:
        """`git ls-files` prints names relative to its `-C` directory, for `--others` as well.

        Review read it the other way round and proposed joining both lists on the repository top
        level instead, which would point every name at the wrong place and drop the subtree
        silently. Measured on git 2.51: `git -C sub ls-files --others` prints `untracked.md` and
        `deep/untracked2.md`, not the `sub/` forms. The tracked half of this is pinned by
        TestDiscovery, and the untracked half is pinned here because the two lists are joined the
        same way and a reader has no reason to expect them to differ.

        The keys reported stay repository-relative whichever form the argument took, which is what
        makes a finding in a subtree name a path the repository recognizes.

        The discovered count is asserted rather than the findings alone. Listing from the top level
        instead reads the whole repository, and every file outside the subtree was clean, so the
        findings agreed under both and proved nothing. What the argument narrows is the count.
        """
        root = self.repo()
        (root / "sub" / "deep").mkdir(parents=True)
        (root / "sub" / "near.md").write_text(self.BAIT, encoding="utf-8")
        (root / "sub" / "deep" / "far.md").write_text(self.BAIT, encoding="utf-8")
        for arg in ("sub", str(root / "sub")):
            with self.subTest(arg=arg):
                err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(1, self.run_in(root, arg, "--diff", "HEAD"))
                reported = sorted(
                    line.split(":")[0] for line in out.getvalue().splitlines() if line.strip()
                )
                self.assertEqual(["sub/deep/far.md", "sub/near.md"], reported)
                # DOC.md sits outside the subtree, so a run that reads three files read too much.
                self.assertIn("2 of 2 file(s) read", err.getvalue())

    def test_a_subtree_holding_no_tracked_file_is_still_described_by_git(self) -> None:
        """The walk is for a tree git cannot describe, not for one whose answer is empty.

        `tracked_paths` returns None for both, deliberately, because an initialized but empty
        checkout answering with an empty list would scan nothing and read as a pass. Reading that
        None as "git cannot describe this" sent a subtree holding only new files down the walk,
        which applies no ignore rules, so a build output under it was scanned and reported. The
        run also printed that git could not describe a tree git describes perfectly well.

        Whether git can describe a tree is settled by asking git, never by the size of its answer.
        """
        root = self.repo()
        (root / ".gitignore").write_text("newdir/ignored.md\n", encoding="utf-8")
        self.git(root, "add", ".gitignore")
        self.git(root, "commit", "-qm", "ignore")
        (root / "newdir").mkdir()
        (root / "newdir" / "authored.md").write_text(self.BAIT, encoding="utf-8")
        (root / "newdir" / "ignored.md").write_text(self.BAIT, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(1, self.run_in(root, "newdir"))
        reported = sorted(
            line.split(":")[0] for line in out.getvalue().splitlines() if line.strip()
        )
        self.assertEqual(["newdir/authored.md"], reported)
        self.assertNotIn("git cannot describe", self.err.getvalue())

    def test_a_relative_diff_setting_does_not_re_anchor_the_keys(self) -> None:
        """`diff.relative` anchors a diff's paths on the process's directory, not the repository.

        This case passed before the fix too, because both sides were anchored on that directory
        and agreed by accident. It is pinned because only one side moved: the keys must come from
        the repository whatever the caller's configuration says, and a setting is an input shape
        like any other.
        """
        root = self.repo()
        self.git(root, "config", "diff.relative", "true")
        (root / "sub").mkdir()
        (root / "sub" / "note.md").write_text("A clean line.\n", encoding="utf-8")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "sub")
        (root / "sub" / "note.md").write_text(self.BAIT, encoding="utf-8")
        self.assertEqual(1, self.run_in(root / "sub", ".", "--diff", "HEAD~1"))

    def test_a_clean_run_states_what_it_read(self) -> None:
        """The class the two defects belong to, which no per-route guard covers.

        Both exited 0 in silence, and so did the three routes closed before them. A reader cannot
        tell a gate that read nothing from a gate with nothing to report unless the run says which
        it was, so the count is printed on a clean verdict rather than only on a busy one.
        """
        root = self.repo()
        self.assertEqual(0, self.run_in(root, "."))
        self.assertIn("1 file(s) read, whole tree", self.err.getvalue())

    def test_a_scope_of_nothing_is_reported_as_nothing(self) -> None:
        """A change the rules cannot read is honestly clean, and says so as a count of zero."""
        root = self.repo()
        (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "add a logo")
        self.assertEqual(0, self.run_in(root, ".", "--diff", "HEAD~1"))
        self.assertIn("0 of 1 file(s) read, 0 changed line(s)", self.err.getvalue())

    def test_the_reported_scope_counts_only_what_the_verdict_covered(self) -> None:
        """A count wider than the read is the same lie in a smaller font.

        The narrowed file set and the lines inside it are what the findings were drawn from, so
        those are what the note states, with the discovered total beside them for contrast.
        """
        root = self.repo()
        (root / "OTHER.md").write_text("# Other\n\nA clean line.\n", encoding="utf-8")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "other")
        (root / "DOC.md").write_text(self.BAIT, encoding="utf-8")
        self.git(root, "commit", "-qam", "change")
        self.assertEqual(1, self.run_in(root, ".", "--diff", "HEAD~1"))
        # Two of the three lines changed, since the blank line between them did not.
        self.assertIn("1 of 2 file(s) read, 2 changed line(s)", self.err.getvalue())


class TestDeadPath(unittest.TestCase):
    """The named-path half of the stale-description class (RESYNC.md section 4).

    Every fixture builds a real repository, because the rule keys on git history: a path is
    reported only when git once tracked it and the tree no longer holds it, which is the
    deletion-sweep shape. The measured incident named no path at all, and that half stays a
    manual read, so nothing here asserts coverage the rule does not have.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def git(self, root: Path, *args: str) -> None:
        # Signing is disabled explicitly, since a host that signs by default cannot commit here.
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.email=gate@example.invalid",
                "-c",
                "user.name=gate test",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    def repo(self) -> Path:
        """A repository that tracked `scripts/gone.py` once and then deleted it."""
        root = self.tmp / "repo"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "gone.py").write_text("print()\n", encoding="utf-8")
        (root / "scripts" / "kept.py").write_text("print()\n", encoding="utf-8")
        self.git(root, "init", "-q")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "base")
        self.git(root, "rm", "-q", "scripts/gone.py")
        self.git(root, "commit", "-qm", "delete")
        return root

    def kinds(self, root: Path, text: str, rel: str = "DOC.md") -> list[str]:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return [kind for _, kind, _ in prose_lint.check_file(path, {"dead-path"}, root)]

    def test_a_deleted_path_in_a_span_is_flagged(self) -> None:
        root = self.repo()
        self.assertIn("dead-path", self.kinds(root, "Run `scripts/gone.py` to apply.\n"))

    def test_a_living_path_is_not_a_finding(self) -> None:
        root = self.repo()
        self.assertEqual([], self.kinds(root, "Run `scripts/kept.py` to apply.\n"))

    def test_a_path_with_no_history_here_is_not_a_finding(self) -> None:
        """A proposal in a backlog and another repository's layout both name unborn paths."""
        root = self.repo()
        self.assertEqual([], self.kinds(root, "Ship `scripts/future.py` next.\n"))

    def test_a_reference_definition_to_a_deleted_target_is_flagged(self) -> None:
        root = self.repo()
        self.assertIn("dead-path", self.kinds(root, "[gone]: ./scripts/gone.py\n"))
        self.assertEqual([], self.kinds(root, "[kept]: ./scripts/kept.py\n"))

    def test_an_inline_link_to_a_deleted_target_is_flagged(self) -> None:
        root = self.repo()
        self.assertIn("dead-path", self.kinds(root, "See [the script](scripts/gone.py).\n"))

    def test_a_relative_mention_anchors_on_the_files_own_directory(self) -> None:
        root = self.repo()
        self.assertIn(
            "dead-path", self.kinds(root, "See [it](../scripts/gone.py).\n", rel="docs/D.md")
        )
        self.assertEqual([], self.kinds(root, "See [it](../scripts/kept.py).\n", rel="docs/D.md"))

    def test_a_ref_or_directory_shaped_span_is_not_a_candidate(self) -> None:
        """`origin/develop` styles a ref and `scripts/` a layout, and neither asserts a file."""
        root = self.repo()
        for text in (
            "Fetched from `origin/develop` here.\n",
            "The `scripts/` tree holds the tools.\n",
            "A run like `./scripts/...` is elided.\n",
        ):
            with self.subTest(text=text.strip()):
                self.assertEqual([], self.kinds(root, text))

    def test_a_fenced_block_is_skipped(self) -> None:
        """A fence quotes a transcript or an example, which may legitimately show any path."""
        root = self.repo()
        self.assertEqual([], self.kinds(root, "```sh\npython3 scripts/gone.py\n```\n"))

    def test_a_manifest_declared_carried_path_is_exempt(self) -> None:
        """The hub's instance of a carried file retires to a snippet, and docs still name it."""
        root = self.repo()
        (root / "spec").mkdir()
        (root / "spec" / "files.json").write_text(
            json.dumps({"baseline": [{"path": "scripts/gone.py"}]}), encoding="utf-8"
        )
        prose_lint.carried_paths.cache_clear()
        self.addCleanup(prose_lint.carried_paths.cache_clear)
        self.assertEqual([], self.kinds(root, "Run `scripts/gone.py` to apply.\n"))

    def test_a_hub_hosted_path_is_exempt(self) -> None:
        """A repo that retired its copy still names the hub's, which is the pointer the rule wants.

        The manifest exemption cannot reach this one, since no repository carries `spec/files.json`,
        so without the literal set every retirement fails its own promotion gate.
        """
        root = self.tmp / "retired"
        (root / "repo-config").mkdir(parents=True)
        (root / "repo-config" / "configure.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        self.git(root, "init", "-q")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "base")
        self.git(root, "rm", "-q", "repo-config/configure.sh")
        self.git(root, "commit", "-qm", "retire")
        self.assertEqual([], self.kinds(root, "Run the hub's `repo-config/configure.sh`.\n"))

    def test_the_hub_hosted_set_matches_the_ledger(self) -> None:
        """The literal is a copy of the ledger, so a retirement that misses it fails here loudly."""
        ledger = json.loads((REPO / "spec" / "divergences.json").read_text(encoding="utf-8"))
        retired = {
            e["path"]
            for group in ("dispositions", "gaps")
            for e in ledger.get(group, [])
            if isinstance(e, dict) and e.get("disposition") == "retire"
        }
        self.assertEqual(retired, set(prose_lint.HUB_HOSTED))

    def test_without_git_the_rule_stands_down(self) -> None:
        """No history means no deletion signature, so nothing is reported rather than guessed."""
        bare = self.tmp / "bare"
        bare.mkdir()
        path = bare / "DOC.md"
        path.write_text("Run `scripts/gone.py` to apply.\n", encoding="utf-8")
        self.assertEqual([], [k for _, k, _ in prose_lint.check_file(path, {"dead-path"})])

    def test_a_shallow_clone_stands_down_loudly(self) -> None:
        """A shallow clone holds no deletion history, so a run there says so and reports nothing.

        Without the announcement a shallow CI checkout would pass every mention forever, which
        is the silent-stop failure the sweep floor exists to prevent.
        """
        root = self.repo()
        (root / "DOC.md").write_text("Run `scripts/gone.py` to apply.\n", encoding="utf-8")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "doc")
        shallow = self.tmp / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{root}", str(shallow)],
            check=True,
            capture_output=True,
        )
        err = io.StringIO()
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
            contextlib.chdir(shallow),
        ):
            rc = prose_lint.main([".", "--check", "dead-path"])
        self.assertEqual(0, rc)
        self.assertIn("shallow", err.getvalue())

    def test_this_repository_is_clean_of_dead_paths(self) -> None:
        """The gate ships clean on its own tree, so every finding after this is a regression."""
        md = [
            p
            for p in REPO.rglob("*.md")
            if not prose_lint.GENERATED_ROOTS.intersection(p.relative_to(REPO).parts)
            and "reports" not in p.relative_to(REPO).parts
        ]
        hits = [
            (prose_lint.rel(p), ln, msg)
            for p in md
            for ln, kind, msg in prose_lint.check_file(p, {"dead-path"}, REPO)
            if kind == "dead-path"
        ]
        self.assertEqual([], hits)


class TestHarness(unittest.TestCase):
    def test_this_module_collects_a_plausible_number_of_cases(self) -> None:
        """A module whose cases fail to load still reports OK, which is a pass proving nothing."""
        loaded = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertGreaterEqual(loaded.countTestCases(), 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
