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
        self.assertEqual(report["schema"], 2)
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
                    "pending_packages": [],
                    "notes": [AWKWARD],
                },
                {
                    "tool": "extra",
                    "installed": "2.0",
                    "available": "2.0",
                    "source": "apt:extra-package",
                    "mechanism": "apt",
                    "status": "current",
                    "pending_packages": [],
                    "notes": [],
                },
            ],
        )
        self.assertEqual(len(report["notes"]), 1)
        self.assertIn("curl is not installed", report["notes"][0])

    def report_rows(self, body: str) -> dict[str, dict[str, object]]:
        """The JSON report's rows by tool, with every host read the body names stubbed and every note silenced."""
        result = self.run_bash(f"JSON_OUTPUT=true\ntool_note() {{ :; }}\n{body}\nreport")
        self.assertEqual(result.returncode, 0, result.stderr)
        return {row["tool"]: row for row in json.loads(result.stdout)["tools"]}

    def test_a_tool_not_installed_without_its_upstream_repository_reads_unmanaged(self) -> None:
        """Neither tool is installed and the distro carries both, but only node's upstream repository is configured."""
        rows = self.report_rows("""
SELECTED=(gh node)
tool_configured() { [[ $1 != gh ]]; }
apt_installed_version() { :; }
apt_candidate_version() { printf '2.0.0-1'; }
""")
        self.assertEqual(rows["gh"]["installed"], None)
        self.assertEqual(rows["gh"]["status"], "unmanaged")
        self.assertEqual(rows["node"]["installed"], None)
        self.assertEqual(rows["node"]["status"], "missing")

    def test_an_unconfigured_upstream_reads_unmanaged_whatever_the_versions(self) -> None:
        cases = (("", "2.0"), ("", ""), ("2.0", "2.0"), ("1.0", "2.0"), ("2.0", ""))
        for installed, target in cases:
            with self.subTest(installed=installed, target=target):
                result = self.run_bash(
                    f'tool_configured() {{ return 1; }}\ntool_effective_status gh "$1" "{target}"',
                    installed,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "unmanaged")

    def test_installing_an_unmanaged_tool_that_is_not_installed_says_so(self) -> None:
        result = self.run_bash("""
MODE=upgrade
tool_configured() { return 1; }
tool_unshadow() { :; }
apt_installed_version() { :; }
apt_candidate_version() { printf '2.0.0-1'; }
gh_install() { :; }
apply_tool gh
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gh: not installed, installing it from cli.github.com", result.stdout)
        self.assertNotIn("2.0.0-1", result.stdout)

    def test_a_part_installed_package_set_is_pending_and_not_current(self) -> None:
        """A current python3 whose required set is part installed has pending work, and so is not current."""
        stubs = """
SELECTED=(python)
apt_installed_version() { printf "$INSTALLED"; }
apt_candidate_version() { printf '3.13.5-1'; }
package_installed() { [[ " $ABSENT " != *" $1 "* ]]; }
"""
        cases: tuple[tuple[str, str, str, list[str]], ...] = (
            ("3.13.5-1", "python3-venv python3-pip", "incomplete", ["python3-venv", "python3-pip"]),
            ("3.13.5-1", "", "current", []),
            ("3.13.4-1", "python3-venv", "outdated", ["python3-venv"]),
        )
        for installed, absent, status, pending in cases:
            with self.subTest(installed=installed, absent=absent):
                rows = self.report_rows(f"INSTALLED='{installed}'\nABSENT='{absent}'\n{stubs}")
                self.assertEqual(rows["python"]["status"], status)
                self.assertEqual(rows["python"]["pending_packages"], pending)

    def test_a_held_package_counts_as_installed(self) -> None:
        """A package held by apt-mark is installed, so it never reads as pending work an apply cannot do."""
        cases = {
            "install ok installed": "yes",
            "hold ok installed": "yes",
            "deinstall ok config-files": "no",
            "install ok half-configured": "no",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                result = self.run_bash(
                    'dpkg-query() { printf "%s" "$STATUS"; }\nSTATUS=$1\npackage_installed pkg && echo yes || echo no',
                    status,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_dotnet_s_other_sdk_lines_are_pending_only_under_optional(self) -> None:
        """The newest SDK line is installed and current, and the two older lines are not installed."""
        stubs = """
SELECTED=(dotnet)
dotnet_sdk_packages() { printf '%s\\n' dotnet-sdk-8.0 dotnet-sdk-9.0 dotnet-sdk-10.0; }
apt_installed_version() { printf '10.0.100-1'; }
apt_candidate_version() { printf '10.0.100-1'; }
package_installed() { [[ $1 == dotnet-sdk-10.0 ]]; }
"""
        cases: tuple[tuple[str, str, list[str]], ...] = (
            ("true", "incomplete", ["dotnet-sdk-8.0", "dotnet-sdk-9.0"]),
            ("false", "current", []),
        )
        for optional, status, pending in cases:
            with self.subTest(optional=optional):
                rows = self.report_rows(f"WITH_OPTIONAL={optional}\n{stubs}")
                self.assertEqual(rows["dotnet"]["status"], status)
                self.assertEqual(rows["dotnet"]["pending_packages"], pending)

    def test_bin_dir_joins_a_path_that_leaves_it_out(self) -> None:
        result = self.run_bash('PATH=/usr/bin:/bin\nensure_bin_dir_on_path\nprintf "%s" "$PATH"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/usr/local/bin:/usr/bin:/bin")

    def test_bin_dir_joins_an_empty_path_without_a_trailing_separator(self) -> None:
        result = self.run_bash(
            'host_path=$PATH\nPATH=""\nensure_bin_dir_on_path\nresult=$PATH\nPATH=$host_path\nprintf "%s" "$result"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/usr/local/bin")

    def test_a_path_naming_bin_dir_keeps_its_order_and_its_real_shadow(self) -> None:
        bin_dir = self.dir / "shadow"
        bin_dir.mkdir()
        tool = bin_dir / "sometool"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
        body = f"""PATH="{bin_dir}:/usr/bin:/usr/local/bin/:/bin"
before=$PATH
ensure_bin_dir_on_path
[[ $PATH == "$before" ]] || {{ echo "changed: $PATH"; exit 1; }}
tool_shadow_path sometool"""
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{bin_dir}/sometool")

    def test_bin_dir_spelled_differently_is_still_on_the_path(self) -> None:
        body = 'PATH="/usr//local/./bin:/usr/bin"\nbefore=$PATH\nensure_bin_dir_on_path\n[[ $PATH == "$before" ]] && printf same'
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "same")

    def test_main_puts_bin_dir_on_the_path_before_reporting(self) -> None:
        body = """parse_args() { :; }
load_repo_tools() { :; }
resolve_selection() { :; }
detect_host() { :; }
report() { printf '%s' "$PATH"; }
host_path=$PATH
PATH=/usr/bin:/bin
main"""
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/usr/local/bin:/usr/bin:/bin")

    def test_minimal_path_reports_no_shadow_for_a_distro_copy(self) -> None:
        distro = self.dir / "distro"
        distro.mkdir()
        tool = distro / "sometool"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
        body = (
            f'host_path=$PATH\nPATH="{distro}"\nensure_bin_dir_on_path\n'
            'result=$(tool_shadow_path sometool)\nPATH=$host_path\nprintf "%s" "$result"'
        )
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

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

    def test_selection_naming_a_tool_other_than_the_last_succeeds(self) -> None:
        result = self.run_bash('REQUESTED=(jq)\nresolve_selection\nprintf "%s\\n" "${SELECTED[@]}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "jq\n")

    def test_note_paths_under_home_are_written_with_a_tilde(self) -> None:
        body = """
HOME=/srv/example-user
JSON_OUTPUT=true
SELECTED=(jq)
tool_shadow_path() { printf '/srv/example-user/.local/bin/%s' "$1"; }
jq_version() { :; }
jq_target() { :; }
report
"""
        result = self.run_bash(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("/srv/example-user", result.stdout)
        notes = json.loads(result.stdout)["tools"][0]["notes"]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("~/.local/bin/jq "), notes[0])

    def test_hide_home_only_rewrites_a_whole_leading_home_component(self) -> None:
        cases = {
            "/srv/example-user": "~",
            "/srv/example-user/bin": "~/bin",
            "/srv/example-user2/bin": "/srv/example-user2/bin",
            "/usr/local/bin": "/usr/local/bin",
            "": "",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                result = self.run_bash('HOME=/srv/example-user\nhide_home "$1"', path)
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


WINGET_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('Read-WingetTable', 'Test-WingetVersion', 'Resolve-InstalledVersion', 'Get-VersionKey', 'Compare-HostVersion', 'Get-ToolStatus')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$script:EXPLICIT = @()
function Get-ExplicitUpgrade { return $script:EXPLICIT }
$results = [ordered]@{}
foreach ($case in (Get-Content -Raw -LiteralPath $Cases | ConvertFrom-Json).PSObject.Properties) {
    $script:EXPLICIT = @(if ($case.Value.PSObject.Properties['explicit']) { $case.Value.explicit })
    $rows = Read-WingetTable -Text $case.Value.table -Id 'jqlang.jq'
    $installed = Resolve-InstalledVersion -Version $rows
    $state = @{ Readable = $true; Rows = $rows; Installed = $installed; Available = '1.8.2'; Package = 'jqlang.jq' }
    $status = Get-ToolStatus -Tool @{ Name = 'jq'; Probe = 'jq' } -State $state
    $results[$case.Name] = [ordered]@{ rows = @($rows); installed = $installed; status = $status }
}
$results | ConvertTo-Json -Depth 4
"""

UNREAD_APPLY_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('Show-Report', 'Invoke-ToolApply', 'Get-NoteText', 'note')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
function log { param([string]$Message = '') Write-Host "LOG $Message" }
function info { param([string]$Message) Write-Host "INFO $Message" }
function warn { param([string]$Message) Write-Host "WARN $Message" }
function die { param([string]$Message) throw $Message }
function Get-Tool { param([string]$Name) @{ Name = $Name; Package = 'jqlang.jq'; Probe = 'jq'; Optional = @() } }
function Get-ToolState {
    param([hashtable]$Tool)
    return @{ Package = 'jqlang.jq'; Installed = $null; Rows = @('Unknown'); Readable = $true; Available = '1.8.2'; Scope = @('user'); Status = 'unknown' }
}
function Add-ToolNote { param([hashtable]$Tool, [hashtable]$State) }
function Invoke-WingetInstall { param([string]$Id) Write-Host "INSTALL $Id"; return 0 }
function Invoke-WingetUpgrade { param([string]$Id) Write-Host "UPGRADE $Id"; return 0 }
$NOTES = @()
$NOTE_TEXTS = @()
$JSON_OUTPUT = $false
$ELEVATED = $true
$SELECTED = @('jq')
$FAILED = @()
$WANT_SCOPE = $null
$WITH_OPTIONAL = $false
$MODE = 'install'
Show-Report
Invoke-ToolApply -ToolName 'jq'
"""


def winget_list(*versions: str) -> str:
    """A `winget list` table carrying one row per version for the id the harness asks about."""
    lines = [
        "Name   Id         Version  Available Source",
        "-------------------------------------------",
        *(f"jq     jqlang.jq  {version:<8} 1.8.2     winget" for version in versions),
    ]
    return "\n".join(lines)


def run_pwsh_harness(harness_text: str, cases: object) -> subprocess.CompletedProcess[str]:
    """Run a harness against the Windows installer, handing it `cases` as a JSON file."""
    with tempfile.TemporaryDirectory() as directory:
        harness = Path(directory, "harness.ps1")
        harness.write_text(harness_text, encoding="utf-8")
        data = Path(directory, "cases.json")
        data.write_text(json.dumps(cases), encoding="utf-8")
        return subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(harness),
                "-Installer",
                str(WINDOWS_INSTALLER),
                "-Cases",
                str(data),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=120,
        )


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows installer's own functions"
)
class TestWindowsInstalledVersion(unittest.TestCase):
    """A Version column token that is not a version leaves the installed version unread."""

    def test_a_non_version_token_is_unread_rather_than_installed(self) -> None:
        cases = {
            "unknown": {"table": winget_list("Unknown")},
            "below": {"table": winget_list("<")},
            "above": {"table": winget_list(">")},
            "unknown beside a version": {"table": winget_list("Unknown", "1.8.1")},
            "unknown and self-updating": {
                "table": winget_list("Unknown"),
                "explicit": ["jqlang.jq"],
            },
            "one version": {"table": winget_list("1.8.1")},
            "majors differ": {"table": winget_list("1.8.1", "2.0.0")},
        }
        result = run_pwsh_harness(WINGET_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "unknown": {"rows": ["Unknown"], "installed": None, "status": "unknown"},
                "below": {"rows": ["<"], "installed": None, "status": "unknown"},
                "above": {"rows": [">"], "installed": None, "status": "unknown"},
                "unknown beside a version": {
                    "rows": ["Unknown", "1.8.1"],
                    "installed": None,
                    "status": "unknown",
                },
                "unknown and self-updating": {
                    "rows": ["Unknown"],
                    "installed": None,
                    "status": "self-updating",
                },
                "one version": {"rows": ["1.8.1"], "installed": "1.8.1", "status": "outdated"},
                "majors differ": {
                    "rows": ["1.8.1", "2.0.0"],
                    "installed": None,
                    "status": "multiple",
                },
            },
        )

    def test_the_report_prints_the_row_and_install_leaves_the_tool_alone(self) -> None:
        result = run_pwsh_harness(UNREAD_APPLY_HARNESS, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        reported = [
            line.split() for line in result.stdout.splitlines() if line.startswith("LOG jq ")
        ]
        self.assertEqual([row[:3] for row in reported], [["LOG", "jq", "Unknown"]], result.stdout)
        self.assertIn("no version comparison is possible", result.stdout)
        self.assertNotIn("INSTALL ", result.stdout)
        self.assertNotIn("UPGRADE ", result.stdout)


DOTNET_SDK_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('Read-DotnetSdkList', 'Get-UntrackedDotnetSdk', 'Get-VersionKey', 'Compare-HostVersion')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$results = [ordered]@{}
foreach ($case in (Get-Content -Raw -LiteralPath $Cases | ConvertFrom-Json).PSObject.Properties) {
    $sdk = Read-DotnetSdkList -Line @($case.Value.listing)
    $results[$case.Name] = [ordered]@{ sdk = $sdk; untracked = (Get-UntrackedDotnetSdk -Installed $case.Value.installed -Sdk $sdk) }
}
$results | ConvertTo-Json -Depth 4
"""


def dotnet_sdks(*versions: str) -> list[str]:
    """The lines `dotnet --list-sdks` prints, one per version, each beside a made-up sdk directory."""
    return [f"{version} [X:\\dotnet\\sdk]" for version in versions]


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows installer's own functions"
)
class TestWindowsUntrackedDotnetSdk(unittest.TestCase):
    """An SDK newer than the one winget installed is named, whoever installed it."""

    def test_only_an_sdk_newer_than_winget_s_copy_is_named(self) -> None:
        cases = {
            "preview ahead of winget": {
                "installed": "10.0.303",
                "listing": dotnet_sdks("9.0.205", "10.0.303", "10.0.400-preview.0.1", "8.0.319"),
            },
            "newest of several ahead": {
                "installed": "10.0.111",
                "listing": dotnet_sdks("10.0.400-preview.0.1", "10.0.204", "10.0.303"),
            },
            "a release beside its own preview": {
                "installed": "10.0.303",
                "listing": dotnet_sdks("10.0.400-preview.0.1", "10.0.400"),
            },
            "a release listed ahead of its own preview": {
                "installed": "10.0.303",
                "listing": dotnet_sdks("10.0.400", "10.0.400-preview.0.1"),
            },
            "the later of two prereleases": {
                "installed": "10.0.303",
                "listing": dotnet_sdks("10.0.400-preview.0.1", "10.0.400-rc.2.1"),
            },
            "winget holds the newest": {
                "installed": "10.0.303",
                "listing": dotnet_sdks("8.0.319", "10.0.204", "10.0.303"),
            },
            "a preview of winget's own release": {
                "installed": "10.0.400",
                "listing": dotnet_sdks("10.0.400-rc.1.2", "10.0.400"),
            },
            "winget version unread": {
                "installed": "",
                "listing": dotnet_sdks("10.0.400"),
            },
            "listing carries no sdk": {
                "installed": "10.0.303",
                "listing": ["", "No .NET SDKs were found."],
            },
        }
        result = run_pwsh_harness(DOTNET_SDK_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            {name: value["untracked"] for name, value in json.loads(result.stdout).items()},
            {
                "preview ahead of winget": "10.0.400-preview.0.1",
                "newest of several ahead": "10.0.400-preview.0.1",
                "a release beside its own preview": "10.0.400",
                "a release listed ahead of its own preview": "10.0.400",
                "the later of two prereleases": "10.0.400-rc.2.1",
                "winget holds the newest": None,
                "a preview of winget's own release": None,
                "winget version unread": None,
                "listing carries no sdk": None,
            },
        )

    def test_the_listing_is_read_down_to_its_versions(self) -> None:
        cases = {"listing": {"installed": "", "listing": dotnet_sdks("8.0.319", "10.0.100-rc.1")}}
        result = run_pwsh_harness(DOTNET_SDK_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["listing"]["sdk"], ["8.0.319", "10.0.100-rc.1"])


