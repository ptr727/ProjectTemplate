#!/usr/bin/env python3
"""Drive every repo_gate check against a tree it must reject.

Each check runs on a crafted temp root rather than on this repo, so a case proves the check
objects to the fault instead of proving the repo is currently clean. The coverage floors are the
other half: a check whose scan matches nothing reports zero issues and reads exactly like a pass.

Run as `python3 scripts/tests/test_repo_gate.py`, or under `python3 -m unittest discover -s scripts/tests`.
"""

from __future__ import annotations

import contextlib
import io
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".github/actions/repo-gate"))
import repo_gate

REPO = Path(__file__).resolve().parent.parent.parent
GOVERNANCE = REPO / "GOVERNANCE.md"

PINNED = "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"


class TreeCase(unittest.TestCase):
    """Base for cases that build a temp root and run one check over it."""

    def setUp(self) -> None:
        repo_gate.NOTES.clear()
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def workflow(self, body: str, name: str = "w.yml") -> list[str]:
        wf = self.tmp / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / name).write_text(body, encoding="utf-8")
        return [f".github/workflows/{name}"]

    def eol_pair(self, gitattributes: str, editorconfig: str) -> list[str]:
        (self.tmp / ".gitattributes").write_text(gitattributes, encoding="utf-8")
        (self.tmp / ".editorconfig").write_text(editorconfig, encoding="utf-8")
        return repo_gate.check_eol(self.tmp, [])


class TestChecksTable(TreeCase):
    def test_every_check_shares_one_signature(self) -> None:
        """CHECKS was not uniformly callable, so no table-driven case could iterate it."""
        for name, fn in sorted(repo_gate.CHECKS.items()):
            with self.subTest(check=name):
                self.assertIsInstance(fn(self.tmp, []), list)

    def test_the_table_holds_every_check_the_cli_offers(self) -> None:
        self.assertGreaterEqual(len(repo_gate.CHECKS), 3)

    def test_the_patterns_compile_and_carry_no_invisible_characters(self) -> None:
        """A shell heredoc turns a backslash escape into a control character no diff shows."""
        for name in ("USES", "PIN", "WORKFLOW", "HTTP_STATUS"):
            with self.subTest(pattern=name):
                self.assertTrue(re.compile(getattr(repo_gate, name).pattern))
        for action in repo_gate.SHA_EXCEPTIONS:
            with self.subTest(exception=action):
                self.assertTrue(action.isascii() and action.isprintable())


class TestShaPin(TreeCase):
    def test_a_floating_ref_is_flagged(self) -> None:
        files = self.workflow("jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n")
        hits = repo_gate.check_sha_pin(self.tmp, files)
        self.assertEqual(1, len(hits))
        self.assertIn("floating ref", hits[0])

    def test_a_missing_ref_is_flagged(self) -> None:
        files = self.workflow("jobs:\n  a:\n    steps:\n      - uses: actions/checkout\n")
        hits = repo_gate.check_sha_pin(self.tmp, files)
        self.assertEqual(1, len(hits))
        self.assertIn("no ref at all", hits[0])

    def test_a_forty_hex_pin_is_accepted(self) -> None:
        files = self.workflow(
            f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{PINNED} # v7\n"
        )
        self.assertEqual([], repo_gate.check_sha_pin(self.tmp, files))

    def test_every_documented_exception_is_accepted(self) -> None:
        """Driven by the live exception set, so adding one without a reason shows up here."""
        for action in repo_gate.SHA_EXCEPTIONS:
            with self.subTest(exception=action):
                files = self.workflow(f"jobs:\n  a:\n    steps:\n      - uses: {action}@master\n")
                self.assertEqual([], repo_gate.check_sha_pin(self.tmp, files))

    def test_a_local_or_self_repository_ref_needs_no_pin(self) -> None:
        for workflow in (
            "./.github/workflows/validate-task.yml",
            ".github/workflows/validate-task.yml",
            "$/.github/workflows/validate-task.yml",
        ):
            with self.subTest(ref=workflow):
                files = self.workflow(f"jobs:\n  a:\n    uses: {workflow}\n")
                self.assertEqual([], repo_gate.check_sha_pin(self.tmp, files))
        files = self.workflow(
            "jobs:\n  a:\n    steps:\n      - uses: $/.github/actions/validate-default\n"
        )
        self.assertEqual([], repo_gate.check_sha_pin(self.tmp, files))


