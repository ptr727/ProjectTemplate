#!/usr/bin/env python3
"""Exercise skills_install.py's materialization and staleness reporting, without touching the
real `claude` CLI state or the real ~/.agents directory.

Run as `python3 scripts/tests/test_skills_install.py`, or under `python3 -m unittest discover -s scripts/tests`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
        self.assertIn(
            str(skills_install.SKILLS_SRC.relative_to(skills_install.ROOT).as_posix()), status_call
        )
        self.assertIn(
            str(skills_install.CLAUDE_PLUGIN_DIR.relative_to(skills_install.ROOT).as_posix()),
            status_call,
        )

    def test_a_handed_in_commit_stands_in_where_git_cannot_answer(self) -> None:
        """A bootstrap runs the installer from a tarball tree with no .git, and hands in the commit
        it resolved before downloading, so the stamp stays checkable rather than permanently stale."""

        def no_git(args, **kwargs):
            result = mock.Mock()
            result.returncode = 128
            result.stdout = ""
            return result

        with mock.patch("subprocess.run", side_effect=no_git):
            with mock.patch.dict("os.environ", {"SKILLS_SOURCE_COMMIT": "cafe1234"}):
                self.assertEqual(
                    skills_install.source_ref(),
                    {"vcs": "archive", "commit": "cafe1234", "dirty": False},
                )
            with mock.patch.dict("os.environ", {}, clear=True):
                self.assertEqual(skills_install.source_ref(), {"vcs": "none"})

    def test_a_git_answer_outranks_a_handed_in_commit(self) -> None:
        """In a real checkout the environment variable is stray state, and the checkout is the truth."""

        def fake_run(args, **kwargs):
            result = mock.Mock()
            result.returncode = 0
            result.stdout = "deadbeef\n" if "rev-parse" in args else ""
            return result

        with (
            mock.patch("subprocess.run", side_effect=fake_run),
            mock.patch.dict("os.environ", {"SKILLS_SOURCE_COMMIT": "cafe1234"}),
        ):
            self.assertEqual(skills_install.source_ref()["commit"], "deadbeef")


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

        self.assertEqual(
            (target / "bar" / "SKILL.md").read_text(encoding="utf-8"), "not ours, no marker"
        )

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
    """--report answers the snapshot copy and the live checkout separately, and exits on the
    snapshot's verdict alone, since the live channel following its checkout is the design."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.stamp = self.tmp / "stamp.json"
        self.addCleanup(mock.patch.stopall)
        self.live = mock.patch(
            "skills_install.live_channel",
            return_value={"registered": True, "branch": "develop", "commit": "dev", "dirty": True},
        ).start()

    def write_stamp(self, source) -> None:
        self.stamp.write_text(
            json.dumps({"stampVersion": skills_install.STAMP_VERSION, "source": source}),
            encoding="utf-8",
        )

    def run_report(self, intended=("abc", "refs/remotes/origin/main"), intended_rev=None):
        mock.patch("skills_install.intended_commit", return_value=intended).start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exit_code = skills_install.report(self.stamp, intended_rev)
        return exit_code, out.getvalue()

    def test_no_stamp_reports_not_installed(self) -> None:
        exit_code, _ = self.run_report()
        self.assertEqual(exit_code, 1)

    def test_a_copy_from_the_intended_revision_is_current_whatever_the_checkout_serves(
        self,
    ) -> None:
        """merge-and-release step 7 installs from main and step 8 returns the
        clone to develop. The copy is exactly right and the live channel serves develop, dirty
        even, and neither of those is the snapshot's fault."""
        self.write_stamp({"commit": "abc", "dirty": False})
        exit_code, out = self.run_report()
        self.assertEqual(exit_code, 0)
        body = json.loads(out)
        self.assertTrue(body["snapshot"]["current"])
        self.assertEqual(body["snapshot"]["installedFrom"], "abc")
        self.assertEqual(body["snapshot"]["intendedRef"], "refs/remotes/origin/main")
        self.assertEqual(body["live"]["branch"], "develop")

    def test_a_copy_from_another_revision_is_not_current(self) -> None:
        self.write_stamp({"commit": "old", "dirty": False})
        exit_code, out = self.run_report()
        self.assertEqual(exit_code, 1)
        self.assertFalse(json.loads(out)["snapshot"]["current"])

    def test_an_explicit_intended_revision_is_passed_through(self) -> None:
        self.write_stamp({"commit": "abc", "dirty": False})
        intended = mock.patch("skills_install.intended_commit", return_value=("abc", "v1")).start()
        with contextlib.redirect_stdout(io.StringIO()):
            skills_install.report(self.stamp, "v1")
        intended.assert_called_once_with("v1")

    def test_an_unresolvable_intended_revision_is_not_current_and_names_what_was_asked(
        self,
    ) -> None:
        self.write_stamp({"commit": "abc", "dirty": False})
        exit_code, out = self.run_report(intended=(None, None), intended_rev="nope")
        self.assertEqual(exit_code, 1)
        snapshot = json.loads(out)["snapshot"]
        self.assertIsNone(snapshot["intended"])
        self.assertEqual(snapshot["intendedRef"], "nope")

    def test_no_intended_revision_at_all_is_not_current_rather_than_a_falsy_none(self) -> None:
        """A tarball tree has no main to resolve. A missing commit on both sides must not
        compare equal, None == None, and pass as current."""
        self.write_stamp({"commit": None})
        exit_code, _ = self.run_report(intended=(None, None))
        self.assertEqual(exit_code, 1)

    def test_a_copy_installed_from_a_dirty_checkout_is_never_current(self) -> None:
        """The stamp records dirty=True from install time, when the copied bytes matched no
        commit. The intended revision being that commit cannot make those bytes verifiable."""
        self.write_stamp({"commit": "abc", "dirty": True})
        exit_code, _ = self.run_report()
        self.assertEqual(exit_code, 1)

    def test_unreadable_stamp_reports_not_current_instead_of_crashing(self) -> None:
        self.stamp.write_text("not valid json {{{", encoding="utf-8")
        exit_code, _ = self.run_report()
        self.assertEqual(exit_code, 1)

    def test_a_valid_json_non_dict_stamp_reports_not_current_instead_of_crashing(self) -> None:
        self.stamp.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        exit_code, _ = self.run_report()
        self.assertEqual(exit_code, 1)

    def test_a_dict_stamp_with_a_non_dict_source_reports_not_current_instead_of_crashing(
        self,
    ) -> None:
        self.write_stamp("oops")
        exit_code, _ = self.run_report()
        self.assertEqual(exit_code, 1)

    def test_an_unrecognized_stamp_version_reports_not_current(self) -> None:
        """A future format bump must not have an old-shaped stamp read as current."""
        self.stamp.write_text(
            json.dumps(
                {"stampVersion": skills_install.STAMP_VERSION + 1, "source": {"commit": "abc"}}
            ),
            encoding="utf-8",
        )
        exit_code, _ = self.run_report()
        self.assertEqual(exit_code, 1)


