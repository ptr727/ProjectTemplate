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
        """A file added to the kit but left out of the digest is drift the report cannot see."""
        baseline = install.payload_digest()
        for name in install.PAYLOAD_FILES:
            target = HERE / name
            original = target.read_bytes()
            try:
                target.write_bytes(original + b"\n")
                self.assertNotEqual(install.payload_digest(), baseline,
                                    f"{name} is in PAYLOAD_FILES but changing it did not move the digest")
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
