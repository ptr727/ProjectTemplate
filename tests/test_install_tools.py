#!/usr/bin/env python3
"""Tests for the host tool installers' JSON report, on both platforms.

The JSON report exists so a program can read what the table tells a person, so its contract is the
shape a program parses: every field present on every row, a version that was not read written as
null, and each note filed under the tool that raised it. Each installer's report is driven through
its own functions with the host reads stubbed, since a real report reads the host it runs on.

Run as `python3 tests/test_install_tools.py`, or under `python3 -m unittest discover -s tests`.
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

ROOT = Path(__file__).resolve().parent.parent
LINUX_INSTALLER = ROOT / "host-setup" / "linux" / "install-tools.sh"
WINDOWS_INSTALLER = ROOT / "host-setup" / "windows" / "install-tools.ps1"

AWKWARD = 'a "quoted" back\\slash\ttab\nnewline\rreturn \x01\x1f \u00e9 end'


def linux_functions() -> str:
    """`install-tools.sh` without its closing `main "$@"`, so a test can source its functions alone."""
    lines = LINUX_INSTALLER.read_text(encoding="utf-8").rstrip("\n").split("\n")
    if lines[-1] != 'main "$@"':
        raise AssertionError(f"install-tools.sh no longer ends with its main call: {lines[-1]!r}")
    return "\n".join(lines[:-1]) + "\n"


@unittest.skipUnless(sys.platform == "linux", "drives the Linux installer's own functions")
class TestLinuxJsonReport(unittest.TestCase):
    """`install-tools.sh --json`, driven through the script's own functions."""

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.functions = self.dir / "functions.sh"
        self.functions.write_text(linux_functions(), encoding="utf-8")

    def run_bash(self, body: str, argument: str = "") -> subprocess.CompletedProcess[str]:
        script = f'source "{self.functions}"\n{body}\n'
        return subprocess.run(
            ["bash", "-c", script, "bash", argument],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )

    def test_json_string_round_trips_every_escaped_character(self) -> None:
        result = self.run_bash('json_string "$1"', AWKWARD)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), AWKWARD)

    def test_report_writes_each_row_and_files_each_note(self) -> None:
        body = f"""
host_path=$PATH
PATH="{self.dir}/empty"
JSON_OUTPUT=true
SELECTED=(jq extra)
REPO_PACKAGES[extra]=extra-package
jq_version() {{ :; }}
jq_target() {{ printf '1.8.2'; }}
apt_installed_version() {{ printf '2.0'; }}
apt_candidate_version() {{ printf '2.0'; }}
tool_note() {{ note "$1" "$AWKWARD"; }}
AWKWARD=$1
report
PATH=$host_path
"""
        result = self.run_bash(body, AWKWARD)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["platform"], "linux")
        self.assertEqual(
            report["tools"],
            [
                {
                    "tool": "jq",
                    "installed": None,
                    "available": "1.8.2",
                    "source": "jqlang/jq",
                    "mechanism": "binary",
                    "status": "missing",
                    "notes": [AWKWARD],
                },
                {
                    "tool": "extra",
                    "installed": "2.0",
                    "available": "2.0",
                    "source": "apt:extra-package",
                    "mechanism": "apt",
                    "status": "current",
                    "notes": [],
                },
            ],
        )
        self.assertEqual(len(report["notes"]), 1)
        self.assertIn("curl is not installed", report["notes"][0])

    def test_json_string_drops_bytes_that_are_not_utf8(self) -> None:
        never_valid = b"\xff"
        above_the_last_code_point = b"\xf4\x90\x80\x80"
        overlong = b"\xe0\x80\x80"
        surrogate = b"\xed\xa0\x80"
        truncated = b"\xe2\x82"
        lead_before_ascii = b"\xc3"
        malformed = (
            b"a"
            + never_valid
            + above_the_last_code_point
            + overlong
            + surrogate
            + lead_before_ascii
            + "b\u00e9\U0001f600".encode()
            + truncated
        )
        for locale in ("C", "C.UTF-8"):
            with self.subTest(locale=locale):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        f'source "{self.functions}"\njson_string "$1"',
                        "bash",
                        malformed,
                    ],
                    capture_output=True,
                    check=False,
                    timeout=30,
                    env={"PATH": os.environ["PATH"], "LC_ALL": locale},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                self.assertTrue(result.stdout.isascii(), result.stdout)
                self.assertEqual(json.loads(result.stdout), "ab\u00e9\U0001f600")

    def test_docker_inside_wsl_names_docker_desktop_as_its_mechanism(self) -> None:
        for is_wsl, expected in (("true", "docker-desktop"), ("false", "apt")):
            with self.subTest(is_wsl=is_wsl):
                result = self.run_bash(f"IS_WSL={is_wsl}\ntool_mechanism docker")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_report_with_no_rows_is_still_an_object(self) -> None:
        result = self.run_bash("JSON_OUTPUT=true\nSELECTED=()\nreport")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["tools"], [])

    def test_json_is_refused_beside_another_action(self) -> None:
        for action in ("--install", "--upgrade", "--list", "--sudo-timestamp"):
            with self.subTest(action=action):
                result = subprocess.run(
                    [str(LINUX_INSTALLER), action, "--json"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("--json", result.stderr)
                self.assertEqual(result.stdout, "")


WINDOWS_HARNESS = r"""
param([string]$Installer, [string]$Awkward, [string]$Mode)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('note', 'Get-NoteText', 'Show-Report', 'Resolve-Mode', 'die')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
function log { param([string]$Message = '') Write-Host $Message }
function info { param([string]$Message) Write-Host "  $Message" }
function Get-Tool { param([string]$Name) @{ Name = $Name } }
function Get-ToolState {
    param([hashtable]$Tool)
    if ($Tool.Name -eq 'jq') {
        return @{ Package = 'jqlang.jq'; Installed = $null; Rows = @(); Available = '1.8.2'; Scope = @(); Status = 'missing' }
    }
    if ($Tool.Name -eq 'dotnet') {
        return @{ Package = 'Microsoft.DotNet.SDK.10'; Installed = $null; Rows = @('8.0.1', '10.0.1'); Available = '10.0.1'; Scope = @('machine'); Status = 'multiple' }
    }
    return @{ Package = 'astral-sh.uv'; Installed = '0.12.4'; Rows = @('0.12.4'); Available = '0.12.4'; Scope = @('user', 'machine'); Status = 'current' }
}
function Add-ToolNote { param([hashtable]$Tool, [hashtable]$State) if ($Tool.Name -eq 'jq') { note 'jq' $Awkward } }
$NOTES = @()
$NOTE_TEXTS = @()
$JSON_OUTPUT = $true
$ELEVATED = $true
$SELECTED = @('jq', 'uv', 'dotnet')
if ($Mode) {
    $ACTIONS = [ordered]@{ report = $false; install = $false; list = $false }
    $ACTIONS[$Mode] = $true
    Resolve-Mode
} else {
    Show-Report
}
"""


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows installer's own functions"
)
class TestWindowsJsonReport(unittest.TestCase):
    """`install-tools.ps1 -Json`, driven through the script's own functions with winget stubbed."""

    def run_harness(self, mode: str = "") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory, "harness.ps1")
            harness.write_text(WINDOWS_HARNESS, encoding="utf-8")
            return subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(harness),
                    "-Installer",
                    str(WINDOWS_INSTALLER),
                    "-Awkward",
                    AWKWARD,
                    "-Mode",
                    mode,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=120,
            )

    def test_report_writes_each_row_and_files_each_note(self) -> None:
        result = self.run_harness()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.isascii(), result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["platform"], "windows")
        self.assertEqual(
            report["tools"],
            [
                {
                    "tool": "jq",
                    "installed": None,
                    "available": "1.8.2",
                    "source": "jqlang.jq",
                    "mechanism": "winget",
                    "status": "missing",
                    "scope": [],
                    "notes": [AWKWARD],
                },
                {
                    "tool": "uv",
                    "installed": "0.12.4",
                    "available": "0.12.4",
                    "source": "astral-sh.uv",
                    "mechanism": "winget",
                    "status": "current",
                    "scope": ["user", "machine"],
                    "notes": [],
                },
                {
                    "tool": "dotnet",
                    "installed": None,
                    "available": "10.0.1",
                    "source": "Microsoft.DotNet.SDK.10",
                    "mechanism": "winget",
                    "status": "multiple",
                    "scope": ["machine"],
                    "notes": [],
                },
            ],
        )
        self.assertEqual(len(report["notes"]), 1)
        self.assertIn("elevated", report["notes"][0])

    def test_json_is_refused_beside_another_action(self) -> None:
        for mode in ("install", "list"):
            with self.subTest(mode=mode):
                result = self.run_harness(mode)
                self.assertEqual(result.returncode, 1)
                self.assertIn("-Json", result.stderr)


if __name__ == "__main__":
    unittest.main()
