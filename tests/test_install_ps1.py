#!/usr/bin/env python3
"""Tests for `host-setup/agent-safety/claude/install.ps1`'s interpreter probe, run under pwsh.

The wrapper must take the first candidate that runs and reports Python 3.11 or newer.
A name found on PATH is not enough, since the WindowsApps `python3` alias stub is found and installs nothing.
Stub candidates stand in for the interpreters, written as `.cmd` files on Windows and as shell scripts elsewhere.
So the same test runs on the native Windows runner.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "host-setup" / "agent-safety" / "claude" / "install.ps1"
PWSH = shutil.which("pwsh")
WINDOWS = os.name == "nt"


@unittest.skipUnless(PWSH, "needs pwsh")
class TestInstallPs1Probe(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        shutil.copy(SCRIPT, self.dir / "install.ps1")
        (self.dir / "install.py").write_text("", encoding="utf-8")

    def stub(self, name: str, probe: str) -> None:
        """Writes a candidate named `name`, whose probe is answered by `probe`.

        `ok` and `old` print that word and exit 0.
        `store` prints the alias stub's message and exits 9009.
        `real` runs the probe text in this interpreter.
        """
        if WINDOWS:
            reply = (
                f'"{sys.executable}" %*\r\nexit /b %errorlevel%'
                if probe == "real"
                else "echo Python was not found; run without arguments to install from the Microsoft Store. 1>&2\r\nexit /b 9009"
                if probe == "store"
                else f"echo {probe}\r\nexit /b 0"
            )
            body = (
                "@echo off\r\n"
                'if "%~1"=="-3" shift\r\n'
                f'if "%~1"=="-c" (\r\n{reply}\r\n)\r\n'
                f"echo ran:{name}\r\nexit /b 0\r\n"
            )
            (self.bin / f"{name}.cmd").write_bytes(body.encode())
            return
        reply = (
            f'exec "{sys.executable}" "$@"'
            if probe == "real"
            else 'echo "Python was not found; install from the Microsoft Store." >&2; exit 9'
            if probe == "store"
            else f"echo {probe}; exit 0"
        )
        body = f'#!/bin/sh\n[ "$1" = "-3" ] && shift\nif [ "$1" = "-c" ]; then {reply}; fi\necho ran:{name}\nexit 0\n'
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run_wrapper(self) -> subprocess.CompletedProcess[str]:
        assert PWSH is not None
        env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}
        return subprocess.run(
            [PWSH, "-NoProfile", "-File", str(self.dir / "install.ps1")],
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=120, env=env,
        )  # fmt: skip

    def test_a_store_alias_python3_is_passed_over_for_a_working_python(self) -> None:
        self.stub("py", "old")
        self.stub("python3", "store")
        self.stub("python", "real")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["ran:python"])
        self.assertIn("Passed over python3", result.stderr)
        self.assertIn("Microsoft Store", result.stderr)

    @unittest.skipIf(WINDOWS, "an unstartable file is written as a POSIX script here")
    def test_a_candidate_that_cannot_start_is_passed_over(self) -> None:
        self.stub("py", "old")
        broken = self.bin / "python3"
        broken.write_bytes(b"\x00\x01 not an executable\n")
        broken.chmod(0o755)
        self.stub("python", "real")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["ran:python"])

    def test_the_first_candidate_that_reports_3_11_runs_the_installer(self) -> None:
        self.stub("py", "old")
        self.stub("python3", "ok")
        self.stub("python", "ok")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["ran:python3"])
        self.assertIn("Passed over py", result.stderr)

    def test_no_candidate_at_3_11_fails_and_names_what_was_tried(self) -> None:
        for name in ("py", "python3", "python"):
            self.stub(name, "old")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn("ran:", result.stdout)
        for name in ("py", "python3", "python"):
            self.assertIn(f"Passed over {name}:", result.stderr)


if __name__ == "__main__":
    unittest.main()