DOTNET_NOTE_HARNESS = r"""
param([string]$Installer, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$PSNativeCommandUseErrorActionPreference = $false
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
$wanted = @('Add-ToolNote', 'note', 'Get-DotnetSdk', 'Read-DotnetSdkList', 'Get-UntrackedDotnetSdk', 'Get-VersionKey', 'Compare-HostVersion')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
$WITH_OPTIONAL = $true
$tool = @{ Name = 'dotnet'; Package = 'Microsoft.DotNet.SDK.10'; Probe = 'dotnet'; Optional = @() }
$results = [ordered]@{}
foreach ($case in (Get-Content -Raw -LiteralPath $Cases | ConvertFrom-Json).PSObject.Properties) {
    $env:PATH = $case.Value.path
    $NOTES = @()
    $NOTE_TEXTS = @()
    $state = @{ Package = 'Microsoft.DotNet.SDK.10'; Installed = $case.Value.installed; Rows = @(); Scope = @(); Status = 'current' }
    Add-ToolNote -Tool $tool -State $state
    $results[$case.Name] = @($NOTES)
}
$results | ConvertTo-Json -Depth 4
"""


def write_dotnet_stub(directory: Path, exit_code: int, *versions: str) -> None:
    """A `dotnet` on `directory` that prints `versions` as `--list-sdks` does, then exits `exit_code`.

    Any other argument exits 9 printing nothing, so a caller asking for the wrong listing reads none.
    """
    if sys.platform == "win32":
        lines = [
            "@echo off",
            'if not "%~1"=="--list-sdks" exit /b 9',
            *(f"echo {version} [X:\\dotnet\\sdk]" for version in versions),
        ]
        Path(directory, "dotnet.cmd").write_text(
            "\r\n".join([*lines, f"exit /b {exit_code}", ""]), encoding="ascii"
        )
    else:
        lines = [
            "#!/bin/sh",
            '[ "$1" = --list-sdks ] || exit 9',
            *(f"echo '{version} [/dotnet/sdk]'" for version in versions),
        ]
        stub = Path(directory, "dotnet")
        stub.write_text("\n".join([*lines, f"exit {exit_code}", ""]), encoding="ascii")
        stub.chmod(0o755)


