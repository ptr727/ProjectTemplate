#!/usr/bin/env python3
"""Drive the hub's own `.husky/pre-commit`, and the catalog's Husky.Net snippet, through real commits.

The hook chooses the base its prose gate diffs against, and only a real commit shows which base it
chose. Each case copies a hook and the prose gate into a throwaway repository and commits there.
The language formatters and the eol check are replaced by stubs, since what is under test is the
prose gate's scope rather than those tools. The snippet's `hub_fetch_run.py` is a stub too. It
runs the local prose gate rather than fetching one, so a case measures the hook and not the hub.
Every case runs against both hooks, the snippet's classes subclassing the hub's.

A hook holds a temporary file for the path list. So its cases also check that the file is gone
after a normal end, a failed list, and an interrupt. An interrupt also has to stop the tool the
hook is running and end the hook by the signal itself. The hub's `.husky/pre-push` holds a child
too, the review engine, so its interrupt cases are here beside them.

The fixture isolates git's global and system configuration. So a host with commit signing on or a
hooks path of its own cannot change what a case measures.

Run as `python3 tests/test_pre_commit_hook.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HUB_HOOK = REPO / ".husky" / "pre-commit"
PRE_PUSH_HOOK = REPO / ".husky" / "pre-push"
SNIPPET_HOOK = REPO / "catalog" / "snippets" / "husky" / "pre-commit"
GATE = REPO / ".github" / "actions" / "prose-gate" / "prose_lint.py"

COMMENT = "# The value is read once, since the second read can disagree.\n"
WAIT_SECONDS = 120


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def wait_until_started(proc: subprocess.Popen[str], ready: Path) -> None:
    """Wait for a stub to mark itself started, failing if the hook ends first or never gets there."""
    deadline = time.monotonic() + WAIT_SECONDS
    while not ready.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise AssertionError("the blocking stub never started")
        time.sleep(0.05)


def assert_group_ended(pgid: int) -> None:
    """Every process the hook started is gone, which a survivor would fail by holding the group."""
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() > deadline:
            os.killpg(pgid, signal.SIGKILL)
            raise AssertionError("a process the hook started outlived it")
        time.sleep(0.05)


def end_group(proc: subprocess.Popen[str]) -> None:
    """Kill what is left of a hook's process group, so a failed case leaves nothing running."""
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=WAIT_SECONDS)


