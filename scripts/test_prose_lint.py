#!/usr/bin/env python3
"""Drive every prose_lint gate against input it must reject.

Each case reintroduces a fault and asserts the gate objects to it, because a gate nobody has
watched fail is a gate nobody knows works. Where a case covers a table, it reads the live table
rather than restating it: a proof that restates the gated data proves only that the function runs.

Run as `python3 scripts/test_prose_lint.py`, or under `python3 -m unittest discover -s scripts`.
"""
from __future__ import annotations
import contextlib, io, re, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import prose_lint

REPO = Path(__file__).resolve().parent.parent
GOVERNANCE = REPO / 'GOVERNANCE.md'

# Bait assembled from two literals, so this module never holds the pattern it feeds the gate.
# A file full of rejected input would otherwise report itself.
DUP = 'the ' + 'the'
SPLICE_BAIT = 'It runs on push; ' + 'it gates the merge'


class BaitCase(unittest.TestCase):
    """Base for cases that write a crafted file and read back what the gate says about it."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def kinds(self, text: str, rules: set[str], name: str = 'bait.md') -> list[str]:
        path = self.tmp / name
        path.write_text(text, encoding='utf-8')
        return [kind for _, kind, _ in prose_lint.check_file(path, rules)]


class TestTierTables(BaitCase):
    def test_every_tier_one_character_is_always_caught(self) -> None:
        """Tier 1 carries no meaning its ASCII form loses, so context never excuses it."""
        for ch, fix in prose_lint.TIER1.items():
            with self.subTest(codepoint=f'U+{ord(ch):04X}', fix=fix):
                self.assertEqual(['charset'], self.kinds(f'left {ch} right\n', {'charset'}))
                self.assertEqual(['charset'], self.kinds(f'8 {ch} 9\n', {'charset'}))

    def test_a_tab_separates_an_operator_from_its_neighbor(self) -> None:
        """Any whitespace, not only a literal space, sits between an operator and its figure."""
        for gap in (' ', '\t', '  '):
            with self.subTest(gap=repr(gap)):
                self.assertEqual([], self.kinds(f'threshold{gap}{chr(0x2264)}{gap}35\n', {'charset'}))

    def test_every_tier_two_character_turns_on_its_neighbors(self) -> None:
        """The same operator is the range it describes next to a figure, and prose between words."""
        for ch in prose_lint.TIER2:
            with self.subTest(codepoint=f'U+{ord(ch):04X}'):
                self.assertEqual([], self.kinds(f'threshold {ch} 35 units\n', {'charset'}))
                self.assertEqual([], self.kinds(f'0 {ch} x\n', {'charset'}))
                self.assertEqual(['charset'],
                                 self.kinds(f'the check {ch} the threshold\n', {'charset'}))

    def test_every_tier_three_character_is_left_alone(self) -> None:
        """A unit symbol whose ASCII form would be a lie is kept in prose and in a table alike."""
        for ch in prose_lint.TIER3:
            with self.subTest(codepoint=f'U+{ord(ch):04X}'):
                self.assertEqual([], self.kinds(f'reads 35 {ch} today\n', {'charset'}))
                self.assertEqual([], self.kinds(f'the {ch} value\n', {'charset'}))

    def test_an_unclassified_character_is_reported_not_passed(self) -> None:
        """A gate that allows whatever it does not recognize stops gating as the set grows."""
        unknown = chr(0x2603)
        self.assertNotIn(unknown, prose_lint.TIER1)
        self.assertNotIn(unknown, prose_lint.TIER2)
        self.assertNotIn(unknown, prose_lint.TIER3)
        self.assertEqual(['charset-unknown'],
                         self.kinds(f'a {unknown} here\n', {'charset-unknown'}))

    def test_keys_are_single_non_ascii_characters(self) -> None:
        """A key is a substitution target, so an ASCII key would flag text that is already fine."""
        for label, table in (('1', prose_lint.TIER1), ('2', prose_lint.TIER2),
                             ('3', prose_lint.TIER3)):
            for ch in table:
                with self.subTest(tier=label, codepoint=f'U+{ord(ch):04X}'):
                    self.assertEqual(1, len(ch))
                    self.assertFalse(ch.isascii())

    def test_replacements_are_printable_ascii(self) -> None:
        """The suggested form has to be typeable.

        `isprintable` rather than a codepoint floor: DEL and the Unicode format characters sit
        above 32 and are just as invisible in a diff. Applied to the replacements only, since the
        keys are non-ASCII by construction and two of them (U+00A0, U+2011) are not printable.
        """
        for label, table in (('1', prose_lint.TIER1), ('2', prose_lint.TIER2)):
            for ch, fix in table.items():
                with self.subTest(tier=label, codepoint=f'U+{ord(ch):04X}', fix=fix):
                    self.assertTrue(fix.isascii())
                    self.assertTrue(fix.isprintable())

    def test_the_source_carries_no_literal_non_ascii(self) -> None:
        """The table is written as escapes.

        A literal curly quote or U+00A0 in this module's own source is invisible in a diff, and
        the file is scanned by the rule it implements, so a literal would make it self-flagging.
        """
        for path in (REPO / 'scripts' / 'prose_lint.py', Path(__file__)):
            with self.subTest(source=path.name):
                bad = [f'U+{ord(c):04X}' for c in path.read_text(encoding='utf-8')
                       if not c.isascii()]
                self.assertEqual([], bad)

    def test_each_tier_covers_a_plausible_number_of_characters(self) -> None:
        """A table that shrank to nothing would satisfy every case above by having no entries."""
        for label, table, floor in (('1', prose_lint.TIER1, 12),
                                    ('2', prose_lint.TIER2, 8),
                                    ('3', prose_lint.TIER3, 7)):
            with self.subTest(tier=label):
                self.assertGreaterEqual(len(table), floor)


class TestGovernanceCoupling(unittest.TestCase):
    """The rule text drives the tables, rather than a copy of the rule driving them.

    These are the cases that catch an incomplete or mis-tiered table, which no bait built from the
    tables themselves can do: bait proves the matching works, not that the data is right.
    """

    def setUp(self) -> None:
        self.doc = GOVERNANCE.read_text(encoding='utf-8')
        section = re.search(r'^### Character Set$(.*?)^### ', self.doc, re.M | re.S)
        if section is None:
            self.fail('the Character Set heading moved, so the parse is blind')
        self.section = section.group(1)

    def tier_codepoints(self, label: str) -> set[int]:
        """Codepoints named in one tier's bullet, read out of the rule text itself."""
        m = re.search(rf'^- \*\*Tier {label},(.*?)(?=^- \*\*)', self.section, re.M | re.S)
        if m is None:
            self.fail(f'the Tier {label} bullet moved, so the parse is blind')
        return {int(h, 16) for h in re.findall(r'U\+([0-9A-Fa-f]{4})', m.group(1))}

    def test_every_tier_names_a_plausible_number_of_characters(self) -> None:
        """A tier bullet that stopped parsing would make every case below it pass vacuously."""
        for label, floor in (('1', 10), ('2', 8), ('3', 7)):
            with self.subTest(tier=label):
                self.assertGreaterEqual(len(self.tier_codepoints(label)), floor)

    def test_tier_one_and_two_are_in_the_gate_tables(self) -> None:
        for label, table in (('1', prose_lint.TIER1), ('2', prose_lint.TIER2)):
            for cp in sorted(self.tier_codepoints(label)):
                with self.subTest(tier=label, codepoint=f'U+{cp:04X}'):
                    self.assertIn(chr(cp), table)

    def test_tier_three_is_allowed_rather_than_replaced(self) -> None:
        """A tier-3 symbol in a replacement table would flag the character the rule protects."""
        for cp in sorted(self.tier_codepoints('3')):
            with self.subTest(codepoint=f'U+{cp:04X}'):
                self.assertIn(chr(cp), prose_lint.TIER3)
                self.assertNotIn(chr(cp), prose_lint.TIER1)
                self.assertNotIn(chr(cp), prose_lint.TIER2)

    def test_the_tiers_do_not_overlap(self) -> None:
        t1, t2, t3 = set(prose_lint.TIER1), set(prose_lint.TIER2), set(prose_lint.TIER3)
        self.assertEqual(set(), t1 & t2)
        self.assertEqual(set(), t1 & t3)
        self.assertEqual(set(), t2 & t3)


