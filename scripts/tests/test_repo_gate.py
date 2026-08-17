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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import repo_gate

REPO = Path(__file__).resolve().parent.parent.parent
GOVERNANCE = REPO / "GOVERNANCE.md"
GITATTRIBUTES = REPO / ".gitattributes"

PINNED = "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
# The pins this repo's .gitattributes marks forward-declared, named so a fourth arrives here too.
# The mark reaches to the next blank line, so an appended pin inherits it and it fails open.
FORWARD_DECLARED = {"uv.lock", "Dockerfile", "*.Dockerfile"}


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
        for ref in (
            "./.github/workflows/validate-task.yml",
            ".github/workflows/validate-task.yml",
            "$/.github/workflows/validate-task.yml",
            "$/.github/actions/validate-default",
        ):
            with self.subTest(ref=ref):
                files = self.workflow(f"jobs:\n  a:\n    uses: {ref}\n")
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
    def test_a_gitattributes_pin_with_no_editorconfig_override_is_flagged(self) -> None:
        hits = self.eol_pair("*.sh text eol=lf\n", "[*]\nend_of_line = crlf\n")
        self.assertEqual(1, len(hits))
        self.assertIn("*.sh", hits[0])

    def test_a_matching_override_is_accepted(self) -> None:
        self.assertEqual([], self.eol_pair("*.sh text eol=lf\n", "[*.sh]\nend_of_line = lf\n"))

    def test_brace_syntax_is_expanded_on_both_sides(self) -> None:
        """EditorConfig brace globs have to expand or a real override reads as missing."""
        self.assertEqual(
            [], self.eol_pair("*.sh text eol=lf\n", "[*.{sh,bash}]\nend_of_line = lf\n")
        )

    def test_a_path_pin_matches_a_directory_glob_override(self) -> None:
        """The three scripts are pinned by path while .editorconfig covers them with a glob."""
        self.assertEqual(
            [],
            self.eol_pair(
                "scripts/prose_lint.py text eol=lf\n", "[scripts/*.py]\nend_of_line = lf\n"
            ),
        )

    def test_a_comment_is_not_read_as_a_pin(self) -> None:
        self.assertEqual([], self.eol_pair("# *.sh text eol=lf\n", "[*]\nend_of_line = crlf\n"))

    def test_a_missing_config_is_named(self) -> None:
        (self.tmp / ".editorconfig").write_text("[*]\n", encoding="utf-8")
        self.assertEqual(["missing .gitattributes"], repo_gate.check_eol(self.tmp, []))

    def test_a_global_lf_default_reports_that_it_read_nothing(self) -> None:
        """The check is vacuous on such a repo, and it renders as a clean line saying otherwise.

        `[*] end_of_line = lf` satisfies the lookup for any path, one that does not exist included,
        so every pin passes whatever it names. `ptr727/Blog` is shaped that way and carried two
        pins naming paths never tracked there while this reported zero.
        """
        self.assertEqual(
            [], self.eol_pair("deploy/absent text eol=lf\n", "[*]\nend_of_line = lf\n")
        )
        self.assertEqual(1, len(repo_gate.NOTES))
        self.assertIn("nothing here read pin content", repo_gate.NOTES[0])

    def test_a_crlf_default_leaves_the_check_saying_nothing(self) -> None:
        """The note is the exception rather than the every-run shape, since here the check ran."""
        self.eol_pair("*.sh text eol=lf\n", "[*]\nend_of_line = crlf\n[*.sh]\nend_of_line = lf\n")
        self.assertEqual([], repo_gate.NOTES)


class TestEolPins(unittest.TestCase):
    """The shared parser, which is where the forward-declared mark is read."""

    def test_a_pin_is_read_with_its_glob(self) -> None:
        self.assertEqual([("*.sh", False)], repo_gate.eol_pins("*.sh text eol=lf\n"))

    def test_a_pin_that_is_not_an_lf_pin_is_not_read(self) -> None:
        self.assertEqual([], repo_gate.eol_pins("* -text\n*.png binary\n"))

    def test_the_mark_carries_from_the_comment_above_to_the_pins_below(self) -> None:
        pins = repo_gate.eol_pins(
            "# nothing yet, so forward-declared.\nDockerfile text eol=lf\n"
            "*.Dockerfile text eol=lf\n"
        )
        self.assertEqual([("Dockerfile", True), ("*.Dockerfile", True)], pins)

    def test_a_blank_line_closes_the_mark(self) -> None:
        """The block is the unit, so the next block does not inherit the one above it."""
        pins = repo_gate.eol_pins(
            "# forward-declared.\nuv.lock text eol=lf\n"
            "\n# and this one is not.\ndeploy/absent text eol=lf\n"
        )
        self.assertEqual([("uv.lock", True), ("deploy/absent", False)], pins)

    def test_a_mark_earlier_in_the_same_block_still_carries(self) -> None:
        """The token need not be the last comment line, since the rationale often follows it."""
        pins = repo_gate.eol_pins(
            "# forward-declared.\n# Because a consumer adds one.\nuv.lock text eol=lf\n"
        )
        self.assertEqual([("uv.lock", True)], pins)