class IntendedCommitCase(unittest.TestCase):
    """The default intended revision is the promoted main, remote-tracking ref first."""

    def test_origin_main_is_preferred_over_a_local_main(self) -> None:
        answers = {
            "refs/remotes/origin/main^{commit}": "remote",
            "refs/heads/main^{commit}": "local",
        }
        with mock.patch("skills_install.git_in", side_effect=lambda _root, *a: answers.get(a[-1])):
            self.assertEqual(
                skills_install.intended_commit(), ("remote", "refs/remotes/origin/main")
            )

    def test_a_local_main_is_the_fallback(self) -> None:
        answers = {"refs/heads/main^{commit}": "local"}
        with mock.patch("skills_install.git_in", side_effect=lambda _root, *a: answers.get(a[-1])):
            self.assertEqual(skills_install.intended_commit(), ("local", "refs/heads/main"))

    def test_a_named_revision_replaces_the_defaults_and_is_not_read_as_an_option(self) -> None:
        calls = []

        def fake(_root, *args):
            calls.append(args)

        with mock.patch("skills_install.git_in", side_effect=fake):
            self.assertEqual(skills_install.intended_commit("--help"), (None, None))
        self.assertEqual(len(calls), 1)
        self.assertLess(calls[0].index("--end-of-options"), calls[0].index("--help^{commit}"))


class LiveChannelCase(unittest.TestCase):
    """The live channel is read from the checkout the marketplace names, which is not
    necessarily the checkout running the report."""

    def setUp(self) -> None:
        self.addCleanup(mock.patch.stopall)
        mock.patch("skills_install.claude_available", return_value=True).start()

    def listing(self, stdout: str, returncode: int = 0) -> None:
        result = mock.Mock(returncode=returncode, stdout=stdout)
        mock.patch("subprocess.run", return_value=result).start()

    def test_claude_absent_says_so(self) -> None:
        mock.patch("skills_install.claude_available", return_value=False).start()
        self.assertIsNone(skills_install.live_channel()["registered"])

    def test_an_unregistered_marketplace_reads_as_not_registered(self) -> None:
        self.listing(json.dumps([{"name": "someone-else", "installLocation": "/x"}]))
        self.assertEqual(skills_install.live_channel(), {"registered": False})

    def test_a_failed_listing_is_not_read_as_unregistered(self) -> None:
        self.listing("", returncode=1)
        self.assertIsNone(skills_install.live_channel()["registered"])

    def test_the_registered_checkout_is_the_one_measured(self) -> None:
        self.listing(
            json.dumps(
                [{"name": skills_install.MARKETPLACE_NAME, "installLocation": "/hub/checkout"}]
            )
        )
        answers = {"symbolic-ref": "develop", "rev-parse": "dev", "status": ""}
        roots = []

        def fake(root, *args):
            roots.append(root)
            return answers[args[0]]

        with mock.patch("skills_install.git_in", side_effect=fake):
            live = skills_install.live_channel()
        self.assertEqual(
            live,
            {
                "registered": True,
                "checkout": str(Path("/hub/checkout")),
                "branch": "develop",
                "commit": "dev",
                "dirty": False,
            },
        )
        self.assertEqual(set(roots), {Path("/hub/checkout")})

    def test_a_failed_status_reads_as_unknown_rather_than_clean(self) -> None:
        self.listing(
            json.dumps(
                [{"name": skills_install.MARKETPLACE_NAME, "installLocation": "/hub/checkout"}]
            )
        )
        answers = {"symbolic-ref": None, "rev-parse": "sha", "status": None}
        with mock.patch("skills_install.git_in", side_effect=lambda _r, *a: answers[a[0]]):
            live = skills_install.live_channel()
        self.assertIsNone(live["dirty"])
        self.assertIsNone(live["branch"])


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

    def test_skills_and_marketplace_outcomes_print_on_separate_lines(self) -> None:
        mock.patch("skills_install.claude_available", return_value=True).start()
        mock.patch("skills_install.register_claude_marketplace", return_value=True).start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            skills_install.main()
        lines = out.getvalue().splitlines()
        self.assertIn(f"Skills materialized to {self.tmp / 'skills'}.", lines)
        self.assertIn("Claude Code marketplace registered: True.", lines)