class TestDupword(BaitCase):
    def test_every_allowlist_entry_is_permitted(self) -> None:
        for phrase in prose_lint.DUP_ALLOW:
            with self.subTest(phrase=phrase):
                self.assertEqual([], self.kinds(f'A case where {phrase} reads correctly.\n',
                                                {'dupword'}))

    def test_a_repetition_outside_the_allowlist_is_flagged(self) -> None:
        """`the the` was in the allowlist while a comment claimed it was flagged."""
        self.assertEqual(['dupword'], self.kinds(f'{DUP} thing\n', {'dupword'}))

    def test_a_word_joining_character_does_not_start_a_repetition(self) -> None:
        """"either/or or" is a phrase followed by a conjunction, not a doubled word."""
        self.assertEqual([], self.kinds('either/or or must-pair inputs\n', {'dupword'}))
        self.assertEqual([], self.kinds('a must-pair pair of inputs\n', {'dupword'}))


class TestSemicolon(BaitCase):
    def test_a_splice_is_flagged(self) -> None:
        self.assertEqual(['semicolon'], self.kinds(f'{SPLICE_BAIT}.\n', {'semicolon'}))

    def test_a_quoted_counter_example_is_exempt_in_markdown(self) -> None:
        """A rule that states its counter-example quotes the construction it bans."""
        self.assertEqual([], self.kinds(f'Recast "{SPLICE_BAIT}" as two.\n', {'semicolon'}))

    def test_a_list_semicolon_is_not_a_splice(self) -> None:
        self.assertEqual([], self.kinds('Inputs: a, b, and c; outputs: d and e.\n', {'semicolon'}))

    def test_a_fenced_block_is_skipped(self) -> None:
        self.assertEqual([], self.kinds('```sh\nrun; it exits\n```\n', {'semicolon'}))


class TestDash(BaitCase):
    def test_a_clause_break_is_flagged(self) -> None:
        self.assertEqual(['dash'], self.kinds('It is capability, not permission - a token is not.\n',
                                              {'dash'}))

    def test_a_paired_aside_is_flagged_at_both_ends(self) -> None:
        self.assertEqual(['dash', 'dash'],
                         self.kinds('The router - a thin file - holds the map.\n', {'dash'}))

    def test_a_label_separator_is_exempt(self) -> None:
        """`- **Label** - explanation` is structurally a colon and the shape every bullet uses."""
        self.assertEqual([], self.kinds('- **Bug** - wrong behavior, missing coverage\n', {'dash'}))

    def test_a_later_dash_on_a_label_line_still_counts(self) -> None:
        """Exempting the separator must not exempt the rest of the line."""
        self.assertEqual(['dash'],
                         self.kinds('- **Bug** - wrong behavior - and worse besides\n', {'dash'}))

    def test_compound_words_and_ranges_are_left_alone(self) -> None:
        for text in ('A well-named must-pair input.\n', 'Sections D1 - D9 apply.\n',
                     '- a plain list item\n'):
            with self.subTest(text=text.strip()):
                self.assertEqual([], self.kinds(text, {'dash'}))


class TestSemicolon2(BaitCase):
    def test_any_prose_semicolon_is_flagged(self) -> None:
        """The rule bans the construction, so the default is to flag rather than to detect a subset."""
        self.assertEqual(['semicolon'], self.kinds(f'{SPLICE_BAIT}.\n', {'semicolon'}))

    def test_the_imperative_splice_the_old_pattern_missed_is_caught(self) -> None:
        """A pronoun-keyed pattern found 170 of 493, and this shape was the documented gap."""
        self.assertEqual(['semicolon'],
                         self.kinds('Delegate exploration; keep synthesis.\n', {'semicolon'}))

    def test_a_list_that_already_carries_commas_keeps_its_semicolon(self) -> None:
        self.assertEqual([], self.kinds('Inputs: a, b, and c; outputs: d and e.\n', {'semicolon'}))

    def test_a_splice_whose_clause_carries_a_comma_is_still_a_splice(self) -> None:
        """A comma earlier on the line is not a list, so it cannot excuse the semicolon."""
        self.assertEqual(['semicolon'],
                         self.kinds('It runs on push, always; it gates the merge.\n', {'semicolon'}))

    def test_prose_rules_do_not_reach_code_files(self) -> None:
        """A shell script carries statement separators, not prose, until comments can be extracted."""
        for name in ('bait.sh', 'bait.py', 'bait.yml'):
            with self.subTest(name=name):
                self.assertEqual([], self.kinds('a=1; b=2; it runs\n', {'semicolon', 'dash'},
                                                name=name))


