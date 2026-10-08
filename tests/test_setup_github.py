#!/usr/bin/env python3
"""Tests for the GitHub CLI half of the setup-github scripts, on both platforms.

Both scripts report the account `gh` is logged in as and whether its git protocol for github.com
is ssh, and neither changes that protocol unless the opt-in flag asks for it, `--configure`
included. Each script is driven through its own functions with `gh` stubbed, since a real run reads
and writes the configuration of the host it runs on.

Run as `python3 tests/test_setup_github.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LINUX_SCRIPT = ROOT / "host-setup" / "linux" / "setup-github.sh"
WINDOWS_SCRIPT = ROOT / "host-setup" / "windows" / "setup-github.ps1"

ACCOUNT = "example-user"
REMEDY = "gh config set git_protocol ssh --host github.com"

# A `gh` answering the three calls the script makes from two state files beside it, and logging each call.
# `account` holds the logged-in account, empty for none, and `protocol` the github.com git protocol.
GH_STUB = r"""#!/bin/sh
here=$(dirname "$0")
printf '%s\n' "$*" >>"$here/calls"
case "$*" in
"auth status --hostname github.com")
    account=$(cat "$here/account")
    if [ -z "$account" ]; then
        echo "You are not logged into any GitHub hosts. To log in, run: gh auth login" >&2
        exit 1
    fi
    echo "github.com"
    echo "  Logged in to github.com account $account (keyring)"
    echo "  - Git operations protocol: $(cat "$here/protocol")"
    ;;
