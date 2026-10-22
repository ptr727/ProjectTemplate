#!/usr/bin/env python3
"""The repository-location variables a self-test must not inherit from a git hook."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "spec"))
import git_env


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """A checked git call that reads UTF-8, run with no ambient location variable."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=git_env.without_git_location(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout


class TestLocationList(unittest.TestCase):
    def test_the_list_covers_every_name_git_calls_local(self) -> None:
        printed = _git(REPO, "rev-parse", "--local-env-vars").split()

        self.assertEqual(sorted(set(printed) - set(git_env.GIT_LOCATION_VARS)), [])

    def test_without_git_location_drops_only_those_names(self) -> None:
        env = {"GIT_DIR": "x", "GIT_INDEX_FILE": "y", "GIT_AUTHOR_NAME": "n", "PATH": "p"}

        self.assertEqual(git_env.without_git_location(env), {"GIT_AUTHOR_NAME": "n", "PATH": "p"})

    def test_the_context_manager_restores_the_environment(self) -> None:
        os.environ["GIT_DIR"] = "sentinel"
        try:
            with git_env.git_location_cleared():
                self.assertNotIn("GIT_DIR", os.environ)
            self.assertEqual(os.environ["GIT_DIR"], "sentinel")
        finally:
            del os.environ["GIT_DIR"]


class TestAuditSelftestIgnoresAmbientGitDir(unittest.TestCase):
    def test_the_selftest_leaves_the_ambient_repository_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            _git(scratch, "init", "-q", "--initial-branch=develop", ".")
            _git(scratch, "config", "user.email", "scratch@example.invalid")
            _git(scratch, "config", "user.name", "scratch")
            _git(scratch, "commit", "-q", "--allow-empty", "-m", "seed")

            def state() -> tuple[str, str, str]:
                return (
                    _git(scratch, "config", "--local", "--list"),
                    _git(scratch, "log", "--all", "--format=%H %s"),
                    _git(scratch, "status", "--porcelain"),
                )

            before = state()
            env = dict(os.environ)
            env["GIT_DIR"] = str(scratch / ".git")
            env["GIT_WORK_TREE"] = str(scratch)
            subprocess.run(
                [sys.executable, str(REPO / "spec" / "audit.py"), "--selftest"],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=600,
            )

            self.assertEqual(state(), before)


if __name__ == "__main__":
    unittest.main()
