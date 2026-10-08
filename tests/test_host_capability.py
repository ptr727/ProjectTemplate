#!/usr/bin/env python3
"""Test the Windows bash selection in host_capability on any host, Linux CI included."""

from __future__ import annotations

import ntpath
import os
import unittest
from collections.abc import Iterable
from unittest import mock

import host_capability

WINDOWS_ENV = {"SystemRoot": r"C:\Windows", "LOCALAPPDATA": r"D:\Profile\AppData\Local"}
SYSTEM = r"C:\WINDOWS\system32"
APPS = r"D:\Profile\AppData\Local\Microsoft\WindowsApps"
GIT = r"C:\Program Files\Git\usr\bin"
SHIMS = r"C:\tools\shims"


def bash_in(directory: str) -> str:
    return ntpath.join(directory, "bash.exe")


class WindowsCase(unittest.TestCase):
    """Runs each case as a Windows host would, whatever host runs the suite."""

    def setUp(self) -> None:
        self.enterContext(mock.patch.object(host_capability, "_WINDOWS", True))
        self.enterContext(mock.patch.dict(os.environ, WINDOWS_ENV))


class RunsAScriptCase(WindowsCase):
    def test_the_wsl_launchers_are_passed_over(self) -> None:
        for launcher in (bash_in(SYSTEM), bash_in(APPS)):
            with self.subTest(launcher):
                self.assertFalse(host_capability.runs_a_script(launcher))

    def test_git_bash_is_taken(self) -> None:
        self.assertTrue(host_capability.runs_a_script(bash_in(GIT)))

    def test_a_sibling_sharing_the_windows_prefix_is_not_under_it(self) -> None:
        self.assertTrue(host_capability.runs_a_script(r"C:\WindowsTools\bash.exe"))

    def test_the_windows_directory_is_still_passed_over_with_no_profile_set(self) -> None:
        with mock.patch.dict(os.environ):
            del os.environ["LOCALAPPDATA"]
            self.assertFalse(host_capability.runs_a_script(bash_in(SYSTEM)))
            self.assertTrue(host_capability.runs_a_script(bash_in(GIT)))

    def test_off_windows_every_bash_runs_a_script(self) -> None:
        with mock.patch.object(host_capability, "_WINDOWS", False):
            self.assertTrue(host_capability.runs_a_script("/usr/bin/bash"))


class BashPathCase(WindowsCase):
    def walk(self, entries: Iterable[str], present: set[str]) -> tuple[str | None, list[str]]:
        """bash_path() over a PATH of entries, where only the paths in present exist."""
        probed: list[str] = []

        def executable(path: str) -> bool:
            probed.append(path)
            return path in present

        with (
            mock.patch.dict(os.environ, {"PATH": ntpath.pathsep.join(entries)}),
            mock.patch.object(host_capability, "_executable", side_effect=executable),
        ):
            return host_capability.bash_path(), probed

    def test_the_first_bash_that_runs_a_script_is_taken(self) -> None:
        present = {bash_in(SYSTEM), bash_in(APPS), bash_in(GIT)}
        found, probed = self.walk([SYSTEM, "", APPS, GIT, r"C:\later"], present)
        self.assertEqual(bash_in(GIT), found)
        self.assertEqual([bash_in(SYSTEM), bash_in(APPS), bash_in(GIT)], probed)

    def test_a_shim_is_never_probed_for(self) -> None:
        found, probed = self.walk([SHIMS], {ntpath.join(SHIMS, "bash.cmd")})
        self.assertIsNone(found)
        self.assertEqual([bash_in(SHIMS)], probed)

    def test_a_relative_entry_is_skipped_rather_than_read_against_the_current_directory(
        self,
    ) -> None:
        found, probed = self.walk([".", "bin", GIT], {bash_in("."), bash_in(GIT)})
        self.assertEqual(bash_in(GIT), found)
        self.assertEqual([bash_in(GIT)], probed)

    def test_a_quoted_entry_is_read_unquoted(self) -> None:
        self.assertEqual(bash_in(GIT), self.walk([f'"{GIT}"'], {bash_in(GIT)})[0])

    def test_a_path_holding_only_launchers_has_no_bash(self) -> None:
        self.assertIsNone(self.walk([SYSTEM, APPS], {bash_in(SYSTEM), bash_in(APPS)})[0])

    def test_off_windows_shutil_which_answers_relative_entries_included(self) -> None:
        with (
            mock.patch.object(host_capability, "_WINDOWS", False),
            mock.patch.object(host_capability.shutil, "which", return_value="bin/bash") as which,
        ):
            self.assertEqual("bin/bash", host_capability.bash_path())
        which.assert_called_once_with("bash")

    def test_path_is_read_on_every_call(self) -> None:
        self.assertIsNone(self.walk([SYSTEM], {bash_in(SYSTEM), bash_in(GIT)})[0])
        self.assertEqual(bash_in(GIT), self.walk([GIT], {bash_in(GIT)})[0])


if __name__ == "__main__":
    unittest.main()
