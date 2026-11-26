#!/usr/bin/env python3
"""Prove the step reader stays inside its step, the promotion history step reads main right, and hook-input reaches a hook only when set."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple

from host_capability import bash_or_skip
from workflow_steps import step_script, step_script_text, yaml_step, yaml_value

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "spec"))
import git_env

VALIDATE_TASK_WORKFLOW = REPO / ".github" / "workflows" / "validate-task.yml"
UPSTREAM_TASK_WORKFLOW = REPO / ".github" / "workflows" / "check-upstream-version-task.yml"

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


class HookRoute(NamedTuple):
    """One task's hook, the two steps that run it, and the hub's own copy of it."""

    workflow: Path
    hook: str
    plain_step: str
    input_step: str


HOOK_ROUTES = (
    HookRoute(
        UPSTREAM_TASK_WORKFLOW,
        "./.github/actions/resolve-upstream",
        "Run resolve-upstream hook step",
        "Run resolve-upstream hook with hook input step",
    ),
    HookRoute(
        VALIDATE_TASK_WORKFLOW,
        "./.github/actions/validate",
        "Run repository validate hook step",
        "Run repository validate hook with hook input step",
    ),
)


def unwrap(expression: str | None) -> str:
    """An `if:` value with any `${{ }}` wrapper removed, so both spellings read alike."""
    text = (expression or "").strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text


def hook_input_violations(text: str, route: HookRoute) -> list[str]:
    """What keeps `route`'s task from handing hook-input to its hook only when the caller set it."""
    found = []
    inputs = ("on", "workflow_call", "inputs", "hook-input")
    if (
        yaml_value(text, *inputs, "type") != "string"
        or yaml_value(text, *inputs, "default") != "''"
    ):
        found.append("hook-input is not an optional string input defaulting to empty")
    present = f"hashFiles('{route.hook.removeprefix('./')}/action.yml') != ''"
    for name, guard in ((route.plain_step, "== ''"), (route.input_step, "!= ''")):
        step = yaml_step(text, name)
        if yaml_value(step, "uses") != route.hook:
            found.append(f"{name} does not run {route.hook}")
        if unwrap(yaml_value(step, "if")) != f"{present} && inputs.hook-input {guard}":
            found.append(f"{name} is not gated on the hook existing and hook-input {guard}")
    if yaml_value(yaml_step(text, route.plain_step), "with") is not None:
        found.append(f"{route.plain_step} passes the hook an input")
    passed = yaml_step(text, route.input_step)
    if yaml_value(passed, "with", "hook-input") != "${{ inputs.hook-input }}":
        found.append(f"{route.input_step} does not pass hook-input through")
    return found


class HookInputCase(unittest.TestCase):
    """Each task hands hook-input to its hook from a step of its own, so a hook that never declared it is never handed it."""

    def test_each_task_is_clean(self) -> None:
        for route in HOOK_ROUTES:
            with self.subTest(workflow=route.workflow.name):
                self.assertEqual([], hook_input_violations(route.workflow.read_text(), route))

    def test_each_mutation_is_caught(self) -> None:
        for route in HOOK_ROUTES:
            text = route.workflow.read_text()
            passed = "          hook-input: ${{ inputs.hook-input }}\n"
            uses = f"        uses: {route.hook}\n"
            start = text.index(f"- name: {route.plain_step}\n")
            end = text.index(uses, start) + len(uses)
            mutations = {
                "input step always passes": (
                    text.replace(" && inputs.hook-input != ''", "", 1),
                    f"{route.input_step} is not gated",
                ),
                "plain step always runs": (
                    text.replace(" && inputs.hook-input == ''", "", 1),
                    f"{route.plain_step} is not gated",
                ),
                "value miswired": (
                    text.replace(passed, passed.replace("inputs.", "inputs.x"), 1),
                    f"{route.input_step} does not pass hook-input through",
                ),
                "input dropped": (
                    text.replace("        with:\n" + passed, "", 1),
                    f"{route.input_step} does not pass hook-input through",
                ),
                "plain step passes it": (
                    text[:end] + "        with:\n" + passed + text[end:],
                    f"{route.plain_step} passes the hook an input",
                ),
                "input required": (
                    text.replace("        default: ''\n    secrets:", "    secrets:", 1),
                    "hook-input is not an optional string input",
                ),
            }
            for label, (mutated, expected) in mutations.items():
                with self.subTest(workflow=route.workflow.name, mutation=label):
                    self.assertNotEqual(text, mutated)
                    self.assertTrue(
                        any(v.startswith(expected) for v in hook_input_violations(mutated, route))
                    )

    def test_the_upstream_task_reads_versions_from_either_step(self) -> None:
        script_env = yaml_value(
            yaml_step(UPSTREAM_TASK_WORKFLOW.read_text(), "Compute upstream version diff step"),
            "env",
            "NEW_VERSIONS",
        )
        self.assertEqual(
            "${{ steps.resolve-upstream.outputs.versions"
            " || steps.resolve-upstream-with-input.outputs.versions }}",
            script_env,
        )

    def test_each_hub_hook_declares_hook_input_as_optional(self) -> None:
        for route in HOOK_ROUTES:
            action = REPO / route.hook.removeprefix("./") / "action.yml"
            with self.subTest(hook=route.hook):
                self.assertEqual(
                    "false", yaml_value(action.read_text(), "inputs", "hook-input", "required")
                )


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

    def test_the_recovery_cut_restores_a_line_the_stale_base_kept(self) -> None:
        """The cut merges from the stale base too, so its tree check is what catches the kept line."""
        self.git("checkout", "-q", "develop")
        self.commit("notes.txt", "keep\n")
        self.promote()
        self.git("checkout", "-q", "develop")
        self.commit("notes.txt", "keep\ndrop\n")
        self.squash()
        self.git("checkout", "-q", "develop")
        self.commit("notes.txt", "keep\n")
        self.git("checkout", "-q", "-b", "promote/develop-to-main", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge develop", "develop")
        self.assertEqual("notes.txt", self.git("diff", "--name-only", "develop", "HEAD"))
        self.git("checkout", "develop", "--", "notes.txt")
        self.git("commit", "-qm", "restore develop's notes")
        self.assertEqual("", self.git("diff", "--name-only", "develop", "HEAD"))
        code, log = self.check("promote/develop-to-main")
        self.assertEqual(0, code, log)
        self.assertIn("confirm its tree matches develop's", log)

    def test_the_recovery_cut_removes_a_file_develop_deleted(self) -> None:
        """A file the squash carried and develop then deleted survives the cut's merge, so it is removed."""
        self.git("checkout", "-q", "develop")
        self.commit("gone.txt", "gone\n")
        self.squash()
        self.git("checkout", "-q", "develop")
        self.git("rm", "-q", "gone.txt")
        self.git("commit", "-qm", "delete gone.txt")
        self.git("checkout", "-q", "-b", "promote/develop-to-main", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge develop", "develop")
        self.assertEqual("gone.txt", self.git("diff", "--name-only", "develop", "HEAD"))
        self.git("rm", "-q", "gone.txt")
        self.git("commit", "-qm", "remove what develop deleted")
        self.assertEqual("", self.git("diff", "--name-only", "develop", "HEAD"))
        code, log = self.check("promote/develop-to-main")
        self.assertEqual(0, code, log)

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
