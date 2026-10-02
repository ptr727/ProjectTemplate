#!/usr/bin/env python3
"""Exercise repo-config/configure.sh's command-line refusals by running the whole script.

The script is copied into a scratch tree beside a `gh` stub that records every call, so each case
asserts both the exit and that a refused or help invocation never reached the API. A run whose
arguments are valid is the control, proving the stub would have recorded a call had one been made.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

CONFIGURE = Path(__file__).resolve().parents[1] / "repo-config" / "configure.sh"


class UsageCase(unittest.TestCase):
    """Help exits 0 and a usage error exits 1, each before any `gh` call."""

    def run_configure(self, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
        bash = shutil.which("bash")
        if bash is None:
            raise unittest.SkipTest("no bash on PATH, so the script cannot be run")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo-config").mkdir()
            (root / "bin").mkdir()
            shutil.copy(CONFIGURE, root / "repo-config" / "configure.sh")
            calls = root / "gh-calls"
            gh_stub = root / "bin" / "gh"
            gh_stub.write_text(
                f'#!/bin/sh\necho "$*" >>"{calls}"\nexit 1\n',
                encoding="utf-8",
            )
            gh_stub.chmod(0o755)
            env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ.get('PATH', os.defpath)}")
            result = subprocess.run(
                [bash, str(root / "repo-config" / "configure.sh"), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
                env=env,
            )
            recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
            return result, recorded

    def test_help_prints_the_usage_and_exits_zero(self) -> None:
        for args in (["--help"], ["-h"], ["apply", "--help"], ["check", "owner/name", "-h"]):
            with self.subTest(args=args):
                result, calls = self.run_configure(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Usage: repo-config/configure.sh apply|check", result.stdout)
                self.assertEqual(calls, "")

    def test_a_usage_error_exits_one_before_any_gh_call(self) -> None:
        cases = {
            (): "No command given",
            ("owner/name",): "Unknown command 'owner/name'",
            ("--bogus-flag",): "Unknown command '--bogus-flag'",
            ("apply", "--bogus-flag"): "Unknown option '--bogus-flag'",
            ("apply", "owner/name", "release", "extra"): "Too many arguments",
            ("apply", "not-a-repo"): "Repo 'not-a-repo' is not shaped owner/name",
            ("apply", "owner/name/extra"): "is not shaped owner/name",
            ("apply", "owner/.."): "Repo 'owner/..' is not shaped owner/name",
            ("apply", "owner/."): "Repo 'owner/.' is not shaped owner/name",
            ("apply", "../name"): "Repo '../name' is not shaped owner/name",
            ("check", "owner/name", "bogus"): "Unknown workflow model 'bogus'",
            ("apply", "release", "owner/name"): "The model 'release' comes before the repo",
            ("check", "release", "operational"): "The model 'release' comes before the repo",
        }
        for args, message in cases.items():
            with self.subTest(args=args):
                result, calls = self.run_configure(*args)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertIn("--help", result.stderr)
                self.assertEqual(calls, "")

    def test_valid_arguments_reach_gh(self) -> None:
        """The control: a well-formed run gets past argument handling to its first API call."""
        for args in (
            ["check", "owner/a.b_c-d", "release"],
            ["check", "owner_x/name", "operational"],
            ["check", "operational"],
        ):
            with self.subTest(args=args):
                result, calls = self.run_configure(*args)
                self.assertNotEqual(calls, "", result.stderr)


if __name__ == "__main__":
    unittest.main()