class ResolveCase(TreeCase):
    """Base for the resolvability pass, with the owner fixed and the network replaced.

    No case here reaches GitHub. The point is what each answer is read as, and a case that made a
    live call would report the fleet's current state rather than this script's reading of it.
    """

    OWNER = "ptr727"

    def setUp(self) -> None:
        super().setUp()
        repo_gate.NOTES.clear()
        self.enterContext(mock.patch.object(repo_gate, "origin_owner", return_value=self.OWNER))

    def pins(self, *refs: str) -> list[str]:
        steps = "".join(f"      - uses: {r}\n" for r in refs)
        return self.workflow(f"jobs:\n  a:\n    steps:\n{steps}")

    def answers(self, mapping: dict[str, bool | None]) -> mock.MagicMock:
        """Replace the GitHub read with a table keyed on the API path it would have called."""
        stub = mock.MagicMock(side_effect=lambda path: mapping.get(path))
        self.enterContext(mock.patch.object(repo_gate, "gh_exists", stub))
        return stub


class TestShaPinResolves(ResolveCase):
    def test_a_forty_hex_pin_that_resolves_to_no_commit_is_flagged(self) -> None:
        """The whole point: a fabricated pin satisfies the shape and fails the reference."""
        self.answers(
            {
                f"repos/{self.OWNER}/Fleet/commits/{'c' * 40}": False,
                f"repos/{self.OWNER}/Fleet": True,
            }
        )
        hits = repo_gate.check_sha_pin(self.tmp, self.pins(f"{self.OWNER}/Fleet@{'c' * 40}"))
        self.assertEqual(1, len(hits))
        self.assertIn("resolves to no commit", hits[0])

    def test_a_pin_that_resolves_is_accepted(self) -> None:
        self.answers({f"repos/{self.OWNER}/Fleet/commits/{PINNED}": True})
        self.assertEqual(
            [], repo_gate.check_sha_pin(self.tmp, self.pins(f"{self.OWNER}/Fleet@{PINNED}"))
        )

    def test_an_action_path_within_a_repository_resolves_against_the_repository(self) -> None:
        """`owner/repo/path/to/action@sha` is one repository, and the path is not part of it."""
        stub = self.answers({f"repos/{self.OWNER}/Fleet/commits/{PINNED}": True})
        ref = f"{self.OWNER}/Fleet/.github/actions/prose-gate@{PINNED}"
        self.assertEqual([], repo_gate.check_sha_pin(self.tmp, self.pins(ref)))
        stub.assert_called_once_with(f"repos/{self.OWNER}/Fleet/commits/{PINNED}")

    def test_a_reusable_workflow_ref_resolves_against_its_repository(self) -> None:
        """A caller stub pins `owner/repo/.github/workflows/x-task.yml@sha`, one repository again."""
        stub = self.answers({f"repos/{self.OWNER}/Fleet/commits/{PINNED}": True})
        ref = f"{self.OWNER}/Fleet/.github/workflows/merge-bot-task.yml@{PINNED}"
        self.assertEqual([], repo_gate.check_sha_pin(self.tmp, self.pins(ref)))
        stub.assert_called_once_with(f"repos/{self.OWNER}/Fleet/commits/{PINNED}")

    def test_a_pin_under_another_owner_is_read_for_shape_and_never_fetched(self) -> None:
        """The scope is a decision, so a case holds it rather than leaving it to the docstring."""
        stub = self.answers({})
        self.assertEqual(
            [], repo_gate.check_sha_pin(self.tmp, self.pins(f"actions/checkout@{PINNED}"))
        )
        stub.assert_not_called()
        self.assertIn("1 under another owner", repo_gate.NOTES[0])

    def test_an_unreadable_owner_leaves_every_pin_on_shape_alone(self) -> None:
        """A checkout with no origin cannot say which pins are the fleet's, so it fetches none."""
        stub = self.answers({})
        with mock.patch.object(repo_gate, "origin_owner", return_value=None):
            self.assertEqual(
                [], repo_gate.check_sha_pin(self.tmp, self.pins(f"{self.OWNER}/Fleet@{PINNED}"))
            )
        stub.assert_not_called()

    def test_an_unreadable_owner_is_not_reported_as_another_owner(self) -> None:
        """The two are different states, and this note exists to describe the narrowing exactly.

        A pin under this owner skipped because the origin is unreadable is not a pin belonging to
        somebody else, and saying so in the one line that reports coverage is the same false clean
        the note was added to prevent.
        """
        self.answers({})
        with mock.patch.object(repo_gate, "origin_owner", return_value=None):
            repo_gate.check_sha_pin(self.tmp, self.pins(f"{self.OWNER}/Fleet@{PINNED}"))
        self.assertIn("0 under another owner", repo_gate.NOTES[0])
        self.assertIn("1 whose owner could not be compared", repo_gate.NOTES[0])

    def test_the_note_carries_every_count_including_the_zeroes(self) -> None:
        """One fixed shape per run, so a zero in any position is as visible as a count."""
        self.answers({f"repos/{self.OWNER}/Fleet/commits/{PINNED}": True})
        repo_gate.check_sha_pin(self.tmp, self.pins(f"{self.OWNER}/Fleet@{PINNED}"))
        for fragment in (
            "resolved 1 pin(s)",
            "0 under another owner",
            "0 whose owner could not be compared",
            "0 GitHub did not answer for",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, repo_gate.NOTES[0])

    def test_the_note_prints_where_every_count_is_zero(self) -> None:
        """The all-zero run is the one the note exists for, and it was the one it skipped.

        Guarded on a non-zero counter, the note went silent on a repository carrying no workflow
        at all, which is exactly the clean line the docstring says nobody should have to infer
        the check's narrowness from.
        """
        repo_gate.check_sha_pin(self.tmp, [])
        self.assertEqual(1, len(repo_gate.NOTES))
        for fragment in (
            "resolved 0 pin(s)",
            "0 under another owner",
            "0 whose owner could not be compared",
            "0 GitHub did not answer for",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, repo_gate.NOTES[0])

    def test_a_pin_github_did_not_answer_for_is_skipped_rather_than_failed(self) -> None:
        """Offline, unauthenticated and rate-limited all read as nothing learned, not as absent."""
        self.answers({f"repos/{self.OWNER}/Fleet/commits/{PINNED}": None})
        self.assertEqual(
            [], repo_gate.check_sha_pin(self.tmp, self.pins(f"{self.OWNER}/Fleet@{PINNED}"))
        )
        self.assertIn("1 GitHub did not answer for", repo_gate.NOTES[0])

    def test_a_missing_commit_in_an_unreadable_repository_is_not_a_finding(self) -> None:
        """A repository-scoped token 404s on a sibling, which is not the pin being wrong."""
        self.answers(
            {f"repos/{self.OWNER}/Fleet/commits/{PINNED}": False, f"repos/{self.OWNER}/Fleet": None}
        )
        self.assertEqual(
            [], repo_gate.check_sha_pin(self.tmp, self.pins(f"{self.OWNER}/Fleet@{PINNED}"))
        )
        self.assertIn("1 GitHub did not answer for", repo_gate.NOTES[0])

    def test_one_pin_named_twice_is_read_once(self) -> None:
        """A pin repeats across workflows, and a gate that re-fetches it burns the rate limit."""
        stub = self.answers({f"repos/{self.OWNER}/Fleet/commits/{PINNED}": True})
        files = self.pins(f"{self.OWNER}/Fleet@{PINNED}")
        files += self.workflow(
            f"jobs:\n  b:\n    steps:\n      - uses: {self.OWNER}/Fleet@{PINNED}\n", name="x.yml"
        )
        self.assertEqual([], repo_gate.check_sha_pin(self.tmp, files))
        self.assertEqual(1, stub.call_count)

    def test_a_floating_ref_is_never_fetched(self) -> None:
        """The shape fails first, so a ref that is not a SHA costs no request."""
        stub = self.answers({})
        self.assertEqual(
            1, len(repo_gate.check_sha_pin(self.tmp, self.pins(f"{self.OWNER}/Fleet@v4")))
        )
        stub.assert_not_called()

    def test_the_documented_exception_is_never_fetched(self) -> None:
        stub = self.answers({})
        for action in repo_gate.SHA_EXCEPTIONS:
            with self.subTest(exception=action):
                self.assertEqual(
                    [], repo_gate.check_sha_pin(self.tmp, self.pins(f"{action}@master"))
                )
        stub.assert_not_called()