@unittest.skipUnless(
    shutil.which("pwsh"), "needs pwsh to drive the Windows installer's own functions"
)
class TestWindowsUntrackedDotnetNote(unittest.TestCase):
    """The dotnet row's note, driven through `Add-ToolNote` with a stub `dotnet` alone on PATH."""

    def test_the_note_follows_what_dotnet_lists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = {name: Path(directory, name) for name in ("newer", "failing", "absent")}
            for path in paths.values():
                path.mkdir()
            write_dotnet_stub(paths["newer"], 0, "10.0.303", "10.0.400-preview.0.1", "10.0.400")
            write_dotnet_stub(paths["failing"], 3, "10.0.400")
            cases = {
                "newer": {"path": str(paths["newer"]), "installed": "10.0.303"},
                "failing": {"path": str(paths["failing"]), "installed": "10.0.303"},
                "absent": {"path": str(paths["absent"]), "installed": "10.0.303"},
                "unread": {"path": str(paths["newer"]), "installed": ""},
            }
            result = run_pwsh_harness(DOTNET_NOTE_HARNESS, cases)
        self.assertEqual(result.returncode, 0, result.stderr)
        notes = json.loads(result.stdout)
        self.assertEqual(len(notes["newer"]), 1, notes)
        self.assertTrue(notes["newer"][0].startswith("dotnet: dotnet --list-sdks holds 10.0.400,"))
        self.assertIn("newer than the 10.0.303 winget installed", notes["newer"][0])
        self.assertIn("other than Microsoft.DotNet.SDK.10", notes["newer"][0])
        self.assertEqual(
            {name: notes[name] for name in ("failing", "absent", "unread")},
            {"failing": [], "absent": [], "unread": []},
        )


if __name__ == "__main__":
    unittest.main()
