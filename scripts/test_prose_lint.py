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

    def test_a_binary_file_is_not_scanned(self) -> None:
        blob = self.tmp / 'payload.md'
        blob.write_bytes(DUP.encode() + b'\x00binary\n')
        self.assertFalse(prose_lint.is_text(blob))


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


class TestHarness(unittest.TestCase):
    def test_this_module_collects_a_plausible_number_of_cases(self) -> None:
        """A module whose cases fail to load still reports OK, which is a pass proving nothing."""
        loaded = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertGreaterEqual(loaded.countTestCases(), 18)


if __name__ == '__main__':
    unittest.main(verbosity=2)
