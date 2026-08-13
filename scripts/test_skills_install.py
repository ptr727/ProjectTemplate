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

    def test_materializing_when_the_target_parent_does_not_exist_yet(self) -> None:
        (self.src / "foo").mkdir(parents=True)
        (self.src / "foo" / "SKILL.md").write_text("x", encoding="utf-8")
        target = self.tmp / "not-yet-created" / "skills"
        skills_install.materialize_global_skills(target)
        self.assertEqual((target / "foo" / "SKILL.md").read_text(encoding="utf-8"), "x")

    def test_materializing_over_a_stray_file_at_the_target_path(self) -> None:
        target = self.tmp / "target"
        target.write_text("not a directory", encoding="utf-8")
        skills_install.materialize_global_skills(target)
        self.assertTrue(target.is_dir())

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

    def test_a_skill_from_another_source_already_in_target_is_left_alone(self) -> None:
        """~/.agents/skills/ is a shared convention, not this fleet's own directory. Installing
        this fleet's skills must never delete a skill some other tool or fleet put there."""
        (self.src / "foo").mkdir(parents=True)
        (self.src / "foo" / "SKILL.md").write_text("fleet content", encoding="utf-8")
        target = self.tmp / "target"
        target.mkdir(parents=True)
        (target / "someone-elses-skill").mkdir()
        (target / "someone-elses-skill" / "SKILL.md").write_text("not ours", encoding="utf-8")

        skills_install.materialize_global_skills(target)

        self.assertEqual((target / "foo" / "SKILL.md").read_text(encoding="utf-8"), "fleet content")
        self.assertEqual(
            (target / "someone-elses-skill" / "SKILL.md").read_text(encoding="utf-8"), "not ours"
        )


class ReportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_no_stamp_reports_not_installed(self) -> None:
        with mock.patch("builtins.print"):
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

    def test_unreadable_stamp_reports_stale_instead_of_crashing(self) -> None:
        stamp = self.tmp / "stamp.json"
        stamp.write_text("not valid json {{{", encoding="utf-8")
        with mock.patch("builtins.print"):
            exit_code = skills_install.report(stamp)
        self.assertEqual(exit_code, 1)

    def test_matching_commit_but_dirty_checkout_reports_stale(self) -> None:
        stamp = self.tmp / "stamp.json"
        stamp.write_text(json.dumps({"source": {"commit": "abc"}}), encoding="utf-8")
        with mock.patch("skills_install.source_ref",
                         return_value={"vcs": "git", "commit": "abc", "dirty": True}):
            exit_code = skills_install.report(stamp)
        self.assertEqual(exit_code, 1)


class MainExitCodeCase(unittest.TestCase):
    """A caller scripting this installer needs the exit code to distinguish a real failure
    (claude present but registration failed) from an expected partial install (no claude on
    this machine at all)."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(mock.patch.stopall)
        mock.patch("skills_install.agents_home", return_value=self.tmp).start()
        mock.patch("skills_install.materialize_global_skills").start()
        mock.patch("sys.argv", ["skills_install.py"]).start()

    def test_claude_present_but_registration_fails_exits_nonzero(self) -> None:
        mock.patch("skills_install.claude_available", return_value=True).start()
        mock.patch("skills_install.register_claude_marketplace", return_value=False).start()
        self.assertEqual(skills_install.main(), 1)

    def test_claude_present_and_registration_succeeds_exits_zero(self) -> None:
        mock.patch("skills_install.claude_available", return_value=True).start()
        mock.patch("skills_install.register_claude_marketplace", return_value=True).start()
        self.assertEqual(skills_install.main(), 0)

    def test_claude_absent_is_a_partial_install_not_a_failure(self) -> None:
        mock.patch("skills_install.claude_available", return_value=False).start()
        self.assertEqual(skills_install.main(), 0)


if __name__ == "__main__":
    unittest.main()
