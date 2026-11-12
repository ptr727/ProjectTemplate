#!/usr/bin/env python3
"""Tests for host-setup/agent-safety/global_rules.py, the writer of Codex's and opencode's global rules.

Every case points CODEX_HOME, XDG_CONFIG_HOME, and the host-local file at a throwaway directory.
It also withholds the real CLIs from PATH.
No case therefore reads or writes the invoking user's own files.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENT_SAFETY = ROOT / "host-setup" / "agent-safety"
SCRIPT = AGENT_SAFETY / "global_rules.py"
CLAUDE_INSTALL = AGENT_SAFETY / "claude" / "install.py"

sys.path.insert(0, str(AGENT_SAFETY))
import global_rules


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.codex = self.root / "codex"
        self.xdg = self.root / "xdg"
        self.opencode = self.xdg / "opencode"
        self.local = self.root / "local.md"
        self.empty_bin = self.root / "bin"
        self.empty_bin.mkdir()
        self.env = {
            **os.environ,
            "CODEX_HOME": str(self.codex),
            "XDG_CONFIG_HOME": str(self.xdg),
            "AGENT_FLEET_LOCAL_INSTRUCTIONS": str(self.local),
            # No codex or opencode CLI is on this PATH, so presence is decided by the directories alone.
            "PATH": str(self.empty_bin),
            "HOME": str(self.root / "home"),
            # Path.home() reads USERPROFILE on Windows rather than HOME.
            "USERPROFILE": str(self.root / "home"),
        }

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=self.env,
            timeout=60,
        )

    def install_both(self) -> None:
        self.codex.mkdir()
        self.opencode.mkdir(parents=True)


class TestPresence(Case):
    def test_a_missing_agent_is_skipped_and_nothing_is_created(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("not installed, skipped"), 2)
        self.assertFalse(self.codex.exists())
        self.assertFalse(self.xdg.exists())

    def test_a_report_with_no_agent_present_exits_zero(self) -> None:
        result = self.run_script("--report")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_config_directory_makes_an_agent_present(self) -> None:
        self.codex.mkdir()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.codex / "AGENTS.md").is_file())
        self.assertFalse(self.opencode.exists())

    def test_a_cli_on_path_makes_an_agent_present_and_creates_its_directory(self) -> None:
        # Windows finds a command on PATH only through a PATHEXT extension.
        cli = self.empty_bin / ("opencode.cmd" if os.name == "nt" else "opencode")
        cli.write_text("#!/bin/sh\n", encoding="utf-8")
        cli.chmod(0o755)
        result = self.run_script("--agent", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.opencode / "AGENTS.md").is_file())

    def test_a_relative_xdg_config_home_is_treated_as_unset(self) -> None:
        self.env["XDG_CONFIG_HOME"] = "relative/config"
        home_config = Path(self.env["HOME"]) / ".config" / "opencode"
        home_config.mkdir(parents=True)
        result = self.run_script("--agent", "opencode")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((home_config / "AGENTS.md").is_file())
        self.assertFalse((Path.cwd() / "relative").exists())

    def test_codex_home_defaults_to_the_users_codex_directory(self) -> None:
        del self.env["CODEX_HOME"]
        (Path(self.env["HOME"]) / ".codex").mkdir(parents=True)
        result = self.run_script("--agent", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((Path(self.env["HOME"]) / ".codex" / "AGENTS.md").is_file())


class TestContent(Case):
    def test_the_file_is_the_kits_rendering_with_only_the_header_changed(self) -> None:
        self.install_both()
        self.local.write_text("A constructed host note.\n", encoding="utf-8")
        self.assertEqual(self.run_script().returncode, 0)
        kit = global_rules.load_kit()
        with mock.patch.dict(os.environ, {"AGENT_FLEET_LOCAL_INSTRUCTIONS": str(self.local)}):
            kit_render = kit.render_instructions(self.local, "A constructed host note.")
        written = (self.codex / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(
            written.split("\n\n", 2)[2],
            kit_render.split("\n\n", 1)[1],
            "the body must match the kit",
        )
        self.assertIn("global_rules.py", written.split("\n", 1)[0])
        self.assertNotIn("global_rules.py", kit_render)
        self.assertEqual(written, (self.opencode / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("A constructed host note.", written)

    def test_a_local_file_carrying_a_kit_marker_stops_the_run(self) -> None:
        self.install_both()
        self.local.write_text("<!-- agent-safety v1 start -->\n", encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Nothing was written", result.stderr)
        self.assertFalse((self.codex / "AGENTS.md").exists())


class TestNote(Case):
    def test_both_files_carry_the_no_hook_note_directly_under_the_header(self) -> None:
        self.install_both()
        self.assertEqual(self.run_script().returncode, 0)
        for path in (self.codex / "AGENTS.md", self.opencode / "AGENTS.md"):
            with self.subTest(path=path.parent.name):
                lines = path.read_text(encoding="utf-8").split("\n")
                self.assertTrue(lines[0].startswith("<!-- Written by ProjectTemplate"))
                self.assertEqual(lines[1], "")
                self.assertEqual(lines[2], global_rules.NOTE)
                self.assertIn("no hook guards its sessions yet", lines[2])
                self.assertIn("describe Claude Code only", lines[2])
                self.assertNotIn("#781", lines[2])

    def test_the_block_text_is_not_edited(self) -> None:
        self.install_both()
        self.run_script()
        kit = global_rules.load_kit()
        with mock.patch.dict(os.environ, {"AGENT_FLEET_LOCAL_INSTRUCTIONS": str(self.local)}):
            claude = kit.render_instructions(self.local, "")
        written = (self.codex / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(written.split("\n\n", 2)[2], claude.split("\n\n", 1)[1])

    def test_the_claude_render_is_byte_unchanged(self) -> None:
        kit = global_rules.load_kit()
        with mock.patch.dict(os.environ, {"AGENT_FLEET_LOCAL_INSTRUCTIONS": str(self.local)}):
            claude = kit.render_instructions(self.local, "")
        self.assertTrue(
            claude.startswith("<!-- Written by ProjectTemplate host-setup/agent-safety, which")
        )
        self.assertNotIn(global_rules.NOTE, claude)
        self.assertNotIn("global_rules.py", claude)


class TestOldPython(Case):
    """An interpreter below 3.11 is simulated by removing datetime.UTC before the script runs."""

    def run_old(self, *args: str) -> subprocess.CompletedProcess[str]:
        driver = (
            "import datetime, sys, runpy\n"
            "del datetime.UTC\n"
            f"sys.argv = [{str(SCRIPT)!r}, *{list(args)!r}]\n"
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
        )
        return subprocess.run(
            [sys.executable, "-c", driver],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=self.env,
            timeout=60,
        )

    def test_with_no_agent_present_it_skips_and_exits_zero(self) -> None:
        for flag in ((), ("--report",)):
            with self.subTest(flag=flag):
                result = self.run_old(*flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("not installed, skipped"), 2)

    def test_with_an_agent_present_it_exits_non_zero_naming_the_floor(self) -> None:
        self.codex.mkdir()
        result = self.run_old()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3.11+", result.stderr)
        self.assertFalse((self.codex / "AGENTS.md").exists())


@unittest.skipIf(
    os.name == "nt",
    "the stub interpreters are extension-less shell scripts, which Windows cannot start",
)
class TestWrapperInterpreterChoice(Case):
    """The wrappers prefer a Python 3.11 or newer and fall back to any Python 3 only when none is."""

    PROBE_STUB = """#!/bin/sh
