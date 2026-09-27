#!/usr/bin/env python3
"""Drive the python-directories resolver against constructed trees.

The resolution itself is pure over a tracked-file list, so most cases pass one in. The end-to-end
cases run main() in a temp git repository, since the GITHUB_OUTPUT shape and the exit code are what
the validator's steps actually read.

Run as `python3 scripts/tests/test_python_directories.py`, or under `python3 -m unittest discover -s scripts/tests`.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".github/actions/python-directories"))
import python_directories as pd


class ResolveTests(unittest.TestCase):
    def test_empty_declaration_takes_the_root_project(self) -> None:
        self.assertEqual(pd.resolve("", ["pyproject.toml", "a.py"]), (["."], False, []))

    def test_empty_declaration_without_a_root_project_gates_nothing(self) -> None:
        self.assertEqual(pd.resolve("", ["Tools/pyproject.toml", "Tools/a.py"]), ([], False, []))

    def test_blank_lines_are_an_empty_declaration(self) -> None:
        self.assertEqual(pd.resolve("\n  \n", ["pyproject.toml"]), (["."], False, []))

    def test_declared_directories_replace_the_root_default(self) -> None:
        tracked = ["pyproject.toml", "Tools/pyproject.toml"]
        self.assertEqual(pd.resolve("Tools\n", tracked), (["Tools"], True, []))

    def test_declared_directories_are_normalized_and_deduplicated(self) -> None:
        tracked = ["pyproject.toml", "Tools/pyproject.toml"]
        self.assertEqual(pd.resolve(" ./Tools/ \nTools\n.\n", tracked), (["Tools", "."], True, []))

    def test_a_directory_without_a_tracked_project_is_refused(self) -> None:
        directories, declared, errors = pd.resolve("Tools", ["Tools/a.py"])
        self.assertTrue(declared)
        self.assertEqual(directories, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("no tracked pyproject.toml", errors[0])

    def test_a_path_outside_the_repository_is_refused(self) -> None:
        for line in ("/abs", "../up", "a/../../up", "win\\path"):
            with self.subTest(line=line):
                _, _, errors = pd.resolve(line, ["pyproject.toml"])
                self.assertEqual(len(errors), 1)

    def test_a_refused_line_does_not_fall_back_to_the_root(self) -> None:
        directories, declared, errors = pd.resolve("../up", ["pyproject.toml"])
        self.assertEqual((directories, declared), ([], True))
        self.assertTrue(errors)


class UncoveredTests(unittest.TestCase):
    def test_the_root_covers_every_file(self) -> None:
        self.assertEqual(pd.uncovered(["."], ["a.py", "deep/b.py"]), [])

    def test_a_subdirectory_covers_only_its_own_files(self) -> None:
        tracked = ["Tools/a.py", "Tools/tests/test_a.py", "Other/b.py", "ToolsX/c.py", "README.md"]
        self.assertEqual(pd.uncovered(["Tools"], tracked), ["Other/b.py", "ToolsX/c.py"])

    def test_no_directory_leaves_every_python_file_uncovered(self) -> None:
        self.assertEqual(pd.uncovered([], ["a.py", "b.txt"]), ["a.py"])


class EscapeTests(unittest.TestCase):
    def test_percent_is_encoded_before_the_line_breaks(self) -> None:
        self.assertEqual(pd.escape_command("a%\r\nb"), "a%25%0D%0Ab")


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.output = self.root.parent / f"{self.root.name}-output"
        self.addCleanup(lambda: self.output.unlink(missing_ok=True))

    def track(self, *paths: str) -> None:
        for path in paths:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "--", *paths], check=True)

    def run_main(self, declared: str, report: str = "false") -> tuple[int, str, str]:
        env = {"DECLARED": declared, "REPORT_UNCOVERED": report, "GITHUB_OUTPUT": str(self.output)}
        stdout = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(stdout):
                code = pd.main()
        finally:
            os.chdir(cwd)
        written = self.output.read_text(encoding="utf-8") if self.output.exists() else ""
        return code, stdout.getvalue(), written

    def test_outputs_carry_the_declared_directories(self) -> None:
        self.track("pyproject.toml", "Tools/pyproject.toml", "Tools/a.py")
        code, _, written = self.run_main("Tools")
        self.assertEqual(code, 0)
        lines = written.splitlines()
        self.assertTrue(lines[0].startswith("directories<<EOF_"))
        self.assertEqual(lines[1], "Tools")
        self.assertEqual(lines[2], lines[0].removeprefix("directories<<"))
        self.assertIn("any=true", lines)
        self.assertIn("declared=true", lines)

    def test_a_tree_with_no_python_project_reports_none(self) -> None:
        self.track("README.md")
        code, _, written = self.run_main("")
        self.assertEqual(code, 0)
        self.assertIn("any=false", written.splitlines())
        self.assertIn("declared=false", written.splitlines())

    def test_a_refused_declaration_exits_one_and_writes_no_output(self) -> None:
        self.track("Tools/a.py")
        code, stdout, written = self.run_main("Tools")
        self.assertEqual(code, 1)
        self.assertIn("::error::", stdout)
        self.assertEqual(written, "")

    def test_uncovered_files_warn_only_when_asked(self) -> None:
        self.track("Tools/pyproject.toml", "Tools/a.py", "Stray/b.py")
        _, quiet, _ = self.run_main("Tools")
        self.assertNotIn("::warning::", quiet)
        code, loud, _ = self.run_main("Tools", report="true")
        self.assertEqual(code, 0)
        self.assertIn("::warning::1 tracked .py file(s)", loud)
        self.assertIn("Stray/b.py", loud)

    def test_the_warning_names_a_bounded_number_of_files(self) -> None:
        self.track(*(f"Stray/m{i}.py" for i in range(pd.UNCOVERED_SHOWN + 3)))
        _, stdout, _ = self.run_main("", report="true")
        self.assertIn("and 3 more", stdout)


if __name__ == "__main__":
    unittest.main()