"config get git_protocol --host github.com") cat "$here/protocol" ;;
"config set git_protocol ssh --host github.com") echo ssh >"$here/protocol" ;;
*) exit 9 ;;
esac
"""


def linux_functions() -> str:
    """`setup-github.sh` without its closing `main "$@"`, so a test can source its functions alone."""
    lines = LINUX_SCRIPT.read_text(encoding="utf-8").rstrip("\n").split("\n")
    if lines[-1] != 'main "$@"':
        raise AssertionError(f"setup-github.sh no longer ends with its main call: {lines[-1]!r}")
    return "\n".join(lines[:-1]) + "\n"


@unittest.skipUnless(sys.platform == "linux", "drives the Linux script's own functions")
class TestLinuxGitHubCli(unittest.TestCase):
    """`setup-github.sh`'s GitHub CLI report and opt-in, with a stub `gh` first on PATH."""

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.functions = self.dir / "functions.sh"
        self.functions.write_text(linux_functions(), encoding="utf-8")
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        stub = self.bin / "gh"
        stub.write_text(GH_STUB, encoding="ascii")
        stub.chmod(0o755)
        # A PATH holding no gh, and only the rm the script's exit trap cleans up with.
        self.empty = self.dir / "empty"
        self.empty.mkdir()
        rm = shutil.which("rm")
        if rm is None:
            raise AssertionError("no rm on PATH to link")
        (self.empty / "rm").symlink_to(rm)
        self.state(ACCOUNT, "https")

    def state(self, account: str, protocol: str) -> None:
        (self.bin / "account").write_text(account + "\n", encoding="ascii")
        (self.bin / "protocol").write_text(protocol + "\n", encoding="ascii")

    def protocol(self) -> str:
        return (self.bin / "protocol").read_text(encoding="ascii").strip()

    def calls(self) -> list[str]:
        log = self.bin / "calls"
        return log.read_text(encoding="ascii").splitlines() if log.exists() else []

    def run_bash(self, body: str) -> subprocess.CompletedProcess[str]:
        script = f'source "{self.functions}"\nPATH="{self.bin}:$PATH"\n{body}\n'
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )

    def test_report_names_the_account_and_an_https_protocol_with_its_remedy(self) -> None:
        result = self.run_bash("report_gh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"  [ ok ] authenticated as {ACCOUNT}\n", result.stdout)
        self.assertIn(
            f"  [    ] git protocol is ssh, it is https, set it with: {REMEDY},"
            " or --configure --gh-ssh-protocol\n",
            result.stdout,
        )
        self.assertNotIn("config set git_protocol ssh --host github.com", self.calls())

    def test_report_reads_ssh_as_done(self) -> None:
        self.state(ACCOUNT, "ssh")
        result = self.run_bash("report_gh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  [ ok ] git protocol is ssh\n", result.stdout)

    def test_report_names_the_login_when_gh_is_not_logged_in(self) -> None:
        self.state("", "https")
        result = self.run_bash("report_gh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "  [    ] authenticated, log in with: gh auth login --hostname github.com"
            " --git-protocol ssh\n",
            result.stdout,
        )

    def test_report_names_the_install_when_gh_is_absent(self) -> None:
        result = self.run_bash(f'PATH="{self.empty}"\nreport_gh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "  [    ] gh installed, which install-tools.sh --install gh provides\n", result.stdout
        )

    def test_configure_alone_leaves_the_protocol_as_it_found_it(self) -> None:
        result = self.run_bash(
            'parse_args --configure\necho "flag=$GH_SSH_PROTOCOL"\nconfigure_gh_protocol'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("flag=false\n", result.stdout)
        self.assertEqual(self.protocol(), "https")
        self.assertEqual(self.calls(), [])

    def test_the_opt_in_sets_ssh_for_github_com(self) -> None:
        result = self.run_bash(
            'parse_args --configure --gh-ssh-protocol\necho "flag=$GH_SSH_PROTOCOL"\n'
            "configure_gh_protocol"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("flag=true\n", result.stdout)
        self.assertIn("Was https, set to ssh", result.stdout)
        self.assertEqual(self.protocol(), "ssh")
        self.assertIn("config set git_protocol ssh --host github.com", self.calls())

    def test_the_opt_in_leaves_ssh_alone(self) -> None:
        self.state(ACCOUNT, "ssh")
        result = self.run_bash("GH_SSH_PROTOCOL=true\nconfigure_gh_protocol")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already ssh", result.stdout)
        self.assertNotIn("config set git_protocol ssh --host github.com", self.calls())

    def test_the_opt_in_under_dry_run_prints_the_command_and_changes_nothing(self) -> None:
        result = self.run_bash("GH_SSH_PROTOCOL=true\nDRY_RUN=true\nconfigure_gh_protocol")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"  [dry run] {REMEDY}\n", result.stdout)
        self.assertEqual(self.protocol(), "https")
        self.assertNotIn("config set git_protocol ssh --host github.com", self.calls())

    def test_the_opt_in_without_gh_refuses_before_changing_anything(self) -> None:
        # A sudo that is present and never run, so main reaches the gh check rather than the sudo one.
        (self.empty / "sudo").write_text("#!/bin/sh\nexit 9\n", encoding="ascii")
        (self.empty / "sudo").chmod(0o755)
        result = self.run_bash(
            f'PATH="{self.empty}"\nconfigure() {{ echo configured; }}\n'
            "main --configure --gh-ssh-protocol --yes"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("--gh-ssh-protocol", result.stderr)
        self.assertIn("gh is not installed", result.stderr)
        self.assertNotIn("configured", result.stdout)


WINDOWS_HARNESS = r"""
param([string]$Script, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw "setup-github.ps1 does not parse: $($errors[0].Message)" }
$wanted = @('log', 'info', 'step', 'ok', 'missing', 'run', 'Get-GhAccount', 'Get-GhProtocol', 'Show-GhStatus', 'Set-GhProtocol')
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
# The remedy command is read from the script itself rather than restated, so the two cannot drift apart.
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.AssignmentStatementAst] -and $statement.Left.Extent.Text -in '$GH_PROTOCOL_ARGUMENTS', '$GH_PROTOCOL_COMMAND') {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
function die { param([string]$Message) throw "die: $Message" }

# A gh answering the three calls the script makes from $script:Stub, and recording each call.
# A function rather than a file on PATH, so the same stub runs on every platform.
function gh {
    $script:Calls += ($args -join ' ')
    $global:LASTEXITCODE = 0
    $line = $args -join ' '
    switch ($line) {
        'auth status --hostname github.com' {
            if (-not $script:Stub.account) { $global:LASTEXITCODE = 1; return 'You are not logged into any GitHub hosts.' }
            return @('github.com', "  Logged in to github.com account $($script:Stub.account) (keyring)", "  - Git operations protocol: $($script:Stub.protocol)")
        }
        'config get git_protocol --host github.com' { return $script:Stub.protocol }
        'config set git_protocol ssh --host github.com' { $script:Stub.protocol = 'ssh'; return }
        default { $global:LASTEXITCODE = 9 }
    }
}

$results = [ordered]@{}
foreach ($case in (Get-Content $Cases -Raw | ConvertFrom-Json)) {
    $script:Stub = @{ account = $case.account; protocol = $case.protocol }
    $script:Calls = @()
    $script:GH_SSH_PROTOCOL = [bool]$case.optIn
    $script:DRY_RUN = [bool]$case.dryRun
    $output = & {
        if ($case.action -eq 'status') { Show-GhStatus } else { Set-GhProtocol }
    } 6>&1 | ForEach-Object { "$_" }
    $results[$case.name] = [ordered]@{ output = @($output); protocol = $script:Stub.protocol; calls = @($script:Calls) }
}
$results | ConvertTo-Json -Depth 4
"""

WINDOWS_CASES: list[dict[str, object]] = [
    {"name": "https", "action": "status", "account": ACCOUNT, "protocol": "https"},
    {"name": "ssh", "action": "status", "account": ACCOUNT, "protocol": "ssh"},
    {"name": "logged-out", "action": "status", "account": "", "protocol": "https"},
    {"name": "configure", "action": "configure", "account": ACCOUNT, "protocol": "https"},
    {
        "name": "opt-in",
        "action": "configure",
        "account": ACCOUNT,
        "protocol": "https",
        "optIn": True,
    },
    {
        "name": "opt-in-ssh",
        "action": "configure",
        "account": ACCOUNT,
        "protocol": "ssh",
        "optIn": True,
    },
    {
        "name": "opt-in-dry-run",
        "action": "configure",
        "account": ACCOUNT,
        "protocol": "https",
        "optIn": True,
        "dryRun": True,
    },
]


def run_pwsh(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )


@unittest.skipUnless(shutil.which("pwsh"), "needs pwsh to drive the Windows script's own functions")
class TestWindowsGitHubCli(unittest.TestCase):
    """`setup-github.ps1`'s GitHub CLI report and opt-in, with `gh` stubbed as a function."""

    results: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory, "harness.ps1")
            harness.write_text(WINDOWS_HARNESS, encoding="utf-8")
            cases = Path(directory, "cases.json")
            # Every case carries every field, since the harness reads them under strict mode.
            full: list[dict[str, object]] = [
                {"optIn": False, "dryRun": False, **case} for case in WINDOWS_CASES
            ]
            cases.write_text(json.dumps(full), encoding="utf-8")
            result = run_pwsh(
                ["-File", str(harness), "-Script", str(WINDOWS_SCRIPT), "-Cases", str(cases)]
            )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        cls.results = json.loads(result.stdout)

    def output(self, name: str) -> list[str]:
        return self.results[name]["output"]

    def test_report_names_the_account_and_an_https_protocol_with_its_remedy(self) -> None:
        self.assertIn(f"  [ ok ] authenticated as {ACCOUNT}", self.output("https"))
        self.assertIn(
            f"  [    ] git protocol is ssh, it is https, set it with: {REMEDY},"
            " or -Configure -GhSshProtocol",
            self.output("https"),
        )
        self.assertEqual(self.results["https"]["protocol"], "https")

    def test_report_reads_ssh_as_done(self) -> None:
        self.assertIn("  [ ok ] git protocol is ssh", self.output("ssh"))

    def test_report_names_the_login_when_gh_is_not_logged_in(self) -> None:
        self.assertIn(
            "  [    ] authenticated, log in with: gh auth login --hostname github.com"
            " --git-protocol ssh",
            self.output("logged-out"),
        )

    def test_configure_alone_leaves_the_protocol_as_it_found_it(self) -> None:
        self.assertEqual(self.results["configure"]["protocol"], "https")
        self.assertEqual(self.results["configure"]["calls"], [])

    def test_the_opt_in_sets_ssh_for_github_com(self) -> None:
        self.assertEqual(self.results["opt-in"]["protocol"], "ssh")
        self.assertIn(
            "config set git_protocol ssh --host github.com", self.results["opt-in"]["calls"]
        )
        self.assertIn("  Was https, set to ssh", self.output("opt-in"))

    def test_the_opt_in_leaves_ssh_alone(self) -> None:
        self.assertIn("  Already ssh", self.output("opt-in-ssh"))
        self.assertNotIn(
            "config set git_protocol ssh --host github.com", self.results["opt-in-ssh"]["calls"]
        )

    def test_the_opt_in_under_dry_run_prints_the_command_and_changes_nothing(self) -> None:
        self.assertIn(f"  [dry run] {REMEDY}", self.output("opt-in-dry-run"))
        self.assertEqual(self.results["opt-in-dry-run"]["protocol"], "https")

    def test_help_names_the_opt_in(self) -> None:
        result = run_pwsh(["-File", str(WINDOWS_SCRIPT), "-Help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-GhSshProtocol", result.stdout)


if __name__ == "__main__":
    unittest.main()
