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

# Bait for the duplicate-word rule, assembled so this module does not itself hold the pattern it
# feeds the gate. A test file full of rejected input would otherwise report itself, which is the
# self-flagging problem the quoted-span exemption solves for prose.
DUP = 'the ' + 'the'


class BaitCase(unittest.TestCase):
    """Base for cases that write a crafted file and read back what the gate says about it."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def kinds(self, text: str, rules: set[str], name: str = 'bait.md') -> list[str]:
        path = self.tmp / name
        path.write_text(text, encoding='utf-8')
        return [kind for _, kind, _ in prose_lint.check_file(path, rules)]


class TestSuggestTable(BaitCase):
    def test_every_entry_is_caught(self) -> None:
        """Each table key, placed in a line, produces one ascii finding."""
        for ch, fix in prose_lint.SUGGEST.items():
            with self.subTest(codepoint=f'U+{ord(ch):04X}', fix=fix):
                self.assertEqual(['ascii'], self.kinds(f'left {ch} right\n', {'ascii'}))

    def test_keys_are_single_non_ascii_characters(self) -> None:
        """A key is a substitution target, so an ASCII key would flag text that is already fine."""
        for ch in prose_lint.SUGGEST:
            with self.subTest(codepoint=f'U+{ord(ch):04X}'):
                self.assertEqual(1, len(ch))
                self.assertFalse(ch.isascii())

    def test_replacements_are_printable_ascii(self) -> None:
        """The suggested form has to be typeable.

        `isprintable` rather than a codepoint floor: DEL and the Unicode format characters sit
        above 32 and are just as invisible in a diff. Applied to the replacements only, since the
        keys are non-ASCII by construction and two of them (U+00A0, U+2011) are not printable.
        """
        for ch, fix in prose_lint.SUGGEST.items():
            with self.subTest(codepoint=f'U+{ord(ch):04X}', fix=fix):
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

    def test_the_table_covers_a_plausible_number_of_characters(self) -> None:
        """A table that shrank to nothing would satisfy every case above by having no entries."""
        self.assertGreaterEqual(len(prose_lint.SUGGEST), 14)


class TestGovernanceCoupling(unittest.TestCase):
    def test_every_codepoint_the_charset_rule_names_is_covered(self) -> None:
        """The rule text drives the table, rather than a copy of the rule driving it.

        This is the case that catches an incomplete table, which no bait built from the table
        itself can do. Three characters in SUGGEST are deliberately not named by the rule
        (U+00A0, U+2022, U+2011), so the relation is one-directional.
        """
        named = {int(m, 16) for m in re.findall(r'U\+([0-9A-Fa-f]{4})',
                                               GOVERNANCE.read_text(encoding='utf-8'))}
        self.assertGreaterEqual(len(named), 8, 'the doc parse found almost nothing, the anchor moved')
        missing = sorted(f'U+{cp:04X}' for cp in named if chr(cp) not in prose_lint.SUGGEST)
        self.assertEqual([], missing)


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
        self.assertEqual(['semicolon'], self.kinds('It runs on push; it gates the merge.\n',
                                                   {'semicolon'}))

    def test_a_quoted_counter_example_is_exempt_in_markdown(self) -> None:
        """A rule that states its counter-example quotes the construction it bans."""
        self.assertEqual([], self.kinds('Recast "it runs on push; it gates the merge" as two.\n',
                                        {'semicolon'}))

    def test_a_quoted_span_is_not_exempt_outside_markdown(self) -> None:
        """In data and code a double quote is structural, so the prose inside it still counts."""
        self.assertEqual(['semicolon'],
                         self.kinds('{ "note": "it runs on push; it gates the merge" }\n',
                                    {'semicolon'}, name='bait.json'))

    def test_a_list_semicolon_is_not_a_splice(self) -> None:
        self.assertEqual([], self.kinds('Inputs: a, b, and c; outputs: d and e.\n', {'semicolon'}))

    def test_a_fenced_block_is_skipped(self) -> None:
        self.assertEqual([], self.kinds('```sh\nrun; it exits\n```\n', {'semicolon'}))


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

    def test_a_binary_file_is_not_scanned(self) -> None:
        blob = self.tmp / 'payload.md'
        blob.write_bytes(DUP.encode() + b'\x00binary\n')
        self.assertFalse(prose_lint.is_text(blob))


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # main() reports findings on stdout/stderr, which would read as real findings in a CI log.
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
