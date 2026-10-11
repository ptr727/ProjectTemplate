#!/usr/bin/env python3
"""Prove the shared step reader stays inside the step it was asked for."""

from __future__ import annotations

import unittest

from workflow_steps import step_script_text

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


if __name__ == "__main__":
    unittest.main()