class TestCommentWrap(BaitCase):
    """The comment rule reaches every syntax the fleet's project types carry, not only the hash ones."""

    RUN_ON = 'One thing happens. Another thing happens.'

    def flag(self, name: str, text: str) -> list[str]:
        """Both comment kinds, so a case cannot pass by asking for the rule it does not test."""
        return self.kinds(text, {'comment-wrap', 'comment-case'}, name=name)

    def test_a_run_on_is_caught_in_every_comment_syntax(self) -> None:
        """One case per syntax, so a failure names the language whose extractor broke."""
        for name, text in (
            ('a.cs', f'// {self.RUN_ON}\n'),
            ('a.cs', f'/* {self.RUN_ON} */\n'),
            ('a.cpp', f'// {self.RUN_ON}\n'),
            ('a.c', f'/* {self.RUN_ON} */\n'),
            ('a.py', f'x = 1  # {self.RUN_ON}\n'),
            ('a.sh', f'# {self.RUN_ON}\n'),
            ('a.ps1', f'<# {self.RUN_ON} #>\n'),
            ('a.yml', f'# {self.RUN_ON}\n'),
            ('a.toml', f'# {self.RUN_ON}\n'),
            ('a.ini', f'; {self.RUN_ON}\n'),
            ('a.jsonc', f'// {self.RUN_ON}\n'),
            ('a.json', f'// {self.RUN_ON}\n'),
            ('a.code-workspace', f'// {self.RUN_ON}\n'),
            ('a.xml', f'<!-- {self.RUN_ON} -->\n'),
            ('a.csproj', f'<!-- {self.RUN_ON} -->\n'),
        ):
            with self.subTest(file=name, syntax=text.strip()[:12]):
                self.assertEqual(['comment-wrap'], self.flag(name, text))

    def test_json_carries_comments_because_jsonc_is_what_ships(self) -> None:
        """VS Code tasks, devcontainer, and workspace files ship comments under a plain .json name."""
        for name in ('a.json', 'a.code-workspace', 'a.jsonc'):
            with self.subTest(file=name):
                self.assertEqual(['comment-wrap'], self.flag(name, f'// {self.RUN_ON}\n'))

    def test_a_marker_inside_a_string_is_not_a_comment(self) -> None:
        for name, text in (('a.cs', 'var s = "// no. Really.";\n'),
                           ('a.sh', 'echo "# no. Really."\n'),
                           ('a.json', '{"url": "https://x/y. Z"}\n'),
                           ('a.py', 'u = "http://x/#f. G"\n')):
            with self.subTest(file=name):
                self.assertEqual([], self.flag(name, text))

    def test_a_documentation_comment_is_left_to_codestyle(self) -> None:
        """An XML doc comment and a docstring may run to paragraphs, which CODESTYLE governs."""
        self.assertEqual([], self.flag('a.cs', f'/// <summary>{self.RUN_ON}</summary>\n'))
        self.assertEqual([], self.flag('a.py', f'"""{self.RUN_ON}"""\n'))

    def test_a_closed_block_doc_gives_the_rest_of_the_line_back(self) -> None:
        """CODESTYLE owns the documentation comment, not the line it happens to sit on.

        A line doc comment does run to end of line, so only the block form gives anything back.
        """
        self.assertEqual([], self.flag('a.cs', f'/** {self.RUN_ON} */\n'))
        self.assertEqual([], self.flag('a.cs', f'/// {self.RUN_ON} // and more\n'))
        self.assertEqual(['comment-wrap'],
                         self.flag('a.cs', '/** Docs. */ // Two things. Here.\n'))

    def test_a_multi_line_doc_block_owns_every_line_until_it_closes(self) -> None:
        """A marker in documentation text is prose, so scanning those lines invents comments.

        The closing line still gives back what follows the closer, which is the one finding here.
        """
        self.assertEqual(['comment-wrap'], self.flag('a.cs', '/** Docs start\n'
                                                             ' * // Two things. Here.\n'
                                                             ' * /* not an opener\n'
                                                             ' */ // Two things. Here.\n'))

    def test_verbatim_rules_apply_to_the_double_quoted_form_only(self) -> None:
        """C# spells a verbatim string with double quotes, so `@` on a char literal is ordinary.

        Under verbatim rules the doubled quote is one escaped character and both are blanked,
        so counting what survives tells the two readings apart.
        """
        masked, _ = prose_lint.strip_strings("var c = @'a''b'; // t", '"\'', True)
        self.assertEqual(4, masked.count("'"))

    def test_a_verbatim_string_spans_lines(self) -> None:
        """It is the one string form here that carries, so masking per line invents comments.

        The line that closes it still gives back what follows the quote.
        """
        # The marker on the second line is string content, so nothing is reported.
        self.assertEqual([], self.flag('a.cs', 'var s = @"line one\n'
                                               '// Two things. Here.\n'
                                               'line three";\n'))
        # The line that closes it still gives back the comment after the quote.
        self.assertEqual(['comment-wrap'], self.flag('a.cs', 'var s = @"line one\n'
                                                             'line two"; // Two things. Here.\n'))
        # A plain string ends on its own line, so the next line is ordinary code.
        self.assertEqual(['comment-wrap'], self.flag('a.cs', 'var s = "line one";\n'
                                                             '// Two things. Here.\n'))
        # Closing one and opening another leaves real code between them, which is not string content.
        self.assertEqual(['comment-wrap'],
                         self.flag('a.cs', 'var s = @"start\n'
                                           'end"; /* Two things. Here. */ var t = @"open again\n'
                                           'still string";\n'))

    def test_a_quote_in_comment_text_is_prose_rather_than_a_string(self) -> None:
        """Masking the comment too lets its quote open a string that blanks the markers after it.

        Within the line that costs the block its closer, and across lines the state carries and
        blanks every marker below until something closes it.
        """
        self.assertEqual(['comment-wrap'],
                         self.flag('a.cs', 'code(); /* note @"x */ code2(); // Two things. Here.\n'))
        # Each block line is its own sentence, so the two findings are the recovered comments.
        self.assertEqual(['comment-wrap', 'comment-wrap'],
                         self.flag('a.cs', '/* A note about @"paths.\n'
                                           '   And more. */ // Two things. Here.\n'
                                           'var x = 1; // Two things. Here.\n'))

    def test_only_a_c_style_continuation_loses_its_leading_asterisk(self) -> None:
        """The `*` continuing a `/* */` line is punctuation, and anywhere else it is prose.

        Taking it off an emphasis marker leaves a lowercase opening that the case rule reports,
        which is the rule judging text the extractor damaged.
        """
        for name, text in (('a.md', '<!-- *emphasis* leads here -->\n'),
                           ('a.ps1', '<# *emphasis* leads here #>\n'),
                           ('a.cs', '/* *emphasis* leads here */\n')):
            with self.subTest(file=name):
                self.assertEqual([], self.flag(name, text))
        # The convention still holds on the lines it was written for.
        self.assertEqual([(1, 'Start here.', True), (2, 'Still going.', True)],
                         prose_lint.extracted_comments(Path('a.cs'),
                                                       ['/* Start here.', ' * Still going. */']))
        # The marker is one `*` against whitespace, so a continuation keeps its own emphasis.
        for text, body in ((' * **bold** here */', '**bold** here'),
                           (' **bold** here */', '**bold** here'),
                           (' *emphasis* here */', '*emphasis* here')):
            with self.subTest(line=text):
                self.assertEqual([(1, 'Start.', True), (2, body, True)],
                                 prose_lint.extracted_comments(Path('a.cs'), ['/* Start.', text]))

    def test_a_format_with_no_comment_syntax_is_skipped(self) -> None:
        for name in ('a.lock', 'a.csv', 'a.txt'):
            with self.subTest(file=name):
                self.assertEqual([], self.flag(name, f'// {self.RUN_ON}\n'))

    def test_a_wrapped_sentence_is_caught_and_adjacency_is_required(self) -> None:
        """Two comments with code between them are separate, not one wrapped sentence."""
        self.assertEqual(['comment-wrap'],
                         self.flag('a.py', '# A sentence that keeps\n# going onto the next line.\n'))
        self.assertEqual([], self.flag('a.py', '# A label here\nx = 1\n# Another label\n'))

    def test_machinery_and_abbreviations_are_not_prose(self) -> None:
        for text in ('#!/usr/bin/env python3\n', '# ------------\n', '# noqa: S603 - fixed argv\n',
                     '# Uses e.g. Docker and i.e. Podman here.\n', '# Bump to 3.13 for the runner.\n',
                     '# See audit.py and validate.py for this.\n'):
            with self.subTest(text=text.strip()[:30]):
                self.assertEqual([], self.flag('a.py', text))

    def test_a_sentence_opening_in_lowercase_is_flagged(self) -> None:
        """A lowercase opening reads as the continuation of the line above it."""
        self.assertEqual(['comment-case'], self.flag('a.py', '# details are allowed here.\n'))

    def test_a_genuine_continuation_is_not_a_case_error(self) -> None:
        """A wrapped sentence is one finding, not two: the lowercase start is expected there."""
        self.assertEqual(['comment-wrap'],
                         self.flag('a.py', '# A sentence that keeps\n# going onto the next line.\n'))

    def test_a_capitalized_opening_and_a_code_token_are_both_accepted(self) -> None:
        """A backticked identifier does not open in lowercase, so it needs no restructuring."""
        for text in ('# The details element is allowed.\n', '# `ruff format` runs first.\n'):
            with self.subTest(text=text.strip()):
                self.assertEqual([], self.flag('a.py', text))

    def test_a_sentence_ending_in_an_acronym_is_still_two_sentences(self) -> None:
        """The initial guard anchored on any capital, and this codebase ends sentences in acronyms."""
        for text in ('# The check runs in CI. Another thing happens.\n',
                     '# Pinned by SHA. Dependabot still bumps it.\n'):
            with self.subTest(text=text.strip()[:40]):
                self.assertEqual(['comment-wrap'], self.flag('a.py', text))

    def test_a_second_sentence_may_open_in_either_case(self) -> None:
        """A lowercase opening is still a second sentence on the line."""
        self.assertEqual(['comment-wrap'],
                         self.flag('a.py', '# One thing happens. another thing happens.\n'))

    def test_an_initial_is_one_name_rather_than_two_sentences(self) -> None:
        """`J. Smith` is the case the guard exists for, and it must survive the widening."""
        self.assertEqual([], self.flag('a.py', '# Reviewed by J. Smith today.\n'))

    def test_a_trailing_comment_can_start_a_wrapped_sentence(self) -> None:
        """Clearing the predecessor on a trailing comment reported the wrong rule, not merely fewer.

        The pair below is a wrapped sentence, and it was reported as a capitalization error, whose
        advice would have been to capitalize the continuation rather than to un-wrap it.
        """
        self.assertEqual(['comment-wrap'],
                         self.flag('a.py', 'x = 1  # a sentence that keeps\n# going onto the next line.\n'))

    def test_a_trailing_annotation_does_not_continue_the_line_above(self) -> None:
        """A trailing comment annotates its own line, so it cannot be a continuation."""
        self.assertEqual([], self.flag('a.py', 'x = 1  # a thing that\ny = 2  # continues\n'))
        self.assertEqual([], self.flag('a.py', 'x = 1  # count of items\n# Another thing entirely.\n'))

    def test_a_comment_inside_a_fenced_block_is_skipped(self) -> None:
        """A fenced example is quoted code, so its comments belong to whatever is being shown."""
        self.assertEqual([], self.flag('a.md',
                                       'Prose.\n\n```html\n<!-- One thing. Another thing. -->\n```\n'))
        self.assertEqual(['comment-wrap'],
                         self.flag('a.md', 'Prose.\n\n<!-- One thing. Another thing. -->\n'))

    def test_a_block_opener_inside_a_line_comment_is_text(self) -> None:
        """Read as a real opener it opens a block, and the code lines below are linted as prose.

        The documentation form is the same case: exempting it from linting must not leave the
        ceiling unbounded, or the syntax whose doc marker is a line comment reopens the defect.
        """
        for name, text in (('a.cs', '// Match a /* opener here\nvar x = 1; // Two things. Here.\n'),
                           ('a.cs', '/// See a /* opener here\nvar x = 1; // Two things. Here.\n'),
                           ('a.ps1', '# Match a <# opener here\n$x = 1 # Two things. Here.\n')):
            with self.subTest(file=name, line=text.split('\n')[0]):
                self.assertEqual(['comment-wrap'], self.flag(name, text))

    def test_every_comment_on_a_line_is_read_not_just_the_first(self) -> None:
        """A ceiling can only describe the first comment, so a later one was unreachable.

        Each case puts the offending sentence in the second comment, which a scan that stops at
        the first reports as clean.
        """
        for name, text in (
            ('a.cs', 'var x = 1; /* Note. */ // Two things. Here.\n'),
            ('a.cs', '/* Note. */ /* Two things. Here. */\n'),
            ('a.cs', '/* Start here.\n   Still going. */ // Two things. Here.\n'),
        ):
            with self.subTest(line=text.split('\n')[0]):
                self.assertEqual(['comment-wrap'], self.flag(name, text))

    def test_a_verbatim_string_keeps_its_own_closing_quote(self) -> None:
        """A backslash is ordinary inside one and a doubled quote is the escape.

        Read with C escape rules the string never closes, so the masker blanks the rest of the
        line and the trailing comment goes unseen.
        """
        # Ending in a backslash, the string swallows its closing quote and hides a real comment.
        self.assertEqual(['comment-wrap'],
                         self.flag('a.cs', 'var p = @"C:\\tmp\\"; // Two things. Here.\n'))
        # Reading a doubled quote as a close then a reopen puts string content outside the string.
        self.assertEqual([],
                         self.flag('a.cs', 'var s = @"a""// One thing. Another thing.""b"; // ok\n'))
        # An interpolated one is spelled either way round, and only one of them abuts the quote.
        for text in ('var s = $@"C:\\tmp\\"; // Two things. Here.\n',
                     'var s = @$"C:\\tmp\\"; // Two things. Here.\n'):
            with self.subTest(line=text.strip()):
                self.assertEqual(['comment-wrap'], self.flag('a.cs', text))

    def test_only_the_syntax_that_has_verbatim_strings_gets_them(self) -> None:
        """C shares the C-like spec without the form, so `@` there is an ordinary character."""
        self.assertTrue(prose_lint.SYNTAX['.cs']['verbatim'])
        self.assertFalse(prose_lint.SYNTAX['.c']['verbatim'])
        self.assertFalse(prose_lint.SYNTAX['.json']['verbatim'])
        # The C escape still hides a marker, which is what the verbatim rule must not undo.
        self.assertEqual(['comment-wrap'],
                         self.flag('a.cs', 'var s = "a\\"b"; // Two things. Here.\n'))

    def test_css_has_block_comments_only(self) -> None:
        """A `//` in CSS is the scheme separator of a URL, not a comment marker."""
        self.assertEqual([], self.flag('a.css', 'a { background: url(http://x/y. Z); }\n'))
        self.assertEqual(['comment-wrap'], self.flag('a.css', '/* One thing. Another thing. */\n'))

    def test_a_version_pin_is_machinery_rather_than_prose(self) -> None:
        """The action-pinning rule requires a trailing `# vX.Y.Z`, which is a label, not a sentence."""
        self.assertEqual([], self.flag('a.yml', '  uses: x@sha # v7.0.0\n'))
        self.assertEqual([], self.flag('a.yml', '  uses: x@sha # v3\n'))

    def test_the_syntax_table_covers_a_plausible_number_of_extensions(self) -> None:
        """A table that shrank would make every case above pass by having nothing to dispatch on."""
        self.assertGreaterEqual(len(prose_lint.SYNTAX), 25)
        for label in ('.cs', '.cpp', '.py', '.sh', '.yml', '.json', '.jsonc', '.xml', '.ps1', '.ini'):
            with self.subTest(ext=label):
                self.assertIsNotNone(prose_lint.syntax_for(Path(f'x{label}')))


