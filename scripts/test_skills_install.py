#!/usr/bin/env python3
"""Exercise skills_install.py's materialization and staleness reporting, without touching the
real `claude` CLI state or the real ~/.agents directory.

Run as `python3 scripts/test_skills_install.py`, or under `python3 -m unittest discover -s scripts`.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import skills_install


class AgentsHomeCase(unittest.TestCase):
    def test_a_tilde_override_expands_to_the_real_home_directory(self) -> None:
        """AGENTS_HOME=~/tmp is a real thing a caller would type. A bare Path() treats "~" as a
        literal directory name rather than the shell-expanded home it looks like."""
        with mock.patch.dict("os.environ", {"AGENTS_HOME": "~/agents-test"}):
            home = skills_install.agents_home()
        self.assertNotIn("~", str(home))
        self.assertEqual(home, Path.home() / "agents-test")


class SourceRefCase(unittest.TestCase):
    """source_ref()'s dirty check must watch every path this installer actually reads from,
    not only .agents/skills/, or a modified marketplace.json/generated plugin would report
    dirty=False over bytes that were never installed."""

    def test_git_status_is_scoped_to_both_watched_paths(self) -> None:
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            result = mock.Mock()
            result.returncode = 0
            result.stdout = "deadbeef\n" if "rev-parse" in args else ""
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            skills_install.source_ref()

        status_call = next(c for c in calls if "status" in c)
        self.assertIn(str(skills_install.SKILLS_SRC.relative_to(skills_install.ROOT).as_posix()), status_call)
        self.assertIn(
            str(skills_install.CLAUDE_PLUGIN_DIR.relative_to(skills_install.ROOT).as_posix()), status_call
        )


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

    def test_a_retired_fleet_skill_is_removed_on_the_next_install(self) -> None:
        (self.src / "foo").mkdir(parents=True)
        (self.src / "foo" / "SKILL.md").write_text("x", encoding="utf-8")
        (self.src / "bar").mkdir(parents=True)
        (self.src / "bar" / "SKILL.md").write_text("x", encoding="utf-8")
        target = self.tmp / "target"
        skills_install.materialize_global_skills(target)
        self.assertTrue((target / "bar").is_dir())

        shutil.rmtree(self.src / "bar")
        skills_install.materialize_global_skills(target)

        self.assertFalse((target / "bar").exists())
        self.assertTrue((target / "foo").is_dir())

    def test_a_symlinked_directory_in_the_cleanup_scan_is_skipped_not_crashed_on(self) -> None:
        """is_dir() alone follows a symlink, and shutil.rmtree() refuses a top-level symlink
        with an uncaught OSError. A stray symlink under the shared target, even one whose real
        target happens to carry the marker, must be skipped rather than blow up the install."""
        real = self.tmp / "elsewhere"
        real.mkdir()
        (real / skills_install.INSTALLED_MARKER).write_text("", encoding="utf-8")
        target = self.tmp / "target"
        target.mkdir(parents=True)
        (target / "retired-name").symlink_to(real)
        self.src.mkdir(parents=True)

        skills_install.materialize_global_skills(target)  # must not raise

        self.assertTrue((target / "retired-name").is_symlink())
        self.assertTrue((real / skills_install.INSTALLED_MARKER).is_file())

    def test_a_same_named_third_party_skill_is_never_removed_as_if_retired(self) -> None:
        """The marker, not the name, decides what this installer may remove. A third-party
        skill happening to share a name with something the fleet once published must survive,
        even though a name-only check would read it as "our old content, now gone"."""
        target = self.tmp / "target"
        target.mkdir(parents=True)
        (target / "bar").mkdir()
        (target / "bar" / "SKILL.md").write_text("not ours, no marker", encoding="utf-8")
        self.src.mkdir(parents=True)

        skills_install.materialize_global_skills(target)

        self.assertEqual((target / "bar" / "SKILL.md").read_text(encoding="utf-8"), "not ours, no marker")

    def test_an_installed_skill_carries_the_marker(self) -> None:
        (self.src / "foo").mkdir(parents=True)
        (self.src / "foo" / "SKILL.md").write_text("x", encoding="utf-8")
        target = self.tmp / "target"
        skills_install.materialize_global_skills(target)
        self.assertTrue((target / "foo" / skills_install.INSTALLED_MARKER).is_file())

    def test_a_directory_without_skill_md_is_not_copied_as_a_skill(self) -> None:
        """Matches build_dist.skill_names()'s own definition of a skill, so a stray cache or
        scratch directory under .agents/skills/ is never treated as one here either."""
        (self.src / "not-a-skill").mkdir(parents=True)
        (self.src / "not-a-skill" / "notes.txt").write_text("wip", encoding="utf-8")
        target = self.tmp / "target"
        skills_install.materialize_global_skills(target)
        self.assertFalse((target / "not-a-skill").exists())

    def test_a_symlink_in_a_skill_directory_is_rejected(self) -> None:
        """shutil.copytree() follows a symlink by default, which would silently pull content
        from outside .agents/skills/ into the shared, machine-wide skills directory."""
        (self.src / "foo").mkdir(parents=True)
        (self.src / "foo" / "SKILL.md").write_text("x", encoding="utf-8")
        (self.src / "foo" / "escape").symlink_to(self.tmp)
        target = self.tmp / "target"
        with self.assertRaises(ValueError):
            skills_install.materialize_global_skills(target)


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
            stamp.write_text(json.dumps({"stampVersion": skills_install.STAMP_VERSION, "source": {"commit": "abc"}}), encoding="utf-8")
            exit_code = skills_install.report(stamp)
        self.assertEqual(exit_code, 0)

    def test_mismatched_commit_reports_stale(self) -> None:
        stamp = self.tmp / "stamp.json"
        stamp.write_text(json.dumps({"stampVersion": skills_install.STAMP_VERSION, "source": {"commit": "old"}}), encoding="utf-8")
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
        stamp.write_text(json.dumps({"stampVersion": skills_install.STAMP_VERSION, "source": {"commit": "abc"}}), encoding="utf-8")
        with mock.patch("skills_install.source_ref",
                         return_value={"vcs": "git", "commit": "abc", "dirty": True}):
            exit_code = skills_install.report(stamp)
        self.assertEqual(exit_code, 1)

    def test_a_valid_json_non_dict_stamp_reports_stale_instead_of_crashing(self) -> None:
        stamp = self.tmp / "stamp.json"
        stamp.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        with mock.patch("builtins.print"):
            exit_code = skills_install.report(stamp)
        self.assertEqual(exit_code, 1)

    def test_a_dict_stamp_with_a_non_dict_source_reports_stale_instead_of_crashing(self) -> None:
        stamp = self.tmp / "stamp.json"
        stamp.write_text(
            json.dumps({"stampVersion": skills_install.STAMP_VERSION, "source": "oops"}),
            encoding="utf-8",
        )
        with mock.patch("skills_install.source_ref", return_value={"vcs": "git", "commit": "abc"}):
            with mock.patch("builtins.print"):
                exit_code = skills_install.report(stamp)
        self.assertEqual(exit_code, 1)

    def test_an_unrecognized_stamp_version_reports_stale(self) -> None:
        """A future format bump must not have an old-shaped stamp read as current."""
        stamp = self.tmp / "stamp.json"
        stamp.write_text(
            json.dumps({"stampVersion": skills_install.STAMP_VERSION + 1, "source": {"commit": "abc"}}),
            encoding="utf-8",
        )
        with mock.patch("skills_install.source_ref", return_value={"vcs": "git", "commit": "abc"}):
            with mock.patch("builtins.print"):
                exit_code = skills_install.report(stamp)
        self.assertEqual(exit_code, 1)

    def test_a_non_git_checkout_reports_stale_rather_than_current(self) -> None:
        """source_ref() returns {"vcs": "none"} with no "commit" key at all outside a git
        checkout. A bare equality/or chain could leave `stale` as None (falsy, same as False)
        instead of asserting staleness when there is no commit to compare against at all."""
        stamp = self.tmp / "stamp.json"
        stamp.write_text(
            json.dumps({"stampVersion": skills_install.STAMP_VERSION, "source": {"commit": None}}),
            encoding="utf-8",
        )
        with mock.patch("skills_install.source_ref", return_value={"vcs": "none"}):
            with mock.patch("builtins.print"):
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