class TestAttrGlob(unittest.TestCase):
    """Gitattributes matching, which is not the pathspec matching `git ls-files` would give."""

    CASES = (
        # An extension glob binds at any depth, because it carries no slash.
        ("*.sh", "a.sh", True),
        ("*.sh", "host-setup/agent-safety/install.sh", True),
        ("*.sh", "a.bash", False),
        # The case the pathspec reading gets wrong: `*` stops at a separator.
        ("scripts/*.py", "scripts/repo_gate.py", True),
        ("scripts/*.py", "scripts/sub/repo_gate.py", False),
        # A slash anchors at the root, and a leading slash is itself a slash.
        ("spec/audit.py", "spec/audit.py", True),
        ("spec/audit.py", "vendor/spec/audit.py", False),
        ("/uv.lock", "uv.lock", True),
        ("/uv.lock", "sub/uv.lock", False),
        ("uv.lock", "sub/uv.lock", True),
        # A whole segment of `**` is the one form that crosses separators.
        ("Docker/s6-overlay/**", "Docker/s6-overlay/run", True),
        ("Docker/s6-overlay/**", "Docker/s6-overlay/svc/finish", True),
        ("Docker/s6-overlay/**", "Docker/other/run", False),
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/y/b", True),
        ("**/pre-commit", ".husky/pre-commit", True),
        # The remaining glob syntax a pin may carry.
        ("*.[ch]", "main.c", True),
        ("*.[ch]", "main.x", False),
        ("a?.sh", "ab.sh", True),
        ("a?.sh", "a/b.sh", False),
    )

    def test_each_pattern_binds_what_git_binds(self) -> None:
        for pattern, path, want in self.CASES:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(want, bool(repo_gate.attr_glob(pattern).match(path)))


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
    def test_an_unpinned_shebang_script_is_flagged(self) -> None:
        """Blog's live defect: an extensionless script systemd runs, matched by no pin at all."""
        hits = self.coverage("* -text\n", {"ops/vps-backup-pull": "#!/usr/bin/env bash\ntrue\n"})
        self.assertEqual(1, len(hits))
        self.assertIn("ops/vps-backup-pull", hits[0])
        self.assertIn("not `lf`", hits[0])

    def test_a_pinned_shebang_script_is_accepted(self) -> None:
        self.assertEqual(
            [],
            self.coverage(
                "* -text\nops/vps-backup-pull text eol=lf\n",
                {"ops/vps-backup-pull": "#!/usr/bin/env bash\ntrue\n"},
            ),
        )

    def test_a_glob_pin_reaching_the_script_is_accepted(self) -> None:
        """The pin need not name the path, only resolve to LF, which is what git is asked."""
        self.assertEqual(
            [], self.coverage("* -text\n*.sh text eol=lf\n", {"tools/run.sh": "#!/bin/sh\ntrue\n"})
        )

    def test_a_file_carrying_no_shebang_is_not_read_as_a_script(self) -> None:
        """The rule is about the interpreter line, so an ordinary source file is out of scope."""
        self.assertEqual([], self.coverage("* -text\n", {"lib.py": "x = 1\n"}))

    def test_the_shebang_decides_rather_than_the_executable_bit(self) -> None:
        """The two move independently, and it is the interpreter line a CRLF breaks.

        Read from the mode instead, this reports the data file and misses the script, which is
        both directions wrong at once.
        """
        self.coverage("* -text\n", {"ops/run": "#!/bin/sh\ntrue\n", "ops/table.csv": "a,b\n"})
        self.git("update-index", "--chmod=+x", "ops/table.csv")
        self.git("update-index", "--chmod=-x", "ops/run")
        hits = repo_gate.check_eol_coverage(self.tmp, repo_gate.tracked(self.tmp))
        self.assertEqual(1, len(hits))
        self.assertIn("ops/run", hits[0])

    def test_a_pin_matching_no_tracked_file_is_flagged(self) -> None:
        """Blog's quieter defect: a pin naming a host artifact the repo never carries."""
        hits = self.coverage("* -text\ndeploy/authorized_keys text eol=lf\n", {"lib.py": "x = 1\n"})
        self.assertEqual(1, len(hits))
        self.assertIn("deploy/authorized_keys", hits[0])
        self.assertIn("no tracked file matches it", hits[0])

    def test_a_forward_declared_pin_matching_nothing_is_exempt(self) -> None:
        """The carried baseline case: the pin goes live when a consumer adds the file it names."""
        self.assertEqual(
            [],
            self.coverage(
                "* -text\n# A repo with no lockfile is unaffected, so this pin is forward-declared.\n"
                "uv.lock text eol=lf\n",
                {"lib.py": "x = 1\n"},
            ),
        )

    def test_the_mark_does_not_reach_past_a_blank_line(self) -> None:
        """Or one block's exemption silently covers every pin appended below it."""
        hits = self.coverage(
            "* -text\n# forward-declared.\nuv.lock text eol=lf\n"
            "\n# A host artifact.\ndeploy/authorized_keys text eol=lf\n",
            {"lib.py": "x = 1\n"},
        )
        self.assertEqual(1, len(hits))
        self.assertIn("deploy/authorized_keys", hits[0])

    def test_a_pin_is_matched_the_way_git_matches_it_rather_than_as_a_pathspec(self) -> None:
        """`git ls-files -- capture/*.py` matches the nested file and reads a dead pin as live."""
        hits = self.coverage(
            "* -text\ncapture/*.py text eol=lf\n", {"capture/sub/deep.py": "x = 1\n"}
        )
        self.assertEqual(1, len(hits))
        self.assertIn("capture/*.py", hits[0])

    def test_a_missing_gitattributes_is_named(self) -> None:
        (self.tmp / "lib.py").write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(["missing .gitattributes"], repo_gate.check_eol_coverage(self.tmp, []))

    def test_git_not_answering_is_a_note_rather_than_a_silent_pass(self) -> None:
        """An unread attribute is nothing learned, and it renders as zero findings either way."""
        with mock.patch.object(repo_gate, "resolved_eol", return_value=None):
            hits = self.coverage("* -text\n", {"ops/run": "#!/bin/sh\ntrue\n"})
        self.assertEqual([], hits)
        self.assertIn("no shebang file was read at all", repo_gate.NOTES[0])

    def test_the_note_carries_every_count_including_the_zeroes(self) -> None:
        """One fixed shape per run, so a scan that reached nothing is as visible as one that did."""
        self.coverage("* -text\n", {"lib.py": "x = 1\n"})
        for fragment in (
            "read 0 LF pin(s)",
            "0 of them forward-declared",
            "over 0 shebang file(s)",
            "2 tracked file(s)",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, repo_gate.NOTES[-1])