@unittest.skipUnless(sys.platform == "linux", "runs a POSIX shell hook with executable stubs")
@unittest.skipUnless(shutil.which("uv"), "the hook runs the prose gate through uv")
class HookCase(unittest.TestCase):
    """A throwaway repository carrying the hook and the gate, with one base commit."""

    hook = HUB_HOOK

    def setUp(self) -> None:
        real_uv, real_git = shutil.which("uv"), shutil.which("git")
        if real_uv is None or real_git is None:
            raise AssertionError("uv and git must both be on PATH")
        self.real_git = real_git
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        empty = self.outside / "empty-gitconfig"
        empty.write_text("", encoding="utf-8")
        stubs = self.outside / "bin"
        stubs.mkdir()
        self.tmp = self.outside / "tmp"
        self.tmp.mkdir()
        write_executable(stubs / "uvx", "#!/bin/sh\nexit 0\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.pop("PROSE_ALLOW_COMMENTS", None)
        self.env["GIT_CONFIG_GLOBAL"] = str(empty)
        self.env["GIT_CONFIG_SYSTEM"] = str(empty)
        self.env["TMPDIR"] = str(self.tmp)
        self.env["PATH"] = f"{stubs}{os.pathsep}{self.env.get('PATH', '')}"

        self.git("init", "-q", "--initial-branch=develop", ".")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", ".husky")
        for source, dest in (
            (self.hook, ".husky/pre-commit"),
            (GATE, ".github/actions/prose-gate/prose_lint.py"),
            (GATE.with_name("prose-gate.json"), ".github/actions/prose-gate/prose-gate.json"),
        ):
            target = self.root / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (self.root / ".husky" / "pre-commit").chmod(0o755)
        if self.hook == SNIPPET_HOOK:
            self.stub_snippet_tools(stubs, real_uv)
        else:
            (self.root / "scripts").mkdir()
            (self.root / "scripts" / "repo_gate.py").write_text(
                "import sys\nsys.exit(0)\n", encoding="utf-8"
            )
        self.write("tool.py", "value = 1\n")
        self.write("conflict.py", "VALUE = 'base'\n")
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "-m", "base")

    def stub_snippet_tools(self, stubs: Path, real_uv: str) -> None:
        """Stand in for what the snippet reaches beyond the prose gate, and run the gate locally.

        The language tools pass, the eol check passes, and `husky.sh` is empty. The local
        `hub_fetch_run.py` runs the prose gate copied into the fixture, the way the real one runs
        the copy it fetched.
        """
        (self.root / ".husky" / "_").mkdir()
        (self.root / ".husky" / "_" / "husky.sh").write_text("", encoding="utf-8")
        write_executable(stubs / "dotnet", "#!/bin/sh\nexit 0\n")
        write_executable(
            stubs / "uv",
            "#!/bin/sh\n"
            'case "$1 $2" in "run ruff" | "run pyright") exit 0 ;; esac\n'
            f'exec "{real_uv}" "$@"\n',
        )
        (self.root / "hub_fetch_run.py").write_text(
            "import subprocess, sys\n"
            "if sys.argv[1] != '.github/actions/prose-gate/prose_lint.py':\n"
            "    sys.exit(0)\n"
            f"cmd = [{real_uv!r}, 'run', '--no-project', '--script', *sys.argv[1:]]\n"
            "sys.exit(subprocess.run(cmd, check=False).returncode)\n",
            encoding="utf-8",
        )

    def write(self, name: str, text: str) -> None:
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(text, encoding="utf-8")

    def run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def git(self, *args: str) -> None:
        """A checked git call for fixture setup, loud on failure so a broken fixture is never silent."""
        result = self.run_git(*args)
        if result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")

    def assert_list_file_removed(self) -> None:
        """No `mktemp` file the hook made is left, which GNU and BSD both name `tmp.*` by default."""
        self.assertEqual([], sorted(p.name for p in self.tmp.glob("tmp.*")))


class TestTheHookDiffsAMergeAgainstItsMergedInParent(HookCase):
    """A merge of develop brings in lines the branch never wrote, and the hook must not read them.

    Measured against `HEAD`, every line develop gained since the fork reads as added. So a comment
    another pull request already landed trips `comment-added` on a merge that adds none.
    """

    def setUp(self) -> None:
        super().setUp()
        self.git("checkout", "-q", "-b", "feature")
        self.write("conflict.py", "VALUE = 'feature'\n")
        self.git("commit", "-q", "--no-verify", "-am", "feature side")

        self.git("checkout", "-q", "develop")
        self.write("tool.py", COMMENT + "value = 1\n")
        self.write("conflict.py", "VALUE = 'develop'\n")
        self.git("commit", "-q", "--no-verify", "-am", "develop side, comment landed by its own PR")

        self.git("checkout", "-q", "feature")
        merge = self.run_git("merge", "develop")
        self.assertNotEqual(0, merge.returncode, "the fixture needs a conflicted merge")

    def commit_resolution(self, resolved: str) -> subprocess.CompletedProcess[str]:
        self.write("conflict.py", resolved)
        self.git("add", "conflict.py")
        return self.run_git("commit", "--no-edit")

    def test_a_comment_the_merged_in_parent_brought_passes(self) -> None:
        result = self.commit_resolution("VALUE = 'resolved'\n")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assert_list_file_removed()

    def test_a_comment_the_resolution_adds_is_still_refused(self) -> None:
        result = self.commit_resolution(
            "# The resolution keeps the develop spelling.\nVALUE = 'develop'\n"
        )
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("conflict.py:1: comment-added", result.stdout + result.stderr)
        self.assertNotIn("tool.py", result.stdout + result.stderr)

    def test_a_commit_that_is_not_a_merge_still_diffs_against_head(self) -> None:
        """A comment the branch already committed is out of scope, and a new one is refused.

        The held comment is on the branch alone, so any base other than HEAD, develop or the fork
        point among them, reads it as added. The new file's comment is added against every base.
        """
        self.git("merge", "--abort")
        self.write("held.py", COMMENT + "value = 2\n")
        self.git("add", "held.py")
        self.git("commit", "-q", "--no-verify", "-m", "held comment")
        self.write("held.py", COMMENT + "value = 3\n")
        self.git("add", "held.py")
        result = self.run_git("commit", "-m", "edit beside the held comment")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

        self.write("feature.py", COMMENT + "value = 2\n")
        self.git("add", "feature.py")
        result = self.run_git("commit", "-m", "feature comment")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("feature.py:1: comment-added", result.stdout + result.stderr)


