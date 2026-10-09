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

SECOND = "other-user"


def auth_status(
    account: str, *, broken: bool = False, second: str = "", active_only: bool = False
) -> tuple[str, int]:
    """What `gh auth status --hostname github.com` prints and exits with, `--active` keeping the active entry.

    `account` is the active account, empty for none, `broken` makes its token invalid, and `second` adds
    an inactive account whose token works.
    """
    if not account:
        return "You are not logged into any GitHub hosts. To log in, run: gh auth login\n", 1
    lines = ["github.com"]
    if broken:
        lines += [
            f"  X Failed to log in to github.com account {account} (keyring)",
            "  - Active account: true",
            "  - The token in keyring is invalid.",
        ]
    else:
        lines += [
            f"  \u2713 Logged in to github.com account {account} (keyring)",
            "  - Active account: true",
            "  - Git operations protocol: https",
        ]
    if second and not active_only:
        lines += [
            "",
            f"  \u2713 Logged in to github.com account {second} (keyring)",
            "  - Active account: false",
            "  - Git operations protocol: https",
        ]
    return "\n".join(lines) + "\n", 1 if broken else 0


# A `gh` answering the calls the script makes from state files beside it, and logging each call.
# `active` and `all` hold what auth status prints with and without --active, `old` makes --active unknown, and `protocol` holds the github.com git protocol.
GH_STUB = r"""#!/bin/sh
here=$(dirname "$0")
printf '%s\n' "$*" >>"$here/calls"
case "$*" in
"auth status --active --hostname github.com")
    if [ -e "$here/old" ]; then
        echo "unknown flag: --active" >&2
        exit 1
    fi
    cat "$here/active"
    exit "$(cat "$here/active.rc")"
    ;;
"auth status --hostname github.com")
    cat "$here/all"
    exit "$(cat "$here/all.rc")"
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


def run_sourced(functions: Path, bin_dir: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Run `body` in bash after sourcing `functions`, with `bin_dir` first on PATH."""
    script = f'source "{functions}"\nPATH="{bin_dir}:$PATH"\n{body}\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )


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

    def state(
        self,
        account: str,
        protocol: str,
        *,
        broken: bool = False,
        second: str = "",
        old_gh: bool = False,
    ) -> None:
        for name, active_only in (("active", True), ("all", False)):
            text, code = auth_status(account, broken=broken, second=second, active_only=active_only)
            (self.bin / name).write_text(text, encoding="utf-8")
            (self.bin / f"{name}.rc").write_text(f"{code}\n", encoding="ascii")
        (self.bin / "old").unlink(missing_ok=True)
        if old_gh:
            (self.bin / "old").write_text("", encoding="ascii")
        (self.bin / "protocol").write_text(protocol + "\n", encoding="ascii")

    def protocol(self) -> str:
        return (self.bin / "protocol").read_text(encoding="ascii").strip()

    def calls(self) -> list[str]:
        log = self.bin / "calls"
        return log.read_text(encoding="ascii").splitlines() if log.exists() else []

    def run_bash(self, body: str) -> subprocess.CompletedProcess[str]:
        return run_sourced(self.functions, self.bin, body)

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
        self.assertIn(
            "  [    ] git protocol is ssh, unchecked until gh is logged in,"
            " and the login above sets it\n",
            result.stdout,
        )
        self.assertNotIn(REMEDY, result.stdout)

    def test_a_broken_active_account_reads_as_logged_out_beside_a_working_one(self) -> None:
        for old_gh in (False, True):
            with self.subTest(old_gh=old_gh):
                self.state(ACCOUNT, "https", broken=True, second=SECOND, old_gh=old_gh)
                result = self.run_bash("report_gh")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("authenticated as", result.stdout)
                self.assertIn("  [    ] authenticated, log in with:", result.stdout)

    def test_the_active_account_is_named_beside_another(self) -> None:
        for old_gh in (False, True):
            with self.subTest(old_gh=old_gh):
                self.state(ACCOUNT, "https", second=SECOND, old_gh=old_gh)
                result = self.run_bash("report_gh")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"  [ ok ] authenticated as {ACCOUNT}\n", result.stdout)

    def test_a_gh_without_active_falls_back_to_the_full_listing(self) -> None:
        self.state(ACCOUNT, "https", old_gh=True)
        result = self.run_bash("report_gh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"  [ ok ] authenticated as {ACCOUNT}\n", result.stdout)
        self.assertEqual(
            self.calls()[:2],
            ["auth status --active --hostname github.com", "auth status --hostname github.com"],
        )

    def test_the_opt_in_with_a_broken_active_account_writes_nothing(self) -> None:
        self.state(ACCOUNT, "https", broken=True, second=SECOND)
        result = self.run_bash("GH_SSH_PROTOCOL=true\nconfigure_gh_protocol")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gh is not logged in, so its git protocol was not set", result.stderr)
        self.assertEqual(self.protocol(), "https")
        self.assertNotIn("config set git_protocol ssh --host github.com", self.calls())

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
        self.assertNotIn("set to ssh", result.stdout)
        self.assertEqual(self.protocol(), "https")
        self.assertNotIn("config set git_protocol ssh --host github.com", self.calls())

    def test_the_opt_in_without_gh_warns_and_carries_on(self) -> None:
        result = self.run_bash(
            f'PATH="{self.empty}"\nGH_SSH_PROTOCOL=true\nconfigure_gh_protocol\necho carried-on'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gh is not installed, so its git protocol was not set", result.stderr)
        self.assertIn("carried-on\n", result.stdout)

    def test_the_opt_in_on_a_logged_out_gh_writes_nothing(self) -> None:
        self.state("", "https")
        result = self.run_bash("GH_SSH_PROTOCOL=true\nconfigure_gh_protocol")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gh is not logged in, so its git protocol was not set", result.stderr)
        self.assertEqual(self.protocol(), "https")
        self.assertNotIn("config set git_protocol ssh --host github.com", self.calls())

    def test_report_names_a_protocol_gh_did_not_report(self) -> None:
        self.state(ACCOUNT, "")
        result = self.run_bash("report_gh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"  [    ] git protocol is ssh, gh did not report one, set it with: {REMEDY},"
            " or --configure --gh-ssh-protocol\n",
            result.stdout,
        )


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
function warn { param([string]$Message) Write-Host "WARNING: $Message" }

# A gh answering the three calls the script makes from $script:Stub, and recording each call.
# Installed as the gh function per case rather than as a file on PATH, so the same stub runs on every platform.
$GhStub = {
    $script:Calls += ($args -join ' ')
    $global:LASTEXITCODE = 0
    $line = $args -join ' '
    switch ($line) {
        'auth status --active --hostname github.com' {
            if ($script:Stub.oldGh) { $global:LASTEXITCODE = 1; return 'unknown flag: --active' }
            $global:LASTEXITCODE = $script:Stub.activeRc
            return $script:Stub.active
        }
        'auth status --hostname github.com' {
            $global:LASTEXITCODE = $script:Stub.allRc
            return $script:Stub.all
        }
        'config get git_protocol --host github.com' { return $script:Stub.protocol }
        'config set git_protocol ssh --host github.com' { $script:Stub.protocol = 'ssh'; return }
        default { $global:LASTEXITCODE = 9 }
    }
}
$empty = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $empty | Out-Null

$results = [ordered]@{}
foreach ($case in (Get-Content $Cases -Raw | ConvertFrom-Json)) {
    $script:Stub = @{ protocol = $case.protocol; oldGh = $case.oldGh; active = $case.active; activeRc = $case.activeRc; all = $case.all; allRc = $case.allRc }
    $script:Calls = @()
    $script:GH_SSH_PROTOCOL = [bool]$case.optIn
    $script:DRY_RUN = [bool]$case.dryRun
    # An absent gh is no stub and a PATH holding nothing.
    $path = $env:PATH
    if ($case.ghAbsent) {
        Remove-Item function:gh -ErrorAction SilentlyContinue
        $env:PATH = $empty
    } else {
        Set-Item function:script:gh $GhStub
    }
    $output = & {
        if ($case.action -eq 'status') { Show-GhStatus } else { Set-GhProtocol }
    } 6>&1 | ForEach-Object { "$_" }
    $env:PATH = $path
    $results[$case.name] = [ordered]@{ output = @($output); protocol = $script:Stub.protocol; calls = @($script:Calls) }
}
Remove-Item $empty
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
    {"name": "unreported", "action": "status", "account": ACCOUNT, "protocol": ""},
    {
        "name": "broken-active",
        "action": "status",
        "account": ACCOUNT,
        "protocol": "https",
        "broken": True,
        "second": SECOND,
    },
    {
        "name": "broken-active-old",
        "action": "status",
        "account": ACCOUNT,
        "protocol": "https",
        "broken": True,
        "second": SECOND,
        "oldGh": True,
    },
    {
        "name": "two-accounts-old",
        "action": "status",
        "account": ACCOUNT,
        "protocol": "https",
        "second": SECOND,
        "oldGh": True,
    },
    {
        "name": "opt-in-broken-active",
        "action": "configure",
        "account": ACCOUNT,
        "protocol": "https",
        "broken": True,
        "second": SECOND,
        "optIn": True,
    },
    {
        "name": "absent",
        "action": "status",
        "account": ACCOUNT,
        "protocol": "https",
        "ghAbsent": True,
    },
    {
        "name": "opt-in-absent",
        "action": "configure",
        "account": ACCOUNT,
        "protocol": "https",
        "optIn": True,
        "ghAbsent": True,
    },
    {
        "name": "opt-in-logged-out",
        "action": "configure",
        "account": "",
        "protocol": "https",
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


def windows_case(case: dict[str, object]) -> dict[str, object]:
    """A case with every field the harness reads under strict mode, its auth status rendered from its accounts."""
    full: dict[str, object] = {
        "optIn": False,
        "dryRun": False,
        "ghAbsent": False,
        "oldGh": False,
        "broken": False,
        "second": "",
        **case,
    }
    for name, active_only in (("active", True), ("all", False)):
        text, code = auth_status(
            str(full["account"]),
            broken=bool(full["broken"]),
            second=str(full["second"]),
            active_only=active_only,
        )
        full[name] = text
        full[f"{name}Rc"] = code
    return full


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
            full = [windows_case(case) for case in WINDOWS_CASES]
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
        self.assertIn(
            "  [    ] git protocol is ssh, unchecked until gh is logged in,"
            " and the login above sets it",
            self.output("logged-out"),
        )
        self.assertFalse(any(REMEDY in line for line in self.output("logged-out")))

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
        self.assertFalse(any("set to ssh" in line for line in self.output("opt-in-dry-run")))
        self.assertEqual(self.results["opt-in-dry-run"]["protocol"], "https")

    def test_report_names_a_protocol_gh_did_not_report(self) -> None:
        self.assertIn(
            f"  [    ] git protocol is ssh, gh did not report one, set it with: {REMEDY},"
            " or -Configure -GhSshProtocol",
            self.output("unreported"),
        )

    def test_report_names_the_install_when_gh_is_absent(self) -> None:
        self.assertIn(
            "  [    ] gh installed, which install-tools.ps1 -Install gh provides",
            self.output("absent"),
        )

    def test_the_opt_in_without_gh_warns_and_carries_on(self) -> None:
        self.assertIn(
            "WARNING: gh is not installed, so its git protocol was not set."
            " install-tools.ps1 -Install gh installs it.",
            self.output("opt-in-absent"),
        )

    def test_the_opt_in_on_a_logged_out_gh_writes_nothing(self) -> None:
        self.assertEqual(self.results["opt-in-logged-out"]["protocol"], "https")
        self.assertNotIn(
            "config set git_protocol ssh --host github.com",
            self.results["opt-in-logged-out"]["calls"],
        )
        self.assertTrue(
            any("gh is not logged in" in line for line in self.output("opt-in-logged-out"))
        )

    def test_a_broken_active_account_reads_as_logged_out_beside_a_working_one(self) -> None:
        for name in ("broken-active", "broken-active-old"):
            with self.subTest(name=name):
                self.assertFalse(any("authenticated as" in line for line in self.output(name)))
                self.assertTrue(
                    any(
                        line.startswith("  [    ] authenticated, log in with:")
                        for line in self.output(name)
                    )
                )

    def test_a_gh_without_active_falls_back_to_the_full_listing(self) -> None:
        self.assertIn(f"  [ ok ] authenticated as {ACCOUNT}", self.output("two-accounts-old"))
        self.assertEqual(
            self.results["two-accounts-old"]["calls"][:2],
            ["auth status --active --hostname github.com", "auth status --hostname github.com"],
        )

    def test_the_opt_in_with_a_broken_active_account_writes_nothing(self) -> None:
        self.assertEqual(self.results["opt-in-broken-active"]["protocol"], "https")
        self.assertNotIn(
            "config set git_protocol ssh --host github.com",
            self.results["opt-in-broken-active"]["calls"],
        )

    def test_help_names_the_opt_in(self) -> None:
        result = run_pwsh(["-File", str(WINDOWS_SCRIPT), "-Help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-GhSshProtocol", result.stdout)


def function_body(text: str, opener: str) -> str:
    """The text from `opener` to the closing brace at column 0 that ends the function it opens."""
    start = text.index(opener)
    return text[start : text.index("\n}\n", start)]


@unittest.skipUnless(sys.platform == "linux", "drives the Linux script's own functions")
class TestPackageInstalled(unittest.TestCase):
    """`setup-github.sh`'s installed-package check, with a stub `dpkg-query` first on PATH."""

    def check(self, status: str, exit_code: int = 0) -> int:
        """The exit status of `package_installed git` when dpkg-query prints `status`."""
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        functions = directory / "functions.sh"
        functions.write_text(linux_functions(), encoding="utf-8")
        stub = directory / "dpkg-query"
        stub.write_text(f"#!/bin/sh\nprintf '%s' '{status}'\nexit {exit_code}\n", encoding="ascii")
        stub.chmod(0o755)
        return run_sourced(functions, directory, "package_installed git").returncode

    def test_an_installed_package_reads_as_installed(self) -> None:
        self.assertEqual(self.check("install ok installed"), 0)

    def test_a_held_package_reads_as_installed(self) -> None:
        self.assertEqual(self.check("hold ok installed"), 0)

    def test_a_removed_package_reads_as_missing(self) -> None:
        self.assertNotEqual(self.check("deinstall ok config-files"), 0)

    def test_an_unknown_package_reads_as_missing(self) -> None:
        self.assertNotEqual(self.check("", 1), 0)


class TestCallSites(unittest.TestCase):
    """Each script's status and configure actions reach the GitHub CLI functions the tests drive."""

    def test_the_linux_actions_call_the_gh_functions(self) -> None:
        text = LINUX_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("\n    report_gh\n", function_body(text, "\nstatus() {"))
        self.assertIn("\n    configure_gh_protocol\n", function_body(text, "\nconfigure() {"))

    def test_the_windows_actions_call_the_gh_functions(self) -> None:
        text = WINDOWS_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("\n    Show-GhStatus\n", function_body(text, "\nfunction Show-Status {"))
        self.assertIn(
            "\n    Set-GhProtocol\n", function_body(text, "\nfunction Invoke-Configure {")
        )


if __name__ == "__main__":
    unittest.main()
