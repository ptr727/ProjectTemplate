#!/usr/bin/env python3
"""Tests for `host-setup/linux/install-agents.sh`, the per-user agent tool updater.

The contract under test is the one a nightly run relies on: an upgrade drives only the tools
already installed, a tool a package manager owns is left to that manager, nothing is installed
unless it is named, and herdr's integrations and restart state reach the output. Each tool is a stub
on a PATH of the test's own that records how it was called, since the real tools update over the
network and would change the host running the test.

Run as `python3 tests/test_install_agents.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "host-setup" / "linux" / "install-agents.sh"

AWKWARD = 'a "quoted" back\\slash\ttab\nnewline\rreturn \x01\x1f é end'

# A stub answers --version with a banner shaped like its tool's own, records every call, and answers herdr's status reads from files the test writes.
STUB = """#!/bin/bash
printf '%s %s\\n' "$(basename "$0")" "$*" >>"$CALLS"
case "$*" in
--version) printf '%s\\n' "$BANNER" ;;
"integration status --outdated-only") cat "$STUB_DIR/outdated" 2>/dev/null || true ;;
"status --json") cat "$STUB_DIR/status.json" ;;
esac
exit 0
"""


@unittest.skipUnless(sys.platform == "linux", "runs the Linux updater")
@unittest.skipIf(os.geteuid() == 0, "the updater refuses root by design")
class TestInstallAgents(unittest.TestCase):
    """`install-agents.sh`, run whole against stub tools."""

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.dir / "home"
        self.home.mkdir()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.calls = self.dir / "calls.log"
        self.calls.touch()
        (self.dir / "status.json").write_text(
            '{"server":{"restart_needed":false},"update":{"restart_needed":false,"server_binary_stale":false}}',
            encoding="utf-8",
        )

    def stub(self, name: str, banner: str, where: Path | None = None) -> Path:
        directory = where or self.bin
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        text = STUB.replace("$BANNER", banner.replace("'", ""))
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        if where is not None:
            (self.bin / name).symlink_to(path)
        return path

    def run_script(self, *arguments: str, pane: bool = False) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "CALLS": str(self.calls),
            "STUB_DIR": str(self.dir),
            "LC_ALL": "C.UTF-8",
        }
        if pane:
            env["HERDR_PANE_ID"] = "test-pane"
        return subprocess.run(
            [str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )

    def called(self) -> list[str]:
        return [
            line
            for line in self.calls.read_text(encoding="utf-8").splitlines()
            if not line.endswith("--version")
        ]

    def report(self) -> dict[str, dict[str, Any]]:
        result = self.run_script("--report", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return {row["tool"]: row for row in json.loads(result.stdout)["tools"]}

    def test_upgrade_drives_only_the_tools_present(self) -> None:
        self.stub("claude", "2.1.0 (Claude Code)")
        self.stub("cswap", "cswap 0.27.1")
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), ["claude update", "cswap upgrade"])
        self.assertIn("codex: not installed, leaving it absent", result.stdout)

    def test_a_package_manager_install_is_left_to_its_manager(self) -> None:
        self.stub(
            "codex",
            "codex-cli 0.149.1",
            where=self.dir / "lib" / "node_modules" / "@openai" / "codex" / "bin",
        )
        result = self.run_script("--upgrade", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), [])
        self.assertIn("codex: installed through npm, leaving it to npm", result.stdout)
        self.assertEqual(self.report()["codex"]["manager"], "npm")

    def test_install_names_what_it_installs(self) -> None:
        result = self.run_script("--install", "--yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nothing is installed by default", result.stderr)

    def test_install_leaves_an_installed_tool_alone(self) -> None:
        self.stub("herdr", "herdr 0.9.4")
        result = self.run_script("--install", "--yes", "herdr")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.called(), [])

    def test_install_dry_run_names_the_installer(self) -> None:
        result = self.run_script("--install", "--dry-run", "claude", "cswap")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(
            result.stdout, r"\[dry run\] curl -fsSL https://claude.ai/install.sh -o \S+ && bash \S+"
        )
        self.assertIn("[dry run] uv tool install claude-swap", result.stdout)

    def test_herdr_upgrade_reinstalls_only_outdated_integrations(self) -> None:
        self.stub("herdr", "herdr 0.9.4")
        (self.dir / "outdated").write_text("claude: outdated (v9) (/x/hook.sh)\n", encoding="utf-8")
        result = self.run_script("--upgrade", "--yes", "herdr")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.called()
        self.assertIn("herdr update", calls)
        self.assertIn("herdr integration install claude", calls)
        self.assertEqual(
            [call for call in calls if call.startswith("herdr integration install")],
            ["herdr integration install claude"],
        )

    def test_herdr_is_not_updated_from_inside_its_own_pane(self) -> None:
        self.stub("herdr", "herdr 0.9.4")
        result = self.run_script("--upgrade", "--yes", "herdr", pane=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("herdr update", self.called())
        self.assertIn("inside a herdr pane", result.stdout)

    def test_restart_pending_reaches_the_report(self) -> None:
        jq = shutil.which("jq")
        if jq is None:
            self.skipTest("herdr's restart state is read with jq")
        (self.bin / "jq").symlink_to(jq)
        self.stub("herdr", "herdr 0.9.4")
        self.assertIs(self.report()["herdr"]["restart_pending"], False)
        status = {
            "server": {"restart_needed": True},
            "update": {"server_binary_stale": True, "restart_needed": True},
        }
        (self.dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        row = self.report()["herdr"]
        self.assertIs(row["restart_pending"], True)
        self.assertTrue(any("restart" in note for note in row["notes"]))
        (self.dir / "status.json").write_text('{"server": {}}', encoding="utf-8")
        self.assertIsNone(self.report()["herdr"]["restart_pending"])

    def test_report_shape(self) -> None:
        self.stub("claude", "2.1.299 (Claude Code)")
        rows = self.report()
        self.assertEqual(list(rows), ["claude", "codex", "opencode", "herdr", "cswap"])
        for row in rows.values():
            self.assertEqual(
                set(row),
                {"tool", "installed", "path", "manager", "status", "restart_pending", "notes"},
            )
        self.assertEqual(rows["claude"]["installed"], "2.1.299")
        self.assertEqual(rows["claude"]["manager"], "self")
        self.assertEqual(
            rows["codex"],
            {
                "tool": "codex",
                "installed": None,
                "path": None,
                "manager": None,
                "status": "absent",
                "restart_pending": None,
                "notes": [],
            },
        )

    def test_refusals(self) -> None:
        for arguments, message in [
            (("--upgrade", "--json"), "applies only to --report"),
            (("--upgrade", "--all"), "--all names every tool for --install"),
            (("--report", "nosuch"), 'Unknown tool "nosuch"'),
            (("--report", "--upgrade"), "More than one action"),
        ]:
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_json_string_round_trips_every_escaped_character(self) -> None:
        lines = SCRIPT.read_text(encoding="utf-8").rstrip("\n").split("\n")
        self.assertEqual(lines[-1], 'main "$@"')
        functions = self.dir / "functions.sh"
        functions.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", "-c", f'source "{functions}"\njson_string "$1"\n', "bash", AWKWARD],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "LC_ALL": "C.UTF-8"},
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), AWKWARD)


PWSH = shutil.which("pwsh")
SCRIPT_PS = ROOT / "host-setup" / "windows" / "install-agents.ps1"


@unittest.skipUnless(PWSH, "needs pwsh to run the Windows updater")
class TestInstallAgentsWindows(unittest.TestCase):
    """`install-agents.ps1`, run whole under pwsh on this host against stub tools.

    The Windows-only checks degrade on another host, so the elevation refusal is not exercised
    here, and neither is a real Windows path. Manager detection reads a path with either separator.
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.dir / "home"
        self.home.mkdir()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.calls = self.dir / "calls.log"
        self.calls.touch()
        (self.dir / "status.json").write_text(
            '{"update":{"restart_needed":false}}', encoding="utf-8"
        )

    def stub(self, name: str, banner: str, directory: Path | None = None) -> Path:
        where = directory or self.bin
        where.mkdir(parents=True, exist_ok=True)
        path = where / name
        path.write_text(STUB.replace("$BANNER", banner.replace("'", "")), encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_script(
        self, *arguments: str, pane: bool = False, extra_path: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        directories = [str(self.bin), "/usr/bin", "/bin"]
        if extra_path is not None:
            directories.insert(0, str(extra_path))
        env = {
            "PATH": os.pathsep.join(directories),
            "HOME": str(self.home),
            "CALLS": str(self.calls),
            "STUB_DIR": str(self.dir),
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "POWERSHELL_TELEMETRY_OPTOUT": "1",
        }
        if pane:
            env["HERDR_PANE_ID"] = "test-pane"
        assert PWSH is not None
        return subprocess.run(
            [PWSH, "-NoProfile", "-File", str(SCRIPT_PS), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
            timeout=120,
        )

    def recorded(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines()

    def test_upgrade_drives_only_present_tools(self) -> None:
        self.stub("claude", "2.1.0 (Claude Code)")
        self.stub("herdr", "herdr 0.9.0")
        result = self.run_script("-Upgrade", "-Yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertIn("claude update", calls)
        self.assertIn("herdr update", calls)
        self.assertFalse([c for c in calls if c.startswith(("codex", "opencode", "uv"))])
        self.assertIn("codex: not installed, leaving it absent", result.stdout)

    def test_cswap_upgrade_runs_uv_directly(self) -> None:
        self.stub("cswap", "cswap 0.1.0")
        self.stub("uv", "uv 0.5.0")
        result = self.run_script("-Upgrade", "-Yes", "cswap")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uv tool upgrade claude-swap", self.recorded())
        self.assertNotIn("cswap upgrade", self.recorded())

    def test_node_modules_path_reads_as_npm_and_is_skipped(self) -> None:
        npm_bin = self.dir / "node_modules" / ".bin"
        self.stub("codex", "codex-cli 0.1.0", npm_bin)
        result = self.run_script("-Upgrade", "-Yes", "codex", extra_path=npm_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("installed through npm, leaving it to npm", result.stdout)
        self.assertNotIn("codex update", self.recorded())

    def test_install_without_names_is_refused(self) -> None:
        result = self.run_script("-Install", "-Yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("-Install names the tools it installs", result.stderr)
        self.assertEqual(self.recorded(), [])

    def test_refusals(self) -> None:
        for arguments, message in [
            (("-Report", "-Upgrade"), "More than one action"),
            (("-Upgrade", "-Json"), "applies only to -Report"),
            (("-Upgrade", "-All"), "-All names every tool for -Install"),
            (("-Install", "-All", "herdr"), "-All and a tool name were both given"),
            (("-Report", "nosuch"), 'Unknown tool "nosuch"'),
        ]:
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_install_refuses_codex_and_opencode_without_a_windows_installer(self) -> None:
        result = self.run_script("-Install", "-Yes", "codex", "opencode")
        self.assertEqual(result.returncode, 1)
        self.assertIn("codex documents no Windows installer", result.stderr)
        self.assertIn("opencode documents no Windows installer", result.stderr)
        self.assertIn("Failed: codex opencode", result.stderr)

    def test_outdated_herdr_integrations_are_reinstalled(self) -> None:
        self.stub("herdr", "herdr 0.9.0")
        (self.dir / "outdated").write_text(
            "claude-code: outdated, run install\npi: outdated\n", encoding="utf-8"
        )
        result = self.run_script("-Upgrade", "-Yes", "herdr")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertIn("herdr integration install claude-code", calls)
        self.assertIn("herdr integration install pi", calls)

    def test_herdr_pane_skips_the_update_but_refreshes_integrations(self) -> None:
        self.stub("herdr", "herdr 0.9.0")
        (self.dir / "outdated").write_text("claude-code: outdated\n", encoding="utf-8")
        result = self.run_script("-Upgrade", "-Yes", "herdr", pane=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("inside a herdr pane", result.stdout)
        calls = self.recorded()
        self.assertNotIn("herdr update", calls)
        self.assertIn("herdr integration install claude-code", calls)

    def test_json_report_shape(self) -> None:
        home_bin = self.home / ".local" / "bin"
        self.stub("herdr", "herdr 0.9.4", home_bin)
        (self.dir / "status.json").write_text(
            '{"update":{"restart_needed":true}}', encoding="utf-8"
        )
        result = self.run_script("-Json", extra_path=home_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["platform"], "windows")
        self.assertEqual(report["notes"], [])
        tools = {entry["tool"]: entry for entry in report["tools"]}
        self.assertEqual(list(tools), ["claude", "codex", "opencode", "herdr", "cswap"])
        for entry in tools.values():
            self.assertEqual(
                set(entry),
                {"tool", "installed", "path", "manager", "status", "restart_pending", "notes"},
            )
        self.assertEqual(tools["codex"]["status"], "absent")
        self.assertIsNone(tools["codex"]["installed"])
        herdr = tools["herdr"]
        self.assertEqual(herdr["status"], "present")
        self.assertEqual(herdr["installed"], "0.9.4")
        self.assertEqual(herdr["manager"], "self")
        self.assertIs(herdr["restart_pending"], True)
        self.assertEqual(herdr["path"], "%USERPROFILE%/.local/bin/herdr")
        self.assertTrue(any("previous binary" in n for n in herdr["notes"]))

    def test_list_and_help_name_the_tools(self) -> None:
        listed = self.run_script("-List")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        for name in ("claude", "codex", "opencode", "herdr", "cswap"):
            self.assertIn(name, listed.stdout)
        helped = self.run_script("-Help")
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertTrue(helped.stdout.startswith("Usage: install-agents.ps1"))


if __name__ == "__main__":
    unittest.main()