class TestGitHubRead(unittest.TestCase):
    """`gh_exists` decides absence from the status GitHub returned, never from a non-zero exit."""

    def run_with(self, rc: int, stderr: str) -> bool | None:
        proc = subprocess.CompletedProcess([], rc, "", stderr)
        with mock.patch.object(repo_gate.subprocess, "run", return_value=proc):
            return repo_gate.gh_exists("repos/o/r/commits/deadbeef")

    def test_a_successful_read_is_present(self) -> None:
        self.assertIs(True, self.run_with(0, ""))

    def test_the_two_absent_statuses_are_absent(self) -> None:
        for status in sorted(repo_gate.ABSENT):
            with self.subTest(status=status):
                self.assertIs(False, self.run_with(1, f"gh: Not Found (HTTP {status})"))

    def test_credentials_and_rate_limits_are_not_absence(self) -> None:
        """Reading a 401 or a 403 as a missing commit fails a correct tree on a narrow token."""
        for status in ("401", "403", "500", "502"):
            with self.subTest(status=status):
                self.assertIsNone(self.run_with(1, f"gh: (HTTP {status})"))

    def test_a_network_error_carrying_no_status_is_not_absence(self) -> None:
        self.assertIsNone(self.run_with(1, "error connecting to api.github.com"))

    def test_gh_being_absent_is_not_absence_and_does_not_raise(self) -> None:
        """The gate stays usable on a machine with no `gh`, reporting shape only."""
        with mock.patch.object(repo_gate.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(repo_gate.gh_exists("repos/o/r"))

    def test_a_hung_read_times_out_rather_than_holding_the_gate(self) -> None:
        with mock.patch.object(
            repo_gate.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 1)
        ):
            self.assertIsNone(repo_gate.gh_exists("repos/o/r"))

    def test_the_read_is_a_get_and_carries_no_state_changing_verb(self) -> None:
        """A gate is read-only, so the one network call it makes is held to that."""
        source = (REPO / "scripts" / "repo_gate.py").read_text(encoding="utf-8")
        for verb in ("-X POST", "-X PATCH", "-X PUT", "-X DELETE", "--method", "mutation"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, source)