class TestGitattributesCoupling(unittest.TestCase):
    """The live marking in this repo, read rather than restated.

    The mark reaches to the next blank line, so a pin appended directly under a marked block
    inherits an exemption nobody wrote for it. That fails open, which is the direction worth a
    case: this one names the three pins the marking is for and fails the moment a fourth arrives.
    """

    def test_exactly_the_intended_pins_are_forward_declared(self) -> None:
        pins = repo_gate.eol_pins(GITATTRIBUTES.read_text(encoding="utf-8"))
        self.assertEqual(FORWARD_DECLARED, {g for g, forward in pins if forward})

    def test_every_other_pin_binds_a_file_this_repo_actually_tracks(self) -> None:
        """The exemption is for the carried baseline, so nothing else may lean on it."""
        if shutil.which("git") is None:
            self.skipTest("git absent, so the tracked set cannot be read")
        files = repo_gate.tracked(REPO)
        for glob, forward in repo_gate.eol_pins(GITATTRIBUTES.read_text(encoding="utf-8")):
            if forward:
                continue
            with self.subTest(pin=glob):
                self.assertTrue(any(repo_gate.attr_glob(glob).match(f) for f in files))


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

    def test_the_shebang_scan_is_not_vacuous(self) -> None:
        """The floor lives here rather than in the check, because a repo may honestly have none.

        A source-only configuration repo shipping no scripts is clean, so a finding there would be
        a false one. What must not go unnoticed is this repo's own scan going quiet, which is what
        this holds: the count only ever grows as fleet tooling is added.
        """
        self.assertGreaterEqual(len(repo_gate.shebang_files(REPO, repo_gate.tracked(REPO))), 10)

    def test_every_shebang_file_here_resolves_to_lf(self) -> None:
        """The state the check protects, asserted directly so a regression names the file."""
        files = repo_gate.shebang_files(REPO, repo_gate.tracked(REPO))
        self.assertEqual(
            {}, {f: v for f, v in (repo_gate.resolved_eol(REPO, files) or {}).items() if v != "lf"}
        )

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


class TestHarness(unittest.TestCase):
    def test_this_module_collects_a_plausible_number_of_cases(self) -> None:
        loaded = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertGreaterEqual(loaded.countTestCases(), 68)


if __name__ == "__main__":
    unittest.main(verbosity=2)
