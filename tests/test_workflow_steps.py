#!/usr/bin/env python3
"""Prove the shared step reader stays inside its step, and the promotion history step reads main right."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from host_capability import bash_or_skip
from workflow_steps import step_script, step_script_text, yaml_step, yaml_value

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "spec"))
import git_env

VALIDATE_TASK_WORKFLOW = REPO / ".github" / "workflows" / "validate-task.yml"

WORKFLOW = """\
jobs:
  build:
    steps:
      - name: First step
        uses: ./action

      - name: Second step
        run: |
          echo second
"""


class StepScriptCase(unittest.TestCase):
    """A step with no run block of its own is an error, never the next step's script."""

    def test_step_without_run_fails_rather_than_reading_the_next_step(self) -> None:
        with self.assertRaises(AssertionError):
            step_script_text(WORKFLOW, "First step")

    def test_named_step_reads_its_own_script(self) -> None:
        self.assertEqual("echo second", step_script_text(WORKFLOW, "Second step").strip())

    def test_strip_chomp_indicator_is_read(self) -> None:
        text = WORKFLOW.replace("run: |", "run: |-")
        self.assertEqual("echo second", step_script_text(text, "Second step").strip())

    def test_keep_chomp_indicator_is_read(self) -> None:
        text = WORKFLOW.replace("run: |", "run: |+")
        self.assertEqual("echo second", step_script_text(text, "Second step").strip())

    def test_run_key_nested_under_another_key_is_not_the_steps(self) -> None:
        text = "- name: A\n  with:\n    run: |\n      echo nested\n"
        with self.assertRaises(AssertionError):
            step_script_text(text, "A")


