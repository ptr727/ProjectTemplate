#!/usr/bin/env python3
"""Hold the audit engine and its Markdown module under their size ceilings."""

from __future__ import annotations

import unittest
from pathlib import Path

SPEC = Path(__file__).resolve().parents[1] / "spec"
CEILINGS = {"audit.py": 3000, "audit_markdown.py": 1000}


class AuditSizeCase(unittest.TestCase):
    """Tests live in tests/ and parsing is a library's, so neither file has a reason to grow back."""

    def test_each_file_stays_under_its_ceiling(self) -> None:
        for name, ceiling in CEILINGS.items():
            with self.subTest(name=name):
                lines = len((SPEC / name).read_text(encoding="utf-8").splitlines())
                self.assertLessEqual(lines, ceiling, f"{name} has {lines} lines")


if __name__ == "__main__":
    unittest.main()