class TestMultiLineStrings(BaitCase):
    """A string that spans lines hides its markers on every line it covers, not only the first.

    Masking a line at a time leaves the lines below readable, so the comment rules report on string
    content and ask a reader to edit text that is data. Each case below puts the bait sentence
    inside a spanning string and asserts nothing is reported, then puts a real comment after the
    line that closes it and asserts that one still is - a carry that never releases would swallow
    the rest of the file, which reads exactly like a clean pass.
    """

    BAIT = 'Two things. Here.'

    def flag(self, name: str, text: str) -> list[str]:
        return self.kinds(text, {'comment-wrap', 'comment-case'}, name=name)

    def test_a_shell_quote_that_does_not_close_spans_lines(self) -> None:
        """Either quote form carries, and the line that closes it gives back what follows."""
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                self.assertEqual([], self.flag('a.sh', f's={quote}line one\n'
                                                       f'# {self.BAIT}\n'
                                                       f'line three{quote}\n'))
                self.assertEqual(['comment-wrap'],
                                 self.flag('a.sh', f's={quote}line one\n'
                                                   f'line two{quote}  # {self.BAIT}\n'))

    def test_a_shell_single_quoted_string_takes_no_backslash_escape(self) -> None:
        """Read with C escape rules the trailing backslash eats the closing quote.

        The string would then carry into every line below it, which is the failure the C# verbatim
        form already had within a line, one newline further on.
        """
        self.assertEqual(['comment-wrap'],
                         self.flag('a.sh', "p='C:\\tmp\\'\n"
                                           f'# {self.BAIT}\n'))

    def test_a_shell_single_quoted_string_is_neither_doubling_nor_escaped(self) -> None:
        """It takes no escape at all and cannot embed its own delimiter, so `'a''b'` is two strings.

        Declaring it doubling would state something false about the language. It happens to mask
        the same either way, since doubling and plain toggling agree on whether a run of quotes
        leaves a string open, so counting the quotes that survive is what tells the readings apart.
        """
        masked, carry = prose_lint.strip_strings(
            "echo 'a''b'", '"\'', False, prose_lint.CLEAR, prose_lint.SHELL['raw'],
            prose_lint.SHELL['escape'], prose_lint.SHELL['escape_in'],
            prose_lint.SHELL['escape_out'])
        self.assertEqual(4, masked.count("'"))
        self.assertEqual('', carry.kind)
        self.assertEqual('', prose_lint.SHELL['raw'])

    def test_a_shell_backslash_outside_a_string_escapes_the_next_character(self) -> None:
        r"""`'\''` is how shell embeds a quote in a single-quoted string, and it balances.

        Read without the outside-string escape it leaves one quote open, which then carries and
        blanks every line below it - a rule that reads nothing reports nothing.
        """
        self.assertEqual(['comment-wrap'],
                         self.flag('a.sh', "echo 'don'\\''t'\n"
                                           f'# {self.BAIT}\n'))

    def test_a_heredoc_runs_from_its_label_to_the_line_that_repeats_it(self) -> None:
        """Bare, quoted, and tab-stripping openers all name the same label."""
        for opener in ('<<EOF', "<<'EOF'", '<<"EOF"', '<<-EOF', '<< EOF'):
            with self.subTest(opener=opener):
                self.assertEqual(['comment-wrap'],
                                 self.flag('a.sh', f'cat {opener}\n'
                                                   f'# {self.BAIT}\n'
                                                   f'EOF\n'
                                                   f'# {self.BAIT}\n'))

    def test_only_the_exact_terminator_ends_a_heredoc(self) -> None:
        """An indented line is body content, and ending there resumes scanning inside the string.

        `<<-` strips leading tabs and nothing else, so a space-indented line is body under either
        form. The bait sits on the line after the near-miss, which a premature end reports.
        """
        for opener, near_miss in (('<<EOF', '  EOF'), ('<<EOF', '\tEOF'),
                                  ('<<-EOF', '  EOF'), ('<<EOF', 'EOF_NOT')):
            with self.subTest(opener=opener, near_miss=repr(near_miss)):
                self.assertEqual([], self.flag('a.sh', f'cat {opener}\n'
                                                       f'{near_miss}\n'
                                                       f'# {self.BAIT}\n'))

    def test_a_tab_indented_terminator_ends_a_dash_heredoc(self) -> None:
        """That is the whole point of `<<-`, so refusing it would run the heredoc to end of file."""
        self.assertEqual(['comment-wrap'],
                         self.flag('a.sh', 'cat <<-EOF\n'
                                           '\tbody\n'
                                           '\tEOF\n'
                                           f'# {self.BAIT}\n'))

    def test_heredocs_stacked_on_one_line_are_read_in_order(self) -> None:
        """Each body belongs to its own label, so clearing the first must open the second."""
        self.assertEqual(['comment-wrap'],
                         self.flag('a.sh', 'cat <<A <<B\n'
                                           f'# {self.BAIT}\n'
                                           'A\n'
                                           f'# {self.BAIT}\n'
                                           'B\n'
                                           f'# {self.BAIT}\n'))

    def test_a_here_string_and_a_quoted_marker_do_not_open_a_heredoc(self) -> None:
        """`<<<` is one line, and a `<<` inside a string or a comment is text."""
        for line in ('jq -r ".x" <<<"$out"', 'echo "cat <<EOF"', '# Match a <<EOF here'):
            with self.subTest(line=line):
                self.assertEqual(['comment-wrap'], self.flag('a.sh', f'{line}\n'
                                                                    f'# {self.BAIT}\n'))

    def test_a_powershell_here_string_spans_lines_in_both_quote_forms(self) -> None:
        """It opens on `@"` or `@'` at the end of a line and closes on the matching token."""
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                self.assertEqual([], self.flag('a.ps1', f'$s = @{quote}\n'
                                                        f'# {self.BAIT}\n'
                                                        f'{quote}@\n'))
                # The closing line gives back what follows the token, as a closing quote does.
                self.assertEqual(['comment-wrap'],
                                 self.flag('a.ps1', f'$s = @{quote}\n'
                                                    f'body\n'
                                                    f'{quote}@  # {self.BAIT}\n'))

    def test_a_powershell_double_quoted_string_is_escaped_and_doubling_at_once(self) -> None:
        """Its escape is a backtick, and it also embeds the delimiter by doubling it.

        Neither property implies the other, so both are read per string. Missing the backtick ends
        the string on the escaped quote, and reading a backslash as an escape consumes the closing
        one. The single-quoted form takes no backtick escape, so its escape is doubling only.
        """
        for code in ('$s = "a`"b"', '$s = "a""b"', "$s = 'a`'", "$s = 'a''b'",
                     '$p = "C:\\tmp\\"'):
            with self.subTest(code=code):
                self.assertEqual(['comment-wrap'], self.flag('a.ps1', f'{code}  # {self.BAIT}\n'))

    def test_a_here_string_closes_only_on_a_token_at_the_start_of_the_line(self) -> None:
        """PowerShell requires the closer at column 0, so an indented one is here-string content."""
        self.assertEqual([], self.flag('a.ps1', '$s = @"\n'
                                                '  "@ is not the closer\n'
                                                f'# {self.BAIT}\n'
                                                '"@\n'))

    def test_a_powershell_quote_carries_because_the_escape_is_a_backtick(self) -> None:
        """A backslash is an ordinary character there, so it cannot consume the closing quote."""
        self.assertEqual([], self.flag('a.ps1', '$s = "line one\n'
                                                f'# {self.BAIT}\n'
                                                'line three"\n'))
        self.assertEqual(['comment-wrap'],
                         self.flag('a.ps1', '$p = "C:\\tmp\\"  # ' + self.BAIT + '\n'))

    def test_a_yaml_block_scalar_is_data_until_the_indentation_drops(self) -> None:
        """Every header form opens one, and the line that dedents is code again."""
        for header in ('key: |', 'key: >', 'key: |-', 'key: >-', 'key: |2', '  - |'):
            with self.subTest(header=header):
                self.assertEqual(['comment-wrap'],
                                 self.flag('a.yml', f'{header}\n'
                                                    f'    # {self.BAIT}\n'
                                                    f'\n'
                                                    f'    still data\n'
                                                    f'next: 1  # {self.BAIT}\n'))

    def test_a_run_scalar_stays_a_script_the_comment_rules_govern(self) -> None:
        """Its `#` lines are shell comments, so treating the block as data would stop linting them.

        A data key holds text the reader cannot edit, which is the case the block rule is for.
        The chomping and indent indicators ride along on the header, so every form of it counts.
        """
        for header in ('run: |', 'run: |-', 'run: |+', 'run: |2', 'run: >', 'run: >-',
                       '      run: |'):
            with self.subTest(header=header):
                self.assertEqual(['comment-wrap'],
                                 self.flag('a.yml', f'{header}\n        # {self.BAIT}\n'))
        for header in ('files: |', 'files: |-', 'tags: >-'):
            with self.subTest(header=header):
                self.assertEqual([], self.flag('a.yml', f'{header}\n  # {self.BAIT}\n'))

    def test_a_pipe_that_is_not_a_block_header_opens_nothing(self) -> None:
        """A plain scalar ending in a pipe is a value, not a block indicator."""
        self.assertEqual(['comment-wrap'], self.flag('a.yml', 'key: a | b\n'
                                                              f'# {self.BAIT}\n'))

    def test_a_single_quoted_yaml_or_toml_scalar_takes_no_backslash_escape(self) -> None:
        r"""A trailing backslash would otherwise consume the closing quote and hide the comment.

        Both languages spell the escape as a doubled quote in the single-quoted form, and both
        keep the backslash escape in the double-quoted one, so the two forms are read differently
        in the same file. The bait is a trailing comment, which only survives a correct read.
        """
        for name, code in (('a.yml', "key: 'C:\\tmp\\'"), ('a.yml', "key: 'it''s'"),
                           ('a.toml', "s = 'C:\\tmp\\'"), ('a.toml', "s = 'it''s'")):
            with self.subTest(file=name, code=code):
                self.assertEqual(['comment-wrap'], self.flag(name, f'{code}  # {self.BAIT}\n'))
        # The double-quoted form keeps it, so an escaped quote does not close the string early.
        for name, code in (('a.yml', 'key: "a\\"b"'), ('a.toml', 's = "a\\"b"')):
            with self.subTest(file=name, code=code):
                self.assertEqual(['comment-wrap'], self.flag(name, f'{code}  # {self.BAIT}\n'))

    def test_a_form_carries_only_in_the_syntax_that_has_it(self) -> None:
        """A YAML plain scalar's apostrophe is not a string, and a TOML file has no heredoc.

        Carrying either would blank every line below it, and a rule that reads nothing reports
        nothing, so the file would go quiet rather than fail.
        """
        for name, opener in (('a.yml', "key: don't"), ('a.yml', 'key: "unclosed'),
                             ('a.toml', "s = 'unclosed"), ('a.toml', 'cat <<EOF'),
                             ('a.ini', "k = don't"), ('a.json', '{"a": "unclosed')):
            with self.subTest(file=name, opener=opener):
                self.assertEqual(['comment-wrap'], self.flag(name, f'{opener}\n'
                                                                   f'# {self.BAIT}\n'
                                                                   f'// {self.BAIT}\n'))

    def test_every_declared_carry_kind_is_one_the_extractor_implements(self) -> None:
        """A typo in a `carry` set would silently disable the form it was meant to turn on."""
        implemented = {'quote', 'verbatim', 'here', 'label', 'block'}
        for label, spec in sorted(prose_lint.SYNTAX.items()):
            with self.subTest(ext=label):
                self.assertLessEqual(set(spec['carry']), implemented)
                # A raw quote has to be one the syntax reads as a quote in the first place.
                self.assertLessEqual(set(spec['raw']), set(spec['quotes']))
                # An escape character that is also a quote would never reach the escape branch.
                self.assertEqual(set(), set(spec['escape']) & set(spec['quotes']))
                # The escape works inside quotes the syntax actually has, and is one character.
                self.assertLessEqual(set(spec['escape_in']), set(spec['quotes']) | {'"', "'"})
                self.assertEqual(1, len(spec['escape']))
                # A syntax with nowhere for its escape to apply would carry a dead field.
                self.assertTrue(spec['escape_in'] or spec['escape_out'])

    def test_the_carrying_syntaxes_are_still_wired_to_their_extensions(self) -> None:
        """Every case above dispatches on a suffix, so a rewired table would pass them vacuously."""
        for label, kind in (('.sh', 'label'), ('.bash', 'quote'), ('.yml', 'block'),
                            ('.yaml', 'block'), ('.ps1', 'here'), ('.cs', 'verbatim')):
            with self.subTest(ext=label):
                self.assertIn(kind, prose_lint.SYNTAX[label]['carry'])
        self.assertIn('label', prose_lint.BY_NAME['dockerfile']['carry'])

    def test_a_string_opens_from_code_after_a_closed_block_comment(self) -> None:
        """The code a line holds is every span outside its comments, not only the first one.

        Reading only the code before the first marker misses an opener that sits after a block
        comment that closed on the same line. The here-string then falls back to an ordinary
        carried quote, which the first quote in its body closes, and scanning resumes inside it.
        """
        for opener in ('<# Note. #> $s = @"', '$s = @"'):
            with self.subTest(opener=opener):
                self.assertEqual([],
                                 self.flag('a.ps1', f'{opener}\n'
                                                    'he said "hi\n'
                                                    f'# {self.BAIT}\n'
                                                    '"@\n'))

    def test_a_string_does_not_open_under_a_block_comment(self) -> None:
        """The block comment owns the lines below, so its text cannot open one."""
        self.assertEqual(['comment-wrap', 'comment-wrap'],
                         self.flag('a.ps1', '<# Note @"\n'
                                            f'   {self.BAIT} #>\n'
                                            f'$x = 1 # {self.BAIT}\n'))


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
        r = subprocess.run(['git', '-C', str(REPO), 'check-ignore', '--stdin'],
                           input='\n'.join(str(p) for p in found),
                           capture_output=True, text=True)
        self.assertEqual('', r.stdout.strip())

    def test_the_fallback_skips_generated_roots(self) -> None:
        """`git check-ignore` fails on exactly the machine with no git, so this path names them."""
        (self.tmp / '.mypy_cache').mkdir()
        (self.tmp / '.mypy_cache' / 'cached.md').write_text(f'{DUP}\n', encoding='utf-8')
        (self.tmp / 'authored.md').write_text('fine\n', encoding='utf-8')
        found = prose_lint.walk_paths(self.tmp)
        self.assertEqual(['authored.md'],
                         sorted(p.relative_to(self.tmp).as_posix() for p in found))

    def test_empty_git_output_falls_back_rather_than_scanning_nothing(self) -> None:
        """An initialized but empty checkout exits 0 with no output.

        Reading that as an empty file set would scan nothing and report success, which is the
        shape of a gate that has silently stopped gating.
        """
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')
        with mock.patch.object(prose_lint.subprocess, 'run', return_value=done):
            self.assertIsNone(prose_lint.tracked_paths(self.tmp))

    def test_a_dot_prefixed_path_keeps_its_prefix(self) -> None:
        """`lstrip` took a character set and ate the leading dot, so --diff never matched .github."""
        self.assertEqual('.github/workflows/x.yml',
                         prose_lint.rel(Path('.github/workflows/x.yml')))
        self.assertEqual('.editorconfig', prose_lint.rel(Path('.editorconfig')))
        self.assertEqual('GOVERNANCE.md', prose_lint.rel(Path('./GOVERNANCE.md')))

    def test_a_subdirectory_root_yields_paths_that_exist(self) -> None:
        """`git ls-files` prints paths relative to its `-C` directory, not the repo top level.

        Review read it the other way round, which would make the join in tracked_paths produce
        broken paths. It does not, and the invariant is pinned here because passing `--full-name`
        would flip the behavior with nothing else to notice.
        """
        for root in (REPO / 'spec', REPO / 'scripts'):
            with self.subTest(root=root.name):
                found = prose_lint.discover([str(root)])
                self.assertGreaterEqual(len(found), 5)
                self.assertEqual([], [str(p) for p in found if not p.exists()])

    def test_an_explicit_file_argument_bypasses_discovery(self) -> None:
        """A single file has to be checkable directly, including one git does not track."""
        loose = self.tmp / 'loose.md'
        loose.write_text('fine\n', encoding='utf-8')
        self.assertEqual([loose], prose_lint.discover([str(loose)]))

    def test_a_directory_git_cannot_describe_warns_and_walks_it(self) -> None:
        """Silently scanning nothing there would report a clean run over an unread tree."""
        (self.tmp / 'authored.md').write_text('fine\n', encoding='utf-8')
        with mock.patch.object(prose_lint, 'tracked_paths', return_value=None), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            found = prose_lint.discover([str(self.tmp)])
        self.assertEqual(['authored.md'], [p.name for p in found])
        self.assertIn('falling back to a filesystem walk', err.getvalue())

    def test_an_excluded_path_is_dropped(self) -> None:
        (self.tmp / 'keep.md').write_text('fine\n', encoding='utf-8')
        (self.tmp / 'drop.md').write_text('fine\n', encoding='utf-8')
        with mock.patch.object(prose_lint, 'tracked_paths', return_value=None), \
                contextlib.redirect_stderr(io.StringIO()):
            found = prose_lint.discover([str(self.tmp)], ('drop.md',))
        self.assertEqual(['keep.md'], [p.name for p in found])

    def test_an_unreadable_root_is_not_a_file_set(self) -> None:
        """`tracked_paths` answers None on the error paths, never an empty list read as clean."""
        with mock.patch.object(prose_lint.subprocess, 'run', side_effect=OSError):
            self.assertIsNone(prose_lint.tracked_paths(self.tmp))
        failed = subprocess.CompletedProcess(args=[], returncode=128, stdout='', stderr='')
        with mock.patch.object(prose_lint.subprocess, 'run', return_value=failed):
            self.assertIsNone(prose_lint.tracked_paths(self.tmp))

    def test_an_unopenable_path_is_not_text(self) -> None:
        self.assertFalse(prose_lint.is_text(self.tmp / 'absent.md'))

    def test_a_binary_file_is_not_scanned(self) -> None:
        blob = self.tmp / 'payload.md'
        blob.write_bytes(DUP.encode() + b'\x00binary\n')
        self.assertFalse(prose_lint.is_text(blob))


