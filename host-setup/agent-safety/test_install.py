#!/usr/bin/env python3
"""Self-test for install.py, proving each stamp verdict by reintroducing the state it reports.

Every case runs against a throwaway CLAUDE_HOME, never the invoking user's. The installer writes to
a real home by default, so a test that forgot the override would rewrite the developer's own kit.

Standard library only, matching the rest of the gates, so CI needs no install step.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
INSTALL = HERE / "install.py"

sys.path.insert(0, str(HERE))
import install  # noqa: E402


def run(home, *args):
    """Invoke the installer as a subprocess, the way a host actually runs it."""
    env = dict(os.environ, CLAUDE_HOME=str(home))
    return subprocess.run([sys.executable, str(INSTALL), *args],
                          capture_output=True, text=True, env=env)


class StampCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = pathlib.Path(self.tmp) / "claude"
        self.stamp = self.home / "agent-safety-stamp.json"
        self.md = self.home / "CLAUDE.md"

    def install(self):
        r = run(self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r


class TestReportVerdicts(StampCase):
    def test_report_on_a_machine_that_never_installed_says_so_and_installs_nothing(self):
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 2)
        self.assertIn("NOT INSTALLED", r.stdout)
        # The report path returns before the directory is created, so a read-only check stays read-only.
        self.assertFalse(self.home.exists())

    def test_install_then_report_is_current(self):
        self.install()
        self.assertTrue(self.stamp.exists())
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CURRENT", r.stdout)

    def test_a_changed_payload_reports_stale(self):
        self.install()
        target = HERE / "claude-md-safety.md"
        original = target.read_bytes()
        self.addCleanup(target.write_bytes, original)
        target.write_bytes(original + b"\n<!-- drift -->\n")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("payload digest differs", r.stdout)

    def test_a_block_deleted_by_hand_reports_stale(self):
        self.install()
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(
            re.sub(r"<!-- fleet-bootstrap v\d+ start -->.*?<!-- fleet-bootstrap v\d+ end -->",
                   "", text, flags=re.DOTALL), encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("CLAUDE.md now holds", r.stdout)

    def test_reinstalling_clears_a_stale_verdict(self):
        """The remedy the report prints has to actually work, or the verdict is a dead end."""
        self.install()
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(re.sub(r"<!-- fleet-bootstrap v\d+ start -->.*?<!-- fleet-bootstrap v\d+ end -->",
                                  "", text, flags=re.DOTALL), encoding="utf-8")
        self.assertEqual(run(self.home, "--report").returncode, 1)
        self.install()
        self.assertEqual(run(self.home, "--report").returncode, 0)


class TestArgumentHandling(StampCase):
    def test_an_unknown_flag_is_rejected_rather_than_ignored(self):
        """The defect this closes: main() took no arguments, so the wrappers' pass-through was
        discarded and `install.py --help` performed a full install instead of printing usage."""
        self.install()
        before = self.stamp.read_text(encoding="utf-8")
        r = run(self.home, "--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unrecognized arguments", r.stderr)
        self.assertEqual(self.stamp.read_text(encoding="utf-8"), before)

    def test_help_prints_usage_and_installs_nothing(self):
        r = run(self.home, "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--report", r.stdout)
        self.assertFalse(self.home.exists())


class TestBlocksPresent(StampCase):
    def test_a_half_written_block_does_not_count_as_present(self):
        """A start marker with no end is the failure a presence check reads as success."""
        self.install()
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(re.sub(r"<!-- agent-safety v\d+ end -->", "", text), encoding="utf-8")
        found = install.blocks_present(self.md)
        self.assertNotIn("agent-safety", found)
        self.assertIn("fleet-bootstrap", found)

    def test_an_absent_file_yields_no_blocks_rather_than_raising(self):
        self.assertEqual(install.blocks_present(self.home / "nothing.md"), {})


class TestInstalledContent(StampCase):
    """Presence is not currency. These are the cases markers and versions cannot see."""

    def test_a_block_edited_between_its_own_markers_reports_stale(self):
        """The marker and version are untouched, so a presence check calls this machine current."""
        self.install()
        text = self.md.read_text(encoding="utf-8")
        edited = text.replace("<!-- agent-safety v1 start -->",
                              "<!-- agent-safety v1 start -->\nSomeone weakened this rule by hand.")
        self.assertNotEqual(edited, text)
        self.md.write_text(edited, encoding="utf-8")
        # Presence is unchanged: the markers and versions still read exactly as before.
        self.assertEqual(install.blocks_present(self.md), {"agent-safety": "v1", "fleet-bootstrap": "v1"})
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("installed content differs", r.stdout)

    def test_a_modified_hook_reports_stale(self):
        """The hook is not marker-delimited, so nothing else on this machine would notice."""
        self.install()
        hook = self.home / "hooks" / "gh-write-guard.py"
        hook.write_text(hook.read_text(encoding="utf-8") + "\n# neutered\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("installed content differs", r.stdout)

    def test_a_deleted_hook_reports_stale_rather_than_crashing(self):
        self.install()
        (self.home / "hooks" / "gh-write-guard.py").unlink()
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not fully installed", r.stdout)

    def test_identical_content_with_crlf_is_current_rather_than_stale(self):
        """CLAUDE.md keeps the endings it had, and a Windows host is not drifted for that alone."""
        self.install()
        raw = self.md.read_bytes()
        self.md.write_bytes(raw.replace(b"\n", b"\r\n"))
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CURRENT", r.stdout)

    def test_reinstalling_clears_an_edited_block(self):
        self.install()
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(text.replace("<!-- agent-safety v1 start -->",
                                        "<!-- agent-safety v1 start -->\nedited"), encoding="utf-8")
        self.assertEqual(run(self.home, "--report").returncode, 1)
        self.install()
        self.assertEqual(run(self.home, "--report").returncode, 0)


class TestDuplicateBlocks(StampCase):
    def test_a_duplicated_block_is_not_reported_as_present(self):
        """Two blocks mean the second silently governs, and naming the first hides that."""
        self.install()
        text = self.md.read_text(encoding="utf-8")
        block = re.search(r"<!-- agent-safety v1 start -->.*?<!-- agent-safety v1 end -->",
                          text, re.DOTALL).group(0)
        self.md.write_text(text + "\n" + block + "\n", encoding="utf-8")
        self.assertNotIn("agent-safety", install.blocks_present(self.md))

    def test_a_duplicated_block_reports_stale_rather_than_current(self):
        self.install()
        text = self.md.read_text(encoding="utf-8")
        block = re.search(r"<!-- agent-safety v1 start -->.*?<!-- agent-safety v1 end -->",
                          text, re.DOTALL).group(0)
        self.md.write_text(text + "\n" + block + "\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


class TestDegradedEnvironments(StampCase):
    def test_a_host_without_git_stamps_rather_than_crashing(self):
        """A tarball install on a minimal host has no git, which is normal rather than an error."""
        env = dict(os.environ, CLAUDE_HOME=str(self.home), PATH="")
        r = subprocess.run([sys.executable, str(INSTALL)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        self.assertEqual(stamp["source"]["vcs"], "none")

    def test_a_stamp_missing_required_keys_gives_a_verdict_rather_than_a_traceback(self):
        self.install()
        self.stamp.write_text(json.dumps({"stampVersion": 1}) + "\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 2)
        self.assertIn("missing", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_stamp_holding_a_non_object_gives_a_verdict_rather_than_a_traceback(self):
        self.install()
        self.stamp.write_text("[]\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_every_required_key_holding_the_wrong_type_gives_a_verdict(self):
        """Presence is not shape. Each of these carries every key and crashes a key-only check."""
        self.install()
        good = json.loads(self.stamp.read_text(encoding="utf-8"))
        for key, bad in (("host", "server"), ("source", "git"), ("payloadDigest", 12),
                         ("blocks", ["agent-safety"]), ("installedUtc", None)):
            with self.subTest(key=key):
                broken = dict(good, **{key: bad})
                self.stamp.write_text(json.dumps(broken) + "\n", encoding="utf-8")
                r = run(self.home, "--report")
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(key, r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_the_formatter_stays_printable_on_a_stamp_the_validator_would_reject(self):
        """Belt and braces: a formatter that raises turns a verdict into the crash it reports on."""
        for broken in ({}, {"host": None, "source": None},
                       {"host": {}, "source": {}, "blocks": None},
                       {"host": {"hostname": "h"}, "source": {"commit": 12345}}):
            with self.subTest(stamp=broken):
                self.assertIsInstance(install.stamp_line(broken), str)


class TestRegistration(StampCase):
    """Correct bytes on disk are not a running guard. These are the inert-kit cases."""

    def _settings(self):
        return json.loads((self.home / "settings.json").read_text(encoding="utf-8"))

    def _write(self, data):
        (self.home / "settings.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def test_an_unregistered_hook_reports_stale_rather_than_current(self):
        """Every byte is correct and the guard never runs, which every other check calls fine."""
        self.install()
        data = self._settings()
        data["hooks"]["PreToolUse"] = []
        self._write(data)
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("never runs", r.stdout)

    def test_a_removed_permission_rule_reports_stale(self):
        self.install()
        data = self._settings()
        data["permissions"]["allow"] = []
        self._write(data)
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("permission rule", r.stdout)

    def test_a_duplicated_hook_registration_reports_stale(self):
        self.install()
        data = self._settings()
        group = data["hooks"]["PreToolUse"][0]
        group["hooks"].append(dict(group["hooks"][0]))
        self._write(data)
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("more than once", r.stdout)

    def test_a_deleted_settings_file_reports_stale_rather_than_crashing(self):
        self.install()
        (self.home / "settings.json").unlink()
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_reinstalling_clears_an_unregistered_hook(self):
        self.install()
        data = self._settings()
        data["hooks"]["PreToolUse"] = []
        self._write(data)
        self.assertEqual(run(self.home, "--report").returncode, 1)
        self.install()
        self.assertEqual(run(self.home, "--report").returncode, 0)


class TestStampVersion(StampCase):
    def test_a_stamp_from_a_different_format_version_is_rejected(self):
        """The field exists so a shape change is detectable, which needs it to be read."""
        self.install()
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        stamp["stampVersion"] = install.STAMP_VERSION + 1
        self.stamp.write_text(json.dumps(stamp) + "\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("stampVersion", r.stderr)

    def test_a_stamp_version_of_the_wrong_type_is_rejected(self):
        self.install()
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        stamp["stampVersion"] = "1"
        self.stamp.write_text(json.dumps(stamp) + "\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("stampVersion", r.stderr)


class TestStampContent(StampCase):
    def test_the_stamp_names_the_machine_the_source_and_what_was_installed(self):
        self.install()
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        self.assertEqual(stamp["stampVersion"], install.STAMP_VERSION)
        self.assertTrue(stamp["host"]["hostname"])
        self.assertTrue(stamp["payloadDigest"])
        self.assertEqual(stamp["blocks"], {"agent-safety": "v1", "fleet-bootstrap": "v1"})
        # Recorded from a real hub checkout, so the commit is present rather than the tarball fallback.
        self.assertIn(stamp["source"]["vcs"], ("git", "none"))

    def test_the_digest_covers_every_file_the_kit_installs(self):
        """A file added to the kit but left out of the digest is drift the report cannot see.

        The sentinel is non-whitespace deliberately. A snippet is embedded stripped, so appending a
        newline is not a change to installed content and this would assert the wrong thing.
        """
        baseline = install.payload_digest()
        for name in install.PAYLOAD_FILES:
            target = HERE / name
            original = target.read_bytes()
            try:
                target.write_bytes(original + b"\n# sentinel\n")
                self.assertNotEqual(install.payload_digest(), baseline,
                                    f"{name} is in PAYLOAD_FILES but changing it did not move the digest")
            finally:
                target.write_bytes(original)

    def test_trailing_whitespace_on_a_snippet_is_not_reported_as_drift(self):
        """The installer strips a snippet before embedding it, so this changes nothing installed.

        Hashing raw bytes reported STALE here and sent the operator to re-run an installer that
        would write the identical block.
        """
        baseline = install.payload_digest()
        target = HERE / "claude-md-safety.md"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n\n")
            self.assertEqual(install.payload_digest(), baseline)
        finally:
            target.write_bytes(original)

    def test_a_real_edit_to_a_snippet_is_still_reported(self):
        """The normalization must not swallow a change that does reach the installed block."""
        baseline = install.payload_digest()
        target = HERE / "claude-md-safety.md"
        original = target.read_bytes()
        try:
            target.write_bytes(original.replace(b"<!-- agent-safety v1 end -->",
                                                b"Weakened by hand.\n<!-- agent-safety v1 end -->"))
            self.assertNotEqual(install.payload_digest(), baseline)
        finally:
            target.write_bytes(original)

    def test_every_deployed_file_is_in_the_digest(self):
        """The inverse: the kit copies gh-write-guard.py and both snippets, and each must be covered."""
        source = INSTALL.read_text(encoding="utf-8")
        for name in re.findall(r'HERE / "([^"]+\.(?:py|md))"', source):
            if name == "install.py":
                continue
            self.assertIn(name, install.PAYLOAD_FILES,
                          f"install.py reads {name} but PAYLOAD_FILES omits it, so the digest misses it")

    def test_the_one_line_summary_names_the_host_and_the_commit(self):
        self.install()
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        line = install.stamp_line(stamp)
        self.assertIn(stamp["host"]["hostname"], line)
        self.assertIn(stamp["payloadDigest"], line)


if __name__ == "__main__":
    unittest.main(verbosity=1)