class PromotionHistoryCase(unittest.TestCase):
    """A single-parent commit on main since its previous promotion fails the next promotion.

    Each case builds main and develop the way the fleet does. A feature lands on develop as one
    commit, and a promotion or a bot pull request lands on main as a merge commit. A squash into
    main is the defect, since it leaves the merge base with develop behind content main holds.
    """

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.git("init", "-q", "-b", "main", str(self.root))
        self.commit("readme.txt", "start\n")
        self.git("branch", "develop")
        self.features = 0

    def git(self, *args: str) -> str:
        identity = ("-c", "user.email=gate@example.invalid", "-c", "user.name=gate test")
        return subprocess.run(
            ["git", "-C", str(self.root), *identity, "-c", "commit.gpgsign=false", *args],
            env=git_env.without_git_location(os.environ),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    def commit(self, name: str, text: str) -> str:
        (self.root / name).write_text(text, encoding="utf-8")
        self.git("add", "--", name)
        self.git("commit", "-qm", f"change {name}")
        return self.git("rev-parse", "HEAD")

    def feature(self) -> None:
        """Land one feature on develop, in a file of its own so no promotion conflicts."""
        self.features += 1
        self.git("checkout", "-q", "develop")
        self.commit(f"feature{self.features}.txt", "feature\n")

    def promote(self) -> str:
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "promote develop", "develop")
        return self.git("rev-parse", "HEAD")

    def squash(self) -> str:
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--squash", "develop")
        self.git("commit", "-qm", "squashed promotion")
        return self.git("rev-parse", "HEAD")

    def bot_bump(self) -> None:
        """A bot pull request cut from main and merged back into it."""
        self.git("checkout", "-q", "-B", "bot", "main")
        self.commit("pin.txt", f"pin {self.features}\n")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "bump", "bot")

    def floor(self) -> str:
        """The floor the workflow ships, so a case run without an override proves it inert."""
        step = yaml_step(
            VALIDATE_TASK_WORKFLOW.read_text(encoding="utf-8"), "Check promotion history step"
        )
        value = yaml_value(step, "env", "SQUASH_FLOOR")
        assert value is not None
        return value.strip()

    def check(self, head: str = "develop", **facts: str) -> tuple[int, str]:
        self.git("update-ref", "refs/remotes/origin/main", "main")
        if self.git("branch", "--list", "develop"):
            self.git("update-ref", "refs/remotes/origin/develop", "develop")
        env = (
            git_env.without_git_location(os.environ)
            | {
                "BASE_REF": "main",
                "DEFAULT_BRANCH": "main",
                "HEAD_REPOSITORY": "example/widget",
                "REPOSITORY": "example/widget",
                "HEAD_SHA": self.git("rev-parse", head),
                "SQUASH_FLOOR": self.floor(),
            }
            | facts
        )
        result = subprocess.run(
            [
                bash_or_skip(),
                "-c",
                step_script(VALIDATE_TASK_WORKFLOW, "Check promotion history step"),
            ],
            cwd=self.root,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        return result.returncode, result.stdout + result.stderr

    def test_merge_only_history_passes(self) -> None:
        self.feature()
        self.promote()
        self.bot_bump()
        self.feature()
        code, log = self.check()
        self.assertEqual(0, code, log)
        self.assertIn("is a merge commit", log)

    def test_squash_since_the_previous_promotion_fails(self) -> None:
        self.feature()
        self.promote()
        self.feature()
        squashed = self.squash()
        self.bot_bump()
        self.feature()
        code, log = self.check()
        self.assertEqual(1, code, log)
        self.assertIn("::error::main's first-parent chain since its previous promotion merge", log)
        self.assertIn(squashed, log)

    def test_a_first_promotion_that_was_squashed_fails(self) -> None:
        """No promotion merge stops the walk, and the shared root is develop history."""
        self.feature()
        squashed = self.squash()
        self.feature()
        code, log = self.check()
        self.assertEqual(1, code, log)
        self.assertIn(squashed, log)
        self.assertIn("with no promotion merge on it", log)

    def test_a_squash_behind_a_later_promotion_passes(self) -> None:
        self.feature()
        self.squash()
        self.feature()
        self.promote()
        self.feature()
        code, log = self.check()
        self.assertEqual(0, code, log)

    def test_the_recovery_cut_passes_and_restores_the_merge_base(self) -> None:
        self.feature()
        self.promote()
        self.feature()
        squashed = self.squash()
        self.feature()
        self.git("checkout", "-q", "-b", "promote/develop-to-main", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge develop", "develop")
        code, log = self.check("promote/develop-to-main")
        self.assertEqual(0, code, log)
        self.assertIn(squashed, log)
        self.assertIn("already holds each of them", log)
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "promote the cut", "promote/develop-to-main")
        self.assertEqual(
            self.git("rev-parse", "develop"), self.git("merge-base", "main", "develop")
        )
        self.feature()
        code, log = self.check()
        self.assertEqual(0, code, log)
        self.assertIn("is a merge commit", log)

    def test_the_recovery_cut_passes_after_a_bot_merge_moves_main(self) -> None:
        self.feature()
        self.promote()
        self.feature()
        squashed = self.squash()
        self.feature()
        self.git("checkout", "-q", "-b", "promote/develop-to-main", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge develop", "develop")
        self.bot_bump()
        code, log = self.check("promote/develop-to-main")
        self.assertEqual(0, code, log)
        self.assertIn(squashed, log)

    def test_a_clean_promotion_over_a_squash_fails_since_it_keeps_a_removed_line(self) -> None:
        """A promotion that merges cleanly is still wrong, so the step fails it rather than only a conflict."""
        self.git("checkout", "-q", "develop")
        self.commit("notes.txt", "keep\n")
        self.promote()
        self.git("checkout", "-q", "develop")
        self.commit("notes.txt", "keep\ndrop\n")
        squashed = self.squash()
        self.git("checkout", "-q", "develop")
        self.commit("notes.txt", "keep\n")
        code, log = self.check()
        self.assertEqual(1, code, log)
        self.assertIn(squashed, log)
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "promote develop", "develop")
        self.assertEqual("keep\ndrop", self.git("show", "main:notes.txt"))

    def test_the_floor_excludes_itself_and_older_history(self) -> None:
        self.feature()
        older = self.squash()
        self.feature()
        squashed = self.squash()
        self.feature()
        code, log = self.check(SQUASH_FLOOR=older)
        self.assertEqual(1, code, log)
        self.assertIn(squashed, log)
        self.assertNotIn(f"holds {older}", log)
        code, log = self.check(SQUASH_FLOOR=squashed)
        self.assertEqual(0, code, log)
        self.assertIn(f"past its floor {squashed}", log)

    def test_a_repository_with_no_develop_promotes_nothing(self) -> None:
        self.git("branch", "-D", "develop")
        code, log = self.check("main")
        self.assertEqual(0, code, log)
        self.assertIn("has no develop branch", log)

    def test_a_bot_pull_request_into_main_is_no_promotion(self) -> None:
        self.feature()
        self.squash()
        self.git("checkout", "-q", "-B", "bot", "main")
        self.commit("pin.txt", "pin next\n")
        code, log = self.check("bot")
        self.assertEqual(0, code, log)
        self.assertIn("is no promotion", log)

    def test_a_fork_head_or_another_base_promotes_nothing(self) -> None:
        self.feature()
        self.squash()
        self.feature()
        for facts in ({"HEAD_REPOSITORY": "someone/widget"}, {"BASE_REF": "develop"}):
            with self.subTest(facts=facts):
                code, log = self.check(**facts)
                self.assertEqual(0, code, log)
                self.assertIn("promotes nothing", log)


if __name__ == "__main__":
    unittest.main()