class TestSentenceSplit(BaitCase):
    """One sentence per line, the markdown counterpart of the comment-wrap rule."""

    def test_a_sentence_continuing_onto_the_next_line_is_flagged(self) -> None:
        self.assertEqual(['sentence-split'],
                         self.kinds('A sentence that keeps\ngoing onto the next line.\n',
                                    {'sentence-split'}))

    def test_a_finished_sentence_does_not_continue(self) -> None:
        for text in ('One sentence.\nAnother sentence.\n',
                     'A question?\nan answer follows.\n',
                     'One sentence.\nA capital opens the next.\n'):
            with self.subTest(text=text.split('\n')[1]):
                self.assertEqual([], self.kinds(text, {'sentence-split'}))

    def test_structure_is_not_a_wrapped_sentence(self) -> None:
        """A table row, a quote, a heading, and a link definition are not prose lines.

        A colon, a dash, or a pipe at the end of the previous line introduces what follows it,
        so the next line starts a new construct rather than continuing a sentence.
        """
        for text in ('| a | b |\n| c | d |\n',
                     '> quoted line\n> continues here\n',
                     '# Heading\nthe text below it.\n',
                     '[ref]: ./a.md\n[other]: ./b.md\n',
                     'The inputs are:\nthe first one.\n',
                     'A line ending in a dash -\nthe continuation.\n'):
            with self.subTest(text=text.split('\n')[0]):
                self.assertEqual([], self.kinds(text, {'sentence-split'}))

    def test_the_rule_is_markdown_only(self) -> None:
        """A source file's wrapped lines are code, which comment-wrap judges instead."""
        self.assertEqual([], self.kinds('a sentence that keeps\ngoing onto the next line.\n',
                                        {'sentence-split'}, name='bait.py'))


