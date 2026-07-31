#!/usr/bin/env python3
"""Drive every repo_gate check against a tree it must reject.

Each check runs on a crafted temp root rather than on this repo, so a case proves the check
objects to the fault instead of proving the repo is currently clean. The coverage floors are the
other half: a check whose scan matches nothing reports zero issues and reads exactly like a pass.

Run as `python3 scripts/test_repo_gate.py`, or under `python3 -m unittest discover -s scripts`.
"""
from __future__ import annotations
import contextlib, io, re, shutil, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import repo_gate

REPO = Path(__file__).resolve().parent.parent
GOVERNANCE = REPO / 'GOVERNANCE.md'

PINNED = '9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0'


class TreeCase(unittest.TestCase):
    """Base for cases that build a temp root and run one check over it."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def workflow(self, body: str, name: str = 'w.yml') -> list[str]:
        wf = self.tmp / '.github' / 'workflows'
        wf.mkdir(parents=True, exist_ok=True)
        (wf / name).write_text(body, encoding='utf-8')
        return [f'.github/workflows/{name}']

    def eol_pair(self, gitattributes: str, editorconfig: str) -> list[str]:
        (self.tmp / '.gitattributes').write_text(gitattributes, encoding='utf-8')
        (self.tmp / '.editorconfig').write_text(editorconfig, encoding='utf-8')
        return repo_gate.check_eol(self.tmp, [])


class TestChecksTable(TreeCase):
    def test_every_check_shares_one_signature(self) -> None:
        """CHECKS was not uniformly callable, so no table-driven case could iterate it."""
        for name, fn in sorted(repo_gate.CHECKS.items()):
            with self.subTest(check=name):
                self.assertIsInstance(fn(self.tmp, []), list)

    def test_the_table_holds_every_check_the_cli_offers(self) -> None:
        self.assertGreaterEqual(len(repo_gate.CHECKS), 2)

    def test_the_patterns_compile_and_carry_no_invisible_characters(self) -> None:
        """A shell heredoc turns a backslash escape into a control character no diff shows."""
        for name in ('USES', 'PIN', 'WORKFLOW'):
            with self.subTest(pattern=name):
                self.assertTrue(re.compile(getattr(repo_gate, name).pattern))
        for action in repo_gate.SHA_EXCEPTIONS:
            with self.subTest(exception=action):
                self.assertTrue(action.isascii() and action.isprintable())


class TestShaPin(TreeCase):
    def test_a_floating_ref_is_flagged(self) -> None:
        files = self.workflow('jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n')
        hits = repo_gate.check_sha_pin(self.tmp, files)
        self.assertEqual(1, len(hits))
        self.assertIn('floating ref', hits[0])

    def test_a_missing_ref_is_flagged(self) -> None:
        files = self.workflow('jobs:\n  a:\n    steps:\n      - uses: actions/checkout\n')
        hits = repo_gate.check_sha_pin(self.tmp, files)
        self.assertEqual(1, len(hits))
        self.assertIn('no ref at all', hits[0])

    def test_a_forty_hex_pin_is_accepted(self) -> None:
        files = self.workflow(f'jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{PINNED} # v7\n')
        self.assertEqual([], repo_gate.check_sha_pin(self.tmp, files))

    def test_every_documented_exception_is_accepted(self) -> None:
        """Driven by the live exception set, so adding one without a reason shows up here."""
        for action in repo_gate.SHA_EXCEPTIONS:
            with self.subTest(exception=action):
                files = self.workflow(f'jobs:\n  a:\n    steps:\n      - uses: {action}@master\n')
                self.assertEqual([], repo_gate.check_sha_pin(self.tmp, files))

    def test_a_local_reusable_workflow_is_not_an_action(self) -> None:
        for ref in ('./.github/workflows/validate-task.yml', '.github/workflows/validate-task.yml'):
            with self.subTest(ref=ref):
                files = self.workflow(f'jobs:\n  a:\n    uses: {ref}\n')
                self.assertEqual([], repo_gate.check_sha_pin(self.tmp, files))


class TestEol(TreeCase):
    def test_a_gitattributes_pin_with_no_editorconfig_override_is_flagged(self) -> None:
        hits = self.eol_pair('*.sh text eol=lf\n', '[*]\nend_of_line = crlf\n')
        self.assertEqual(1, len(hits))
        self.assertIn('*.sh', hits[0])

    def test_a_matching_override_is_accepted(self) -> None:
        self.assertEqual([], self.eol_pair('*.sh text eol=lf\n',
                                           '[*.sh]\nend_of_line = lf\n'))

    def test_brace_syntax_is_expanded_on_both_sides(self) -> None:
        """EditorConfig brace globs have to expand or a real override reads as missing."""
        self.assertEqual([], self.eol_pair('*.sh text eol=lf\n',
                                           '[*.{sh,bash}]\nend_of_line = lf\n'))

    def test_a_path_pin_matches_a_directory_glob_override(self) -> None:
        """The three scripts are pinned by path while .editorconfig covers them with a glob."""
        self.assertEqual([], self.eol_pair('scripts/prose_lint.py text eol=lf\n',
                                           '[scripts/*.py]\nend_of_line = lf\n'))

    def test_a_comment_is_not_read_as_a_pin(self) -> None:
        self.assertEqual([], self.eol_pair('# *.sh text eol=lf\n', '[*]\nend_of_line = crlf\n'))

    def test_a_missing_config_is_named(self) -> None:
        (self.tmp / '.editorconfig').write_text('[*]\n', encoding='utf-8')
        self.assertEqual(['missing .gitattributes'], repo_gate.check_eol(self.tmp, []))


class TestGovernanceCoupling(unittest.TestCase):
    def test_the_exception_set_matches_what_the_doc_documents(self) -> None:
        """The doc calls it the one documented exception, so the code must not carry a second."""
        text = GOVERNANCE.read_text(encoding='utf-8')
        for action in repo_gate.SHA_EXCEPTIONS:
            with self.subTest(exception=action):
                self.assertIn(action, text, 'the code exempts an action the doc never names')
        self.assertEqual(1, len(repo_gate.SHA_EXCEPTIONS))


class TestCoverageFloors(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which('git') is None:
            self.skipTest('git absent, so the tracked-file floors cannot be read')

    def test_the_repo_sweep_finds_its_tracked_files(self) -> None:
        self.assertGreaterEqual(len(repo_gate.tracked(REPO)), 90)

    def test_the_sha_pin_scan_is_not_vacuous(self) -> None:
        """A workflow glob that matched nothing would print `0 issue(s)` and read as clean."""
        self.assertGreaterEqual(len(repo_gate.workflow_files(repo_gate.tracked(REPO))), 4)

    def test_a_root_with_no_tracked_files_exits_two_rather_than_zero(self) -> None:
        """An empty file set is a broken invocation, not a clean repo."""
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(2, repo_gate.main(['--root', d]))
        self.assertIn('not a git repo or no tracked files', err.getvalue())

    def test_this_repo_passes_both_checks_from_the_cli(self) -> None:
        """The gates run in CI from this entry point, so the entry point is what is proven."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, repo_gate.main(['--root', str(REPO)]))
        printed = out.getvalue()
        for name in repo_gate.CHECKS:
            with self.subTest(check=name):
                self.assertIn(f'[ok  ] {name}', printed)

    def test_a_single_check_runs_alone(self) -> None:
        """`--check` scopes the run, and a name outside CHECKS is rejected by the parser."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, repo_gate.main(['--root', str(REPO), '--check', 'sha-pin']))
        self.assertIn('sha-pin', out.getvalue())
        self.assertNotIn('eol', out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            repo_gate.main(['--check', 'no-such-check'])

    def test_a_failing_check_exits_one_and_prints_each_hit(self) -> None:
        """A gate that finds something has to say so and fail, not report and pass."""
        with mock.patch.dict(repo_gate.CHECKS, {'sha-pin': lambda root, files: ['a.yml: unpinned']}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(1, repo_gate.main(['--root', str(REPO), '--check', 'sha-pin']))
        self.assertIn('[FAIL] sha-pin      1 issue(s)', out.getvalue())
        self.assertIn('a.yml: unpinned', out.getvalue())

    def test_an_unreadable_workflow_is_skipped_rather_than_raising(self) -> None:
        """`git ls-files` lists a path a later commit deleted from the working tree."""
        self.assertEqual([], repo_gate.check_sha_pin(REPO, ['.github/workflows/absent.yml']))


class TestHarness(unittest.TestCase):
    def test_this_module_collects_a_plausible_number_of_cases(self) -> None:
        loaded = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertGreaterEqual(loaded.countTestCases(), 22)


if __name__ == '__main__':
    unittest.main(verbosity=2)
