#!/usr/bin/env python3
"""Hold the Claude Code write guard under its size ceiling."""

from __future__ import annotations

import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "host-setup/agent-safety/claude/gh-write-guard.py"
MAX_LINES = 2500


class GuardSizeCase(unittest.TestCase):
    """The guard bounds a good-faith mistake, so every later review pays for each line it grows."""

    def test_guard_stays_under_the_ceiling(self) -> None:
        lines = len(GUARD.read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(lines, MAX_LINES, f"{GUARD.name} has {lines} lines")


if __name__ == "__main__":
    unittest.main()
