#!/usr/bin/env python3
"""Hold the review gate, its readers, and the cleanup script under their size ceilings."""

from __future__ import annotations

import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
CEILINGS = {
    "pr_review.py": 2200,
    "pr_review_checks.py": 450,
    "pr_review_check_lines.py": 200,
    "pr_review_rounds.py": 700,
    "pr_review_digest.py": 350,
    "pr_review_copilot.py": 1500,
    "pr_cleanup.py": 1200,
}


class ReviewSizeCase(unittest.TestCase):
    """A reviewer's new spelling is a vocabulary edit, so none of these grows by one more branch.

    A ceiling reached is a prompt to redesign the file along its boundaries, never to raise it.
    """

    def test_each_script_stays_under_its_ceiling(self) -> None:
        for name, ceiling in CEILINGS.items():
            with self.subTest(name=name):
                lines = len((SCRIPTS / name).read_text(encoding="utf-8").splitlines())
                self.assertLess(lines, ceiling, f"{name} has {lines} lines")


if __name__ == "__main__":
    unittest.main()
