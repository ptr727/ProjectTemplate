#!/usr/bin/env python3
"""Tests for host-setup/linux/restore-secrets.sh and host-setup/windows/restore-secrets.ps1.

Each case copies the script into a throwaway tree beside a stub `scripts/secrets_sync.py`.
The stub records its arguments.
The script runs with a PATH holding only the stubs a case names.
No real `op`, no real home, and no real sync is reached.

Run as `python3 tests/test_restore_secrets.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from host_capability import bash_or_skip, requires_unprivileged_root

ROOT = Path(__file__).resolve().parent.parent
SH = ROOT / "host-setup" / "linux" / "restore-secrets.sh"
PS1 = ROOT / "host-setup" / "windows" / "restore-secrets.ps1"

STUB_SYNC = """\
import os, sys
with open(os.environ["SYNC_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
"""

STUB_OP = "#!/bin/sh\nexit 0\n"

# A python3 that is too old for the sync, which a case puts first on PATH.
STUB_OLD_PYTHON = "#!/bin/sh\nexit 1\n"


class Case(unittest.TestCase):
    """A throwaway tree, a bin directory of stubs, and a log the stub sync appends to."""

    script: Path
    relative: str

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.tree = self.root / "tree"
        (self.tree / "scripts").mkdir(parents=True)
        (self.tree / "scripts" / "secrets_sync.py").write_text(STUB_SYNC, encoding="utf-8")
        target = self.tree / self.relative
        target.parent.mkdir(parents=True)
        shutil.copy2(self.script, target)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "sync.log"

    def stub(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def link(self, name: str, target: str | None = None) -> None:
        found = target or shutil.which(name)
        if found is None:
            self.skipTest(f"{name} is not installed")
        (self.bin / name).symlink_to(found)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


@unittest.skipUnless(os.name == "posix", "runs the Linux script under a PATH of symlinked stubs")
class TestLinuxScript(Case):
    script = SH
    relative = "host-setup/linux/restore-secrets.sh"

    def setUp(self) -> None:
        super().setUp()
        self.link("dirname")
        self.link("cat")
        self.link("python3", sys.executable)

    def run_script(
        self, *args: str, with_op: bool = True, root: bool = False
    ) -> subprocess.CompletedProcess[str]:
        if with_op:
            self.stub("op", STUB_OP)
        return subprocess.run(
            [
                *([shutil.which("unshare") or "unshare", "-r"] if root else []),
                bash_or_skip(),
                str(self.tree / self.relative),
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
            env={"PATH": str(self.bin), "HOME": str(self.root / "home"), "SYNC_LOG": str(self.log)},
        )

    def test_it_pulls_exactly_the_named_buckets(self) -> None:
        result = self.run_script("--bucket", "alpha", "--bucket", "beta-2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["pull --bucket alpha --bucket beta-2"])

    def test_yes_and_dry_run_pass_through(self) -> None:
        result = self.run_script("--bucket", "alpha", "--yes", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["pull --bucket alpha --dry-run --yes"])

    def test_help_prints_usage_and_runs_nothing(self) -> None:
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                result = self.run_script(flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith("Usage: restore-secrets.sh"))
                self.assertEqual(self.calls(), [])

    def test_an_unknown_option_is_refused_before_anything_runs(self) -> None:
        result = self.run_script("--bucket", "alpha", "--everything")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unknown option "--everything"', result.stderr)
        self.assertEqual(self.calls(), [])

    def test_no_bucket_is_refused(self) -> None:
        for args in ((), ("--yes",)):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Name at least one bucket", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_a_bad_bucket_name_is_refused(self) -> None:
        for given in ("Alpha", "a_b", "-x", "a b", "alpha/beta", "al.pha", ""):
            with self.subTest(given=given):
                result = self.run_script("--bucket", given)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])
        self.assertIn(
            "lowercase letters, digits, and hyphens", self.run_script("--bucket", "A").stderr
        )

    def test_a_bucket_option_with_no_value_is_refused(self) -> None:
        result = self.run_script("--bucket")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--bucket takes a bucket name", result.stderr)

    @requires_unprivileged_root
    def test_root_is_refused_before_op_or_python_is_looked_for(self) -> None:
        result = self.run_script("--bucket", "alpha", root=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the sync refuses root", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_missing_op_is_named_with_its_remedy(self) -> None:
        result = self.run_script("--bucket", "alpha", with_op=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install-tools.sh --install op", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_dry_run_without_op_prints_the_command_and_succeeds(self) -> None:
        result = self.run_script("--bucket", "alpha", "--dry-run", with_op=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Would run the sync:", result.stdout)
        self.assertIn("pull --bucket alpha --dry-run", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_an_interpreter_older_than_the_sync_needs_is_named(self) -> None:
        (self.bin / "python3").unlink()
        self.stub("python3", STUB_OLD_PYTHON)
        result = self.run_script("--bucket", "alpha")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.11 or newer not found", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_tree_without_the_sync_is_named(self) -> None:
        (self.tree / "scripts" / "secrets_sync.py").unlink()
        result = self.run_script("--bucket", "alpha")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("carries no scripts/secrets_sync.py", result.stderr)

    def test_the_sync_exit_status_is_the_script_exit_status(self) -> None:
        (self.tree / "scripts" / "secrets_sync.py").write_text(
            "raise SystemExit(2)\n", encoding="utf-8"
        )
        result = self.run_script("--bucket", "alpha")
        self.assertEqual(result.returncode, 2, result.stderr)


@unittest.skipUnless(shutil.which("pwsh") and os.name == "posix", "needs pwsh on a POSIX host")
class TestWindowsScript(Case):
    script = PS1
    relative = "host-setup/windows/restore-secrets.ps1"

    def setUp(self) -> None:
        super().setUp()
        self.link("python3", sys.executable)

    def run_script(self, *args: str, with_op: bool = True) -> subprocess.CompletedProcess[str]:
        if with_op:
            self.stub("op", STUB_OP)
        pwsh = shutil.which("pwsh")
        assert pwsh is not None
        return subprocess.run(
            [
                pwsh,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(self.tree / self.relative),
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=120,
            env={
                "PATH": str(self.bin),
                "HOME": str(self.root / "home"),
                "SYNC_LOG": str(self.log),
                "NO_COLOR": "1",
            },
        )

    def test_it_pulls_exactly_the_named_buckets(self) -> None:
        result = self.run_script("-Bucket", "alpha,beta-2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["pull --bucket alpha --bucket beta-2"])

    def test_yes_and_dry_run_pass_through(self) -> None:
        result = self.run_script("-Bucket", "alpha", "-Yes", "-DryRun")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["pull --bucket alpha --dry-run --yes"])

    def test_help_prints_usage_and_runs_nothing(self) -> None:
        for flag in ("-Help", "-h"):
            with self.subTest(flag=flag):
                result = self.run_script(flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Usage: restore-secrets.ps1", result.stdout)
                self.assertEqual(self.calls(), [])

    def test_an_unknown_parameter_is_refused_before_anything_runs(self) -> None:
        result = self.run_script("-Bucket", "alpha", "-Everything")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_no_bucket_is_refused(self) -> None:
        result = self.run_script("-Yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Name at least one bucket", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_bad_bucket_name_is_refused(self) -> None:
        for given in ("Alpha", "a_b", "-x", "alpha,-x", "alpha/beta", "al.pha"):
            with self.subTest(given=given):
                result = self.run_script("-Bucket", given)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])
        self.assertIn(
            "lowercase letters, digits, and hyphens", self.run_script("-Bucket", "A").stderr
        )

    def test_a_missing_op_is_named_with_its_remedy(self) -> None:
        result = self.run_script("-Bucket", "alpha", with_op=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install-tools.ps1 -Install op", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_dry_run_without_op_prints_the_command_and_succeeds(self) -> None:
        result = self.run_script("-Bucket", "alpha", "-DryRun", with_op=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Would run the sync:", result.stdout)
        self.assertIn("pull --bucket alpha --dry-run", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_tree_without_the_sync_is_named(self) -> None:
        (self.tree / "scripts" / "secrets_sync.py").unlink()
        result = self.run_script("-Bucket", "alpha")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("carries no scripts\\secrets_sync.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
