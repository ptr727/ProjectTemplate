#!/usr/bin/env python3
"""Drive the hub's own `.husky/pre-commit` through real commits, a merge commit among them.

The hook chooses the base its prose gate diffs against, and only a real commit shows which base it
chose. Each case copies the hook and the prose gate into a throwaway repository and commits there,
with the language formatters and the eol check replaced by stubs, since what is under test is the
prose gate's scope rather than those tools.

The fixture isolates git's global and system configuration, so a host with commit signing on or a
hooks path of its own cannot change what a case measures.

Run as `python3 tests/test_pre_commit_hook.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / ".husky" / "pre-commit"
GATE_ENTRY = REPO / "scripts" / "prose_lint.py"
GATE = REPO / ".github" / "actions" / "prose-gate" / "prose_lint.py"

COMMENT = "# The value is read once, since the second read can disagree.\n"


@unittest.skipUnless(sys.platform == "linux", "runs a POSIX shell hook with executable stubs")
class TestTheHookDiffsAMergeAgainstItsMergedInParent(unittest.TestCase):
    """A merge of develop brings in lines the branch never wrote, and the hook must not read them.

    Measured against `HEAD`, every line develop gained since the fork reads as added, so a comment
    another pull request already landed trips `comment-added` on a merge that adds none.
    """

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        empty = outside / "empty-gitconfig"
        empty.write_text("", encoding="utf-8")
        stubs = outside / "bin"
        stubs.mkdir()
        uvx = stubs / "uvx"
        uvx.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        uvx.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.pop("PROSE_ALLOW_COMMENTS", None)
        self.env["GIT_CONFIG_GLOBAL"] = str(empty)
        self.env["GIT_CONFIG_SYSTEM"] = str(empty)
        self.env["PATH"] = f"{stubs}{os.pathsep}{self.env.get('PATH', '')}"

        self.git("init", "-q", "--initial-branch=develop", ".")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", ".husky")
        for source, dest in (
            (HOOK, ".husky/pre-commit"),
            (GATE_ENTRY, "scripts/prose_lint.py"),
            (GATE, ".github/actions/prose-gate/prose_lint.py"),
        ):
            target = self.root / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (self.root / "scripts" / "repo_gate.py").write_text(
            "import sys\nsys.exit(0)\n", encoding="utf-8"
        )
        self.write("tool.py", "value = 1\n")
        self.write("conflict.py", "VALUE = 'base'\n")
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "-m", "base")

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

    def write(self, name: str, text: str) -> None:
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

    def commit_resolution(self, resolved: str) -> subprocess.CompletedProcess[str]:
        self.write("conflict.py", resolved)
        self.git("add", "conflict.py")
        return self.run_git("commit", "--no-edit")

    def test_a_comment_the_merged_in_parent_brought_passes(self) -> None:
        result = self.commit_resolution("VALUE = 'resolved'\n")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

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


if __name__ == "__main__":
    unittest.main()
