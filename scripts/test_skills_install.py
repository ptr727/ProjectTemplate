#!/usr/bin/env python3
"""Exercise skills_install.py's materialization and staleness reporting, without touching the
real `claude` CLI state or the real ~/.agents directory.

Run as `python3 scripts/test_skills_install.py`, or under `python3 -m unittest discover -s scripts`.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import skills_install


class MaterializeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.src = self.tmp / "src"
        self.addCleanup(self._restore, skills_install.SKILLS_SRC)
        skills_install.SKILLS_SRC = self.src

    def _restore(self, src) -> None:
        skills_install.SKILLS_SRC = src

    def test_materializing_with_no_source_creates_an_empty_target(self) -> None:
        target = self.tmp / "target"
        skills_install.materialize_global_skills(target)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_materializing_copies_the_source_tree(self) -> None:
        (self.src / "foo").mkdir(parents=True)
        (self.src / "foo" / "SKILL.md").write_text("x", encoding="utf-8")
        target = self.tmp / "target"
        skills_install.materialize_global_skills(target)
        self.assertEqual((target / "foo" / "SKILL.md").read_text(encoding="utf-8"), "x")

    def test_re_materializing_replaces_stale_content(self) -> None:
        (self.src / "foo").mkdir(parents=True)
        (self.src / "foo" / "SKILL.md").write_text("v1", encoding="utf-8")
        target = self.tmp / "target"
        skills_install.materialize_global_skills(target)
        (self.src / "foo" / "SKILL.md").write_text("v2", encoding="utf-8")
        skills_install.materialize_global_skills(target)
        self.assertEqual((target / "foo" / "SKILL.md").read_text(encoding="utf-8"), "v2")


class ReportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_no_stamp_reports_not_installed(self) -> None:
        with mock.patch.object(__import__("sys").modules["skills_install"], "print"):
            exit_code = skills_install.report(self.tmp / "missing-stamp.json")
        self.assertEqual(exit_code, 1)

    def test_matching_commit_reports_current(self) -> None:
        stamp = self.tmp / "stamp.json"
        with mock.patch("skills_install.source_ref", return_value={"vcs": "git", "commit": "abc"}):
            stamp.write_text(json.dumps({"source": {"commit": "abc"}}), encoding="utf-8")
            exit_code = skills_install.report(stamp)
        self.assertEqual(exit_code, 0)

    def test_mismatched_commit_reports_stale(self) -> None:
        stamp = self.tmp / "stamp.json"
        stamp.write_text(json.dumps({"source": {"commit": "old"}}), encoding="utf-8")
        with mock.patch("skills_install.source_ref", return_value={"vcs": "git", "commit": "new"}):
            exit_code = skills_install.report(stamp)
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