class TestTheHookReadsOnlyWhatTheCommitStages(HookCase):
    """An untracked scratch file is no part of a commit, so it cannot refuse one.

    The hook once scanned the whole working tree, untracked files in full. So a stray note with a
    comment refused every commit in the clone, whatever the commit touched.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write("note.py", COMMENT + "scratch = 1\n")

    def commit(self) -> subprocess.CompletedProcess[str]:
        return self.run_git("commit", "-m", "change")

    def test_a_deletion_beside_an_untracked_finding_passes(self) -> None:
        """A commit staging no file it can read skips the gate rather than scanning the tree."""
        self.git("rm", "-q", "conflict.py")
        result = self.commit()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_an_edit_beside_an_untracked_finding_passes_and_a_staged_report_is_skipped(
        self,
    ) -> None:
        """The edit is read, the report under the generated tree is not, and the note is not."""
        self.write("tool.py", "value = 2\n")
        self.write("reports/run/audit.md", "# Audit\n\nA line with " + "the " + "the word.\n")
        self.git("add", "tool.py", "reports/run/audit.md")
        result = self.commit()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_a_path_staged_and_then_removed_is_left_out_of_the_list(self) -> None:
        """The gate refuses a path that is not a file, so the hook drops it rather than failing."""
        self.write("gone.py", "value = 3\n")
        self.git("add", "gone.py")
        (self.root / "gone.py").unlink()
        self.write("tool.py", "value = 2\n")
        self.git("add", "tool.py")
        result = self.commit()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_a_symlink_replaced_by_a_file_is_read(self) -> None:
        """A typechange is staged content too, so a finding in it is refused."""
        (self.root / "link.py").symlink_to("tool.py")
        self.git("add", "link.py")
        self.git("commit", "-q", "--no-verify", "-m", "link")
        (self.root / "link.py").unlink()
        self.write("link.py", COMMENT + "value = 2\n")
        self.git("add", "link.py")
        result = self.commit()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("link.py:1: comment-added", result.stdout + result.stderr)

    def test_a_staged_finding_is_still_refused_and_the_untracked_one_is_not_named(self) -> None:
        self.write("feature.py", COMMENT + "value = 2\n")
        self.git("add", "feature.py")
        result = self.commit()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("feature.py:1: comment-added", result.stdout + result.stderr)
        self.assertNotIn("note.py", result.stdout + result.stderr)


class TestTheHookRefusesWhenItsPathListFails(HookCase):
    """A path list that fails must refuse the commit, never reach the gate as an empty list.

    The hook once read the list twice, checking the first read and piping the second into the gate.
    A failure of the second then reached the gate as an empty list, which skips it. So each case
    here fails one read of the list and stages a finding, and either the failed read or the gate
    has to refuse. The hook runs directly, since git puts its own directory first on a hook's PATH
    and so would shadow the failing `git` stub.
    """

    def run_hook_failing_list_read(self, failing: int) -> subprocess.CompletedProcess[str]:
        count = self.outside / "list-reads"
        count.write_text("0\n", encoding="utf-8")
        shim = self.outside / f"failing-git-{failing}"
        shim.mkdir()
        write_executable(
            shim / "git",
            "#!/bin/sh\n"
            'case "$*" in "diff --cached --name-only"*)\n'
            f'    n=$(($(cat "{count}") + 1))\n'
            f'    echo "$n" >"{count}"\n'
            f'    if [ "$n" -eq {failing} ]; then echo "git: read {failing} fails" >&2; exit 128; fi\n'
            "    ;;\n"
            "esac\n"
            f'exec "{self.real_git}" "$@"\n',
        )
        env = dict(self.env)
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
        return subprocess.run(
            ["sh", ".husky/pre-commit"],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=WAIT_SECONDS,
        )

    def test_a_failed_read_of_the_list_refuses_the_commit(self) -> None:
        self.write("feature.py", COMMENT + "value = 2\n")
        self.git("add", "feature.py")
        for failing in (1, 2):
            with self.subTest(failing=failing):
                result = self.run_hook_failing_list_read(failing)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assert_list_file_removed()

    def test_the_first_read_failing_refuses_before_the_gate_runs(self) -> None:
        self.write("tool.py", "value = 2\n")
        self.git("add", "tool.py")
        result = self.run_hook_failing_list_read(1)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("git: read 1 fails", result.stderr)
        self.assert_list_file_removed()


class TestTheHookCleansUpWhenInterrupted(HookCase):
    """An interrupt while the gate runs removes the list file and ends the hook by the signal.

    The gate is a stub that marks itself started and then sleeps, so the signal lands while the
    hook holds the file. The whole process group takes the signal, as a terminal's Ctrl-C sends it.
    A hook that exits 130 or 143 instead has not died by the signal, so a calling loop runs on.
    """

    def block_last_step(self, ready: Path) -> None:
        """Make the eol step, the one the hook starts in the background, mark itself and sleep."""
        (self.root / "scripts" / "repo_gate.py").write_text(
            f"import pathlib, time\npathlib.Path({str(ready)!r}).touch()\ntime.sleep({WAIT_SECONDS})\n",
            encoding="utf-8",
        )

    def interrupt(self, sig: signal.Signals) -> tuple[int, str]:
        ready = self.outside / "gate-started"
        blocking = self.outside / "blocking-uv"
        blocking.mkdir()
        write_executable(
            blocking / "uv",
            "#!/bin/sh\n"
            f'case "$*" in *prose_lint.py*) : >"{ready}"; exec sleep {WAIT_SECONDS} ;; esac\n'
            "exit 0\n",
        )
        self.write("tool.py", "value = 2\n")
        self.git("add", "tool.py")
        env = dict(self.env)
        env["PATH"] = f"{blocking}{os.pathsep}{env['PATH']}"
        hook = subprocess.Popen(
            ["sh", ".husky/pre-commit"],
            cwd=str(self.root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )
        try:
            wait_until_started(hook, ready)
            self.assertNotEqual([], list(self.tmp.glob("tmp.*")), "the hook holds no list file")
            os.killpg(hook.pid, sig)
            _, stderr = hook.communicate(timeout=WAIT_SECONDS)
            assert_group_ended(hook.pid)
        finally:
            end_group(hook)
        return hook.returncode, stderr

    def interrupt_the_hook_alone(self, sig: signal.Signals) -> tuple[int, str]:
        """Signal the hook's own process while its background step runs, as a `kill` of one PID does.

        The step takes no signal of its own here, so only the hook's trap can stop it. Under
        Husky.Net the hook runs as the inner shell its generated `husky.sh` starts, which is the
        shell this case signals.
        """
        ready = self.outside / "step-started"
        ready.unlink(missing_ok=True)
        self.block_last_step(ready)
        hook = subprocess.Popen(
            ["sh", ".husky/pre-commit"],
            cwd=str(self.root),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )
        try:
            wait_until_started(hook, ready)
            os.kill(hook.pid, sig)
            _, stderr = hook.communicate(timeout=WAIT_SECONDS)
            assert_group_ended(hook.pid)
        finally:
            end_group(hook)
        return hook.returncode, stderr

    def test_sigint_removes_the_list_file_and_dies_by_the_signal(self) -> None:
        status, stderr = self.interrupt(signal.SIGINT)
        self.assertEqual(-signal.SIGINT, status, stderr)
        self.assertIn("pre-commit: interrupted by SIGINT.", stderr)
        self.assertEqual([], list(self.tmp.iterdir()))

    def test_sigterm_removes_the_list_file_and_dies_by_the_signal(self) -> None:
        status, stderr = self.interrupt(signal.SIGTERM)
        self.assertEqual(-signal.SIGTERM, status, stderr)
        self.assertIn("pre-commit: interrupted by SIGTERM.", stderr)
        self.assertEqual([], list(self.tmp.iterdir()))

    def test_a_signal_to_the_hook_alone_stops_the_step_it_runs(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig.name):
                status, stderr = self.interrupt_the_hook_alone(sig)
                self.assertEqual(-sig, status, stderr)
                self.assertIn(f"pre-commit: interrupted by {sig.name}.", stderr)
                self.assertEqual([], list(self.tmp.iterdir()))


class TestTheSnippetDiffsAMergeAgainstItsMergedInParent(
    TestTheHookDiffsAMergeAgainstItsMergedInParent
):
    hook = SNIPPET_HOOK


class TestTheSnippetReadsOnlyWhatTheCommitStages(TestTheHookReadsOnlyWhatTheCommitStages):
    hook = SNIPPET_HOOK


class TestTheSnippetRefusesWhenItsPathListFails(TestTheHookRefusesWhenItsPathListFails):
    hook = SNIPPET_HOOK


class TestTheSnippetCleansUpWhenInterrupted(TestTheHookCleansUpWhenInterrupted):
    hook = SNIPPET_HOOK

    def block_last_step(self, ready: Path) -> None:
        """Make the helper block on the eol step, the one the snippet starts in the background."""
        (self.root / "hub_fetch_run.py").write_text(
            f"import pathlib, time\npathlib.Path({str(ready)!r}).touch()\ntime.sleep({WAIT_SECONDS})\n",
            encoding="utf-8",
        )


@unittest.skipUnless(sys.platform == "linux", "runs a bash hook with executable stubs")
class TestThePrePushHookStopsTheEngineWhenInterrupted(unittest.TestCase):
    """An interrupt while the review engine runs stops it and ends the push hook by the signal.

    The engine is a stub that marks itself started and then sleeps. The hook starts it in the
    background, which leaves it ignoring SIGINT, so only the hook's trap can stop it.
    """

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.ready = self.outside / "engine-started"
        empty = self.outside / "empty-gitconfig"
        empty.write_text("", encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env["GIT_CONFIG_GLOBAL"] = str(empty)
        self.env["GIT_CONFIG_SYSTEM"] = str(empty)
        files = {
            "spec/host-tools.json": '{"tools": [{"name": "python3", "minimum": "3.0"}]}\n',
            "scripts/local_review.py": (
                f"import pathlib, time\npathlib.Path({str(self.ready)!r}).touch()\n"
                f"time.sleep({WAIT_SECONDS})\n"
            ),
        }
        for name, text in files.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text, encoding="utf-8")
        (self.root / ".husky").mkdir()
        shutil.copy2(PRE_PUSH_HOOK, self.root / ".husky" / "pre-push")
        identity = ("-c", "user.email=test@example.invalid", "-c", "user.name=Test")
        self.git("init", "-q", "--initial-branch=develop", ".")
        self.git("add", "-A")
        self.git(*identity, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "a")
        self.head = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=WAIT_SECONDS,
        ).stdout

    def interrupt(self, sig: signal.Signals, group: bool) -> tuple[int, str]:
        """Push one branch at HEAD, which the hook passes on to the engine, then signal it."""
        updates = self.outside / "updates"
        updates.write_text(
            f"refs/heads/develop {self.head} refs/heads/develop {'0' * len(self.head)}\n",
            encoding="utf-8",
        )
        with updates.open(encoding="utf-8") as stdin:
            hook = subprocess.Popen(
                ["bash", ".husky/pre-push", "origin", "https://example.invalid/repo.git"],
                cwd=str(self.root),
                env=self.env,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                start_new_session=True,
            )
        try:
            wait_until_started(hook, self.ready)
            if group:
                os.killpg(hook.pid, sig)
            else:
                os.kill(hook.pid, sig)
            _, stderr = hook.communicate(timeout=WAIT_SECONDS)
            assert_group_ended(hook.pid)
        finally:
            end_group(hook)
        return hook.returncode, stderr

    def test_a_signal_stops_the_engine_and_ends_the_hook_by_it(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            for group in (True, False):
                with self.subTest(signal=sig.name, group=group):
                    self.ready.unlink(missing_ok=True)
                    status, stderr = self.interrupt(sig, group)
                    self.assertEqual(-sig, status, stderr)
                    self.assertIn(f"pre-push: interrupted by {sig.name}.", stderr)


if __name__ == "__main__":
    unittest.main()