LINUX_WRAPPER = (
    Path(__file__).resolve().parent.parent.parent / "host-setup" / "linux" / "install-skills.sh"
)


def unprivileged_root_is_available() -> bool:
    """Whether `unshare -r` gives this process an EUID of 0 without any real privilege.

    The guard under test reads `$EUID`, which bash makes read-only, so the sudo'd run cannot be
    faked by setting a variable and has to be a real EUID 0. A user namespace is the one way to
    reach that without root, and it is unavailable on a host that restricts unprivileged user
    namespaces, which is why this is probed rather than assumed.
    """
    if sys.platform != "linux" or not shutil.which("unshare"):
        return False
    try:
        return (
            subprocess.run(
                ["unshare", "-r", "id", "-u"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                check=False,
            ).stdout.strip()
            == "0"
        )
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(
    unprivileged_root_is_available(), "needs an unprivileged user namespace to reach EUID 0"
)
class LinuxWrapperSudoGuardCase(unittest.TestCase):
    """`install-skills.sh` refuses a sudo'd run rather than installing for root and exiting 0.

    Every case runs `--dry-run`, so the installer itself is never reached and nothing is written
    under any home. What is under test is which runs get past the guard at all.
    """

    def run_wrapper(self, root: bool, sudo_user: str | None) -> subprocess.CompletedProcess[str]:
        argv = ["unshare", "-r"] if root else []
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        if sudo_user is not None:
            env["SUDO_USER"] = sudo_user
        return subprocess.run(
            [*argv, str(LINUX_WRAPPER), "--dry-run"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
            env=env,
        )

    def test_a_sudo_run_is_refused_and_names_the_invoking_user(self) -> None:
        """The defect this guards: the run installed to /root/.agents/skills and exited 0."""
        r = self.run_wrapper(root=True, sudo_user="someone")
        self.assertEqual(r.returncode, 1)
        self.assertIn("someone", r.stderr)
        self.assertNotIn("[dry run]", r.stdout)

    def test_a_root_account_with_no_sudo_user_still_installs(self) -> None:
        """A container or a Proxmox node whose working account is root installs for root, which
        is correct there, so the guard reads SUDO_USER rather than EUID alone."""
        r = self.run_wrapper(root=True, sudo_user=None)
        self.assertEqual(r.returncode, 0)
        self.assertIn("[dry run]", r.stdout)

    def test_root_running_its_own_sudo_is_not_a_user_install_gone_wrong(self) -> None:
        """SUDO_USER=root names no user whose home was missed, so there is nothing to refuse."""
        r = self.run_wrapper(root=True, sudo_user="root")
        self.assertEqual(r.returncode, 0)
        self.assertIn("[dry run]", r.stdout)

    def test_an_ordinary_user_with_sudo_user_set_is_not_refused(self) -> None:
        """SUDO_USER survives into a shell started under sudo -u, so reading it alone would refuse
        a run that is already targeting the right home."""
        r = self.run_wrapper(root=False, sudo_user="someone")
        self.assertEqual(r.returncode, 0)
        self.assertIn("[dry run]", r.stdout)

    def test_help_still_answers_under_sudo(self) -> None:
        """A refused run has to be able to say what to run instead, and --help is where that is."""
        r = subprocess.run(
            ["unshare", "-r", str(LINUX_WRAPPER), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
            env={"PATH": os.environ.get("PATH", ""), "SUDO_USER": "someone"},
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn("never under sudo", r.stdout)


if __name__ == "__main__":
    unittest.main()
