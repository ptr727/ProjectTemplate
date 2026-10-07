#!/usr/bin/env python3
"""Test the Windows bash selection in host_capability on any host, Linux CI included."""

from __future__ import annotations

import os
import unittest
from unittest import mock

import host_capability

WINDOWS_ENV = {"SystemRoot": r"C:\Windows", "LOCALAPPDATA": r"D:\Profile\AppData\Local"}
LAUNCHER = r"C:\WINDOWS\system32\bash.exe"
ALIAS = r"D:\Profile\AppData\Local\Microsoft\WindowsApps\bash.exe"
GIT_BASH = r"C:\Program Files\Git\usr\bin\bash.EXE"


class WindowsCase(unittest.TestCase):
    """Runs each case as a Windows host would, whatever host runs the suite."""

    def setUp(self) -> None:
        self.enterContext(mock.patch.object(host_capability, "_WINDOWS", True))
        self.enterContext(mock.patch.dict(os.environ, WINDOWS_ENV))


class RunsAScriptCase(WindowsCase):
    def test_the_wsl_launchers_are_passed_over(self) -> None:
        for launcher in (LAUNCHER, ALIAS):
            with self.subTest(launcher):
                self.assertFalse(host_capability.runs_a_script(launcher))

    def test_a_shim_cmd_re_parses_is_passed_over(self) -> None:
        for shim in (r"C:\tools\shims\bash.CMD", r"C:\tools\shims\bash.bat"):
            with self.subTest(shim):
                self.assertFalse(host_capability.runs_a_script(shim))

    def test_git_bash_is_taken(self) -> None:
        self.assertTrue(host_capability.runs_a_script(GIT_BASH))

    def test_a_sibling_sharing_the_windows_prefix_is_not_under_it(self) -> None:
        self.assertTrue(host_capability.runs_a_script(r"C:\WindowsTools\bash.exe"))

    def test_off_windows_every_bash_runs_a_script(self) -> None:
        with mock.patch.object(host_capability, "_WINDOWS", False):
            self.assertTrue(host_capability.runs_a_script("/usr/bin/bash"))


class BashPathCase(WindowsCase):
    def walk(self, entries: dict[str, str | None]) -> tuple[str | None, list[str]]:
        """bash_path() over a PATH of entries, each mapped to the bash which() finds there."""
        asked: list[str] = []

        def which(name: str, path: str | None = None) -> str | None:
            assert name == "bash" and path is not None
            asked.append(path)
            return entries[path]

        with (
            mock.patch.dict(os.environ, {"PATH": os.pathsep.join(entries)}),
            mock.patch.object(host_capability.shutil, "which", side_effect=which),
        ):
            return host_capability.bash_path(), asked

    def test_the_first_bash_that_runs_a_script_is_taken(self) -> None:
        found, asked = self.walk(
            {"sys": LAUNCHER, "": None, "shims": r"C:\shims\bash.cmd", "git": GIT_BASH, "z": None}
        )
        self.assertEqual(GIT_BASH, found)
        self.assertEqual(["sys", "shims", "git"], asked)

    def test_a_path_holding_only_launchers_has_no_bash(self) -> None:
        self.assertIsNone(self.walk({"sys": LAUNCHER, "apps": ALIAS})[0])

    def test_path_is_read_on_every_call(self) -> None:
        self.assertIsNone(self.walk({"sys": LAUNCHER})[0])
        self.assertEqual(GIT_BASH, self.walk({"git": GIT_BASH})[0])


if __name__ == "__main__":
    unittest.main()