class TestSyntaxDispatch(unittest.TestCase):
    def test_an_extensionless_file_is_read_as_hash_commented(self) -> None:
        """A shebang script or a config with no suffix is far more often `#` than nothing."""
        self.assertEqual(prose_lint.HASH, prose_lint.syntax_for(Path('somescript')))

    def test_a_format_with_no_comments_and_an_unknown_suffix_are_both_skipped(self) -> None:
        for name in ('data.lock', 'data.csv', 'image.png', 'archive.7z'):
            with self.subTest(file=name):
                self.assertIsNone(prose_lint.syntax_for(Path(name)))

    def test_a_name_match_beats_the_suffix_table(self) -> None:
        self.assertEqual(prose_lint.INI, prose_lint.syntax_for(Path('.editorconfig')))

    def test_python_that_will_not_tokenize_falls_back_to_the_line_scan(self) -> None:
        """`tokenize` raises on a half-written file, and the rule still has to read its comments."""
        self.assertIsNone(prose_lint.python_comments('def f(:\n'))
        self.assertEqual(['comment-wrap'],
                         self.flag('def f(:\n# Two things. Here.\n'))

    def flag(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'bait.py'
            path.write_text(text, encoding='utf-8')
            return [kind for _, kind, _ in
                    prose_lint.check_file(path, {'comment-wrap', 'comment-case'})]


class TestChangedLines(unittest.TestCase):
    """The `--diff` scope, which decides which findings a CI run is allowed to report.

    The repo policy is that existing prose is corrected as each file is next edited rather than
    swept, and this parse is the whole mechanism behind it. A parse that returns too little makes
    the warn-only step silently stop reporting, and one that returns too much reports the backlog
    as if the change introduced it.
    """

    DIFF = (
        'diff --git a/a.md b/a.md\n'
        '--- a/a.md\n'
        '+++ b/a.md\n'
        '@@ -1 +1 @@\n'
        '-old\n'
        '+new\n'
        '@@ -10,0 +11,3 @@\n'
        '+one\n+two\n+three\n'
        'diff --git a/.github/workflows/x.yml b/.github/workflows/x.yml\n'
        '--- a/.github/workflows/x.yml\n'
        '+++ b/.github/workflows/x.yml\n'
        '@@ -5,2 +5,0 @@\n'
        '-gone\n-also gone\n'
    )

    def run_diff(self, stdout: str = '', returncode: int = 0):
        done = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr='')
        with mock.patch.object(prose_lint.subprocess, 'run', return_value=done):
            return prose_lint.changed_lines('origin/develop')

    def test_each_hunk_maps_to_the_lines_it_adds(self) -> None:
        """A single-line hunk carries no count, and a deletion-only hunk adds nothing."""
        got = self.run_diff(self.DIFF)
        self.assertEqual({1, 11, 12, 13}, got['a.md'])
        self.assertEqual(set(), got['.github/workflows/x.yml'])

    def test_a_dot_prefixed_path_survives_the_parse(self) -> None:
        """The key has to match `rel()`, or a finding under a dot directory is never in scope."""
        self.assertIn('.github/workflows/x.yml', self.run_diff(self.DIFF))

    def test_a_hunk_before_its_file_header_is_not_attributed_to_the_previous_file(self) -> None:
        """Reading a stray hunk against whichever file came last invents a scope."""
        self.assertEqual({}, self.run_diff('@@ -1 +1 @@\n+orphan\n'))

    def test_a_git_failure_is_none_rather_than_an_empty_scope(self) -> None:
        """An empty scope filters every file out and reports a clean run, which is a false pass."""
        with mock.patch.object(prose_lint.subprocess, 'run',
                               side_effect=subprocess.CalledProcessError(1, 'git')):
            self.assertIsNone(prose_lint.changed_lines('origin/develop'))
        with mock.patch.object(prose_lint.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertIsNone(prose_lint.changed_lines('origin/develop'))


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # The main() call prints findings, which would read as real ones in a CI log.
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def test_main_exits_nonzero_on_a_bait_tree(self) -> None:
        """The CLI path, not only check_file, reports the finding and exits 1."""
        bait = self.tmp / 'bait.md'
        bait.write_text(f'{DUP} thing\n', encoding='utf-8')
        with mock.patch.object(prose_lint, 'discover', return_value=[bait]):
            self.assertEqual(1, prose_lint.main(['--check', 'dupword']))

    def test_main_exits_zero_on_a_clean_tree(self) -> None:
        clean = self.tmp / 'clean.md'
        clean.write_text('Nothing here breaks a rule.\n', encoding='utf-8')
        with mock.patch.object(prose_lint, 'discover', return_value=[clean]):
            self.assertEqual(0, prose_lint.main(['--check', 'dupword']))

    def test_every_rule_name_is_offered_by_the_cli(self) -> None:
        """RULES is the single source, so a rule cannot exist in check_file and not in --check."""
        for name in prose_lint.RULES:
            with self.subTest(rule=name), mock.patch.object(prose_lint, 'discover',
                                                           return_value=[]):
                self.assertEqual(0, prose_lint.main(['--check', name]))

    def test_default_rules_are_a_subset_of_the_declared_rules(self) -> None:
        self.assertLessEqual(set(prose_lint.DEFAULT_RULES), set(prose_lint.RULES))

    def test_diff_scope_reports_only_the_changed_lines(self) -> None:
        """A finding on an untouched line is the backlog, which the diff run must not attribute."""
        bait = self.tmp / 'bait.md'
        bait.write_text(f'{DUP} thing\nA clean line.\n{DUP} again\n', encoding='utf-8')
        with mock.patch.object(prose_lint, 'discover', return_value=[bait]), \
                mock.patch.object(prose_lint, 'changed_lines',
                                  return_value={prose_lint.rel(bait): {3}}):
            self.assertEqual(1, prose_lint.main(['--check', 'dupword', '--diff', 'HEAD']))
        with mock.patch.object(prose_lint, 'discover', return_value=[bait]), \
                mock.patch.object(prose_lint, 'changed_lines',
                                  return_value={prose_lint.rel(bait): {2}}):
            self.assertEqual(0, prose_lint.main(['--check', 'dupword', '--diff', 'HEAD']))

    def test_a_file_outside_the_diff_is_dropped_entirely(self) -> None:
        bait = self.tmp / 'bait.md'
        bait.write_text(f'{DUP} thing\n', encoding='utf-8')
        with mock.patch.object(prose_lint, 'discover', return_value=[bait]), \
                mock.patch.object(prose_lint, 'changed_lines', return_value={'other.md': {1}}):
            self.assertEqual(0, prose_lint.main(['--check', 'dupword', '--diff', 'HEAD']))

    def test_a_failed_diff_falls_back_to_the_whole_tree(self) -> None:
        """Scoping to nothing would report a clean run, so an unusable diff widens instead."""
        bait = self.tmp / 'bait.md'
        bait.write_text(f'{DUP} thing\n', encoding='utf-8')
        with mock.patch.object(prose_lint, 'discover', return_value=[bait]), \
                mock.patch.object(prose_lint, 'changed_lines', return_value=None):
            self.assertEqual(1, prose_lint.main(['--check', 'dupword', '--diff', 'HEAD']))

    def test_list_files_prints_the_scope_and_reports_nothing(self) -> None:
        """The audit path for the sweep scope exits 0 even on a tree full of findings."""
        bait = self.tmp / 'bait.md'
        bait.write_text(f'{DUP} thing\n', encoding='utf-8')
        with mock.patch.object(prose_lint, 'discover', return_value=[bait]):
            self.assertEqual(0, prose_lint.main(['--check', 'dupword', '--list-files']))

    def test_summary_mode_reports_the_totals_without_the_per_finding_lines(self) -> None:
        bait = self.tmp / 'bait.md'
        bait.write_text(f'{DUP} thing\n', encoding='utf-8')
        with mock.patch.object(prose_lint, 'discover', return_value=[bait]):
            self.assertEqual(1, prose_lint.main(['--check', 'dupword', '--summary']))

    def test_an_unreadable_file_is_skipped_rather_than_raising(self) -> None:
        """A sweep is scoped by what git tracks, which includes files this process cannot decode."""
        blob = self.tmp / 'payload.md'
        blob.write_bytes(b'\xff\xfe not utf-8\n')
        self.assertEqual([], prose_lint.check_file(blob, {'dupword'}))
        self.assertEqual([], prose_lint.check_file(self.tmp / 'absent.md', {'dupword'}))


class TestHarness(unittest.TestCase):
    def test_this_module_collects_a_plausible_number_of_cases(self) -> None:
        """A module whose cases fail to load still reports OK, which is a pass proving nothing."""
        loaded = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertGreaterEqual(loaded.countTestCases(), 100)


if __name__ == '__main__':
    unittest.main(verbosity=2)