class TestNotes(TreeCase):
    def test_a_note_prints_without_failing_the_run(self) -> None:
        """A check that quietly did less than its name prints the same clean line as one that ran."""

        def noting(root: Path, files: list[str]) -> list[str]:
            repo_gate.NOTES.append("did less")
            return []

        with (
            mock.patch.dict(repo_gate.CHECKS, {"sha-pin": noting}),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(0, repo_gate.main(["--root", str(REPO), "--check", "sha-pin"]))
        self.assertIn("note: did less", out.getvalue())

    def test_a_note_from_one_check_is_not_reported_under_the_next(self) -> None:
        repo_gate.NOTES.append("left over from a previous run")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, repo_gate.main(["--root", str(REPO), "--check", "eol"]))
        self.assertNotIn("left over", out.getvalue())

    def test_this_repo_reports_what_its_resolvability_pass_covered(self) -> None:
        """Today it covers nothing here, and a silent zero is what this refuses to ship."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, repo_gate.main(["--root", str(REPO), "--check", "sha-pin"]))
        self.assertRegex(out.getvalue(), r"note: resolved \d+ pin\(s\) against GitHub")


class TestEol(TreeCase):
    GITATTRIBUTES = "* text=auto eol=lf\n*.bat text eol=crlf\n*.cmd text eol=crlf\n"
    EDITORCONFIG = "[*]\nend_of_line = lf\n[*.{bat,cmd}]\nend_of_line = crlf\n"

    def test_matching_global_defaults_and_windows_exceptions_are_accepted(self) -> None:
        self.assertEqual([], self.eol_pair(self.GITATTRIBUTES, self.EDITORCONFIG))

    def test_a_global_default_mismatch_is_flagged(self) -> None:
        hits = self.eol_pair(self.GITATTRIBUTES.replace("eol=lf", "eol=crlf", 1), self.EDITORCONFIG)
        self.assertEqual(1, len(hits))
        self.assertIn("global ending differs", hits[0])

    def test_a_missing_git_global_default_is_flagged(self) -> None:
        hits = self.eol_pair("*.bat text eol=crlf\n*.cmd text eol=crlf\n", self.EDITORCONFIG)
        self.assertEqual(1, len(hits))
        self.assertIn("text=auto", hits[0])

    def test_missing_windows_exceptions_are_flagged(self) -> None:
        hits = self.eol_pair("* text=auto eol=lf\n", "[*]\nend_of_line = lf\n")
        self.assertEqual(3, len(hits))

    def test_equivalent_editorconfig_windows_sections_are_accepted(self) -> None:
        separate = (
            "[*]\nend_of_line = lf\n[*.bat]\nend_of_line = crlf\n[*.cmd]\nend_of_line = crlf\n"
        )
        reversed_group = "[*]\nend_of_line = lf\n[*.{cmd,bat}]\nend_of_line = crlf\n"
        self.assertEqual([], self.eol_pair(self.GITATTRIBUTES, separate))
        self.assertEqual([], self.eol_pair(self.GITATTRIBUTES, reversed_group))

    def test_a_combined_windows_section_may_include_other_extensions(self) -> None:
        editorconfig = "[*]\nend_of_line = lf\n[*.{bat,cmd,ps1}]\nend_of_line = crlf\n"
        self.assertEqual([], self.eol_pair(self.GITATTRIBUTES, editorconfig))

    def test_a_missing_config_is_named(self) -> None:
        (self.tmp / ".editorconfig").write_text("[*]\n", encoding="utf-8")
        self.assertEqual(["missing .gitattributes"], repo_gate.check_eol(self.tmp, []))


class GitTreeCase(unittest.TestCase):
    """Base for the coverage check, which needs a real repository for `git check-attr`.

    The attribute is asked of git rather than re-derived from the file, so a case has to give git
    something to answer about. `git init` plus `git add` is enough, since nothing here reads a
    commit, and it keeps the case honest about what a checkout would actually apply.
    """

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git absent, so check-attr cannot be asked")
        repo_gate.NOTES.clear()
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", ".")

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.tmp), *args], check=True, capture_output=True)

    def coverage(self, gitattributes: str, tree: dict[str, str]) -> list[str]:
        """Track `tree` under a `.gitattributes` and run the check over what git then lists."""
        for rel, body in {**tree, ".gitattributes": gitattributes}.items():
            p = self.tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        self.git("add", "-A")
        return repo_gate.check_eol_coverage(self.tmp, repo_gate.tracked(self.tmp))


class TestEolCoverage(GitTreeCase):
    GITATTRIBUTES = "* text=auto eol=lf\n*.bat text eol=crlf\n*.cmd text eol=crlf\n"

    def test_the_global_default_covers_every_representative_class(self) -> None:
        self.assertEqual([], self.coverage(self.GITATTRIBUTES, {"seed.txt": "seed\n"}))

    def test_a_crlf_native_repository_uses_its_declared_global_default(self) -> None:
        gitattributes = self.GITATTRIBUTES.replace("eol=lf", "eol=crlf", 1)
        self.assertEqual([], self.coverage(gitattributes, {"seed.txt": "seed\n"}))

    def test_a_crlf_native_repository_may_pin_posix_paths_to_lf(self) -> None:
        gitattributes = self.GITATTRIBUTES.replace("eol=lf", "eol=crlf", 1)
        gitattributes += (
            "*.py text eol=lf\n*.sh text eol=lf\ntool text eol=lf\nuv.lock text eol=lf\n"
        )
        self.assertEqual([], self.coverage(gitattributes, {"seed.txt": "seed\n"}))

    def test_a_tracked_shebang_path_must_resolve_to_lf(self) -> None:
        gitattributes = self.GITATTRIBUTES.replace("eol=lf", "eol=crlf", 1)
        hits = self.coverage(gitattributes, {"run-tool": "#!/bin/sh\nexit 0\n"})
        self.assertTrue(any("run-tool: tracked shebang path" in hit for hit in hits))

        gitattributes += "run-tool text eol=lf\n"
        self.assertEqual([], self.coverage(gitattributes, {"run-tool": "#!/bin/sh\nexit 0\n"}))

    def test_a_missing_global_default_is_flagged_across_representative_classes(self) -> None:
        hits = self.coverage("*.bat text eol=crlf\n*.cmd text eol=crlf\n", {"seed.txt": "seed\n"})
        self.assertEqual([".gitattributes has no `* text=auto eol=<ending>` default"], hits)

    def test_missing_windows_exceptions_are_flagged(self) -> None:
        hits = self.coverage("* text=auto eol=lf\n", {"seed.txt": "seed\n"})
        self.assertEqual(2, len(hits))
        self.assertTrue(any("script.bat" in hit for hit in hits))
        self.assertTrue(any("script.cmd" in hit for hit in hits))

    def test_a_missing_gitattributes_is_named(self) -> None:
        (self.tmp / "lib.py").write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(["missing .gitattributes"], repo_gate.check_eol_coverage(self.tmp, []))

    def test_git_not_answering_is_a_note_rather_than_a_silent_pass(self) -> None:
        """An unread attribute is nothing learned, and it renders as zero findings either way."""
        with mock.patch.object(repo_gate, "resolved_eol", return_value=None):
            hits = self.coverage(self.GITATTRIBUTES, {"seed.txt": "seed\n"})
        self.assertEqual([], hits)
        self.assertIn("no representative path was read", repo_gate.NOTES[0])

    def test_the_note_carries_every_count_including_the_zeroes(self) -> None:
        self.coverage(self.GITATTRIBUTES, {"seed.txt": "seed\n"})
        self.assertTrue(
            any("resolved 9 representative path(s)" in note for note in repo_gate.NOTES)
        )


class TestGovernanceCoupling(unittest.TestCase):
    def test_the_exception_set_matches_what_the_doc_documents(self) -> None:
        """The doc calls it the one documented exception, so the code must not carry a second."""
        text = GOVERNANCE.read_text(encoding="utf-8")
        for action in repo_gate.SHA_EXCEPTIONS:
            with self.subTest(exception=action):
                self.assertIn(action, text, "the code exempts an action the doc never names")
        self.assertEqual(1, len(repo_gate.SHA_EXCEPTIONS))


class TestCoverageFloors(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git absent, so the tracked-file floors cannot be read")

    def test_the_repo_sweep_finds_its_tracked_files(self) -> None:
        self.assertGreaterEqual(len(repo_gate.tracked(REPO)), 90)

    def test_the_sha_pin_scan_is_not_vacuous(self) -> None:
        """A workflow glob that matched nothing would print `0 issue(s)` and read as clean."""
        self.assertGreaterEqual(len(repo_gate.workflow_files(repo_gate.tracked(REPO))), 4)

    def test_excluding_every_workflow_empties_the_sha_pin_scan(self) -> None:
        """Proves the `--exclude` plumbing actually reaches sha-pin's own tracked-file scan.

        Two directories carry a `workflows/*.yml` path in this repo: the real workflows and the
        `catalog/snippets/workflows/` examples the audit also scans, so both are excluded.
        """
        self.assertEqual(
            [],
            repo_gate.workflow_files(
                repo_gate.tracked(REPO, [".github/workflows/**", "catalog/snippets/workflows/**"])
            ),
        )

    def test_the_representative_eol_check_runs_against_this_repo(self) -> None:
        self.assertEqual([], repo_gate.check_eol_coverage(REPO, repo_gate.tracked(REPO)))

    def test_a_root_with_no_tracked_files_exits_two_rather_than_zero(self) -> None:
        """An empty file set is a broken invocation, not a clean repo."""
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(2, repo_gate.main(["--root", d]))
        self.assertIn("not a git repo or no tracked files", err.getvalue())

    def test_this_repo_passes_both_checks_from_the_cli(self) -> None:
        """The gates run in CI from this entry point, so the entry point is what is proven."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, repo_gate.main(["--root", str(REPO)]))
        printed = out.getvalue()
        for name in repo_gate.CHECKS:
            with self.subTest(check=name):
                self.assertIn(f"[ok  ] {name}", printed)

    def test_a_single_check_runs_alone(self) -> None:
        """`--check` scopes the run, and a name outside CHECKS is rejected by the parser."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, repo_gate.main(["--root", str(REPO), "--check", "sha-pin"]))
        self.assertIn("sha-pin", out.getvalue())
        self.assertNotIn("eol", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            repo_gate.main(["--check", "no-such-check"])

    def test_a_failing_check_exits_one_and_prints_each_hit(self) -> None:
        """A gate that finds something has to say so and fail, not report and pass."""
        with (
            mock.patch.dict(repo_gate.CHECKS, {"sha-pin": lambda root, files: ["a.yml: unpinned"]}),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(1, repo_gate.main(["--root", str(REPO), "--check", "sha-pin"]))
        self.assertIn("[FAIL] sha-pin      1 issue(s)", out.getvalue())
        self.assertIn("a.yml: unpinned", out.getvalue())

    def test_an_unreadable_workflow_is_skipped_rather_than_raising(self) -> None:
        """`git ls-files` lists a path a later commit deleted from the working tree."""
        self.assertEqual([], repo_gate.check_sha_pin(REPO, [".github/workflows/absent.yml"]))


class TestExcludeGlobs(unittest.TestCase):
    """A caller vendoring a subtree it does not author needs `tracked()` narrowed, not re-derived.

    Own git repo rather than REPO, so a case proves the exclude reaches a real subtree it built
    rather than one this repo happens to carry today.
    """

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git absent, so ls-files cannot be asked")
        repo_gate.NOTES.clear()
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "-C", str(self.tmp), "init", "-q", "."], check=True)
        for rel in ("kept.py", "vendor/upstream.py", "vendor/nested/deep.py"):
            p = self.tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("pass\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.tmp), "add", "-A"], check=True, capture_output=True)

    def test_a_pathspec_pattern_drops_the_matching_subtree(self) -> None:
        self.assertEqual(
            ["kept.py", "vendor/nested/deep.py", "vendor/upstream.py"],
            sorted(repo_gate.tracked(self.tmp)),
        )
        self.assertEqual(["kept.py"], repo_gate.tracked(self.tmp, ["vendor/**"]))

    def test_no_exclude_scans_exactly_as_before(self) -> None:
        """`exclude=[]` and `exclude=None` are both the CLI's own no-flag default."""
        baseline = repo_gate.tracked(self.tmp)
        self.assertEqual(baseline, repo_gate.tracked(self.tmp, []))
        self.assertEqual(baseline, repo_gate.tracked(self.tmp, None))

    def test_every_pattern_given_is_applied(self) -> None:
        self.assertEqual([], repo_gate.tracked(self.tmp, ["kept.py", "vendor/**"]))

    def test_the_cli_wires_the_exclude_flag_through_to_tracked(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = repo_gate.main(
                ["--root", str(self.tmp), "--check", "sha-pin", "--exclude", "vendor/**"]
            )
        self.assertEqual(0, code)
        self.assertIn(
            "note: 1 exclude pattern(s) narrowed the tracked-file scan by 2 file(s): vendor/**",
            out.getvalue(),
        )

    def test_the_narrowing_note_is_silent_when_no_exclude_was_given(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            repo_gate.main(["--root", str(self.tmp), "--check", "sha-pin"])
        self.assertNotIn("narrowed the tracked-file scan", out.getvalue())

    def test_a_pattern_matching_nothing_is_not_reported_as_narrowing(self) -> None:
        """A caller's own typo drops zero files, and a false 'narrowed' note would hide that."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            repo_gate.main(
                ["--root", str(self.tmp), "--check", "sha-pin", "--exclude", "no/such/path/**"]
            )
        printed = out.getvalue()
        self.assertIn("matched no tracked file, so nothing was narrowed", printed)
        self.assertNotIn("narrowed the tracked-file scan by", printed)

    def test_a_failed_ls_files_call_prints_gits_own_error(self) -> None:
        """A command failure and a valid empty result both return `[]`; only this prints why."""
        proc = subprocess.CompletedProcess([], 128, "", "fatal: Invalid pathspec magic 'bogus'\n")
        with (
            mock.patch.object(repo_gate.subprocess, "run", return_value=proc),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            files = repo_gate.tracked(self.tmp, ["bogus/**"])
        self.assertEqual([], files)
        self.assertIn("git ls-files failed", err.getvalue())
        self.assertIn("Invalid pathspec magic", err.getvalue())

    def test_a_failed_call_is_never_trusted_even_with_nonempty_stdout(self) -> None:
        """A partial listing read as a complete one is a scan that missed files silently."""
        proc = subprocess.CompletedProcess([], 128, "kept.py\n", "fatal: something went wrong\n")
        with mock.patch.object(repo_gate.subprocess, "run", return_value=proc):
            files = repo_gate.tracked(self.tmp, ["vendor/**"])
        self.assertEqual([], files)


class TestHarness(unittest.TestCase):
    def test_this_module_collects_a_plausible_number_of_cases(self) -> None:
        loaded = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertGreaterEqual(loaded.countTestCases(), 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