case "$*" in
*">= 11"*) exit {modern} ;;
*"[0] == 3"*) exit 0 ;;
esac
echo "ran-{name}"
"""

    def stub(self, name: str, modern: bool) -> None:
        path = self.empty_bin / name
        path.write_text(
            self.PROBE_STUB.format(name=name, modern=0 if modern else 1), encoding="utf-8"
        )
        path.chmod(0o755)

    def run_wrapper(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env["PATH"] = f"{self.empty_bin}{os.pathsep}/usr/bin{os.pathsep}/bin"
        env.pop("SUDO_USER", None)
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=env,
            timeout=60,
        )

    def wrappers(self) -> list[tuple[str, list[str]]]:
        found = [("bash", ["bash", str(ROOT / "host-setup" / "linux" / "install-global-rules.sh")])]
        pwsh = shutil.which("pwsh")
        if pwsh:
            script = ROOT / "host-setup" / "windows" / "install-global-rules.ps1"
            found.append(("pwsh", [pwsh, "-NoProfile", "-File", str(script)]))
        return found

    def test_a_modern_later_candidate_is_preferred_over_an_older_earlier_one(self) -> None:
        self.stub("python3", modern=False)
        self.stub("python", modern=True)
        for label, command in self.wrappers():
            with self.subTest(wrapper=label):
                result = self.run_wrapper(command)
                self.assertIn("ran-python\n", result.stdout + "\n", result.stderr)
                self.assertNotIn("ran-python3", result.stdout)

    def test_with_no_modern_candidate_the_first_python_3_is_used(self) -> None:
        self.stub("python3", modern=False)
        self.stub("python", modern=False)
        for label, command in self.wrappers():
            with self.subTest(wrapper=label):
                result = self.run_wrapper(command)
                self.assertIn("ran-python3", result.stdout, result.stderr)


class TestKitWrapperReport(Case):
    @unittest.skipUnless(sys.platform != "win32", "runs the Linux wrapper")
    def test_a_report_under_a_dry_run_runs_the_installer_for_real(self) -> None:
        wrapper = ROOT / "host-setup" / "linux" / "install-safety-kit.sh"
        claude_home = self.root / "claude"
        claude_home.mkdir()
        env = {**os.environ, "CLAUDE_HOME": str(claude_home), "HOME": self.env["HOME"]}
        env.pop("SUDO_USER", None)
        report = subprocess.run(
            ["bash", str(wrapper), "--report", "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", check=False, env=env, timeout=60,
        )  # fmt: skip
        self.assertNotIn("[dry run]", report.stdout)
        self.assertIn("NOT INSTALLED", report.stdout)
        install = subprocess.run(
            ["bash", str(wrapper), "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", check=False, env=env, timeout=60,
        )  # fmt: skip
        self.assertIn("[dry run]", install.stdout)
        self.assertEqual(list(claude_home.iterdir()), [])


class TestWriting(Case):
    def setUp(self) -> None:
        super().setUp()
        self.install_both()

    def test_a_second_run_changes_nothing(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        before = (self.codex / "AGENTS.md").read_bytes()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("already current"), 2)
        self.assertEqual((self.codex / "AGENTS.md").read_bytes(), before)
        self.assertEqual(list(self.codex.glob("*.bak")), [])

    def test_a_prior_file_this_script_did_not_write_is_backed_up(self) -> None:
        target = self.codex / "AGENTS.md"
        target.write_text("A constructed hand-written note.\n", encoding="utf-8")
        result = self.run_script("--agent", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list(self.codex.glob("AGENTS.md.*.bak"))
        self.assertEqual(len(backups), 1, result.stdout)
        self.assertEqual(
            backups[0].read_text(encoding="utf-8"), "A constructed hand-written note.\n"
        )
        self.assertIn("global_rules.py", target.read_text(encoding="utf-8"))
        self.assertIn("backed up", result.stdout)

    def test_an_unedited_file_whose_rules_changed_is_rewritten_without_a_backup(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        self.local.write_text("A constructed new note.\n", encoding="utf-8")
        result = self.run_script("--agent", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.codex.glob("*.bak")), [])
        self.assertIn(
            "A constructed new note.", (self.codex / "AGENTS.md").read_text(encoding="utf-8")
        )

    def test_a_hand_edit_below_the_header_is_backed_up_and_rewritten(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        target = self.codex / "AGENTS.md"
        edited = target.read_text(encoding="utf-8") + "\nA constructed hand edit.\n"
        target.write_text(edited, encoding="utf-8")
        result = self.run_script("--agent", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list(self.codex.glob("AGENTS.md.*.bak"))
        self.assertEqual(len(backups), 1, result.stdout)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), edited)
        self.assertNotIn("hand edit", target.read_text(encoding="utf-8"))

    def test_a_file_from_a_release_with_no_digest_is_an_edit_only_if_its_body_differs(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        target = self.codex / "AGENTS.md"
        text = target.read_text(encoding="utf-8")
        first, rest = text.split("\n", 1)
        stripped = first.replace(" Edits below the note are backed up first.", "")
        stripped = stripped[: stripped.index(" body=")] + " -->"
        target.write_text(stripped + "\n" + rest, encoding="utf-8")
        same_body = self.run_script("--agent", "codex")
        self.assertEqual(same_body.returncode, 0, same_body.stderr)
        self.assertEqual(list(self.codex.glob("*.bak")), [])
        target.write_text(stripped + "\n" + rest + "\nA constructed edit.\n", encoding="utf-8")
        self.assertEqual(self.run_script("--agent", "codex").returncode, 0)
        self.assertEqual(len(list(self.codex.glob("AGENTS.md.*.bak"))), 1)

    def test_a_dry_run_says_an_edited_file_would_be_backed_up_and_changes_nothing(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        target = self.codex / "AGENTS.md"
        target.write_text(target.read_text(encoding="utf-8") + "edit\n", encoding="utf-8")
        before = target.read_bytes()
        result = self.run_script("--dry-run", "--agent", "codex")
        self.assertIn("the prior file backed up first", result.stdout)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.codex.glob("*.bak")), [])

    def test_a_crlf_file_keeps_its_line_endings(self) -> None:
        target = self.codex / "AGENTS.md"
        target.write_bytes(
            b"<!-- Written by ProjectTemplate host-setup/agent-safety/global_rules.py\r\nold\r\n"
        )
        self.assertEqual(self.run_script("--agent", "codex").returncode, 0)
        raw = target.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_a_directory_at_the_target_is_refused_and_named(self) -> None:
        (self.codex / "AGENTS.md").mkdir()
        result = self.run_script("--agent", "codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a regular file", result.stderr)

    def test_one_agent_failing_does_not_stop_the_other(self) -> None:
        (self.codex / "AGENTS.md").mkdir()
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.opencode / "AGENTS.md").is_file())

    def test_a_symlinked_target_stays_a_link_and_its_destination_takes_the_content(self) -> None:
        destination = self.root / "dotfiles-agents.md"
        destination.write_text("", encoding="utf-8")
        try:
            (self.codex / "AGENTS.md").symlink_to(destination)
        except OSError as error:
            self.skipTest(f"this host refuses to create a symlink for this user ({error})")
        result = self.run_script("--agent", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.codex / "AGENTS.md").is_symlink())
        self.assertIn("global_rules.py", destination.read_text(encoding="utf-8"))


class TestDigest(Case):
    def test_the_digest_in_the_header_round_trips_to_the_body(self) -> None:
        self.install_both()
        self.assertEqual(self.run_script().returncode, 0)
        kit = global_rules.load_kit()
        text = (self.codex / "AGENTS.md").read_text(encoding="utf-8")
        held = global_rules.DIGEST_FIELD.search(text.split("\n", 1)[0])
        self.assertIsNotNone(held)
        body = global_rules.body_of(text)
        assert held is not None and body is not None
        self.assertEqual(held.group(1), kit.text_digest(body))

    def test_the_claude_render_is_unchanged_by_the_digest(self) -> None:
        kit = global_rules.load_kit()
        with mock.patch.dict(os.environ, {"AGENT_FLEET_LOCAL_INSTRUCTIONS": str(self.local)}):
            claude = kit.render_instructions(self.local, "")
        self.assertNotIn("body=", claude)


class TestShadowedByOverride(Case):
    def setUp(self) -> None:
        super().setUp()
        self.install_both()
        self.override = self.codex / "AGENTS.override.md"

    def test_the_report_calls_a_current_file_shadowed_and_not_current(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        self.override.write_text("A constructed override.\n", encoding="utf-8")
        result = self.run_script("--report", "--agent", "codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("CURRENT", result.stdout)
        self.assertIn(
            "SHADOWED: AGENTS.override.md takes precedence, so Codex does not read these rules",
            result.stdout,
        )

    def test_the_setup_writes_agents_md_warns_and_leaves_the_override_alone(self) -> None:
        self.override.write_text("A constructed override.\n", encoding="utf-8")
        result = self.run_script("--agent", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.codex / "AGENTS.md").is_file())
        self.assertIn("SHADOWED: AGENTS.override.md takes precedence", result.stdout)
        self.assertEqual(self.override.read_text(encoding="utf-8"), "A constructed override.\n")

    def test_opencode_is_never_shadowed(self) -> None:
        (self.opencode / "AGENTS.override.md").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.run_script("--agent", "opencode").returncode, 0)
        result = self.run_script("--report", "--agent", "opencode")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("SHADOWED", result.stdout)

    def test_no_override_means_no_warning(self) -> None:
        result = self.run_script("--agent", "codex")
        self.assertNotIn("SHADOWED", result.stdout)


class TestDryRun(Case):
    def setUp(self) -> None:
        super().setUp()
        self.install_both()

    def test_a_dry_run_writes_and_backs_up_nothing(self) -> None:
        (self.codex / "AGENTS.md").write_text("A constructed note.\n", encoding="utf-8")
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would be updated, the prior file backed up first", result.stdout)
        self.assertIn("would be written", result.stdout)
        self.assertEqual(
            (self.codex / "AGENTS.md").read_text(encoding="utf-8"), "A constructed note.\n"
        )
        self.assertFalse((self.opencode / "AGENTS.md").exists())
        self.assertEqual(list(self.codex.glob("*.bak")), [])

    def test_a_dry_run_with_report_is_refused(self) -> None:
        result = self.run_script("--dry-run", "--report")
        self.assertNotEqual(result.returncode, 0)


class TestReport(Case):
    def setUp(self) -> None:
        super().setUp()
        self.install_both()

    def test_an_absent_file_reports_absent_and_exits_non_zero(self) -> None:
        result = self.run_script("--report")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.count("ABSENT"), 2)
        self.assertFalse((self.codex / "AGENTS.md").exists())

    def test_a_current_file_reports_current_and_exits_zero(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        result = self.run_script("--report")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count("CURRENT"), 2)

    def test_a_changed_host_local_file_reports_stale(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        self.local.write_text("A constructed new note.\n", encoding="utf-8")
        result = self.run_script("--report")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.count("STALE"), 2)

    def test_a_hand_written_file_reports_stale_and_says_a_write_backs_it_up(self) -> None:
        (self.codex / "AGENTS.md").write_text("A constructed note.\n", encoding="utf-8")
        result = self.run_script("--report", "--agent", "codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("STALE", result.stdout)
        self.assertIn("backs it up first", result.stdout)

    def test_a_hand_edit_reports_edited_not_the_false_rules_changed_reason(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        target = self.codex / "AGENTS.md"
        target.write_text(target.read_text(encoding="utf-8") + "edit\n", encoding="utf-8")
        result = self.run_script("--report", "--agent", "codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("EDITED", result.stdout)
        self.assertIn("changed since this script wrote it, the next run backs it up", result.stdout)
        self.assertNotIn("the rules or the host-local file have changed", result.stdout)

    def test_one_stale_agent_fails_the_report_even_when_the_other_is_current(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        (self.opencode / "AGENTS.md").write_text("edited\n", encoding="utf-8")
        result = self.run_script("--report")
        self.assertEqual(result.returncode, 1)
        self.assertIn("CURRENT", result.stdout)

    def test_a_report_changes_nothing(self) -> None:
        self.run_script("--report")
        self.assertFalse((self.codex / "AGENTS.md").exists())


class TestArguments(Case):
    def test_help_prints_usage_and_exits_zero(self) -> None:
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith("usage:"))

    def test_an_unknown_flag_is_refused_by_name(self) -> None:
        result = self.run_script("--bogus-flag")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments: --bogus-flag", result.stderr)

    def test_an_unknown_agent_is_refused(self) -> None:
        result = self.run_script("--agent", "claude")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)


@unittest.skipIf(sys.platform == "win32", "Windows cannot deliver SIGINT or SIGTERM to a script")
class TestInterrupt(Case):
    """The script holds no lock, temporary path, or child, so the check is the exit status and the one line."""

    def interrupted(self, signum: int) -> subprocess.CompletedProcess[str]:
        """Starts the real entry point with the run replaced by a sleep, then signals it mid-run."""
        self.install_both()
        driver = (
            "import importlib.util, sys, time\n"
            f"sys.argv = [{str(SCRIPT)!r}]\n"
            f"spec = importlib.util.spec_from_file_location('gr', {str(SCRIPT)!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "sys.modules['gr'] = module\n"
            "spec.loader.exec_module(module)\n"
            "module.run = lambda args: time.sleep(30)\n"
            "print('ready', flush=True)\n"
            "sys.exit(module.main())\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", driver],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert process.stdout is not None
        self.assertEqual(process.stdout.readline().strip(), "ready")
        time.sleep(0.2)
        process.send_signal(signum)
        try:
            out, err = process.communicate(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        return subprocess.CompletedProcess(process.args, process.returncode, out, err)

    def test_sigint_exits_130_with_one_line_and_no_traceback(self) -> None:
        result = self.interrupted(signal.SIGINT)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("SIGINT", result.stderr)

    def test_sigterm_exits_143_with_one_line_and_no_traceback(self) -> None:
        result = self.interrupted(signal.SIGTERM)
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("SIGTERM", result.stderr)

    def test_no_stray_file_is_left_behind(self) -> None:
        self.interrupted(signal.SIGTERM)
        self.assertFalse((self.codex / "AGENTS.md").exists())


class TestKitIsReusedNotCopied(unittest.TestCase):
    def test_the_script_defines_none_of_the_kits_render_or_write_helpers(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for name in (
            "render_instructions",
            "local_instructions_path",
            "read_local_instructions",
            "write_regular_file",
            "write_backup",
        ):
            with self.subTest(name=name):
                self.assertNotIn(f"def {name}", source)
                self.assertIn(f"kit.{name}", source)

    def test_the_kit_exists_where_the_script_loads_it(self) -> None:
        self.assertTrue(CLAUDE_INSTALL.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=1)
