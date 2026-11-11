"""Exercise repo-config/configure.sh's refusal of an apply for a repository the registry does not list.

The real script runs from a copy placed beside a constructed registry, since the script resolves the registry relative to itself.
A stub `gh` on PATH records every call, so a write that slipped through shows up as a recorded call.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from host_capability import bash_or_skip

CONFIGURE = Path(__file__).resolve().parents[1] / "repo-config" / "configure.sh"


class UnregisteredApplyCase(unittest.TestCase):
    """An apply for a name with no registry entry stops before any write, and a check does not."""

    def run_configure(
        self, entries: list[dict[str, object]], args: list[str]
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        """The script's result and the gh calls it made, from a constructed hub tree."""
        bash = bash_or_skip()
        if shutil.which("jq") is None:
            raise unittest.SkipTest("no jq on PATH, so the registry lookup cannot run")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for tree in ("repo-config", "scripts", "spec", "lib"):
                shutil.copytree(CONFIGURE.parents[1] / tree, root / tree)
            (root / "registry").mkdir()
            script = root / "repo-config" / "configure.sh"
            shutil.copy(CONFIGURE, script)
            (root / "registry" / "repos.json").write_text(
                json.dumps({"repos": entries}), encoding="utf-8"
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "gh-calls.txt"
            stub = bin_dir / "gh"
            stub.write_text(
                f'#!/bin/sh\necho "$*" >> {shlex.quote(str(calls))}\nexit 1\n', encoding="utf-8"
            )
            stub.chmod(0o755)
            env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}
            result = subprocess.run(
                [bash, str(script), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
                env=env,
            )
            made = calls.read_text(encoding="utf-8") if calls.exists() else ""
            return result, made

    def test_apply_for_an_unlisted_name_refuses_before_any_gh_call(self) -> None:
        result, made = self.run_configure(
            [{"name": "Other", "status": "cataloged"}], ["apply", "owner/Fixture", "release"]
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("owner/Fixture has no entry in", result.stderr)
        self.assertIn("repos.json", result.stderr)
        self.assertIn("develop only", result.stderr)
        self.assertEqual(made, "")

    def test_apply_with_checkout_for_an_unlisted_name_refuses_too(self) -> None:
        with tempfile.TemporaryDirectory() as checkout:
            result, made = self.run_configure(
                [{"name": "Other", "status": "cataloged"}],
                ["apply", "owner/Fixture", "release", "--checkout", checkout],
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("has no entry in", result.stderr)
            self.assertEqual(made, "")
            self.assertEqual(list(Path(checkout).iterdir()), [])

    def test_apply_for_a_listed_name_reaches_gh(self) -> None:
        result, made = self.run_configure(
            [{"name": "Fixture", "status": "cataloged"}], ["apply", "owner/Fixture", "release"]
        )
        self.assertNotEqual(made, "")
        self.assertNotIn("has no entry in", result.stderr)

    def test_check_for_an_unlisted_name_is_not_refused(self) -> None:
        result, _ = self.run_configure(
            [{"name": "Other", "status": "cataloged"}], ["check", "owner/Fixture", "release"]
        )
        self.assertNotIn("has no entry in", result.stderr)


if __name__ == "__main__":
    unittest.main()
