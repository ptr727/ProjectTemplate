#!/usr/bin/env python3
"""Tests for the GitHub CLI half of the setup-github scripts, on both platforms.

Both scripts report the account `gh` is logged in as and whether its git protocol for github.com
is ssh. Neither script changes that protocol unless the opt-in flag asks for it, `--configure`
included. Each script is driven through its own functions with the `gh` command stubbed. The reason is that a
real run reads and writes the configuration of the host it runs on.

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
"auth status --active --help")
    if [ -e "$here/old" ]; then
        echo "unknown flag: --active" >&2
        exit 1
    fi
    ;;
"auth status --active --hostname github.com")
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
            ["auth status --active --help", "auth status --hostname github.com"],
        )

    def test_a_gh_taking_active_reads_the_active_listing(self) -> None:
        self.state(ACCOUNT, "https", second=SECOND)
        result = self.run_bash("report_gh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls()[:2],
            ["auth status --active --help", "auth status --active --hostname github.com"],
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


def windows_harness(params: str, functions: list[str], body: str) -> str:
    """A pwsh harness taking `params` that dot-sources `functions` out of the script, then runs `body`."""
    wanted = ", ".join(f"'{name}'" for name in functions)
    return f"""
param([string]$Script, {params})
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw "setup-github.ps1 does not parse: $($errors[0].Message)" }}
$wanted = @({wanted})
foreach ($definition in $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }}, $true)) {{
    . ([scriptblock]::Create($definition.Extent.Text))
}}
{body}"""


WINDOWS_FUNCTIONS = [
    "log",
    "info",
    "step",
    "ok",
    "missing",
    "run",
    "Get-GhAccount",
    "Get-GhProtocol",
    "Show-GhStatus",
    "Set-GhProtocol",
]
WINDOWS_BODY = r"""
# The remedy command is read from the script itself rather than restated, so the two cannot drift apart.
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.AssignmentStatementAst] -and $statement.Left.Extent.Text -in '$GH_PROTOCOL_ARGUMENTS', '$GH_PROTOCOL_COMMAND') {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
function die { param([string]$Message) throw "die: $Message" }
function warn { param([string]$Message) Write-Host "WARNING: $Message" }

# A gh answering the calls the script makes from $script:Stub, and recording each call.
# Installed as the gh function per case rather than as a file on PATH, so the same stub runs on every platform.
$GhStub = {
    $script:Calls += ($args -join ' ')
    $global:LASTEXITCODE = 0
    $line = $args -join ' '
    switch ($line) {
        'auth status --active --help' {
            if ($script:Stub.oldGh) { $global:LASTEXITCODE = 1; return 'unknown flag: --active' }
            return
        }
        'auth status --active --hostname github.com' {
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


KNOWN_HOST_BODY = r"""
function ssh-keygen { $global:LASTEXITCODE = $Code; return $Output }
$script:DRY_RUN = $true
"known=$(Test-GitHubKnownHost)"
Add-KnownHost 6>&1 | ForEach-Object { "$_" }
"""


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
class TestWindowsKnownHost(unittest.TestCase):
    """Whether github.com is known is `ssh-keygen -F`'s exit status alone, for every caller."""

    def add_known_host(self, code: int, output: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory, "harness.ps1")
            text = windows_harness(
                "[int]$Code, [string]$Output",
                ["log", "info", "step", "Test-GitHubKnownHost", "Add-KnownHost"],
                KNOWN_HOST_BODY,
            )
            harness.write_text(text, encoding="utf-8")
            arguments = ["-Script", str(WINDOWS_SCRIPT), "-Code", str(code), "-Output", output]
            result = run_pwsh(["-File", str(harness), *arguments])
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_found_host_is_already_known(self) -> None:
        output = self.add_known_host(0, "# Host github.com found: line 1")
        self.assertIn("known=True", output)
        self.assertIn("Already known", output)

    def test_a_failure_whose_output_says_found_is_not_known(self) -> None:
        output = self.add_known_host(255, "known_hosts not found")
        self.assertIn("known=False", output)
        self.assertNotIn("Already known", output)
        self.assertIn("Adding the github.com host keys", output)


@unittest.skipUnless(shutil.which("pwsh"), "needs pwsh to drive the Windows script's own functions")
class TestWindowsGitHubCli(unittest.TestCase):
    """`setup-github.ps1`'s GitHub CLI report and opt-in, with `gh` stubbed as a function."""

    results: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory, "harness.ps1")
            text = windows_harness("[string]$Cases", WINDOWS_FUNCTIONS, WINDOWS_BODY)
            harness.write_text(text, encoding="utf-8")
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
            ["auth status --active --help", "auth status --hostname github.com"],
        )

    def test_a_gh_taking_active_reads_the_active_listing(self) -> None:
        self.assertEqual(
            self.results["broken-active"]["calls"][:2],
            ["auth status --active --help", "auth status --active --hostname github.com"],
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


PUBLIC_KEY = "ssh-ed25519 AAAAexampleconstructedkeybody"
FINGERPRINT = "SHA256:exampleconstructedfingerprint"
ADD_REFRESH = "gh auth refresh -h github.com -s admin:public_key,admin:ssh_signing_key"

# What the isolated, non-prompting managed-key probe prints for each state it can meet, as `ssh -v` would.
# The ssh client sends the public half first, as an offer, and loads the private half only after the server says it accepts the key.
# So only "offered" is positive evidence of a key registered nowhere: the offer, then the refusal, and no accept line.
# A registered key whose private half then fails to load prints the offer, the accept line, the load failure and the same refusal.
# Under BatchMode ssh never prompts for a passphrase, so such a key with no agent prints the offer, the accept line and the refusal.
DENIAL = "git@github.com: Permission denied (publickey)."
OFFER = f"debug1: Offering public key: KEYPATH ED25519 {FINGERPRINT} explicit"
ACCEPT = f"debug1: Server accepts key: KEYPATH ED25519 {FINGERPRINT} explicit"
SSH_STATES: dict[str, str] = {
    "offered": f"{OFFER}\n{DENIAL}",
    "wrong-offer": f"debug1: Offering public key: KEYPATH ED25519 SHA256:other explicit\n{DENIAL}",
    "passphrase": f"{OFFER}\n{ACCEPT}\n{DENIAL}",
    "unprotected": f'{OFFER}\n{ACCEPT}\nLoad key "KEYPATH": bad permissions\n{DENIAL}',
    "silent": "",
}
CANNOT_TELL = ("wrong-offer", "passphrase", "unprotected", "silent")
HOST_MISMATCH = f"gh is logged in as {ACCOUNT}, while this host's ssh identity answers as {SECOND}"

# A gh answering the calls the registration makes, recording each, and keeping the registered kinds in $STATE.
# Both gh and ssh are shell functions, so nothing reaches a real account and no real host is read.
# The public key lists stay empty throughout, so a check that read them instead of gh's would report a missing key.
LINUX_REGISTER_BODY = r"""
ssh-keygen() { printf '256 %s comment (ED25519)\n' "$FINGERPRINT"; }
gh() {
    printf '%s\n' "$*" >>"$CALLS"
    case "$*" in
    "auth status --active --help") return 0 ;;
    "auth status --active --hostname github.com")
        [[ -n $ACCOUNT ]] || return 1
        printf 'github.com\n  \xe2\x9c\x93 Logged in to github.com account %s (keyring)\n  - Active account: true\n' "$ACCOUNT"
        ;;
    "ssh-key list")
        [[ $(printenv GH_HOST) == github.com ]] || return 9
        [[ ${LIST_RC:-0} == 0 ]] || return "$LIST_RC"
        local kind
        for kind in $(<"$STATE"); do
            [[ $kind == signing && ${HIDE_SIGNING:-no} == yes ]] && continue
            printf '%s\t%s\t%s\t2024-01-01\t1\n' "${LIST_TITLE:-example-host}" "$PUB" "$kind"
        done
        ;;
    "ssh-key add "*)
        [[ $(printenv GH_HOST) == github.com ]] || return 9
        [[ ${ADD_RC:-0} == 0 && ( $* != *"--type signing" || ${ADD_SIGNING_RC:-0} == 0 ) ]] || return "${ADD_SIGNING_RC:-$ADD_RC}"
        printf '%s\n' "${*##* }" >>"$STATE"
        ;;
    *) return 9 ;;
    esac
}
ssh() {
    printf 'ssh %s\n' "$*" >>"$CALLS"
    case "$*" in
    *BatchMode=yes*)
        if [[ -n $BATCH_USER ]]; then
            printf 'Hi %s! You have successfully authenticated.\n' "$BATCH_USER"
        else
            printf '%s' "$SSH_OUTPUT"
        fi
        ;;
    *"-i "*) [[ -z $MANAGED ]] || printf 'Hi %s! You have successfully authenticated.\n' "$MANAGED" ;;
    *) [[ -z $GENERAL ]] || printf 'Hi %s! You have successfully authenticated.\n' "$GENERAL" ;;
    esac
}
fetch() {
    [[ ${PUBLIC_ERROR:-no} == no ]] || return 1
    case "$1" in
    */ssh_signing_keys)
        if [[ $PUBLIC_SIGNING == yes ]]; then
            printf '[{"id": 1, "key": "%s", "title": "t"}]' "$PUB"
        else
            printf '[]'
        fi
        ;;
    esac
}
"""


@unittest.skipUnless(sys.platform == "linux", "drives the Linux script's own functions")
class TestLinuxKeyRegistration(unittest.TestCase):
    """`setup-github.sh` registers both keys through a logged-in gh, and otherwise leaves the paste."""

    def register(
        self,
        *,
        account: str = ACCOUNT,
        registered: str = "",
        add_rc: int = 0,
        dry_run: bool = False,
        managed: str = "",
        key: bool = True,
        ssh_state: str = "offered",
        batch_user: str | None = None,
        general: str = "",
        list_rc: int = 0,
        add_signing_rc: int | None = None,
        public_signing: bool = False,
        hide_signing: bool = False,
        public_error: bool = False,
        list_title: str = "",
        then: str = "",
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.tmp = directory
        functions = directory / "functions.sh"
        functions.write_text(linux_functions(), encoding="utf-8")
        home = directory / "home"
        (home / ".ssh").mkdir(parents=True)
        if key:
            (home / ".ssh" / "id_ed25519").write_text("", encoding="ascii")
            (home / ".ssh" / "id_ed25519.pub").write_text(
                PUBLIC_KEY + " comment\n", encoding="ascii"
            )
        calls = directory / "calls"
        state = directory / "state"
        state.write_text(registered.replace("auth", "authentication") + "\n", encoding="ascii")
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "HOSTNAME": "example-host",
            "CALLS": str(calls),
            "STATE": str(state),
            "ACCOUNT": account,
            "ADD_RC": str(add_rc),
            "PUB": PUBLIC_KEY,
            "FINGERPRINT": FINGERPRINT,
            "MANAGED": managed,
            "BATCH_USER": managed if batch_user is None else batch_user,
            "GENERAL": general,
            "LIST_RC": str(list_rc),
            "ADD_SIGNING_RC": str(add_rc if add_signing_rc is None else add_signing_rc),
            "PUBLIC_SIGNING": "yes" if public_signing else "no",
            "HIDE_SIGNING": "yes" if hide_signing else "no",
            "PUBLIC_ERROR": "yes" if public_error else "no",
            "LIST_TITLE": list_title,
            "SSH_OUTPUT": SSH_STATES[ssh_state].replace(
                "KEYPATH", str(home / ".ssh" / "id_ed25519")
            ),
        }
        dry = "DRY_RUN=true\n" if dry_run else ""
        script = f'source "{functions}"\nMODE=configure\n{LINUX_REGISTER_BODY}\n{dry}register_keys_through_gh\n{then}\n'
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
            env=env,
        )
        lines = calls.read_text().splitlines() if calls.exists() else []
        return result, [c for c in lines if "ssh-key add" in c]

    def test_both_missing_keys_are_registered_with_their_types_in_order(self) -> None:
        result, adds = self.register()
        self.assertEqual(result.returncode, 0, result.stderr)
        home_key = adds[0].split(" ")[2]
        self.assertTrue(home_key.endswith("/.ssh/id_ed25519.pub"), adds)
        self.assertEqual(
            [a.replace(home_key, "KEY") for a in adds],
            [
                "ssh-key add KEY --title example-host --type authentication",
                "ssh-key add KEY --title example-host --type signing",
            ],
        )

    def test_only_the_missing_kind_is_registered(self) -> None:
        result, adds = self.register(registered="auth")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.tmp / "calls").read_text().splitlines()
        self.assertEqual(calls.count("ssh-key list"), 2, calls)
        self.assertEqual(len(adds), 1, adds)
        self.assertTrue(adds[0].endswith("--type signing"), adds)

    def test_a_host_titled_after_a_kind_does_not_pass_for_it(self) -> None:
        result, adds = self.register(registered="auth", list_title="signing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(adds), 1, adds)
        self.assertTrue(adds[0].endswith("--type signing"), adds)

    def test_a_rerun_with_both_registered_changes_nothing(self) -> None:
        result, adds = self.register(registered="auth signing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adds, [])
        self.assertIn("Already registered as the authentication key", result.stdout)
        self.assertIn("Already registered as the signing key", result.stdout)

    def test_a_logged_out_gh_registers_nothing_and_never_logs_in(self) -> None:
        result, adds = self.register(account="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adds, [])
        self.assertNotIn("Registering the key through gh", result.stdout)
        calls = (self.tmp / "calls").read_text().splitlines()
        self.assertFalse([c for c in calls if c.startswith(("auth login", "auth refresh"))], calls)

    def test_a_refused_authentication_add_skips_the_signing_add(self) -> None:
        result, adds = self.register(add_rc=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(adds), 1, adds)
        self.assertTrue(adds[0].endswith("--type authentication"), adds)
        self.assertIn(ADD_REFRESH, result.stderr)
        self.assertIn("in use on another account", result.stderr)

    def test_a_dry_run_prints_the_commands_and_registers_nothing(self) -> None:
        result, adds = self.register(dry_run=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adds, [])
        self.assertIn("[dry run] gh ssh-key add", result.stdout)

    def test_a_dry_run_without_a_key_says_what_it_would_do_without_warning(self) -> None:
        result, adds = self.register(dry_run=True, key=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adds, [])
        self.assertIn("--type authentication", result.stdout)
        self.assertIn("--type signing", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_a_key_belonging_to_another_account_is_not_registered_through_gh(self) -> None:
        result, adds = self.register(managed=SECOND)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adds, [])
        self.assertIn(f"the managed key belongs to {SECOND}", result.stderr)

    def test_a_key_authenticating_as_the_gh_account_is_registered(self) -> None:
        result, adds = self.register(managed=ACCOUNT, registered="auth")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(adds), 1, adds)

    def test_a_host_identity_answering_as_another_account_registers_nothing(self) -> None:
        paste = "GITHUB_USER=x\nsigning_registration_state || registration_needed signing Signing"
        result, adds = self.register(general=SECOND, then=paste)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adds, [])
        self.assertIn(HOST_MISMATCH, result.stderr)
        self.assertNotIn("Registering the key through gh", result.stdout)
        self.assertIn('Set the key type to "Signing key"', result.stdout)
        self.assertIn(PUBLIC_KEY, result.stdout)

    def test_a_host_identity_answering_as_another_account_refuses_beside_a_key_on_gh_s(
        self,
    ) -> None:
        result, adds = self.register(managed=ACCOUNT, registered="auth", general=SECOND)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adds, [])
        self.assertIn(HOST_MISMATCH, result.stderr)

    def test_gh_listing_the_authentication_key_proves_whose_it_is(self) -> None:
        result, adds = self.register(registered="auth", batch_user="", ssh_state="passphrase")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(adds), 1, adds)
        self.assertTrue(adds[0].endswith("--type signing"), adds)
        self.assertNotIn("Could not tell", result.stderr)

    def test_a_configured_host_is_not_told_to_paste(self) -> None:
        result, adds = self.register(
            registered="auth signing", batch_user="", ssh_state="passphrase", general=SECOND
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adds, [])
        self.assertNotIn("paste", result.stderr)

    def test_a_listing_without_the_signing_scope_asks_the_public_list(self) -> None:
        result, adds = self.register(
            registered="auth signing", hide_signing=True, public_signing=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adds, [])
        self.assertEqual(result.stderr, "")
        self.assertIn("Already registered as the signing key", result.stdout)

    def test_an_unreadable_public_list_leaves_the_signing_add_to_gh(self) -> None:
        result, adds = self.register(
            registered="auth signing", hide_signing=True, public_error=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(adds), 1, adds)
        self.assertTrue(adds[0].endswith("--type signing"), adds)
        self.assertNotIn("gh could not list its keys", result.stderr)

    def test_the_host_s_own_probe_is_asked_again_only_after_an_authentication_add(self) -> None:
        for general, registered, probes in (
            (SECOND, "", 1),
            (ACCOUNT, "", 2),
            (ACCOUNT, "auth", 1),
        ):
            with self.subTest(general=general, registered=registered):
                result, _ = self.register(
                    general=general, registered=registered, then="resolve_github_user"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = (self.tmp / "calls").read_text().splitlines()
                host = [c for c in calls if c.startswith("ssh ") and "IdentitiesOnly" not in c]
                self.assertEqual(len(host), probes, calls)

    def test_a_host_identity_answering_as_the_gh_account_is_registered(self) -> None:
        result, adds = self.register(general=ACCOUNT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(adds), 2, adds)
        self.assertNotIn("ssh identity answers as", result.stderr)

    def test_a_denial_without_an_offer_of_this_key_registers_nothing(self) -> None:
        for state in CANNOT_TELL:
            with self.subTest(state=state):
                result, adds = self.register(ssh_state=state)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(adds, [])
                self.assertIn(
                    "Could not tell which account the managed key belongs to", result.stderr
                )

    def test_only_the_evidence_probe_is_verbose_and_non_prompting(self) -> None:
        result, _ = self.register(managed=ACCOUNT, then="resolve_github_user")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.tmp / "calls").read_text().splitlines()
        probes = [c for c in calls if "IdentitiesOnly" in c]
        self.assertEqual(len(probes), 2, calls)
        evidence = [c for c in probes if " -v " in c]
        self.assertEqual(len(evidence), 1, probes)
        self.assertIn("BatchMode=yes", evidence[0])
        resolving = [c for c in probes if " -v " not in c]
        self.assertNotIn("BatchMode=yes", resolving[0])

    def test_a_passphrase_key_still_resolves_as_the_managed_key_through_the_prompting_probe(
        self,
    ) -> None:
        result, adds = self.register(
            managed=ACCOUNT,
            batch_user="",
            ssh_state="passphrase",
            general=SECOND,
            then='resolve_github_user\necho "user=$GITHUB_USER managed=$MANAGED_KEY_AUTHENTICATES"',
        )
        self.assertEqual(adds, [])
        self.assertIn(f"user={ACCOUNT} managed=true", result.stdout)

    def test_a_gh_listing_that_fails_registers_nothing_and_the_public_list_answers(self) -> None:
        check = "GITHUB_USER=x\nrc=0\nsigning_registration_state || rc=$?\necho rc=$rc"
        result, adds = self.register(list_rc=1, public_signing=True, then=check)
        self.assertEqual(adds, [])
        self.assertIn("gh could not list its keys", result.stderr)
        self.assertIn(ADD_REFRESH, result.stderr)
        self.assertIn("rc=0", result.stdout)
        result, _ = self.register(list_rc=1, then=check)
        self.assertIn("rc=1", result.stdout)

    def test_a_listing_gh_cannot_read_names_the_scope_remedy_before_any_probe(self) -> None:
        result, adds = self.register(list_rc=1, batch_user="", ssh_state="passphrase")
        self.assertEqual(adds, [])
        self.assertIn(ADD_REFRESH, result.stderr)
        self.assertNotIn("Could not tell", result.stderr)
        calls = (self.tmp / "calls").read_text().splitlines()
        self.assertFalse([c for c in calls if "IdentitiesOnly" in c], calls)

    def test_a_login_with_only_the_authentication_scope_falls_back_to_the_public_list(self) -> None:
        check = "GITHUB_USER=x\nrc=0\nsigning_registration_state || rc=$?\necho rc=$rc"
        result, adds = self.register(add_signing_rc=1, then=check)
        self.assertEqual(len(adds), 2, adds)
        self.assertIn("rc=1", result.stdout)
        result, _ = self.register(add_signing_rc=1, public_signing=True, then=check)
        self.assertIn("rc=0", result.stdout)

    def test_an_unreachable_github_at_the_final_check_reads_as_unknown(self) -> None:
        check = "PUBLIC_ERROR=yes\nGITHUB_USER=x\nrc=0\nsigning_registration_state || rc=$?\necho rc=$rc"
        result, adds = self.register(
            registered="auth signing", hide_signing=True, public_signing=True, then=check
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adds, [])
        self.assertIn("rc=2", result.stdout)

    def test_a_lagging_public_list_cannot_cause_a_false_paste_or_a_second_add(self) -> None:
        check = 'signing_registration_state && echo "state=registered"'
        for registered, add_count in (("", 2), ("auth signing", 0)):
            with self.subTest(registered=registered):
                result, adds = self.register(registered=registered, then=check)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(adds), add_count, adds)
                self.assertIn("state=registered", result.stdout)

    def test_help_no_longer_says_registration_cannot_be_automated(self) -> None:
        text = LINUX_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("cannot be automated", text)
        self.assertIn("gh ssh-key add", function_body(text, "\nusage() {"))

    def test_configure_registers_before_it_checks_authentication(self) -> None:
        body = function_body(LINUX_SCRIPT.read_text(encoding="utf-8"), "\nconfigure() {")
        self.assertIn("\n    register_keys_through_gh\n", body)
        self.assertLess(body.index("register_keys_through_gh"), body.index("resolve_github_user"))


WINDOWS_REGISTER_FUNCTIONS = [
    "log",
    "info",
    "step",
    "run",
    "Get-KeyBody",
    "Get-GhAccount",
    "Get-SshGreeting",
    "Get-GreetingUser",
    "Get-GeneralUser",
    "Resolve-GitHubUser",
    "Test-ManagedKeyDenied",
    "Invoke-GhOnGitHubCom",
    "Get-GhKeyState",
    "Test-KeyRegistered",
    "Get-SigningRegistrationState",
    "Register-KeyThroughGh",
    "Test-GhRegistrationAllowed",
    "Register-KeysThroughGh",
    "Show-RegistrationNeeded",
    "Test-Authentication",
]
WINDOWS_REGISTER_BODY = r"""
function warn { param([string]$Message) Write-Host "WARNING: $Message" }
# GitHub's public lists stay empty and gh's are kept in $script:Reg, so a check reading the wrong one reports a missing key.
function Invoke-RestMethod {
    param([string]$Uri, [int]$TimeoutSec)
    if ($script:Case.publicError) { throw 'constructed network failure' }
    if ($Uri -like '*ssh_signing_keys') {
        if ($script:Case.publicSigning) { return @([pscustomobject]@{ key = $script:Case.pub }) }
        return @()
    }
    return "`n"
}
function ssh-keygen { $global:LASTEXITCODE = 0; return "256 $($script:Case.fingerprint) comment (ED25519)" }
function ssh {
    $line = ($args -join ' ')
    $script:SshCalls += $line
    if ($line -like '*BatchMode=yes*') {
        if ($script:Case.batchUser) { return "Hi $($script:Case.batchUser)! You have successfully authenticated." }
        # Real ssh writes its verbose lines to stderr, which reach the script's 2>&1 as error records rather than strings.
        # So a native process writes them here, and the script's own rendering of those records is what the evidence check reads.
        if ($script:Case.sshOutput) {
            $env:SETUP_GITHUB_FAKE_SSH = $script:Case.sshOutput
            & (Get-Process -Id $PID).Path -NoProfile -NonInteractive -Command '[Console]::Error.Write($env:SETUP_GITHUB_FAKE_SSH)'
        }
        return
    }
    if ($line -like '*-i *') {
        if ($script:Case.managed) { return "Hi $($script:Case.managed)! You have successfully authenticated." }
        return
    }
    if ($script:Case.general) { return "Hi $($script:Case.general)! You have successfully authenticated." }
}
$GhStub = {
    $script:Calls += ($args -join ' ')
    $global:LASTEXITCODE = 0
    switch -Wildcard ($args -join ' ') {
        'auth status --active --help' { return }
        'auth status --active --hostname github.com' {
            if (-not $script:Case.account) { $global:LASTEXITCODE = 1; return 'You are not logged into any GitHub hosts.' }
            return @("github.com", "  Logged in to github.com account $($script:Case.account) (keyring)", "  - Active account: true")
        }
        'ssh-key list' {
            if ($env:GH_HOST -ne 'github.com') { $global:LASTEXITCODE = 9; return }
            if ($script:Case.listRc) { $global:LASTEXITCODE = [int]$script:Case.listRc; return }
            foreach ($kind in $script:Reg) {
                if ($kind -eq 'signing' -and $script:Case.hideSigning) { continue }
                "$(if ($script:Case.listTitle) { $script:Case.listTitle } else { 'example-host' })`t$($script:Case.pub)`t$kind`t2024-01-01`t1"
            }
            return
        }
        'ssh-key add *' {
            if ($env:GH_HOST -ne 'github.com') { $global:LASTEXITCODE = 9; return }
            $global:LASTEXITCODE = if ($args[-1] -eq 'signing') { [int]$script:Case.addSigningRc } else { [int]$script:Case.addRc }
            if ($global:LASTEXITCODE -eq 0) { $script:Reg += $args[-1] }
            return
        }
        default { $global:LASTEXITCODE = 9 }
    }
}
Set-Item function:script:gh $GhStub
$script:KEY_SETTINGS_URL = 'https://github.com/settings/ssh/new'
$script:GH_SCOPE_COMMAND = 'gh auth refresh -h github.com -s admin:public_key,admin:ssh_signing_key'
$script:MODE = 'configure'
$directory = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $directory | Out-Null
$script:KEY = Join-Path $directory 'id_ed25519'
$results = [ordered]@{}
foreach ($case in (Get-Content $Cases -Raw | ConvertFrom-Json)) {
    $script:Case = $case
    $script:Calls = @()
    $script:Reg = @($case.registered | ForEach-Object { $_ })
    $script:DRY_RUN = [bool]$case.dryRun
    $script:GITHUB_USER = ''
    $script:GH_SIGNING_LISTED = $false
    $script:GH_ACCOUNT = ''
    $script:GENERAL_USER = ''
    $script:GENERAL_PROBED = $false
    $script:SshCalls = @()
    Remove-Item "$($script:KEY)*" -ErrorAction SilentlyContinue
    if (-not $case.noKey) {
        Set-Content -Path $script:KEY -Value ''
        Set-Content -Path "$($script:KEY).pub" -Value "$($case.pub) comment"
    }
    $output = & {
        Register-KeysThroughGh
        if ($case.resolve) {
            Resolve-GitHubUser
            "user=$($script:GITHUB_USER) managed=$($script:MANAGED_KEY_AUTHENTICATES)"
        }
        if ($case.check) {
            if ($case.publicErrorAfter) { $script:Case.publicError = $true }
            $script:GITHUB_USER = 'x'
            "state=$(Get-SigningRegistrationState)"
        }
        if ($case.auth) { $null = Test-Authentication }
        if ($case.paste) {
            $script:GITHUB_USER = 'x'
            if ((Get-SigningRegistrationState) -ne 'registered') { Show-RegistrationNeeded -Kind 'signing' -Type 'Signing' }
        }
    } 6>&1 | ForEach-Object { "$_" }
    $results[$case.name] = [ordered]@{ output = @($output); calls = @($script:Calls | Where-Object { $_ -like 'ssh-key add*' }); lists = @($script:Calls | Where-Object { $_ -eq 'ssh-key list' }).Count; logins = @($script:Calls | Where-Object { $_ -like 'auth login*' -or $_ -like 'auth refresh*' }); probes = @($script:SshCalls | Where-Object { $_ -like '*IdentitiesOnly*' }); host = @($script:SshCalls | Where-Object { $_ -notlike '*IdentitiesOnly*' }) }
}
Remove-Item $directory -Recurse
$results | ConvertTo-Json -Depth 4
"""


def windows_register_case(
    name: str,
    *,
    account: str = ACCOUNT,
    registered: list[str] | None = None,
    add_rc: int = 0,
    dry_run: bool = False,
    managed: str = "",
    no_key: bool = False,
    ssh_state: str = "offered",
    batch_user: str | None = None,
    general: str = "",
    list_rc: int = 0,
    add_signing_rc: int | None = None,
    public_signing: bool = False,
    resolve: bool = False,
    check: bool = False,
    paste: bool = False,
    hide_signing: bool = False,
    auth: bool = False,
    public_error: bool = False,
    list_title: str = "",
    public_error_after: bool = False,
) -> dict[str, object]:
    return {
        "name": name,
        "account": account,
        "registered": registered or [],
        "addRc": add_rc,
        "dryRun": dry_run,
        "managed": managed,
        "batchUser": managed if batch_user is None else batch_user,
        "general": general,
        "listRc": list_rc,
        "addSigningRc": add_rc if add_signing_rc is None else add_signing_rc,
        "publicSigning": public_signing,
        "noKey": no_key,
        "sshOutput": SSH_STATES[ssh_state].replace("KEYPATH", "id_ed25519"),
        "fingerprint": FINGERPRINT,
        "resolve": resolve,
        "check": check,
        "paste": paste,
        "hideSigning": hide_signing,
        "auth": auth,
        "publicError": public_error,
        "listTitle": list_title,
        "publicErrorAfter": public_error_after,
        "pub": PUBLIC_KEY,
    }


@unittest.skipUnless(shutil.which("pwsh"), "needs pwsh to drive the Windows script's own functions")
class TestWindowsKeyRegistration(unittest.TestCase):
    """`setup-github.ps1` registers both keys through a logged-in gh, with `gh` and GitHub stubbed."""

    results: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cases = [
            windows_register_case("none"),
            windows_register_case("auth-done", registered=["authentication"]),
            windows_register_case(
                "titled-signing", registered=["authentication"], list_title="signing"
            ),
            windows_register_case("both-done", registered=["authentication", "signing"]),
            windows_register_case("logged-out", account=""),
            windows_register_case("refused", add_rc=1),
            windows_register_case("dry-run", dry_run=True),
            windows_register_case("dry-run-no-key", dry_run=True, no_key=True),
            windows_register_case("other-account", managed=SECOND, ssh_state="silent"),
            windows_register_case("same-account", managed=ACCOUNT, registered=["authentication"]),
            windows_register_case("host-other", general=SECOND, paste=True, auth=True),
            windows_register_case(
                "managed-auth", managed=ACCOUNT, registered=["authentication"], auth=True
            ),
            windows_register_case(
                "signing-public-unreadable",
                registered=["authentication", "signing"],
                hide_signing=True,
                public_error=True,
            ),
            windows_register_case(
                "host-other-key-on-gh",
                managed=ACCOUNT,
                registered=["authentication"],
                general=SECOND,
            ),
            windows_register_case("host-same", general=ACCOUNT),
            windows_register_case(
                "signing-scope-missing",
                registered=["authentication", "signing"],
                hide_signing=True,
                public_signing=True,
            ),
            windows_register_case("host-refused-resolve", general=SECOND, resolve=True),
            windows_register_case("host-added-resolve", general=ACCOUNT, resolve=True),
            windows_register_case(
                "host-auth-done-resolve",
                general=ACCOUNT,
                registered=["authentication"],
                resolve=True,
            ),
            windows_register_case("probe-once", managed=ACCOUNT, resolve=True),
            windows_register_case(
                "listed-auth-passphrase",
                registered=["authentication"],
                batch_user="",
                ssh_state="passphrase",
            ),
            windows_register_case(
                "configured-host",
                registered=["authentication", "signing"],
                batch_user="",
                ssh_state="passphrase",
                general=SECOND,
            ),
            windows_register_case(
                "passphrase-resolve",
                managed=ACCOUNT,
                batch_user="",
                ssh_state="passphrase",
                general=SECOND,
                resolve=True,
            ),
            windows_register_case("list-fails", list_rc=1, public_signing=True, check=True),
            windows_register_case("list-fails-empty", list_rc=1, check=True),
            windows_register_case(
                "list-fails-passphrase", list_rc=1, batch_user="", ssh_state="passphrase"
            ),
            windows_register_case("auth-scope-only", add_signing_rc=1, check=True),
            windows_register_case(
                "auth-scope-only-listed", add_signing_rc=1, public_signing=True, check=True
            ),
            windows_register_case("lag", check=True),
            windows_register_case(
                "listed-lag", registered=["authentication", "signing"], check=True
            ),
            windows_register_case(
                "fallback-then-unreachable",
                registered=["authentication", "signing"],
                hide_signing=True,
                public_signing=True,
                public_error_after=True,
                check=True,
            ),
            *(windows_register_case(f"tell-{state}", ssh_state=state) for state in CANNOT_TELL),
        ]
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory, "harness.ps1")
            text = windows_harness(
                "[string]$Cases", WINDOWS_REGISTER_FUNCTIONS, WINDOWS_REGISTER_BODY
            )
            harness.write_text(text, encoding="utf-8")
            case_file = Path(directory, "cases.json")
            case_file.write_text(json.dumps(cases), encoding="utf-8")
            result = run_pwsh(
                ["-File", str(harness), "-Script", str(WINDOWS_SCRIPT), "-Cases", str(case_file)]
            )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        cls.results = json.loads(result.stdout)

    def listed(self, name: str, field: str) -> list[str]:
        value = self.results[name][field]
        return [value] if isinstance(value, str) else list(value)

    def adds(self, name: str) -> list[str]:
        return self.listed(name, "calls")

    def output(self, name: str) -> str:
        return "\n".join(self.listed(name, "output"))

    def test_both_missing_keys_are_registered_with_their_types_in_order(self) -> None:
        adds = self.adds("none")
        self.assertEqual(len(adds), 2, adds)
        self.assertTrue(adds[0].endswith("--type authentication"), adds)
        self.assertTrue(adds[1].endswith("--type signing"), adds)
        self.assertIn("--title", adds[0])

    def test_a_host_titled_after_a_kind_does_not_pass_for_it(self) -> None:
        adds = self.adds("titled-signing")
        self.assertEqual(len(adds), 1, adds)
        self.assertTrue(adds[0].endswith("--type signing"), adds)

    def test_only_the_missing_kind_is_registered(self) -> None:
        self.assertEqual(self.results["auth-done"]["lists"], 2)
        adds = self.adds("auth-done")
        self.assertEqual(len(adds), 1, adds)
        self.assertTrue(adds[0].endswith("--type signing"), adds)

    def test_a_rerun_with_both_registered_changes_nothing(self) -> None:
        self.assertEqual(self.adds("both-done"), [])
        self.assertIn("Already registered as the signing key", self.output("both-done"))

    def test_a_logged_out_gh_registers_nothing_and_never_logs_in(self) -> None:
        self.assertEqual(self.adds("logged-out"), [])
        self.assertNotIn("Registering the key through gh", self.output("logged-out"))
        logins = self.listed("logged-out", "logins")
        self.assertEqual(logins, [], logins)

    def test_a_refused_authentication_add_skips_the_signing_add(self) -> None:
        adds = self.adds("refused")
        self.assertEqual(len(adds), 1, adds)
        self.assertTrue(adds[0].endswith("--type authentication"), adds)
        self.assertIn(ADD_REFRESH, self.output("refused"))
        self.assertIn("in use on another account", self.output("refused"))

    def test_a_dry_run_prints_the_commands_and_registers_nothing(self) -> None:
        self.assertEqual(self.adds("dry-run"), [])
        self.assertIn("[dry run] gh ssh-key add", self.output("dry-run"))

    def test_a_dry_run_without_a_key_says_what_it_would_do_without_warning(self) -> None:
        self.assertEqual(self.adds("dry-run-no-key"), [])
        output = self.output("dry-run-no-key")
        self.assertIn("--type authentication", output)
        self.assertIn("--type signing", output)
        self.assertNotIn("WARNING", output)

    def test_a_key_belonging_to_another_account_is_not_registered_through_gh(self) -> None:
        self.assertEqual(self.adds("other-account"), [])
        self.assertIn(f"the managed key belongs to {SECOND}", self.output("other-account"))

    def test_a_key_authenticating_as_the_gh_account_is_registered(self) -> None:
        adds = self.adds("same-account")
        self.assertEqual(len(adds), 1, adds)

    def test_a_host_identity_answering_as_another_account_registers_nothing(self) -> None:
        self.assertEqual(self.adds("host-other"), [])
        output = self.output("host-other")
        self.assertIn(HOST_MISMATCH, output)
        self.assertNotIn("Registering the key through gh", output)
        self.assertIn('Set the key type to "Signing key"', output)
        self.assertIn('Set the key type to "Authentication key"', output)
        self.assertIn(PUBLIC_KEY, output)

    def test_a_host_the_managed_key_authenticates_prints_no_authentication_paste(self) -> None:
        output = self.output("managed-auth")
        self.assertIn(f"Authenticated as {ACCOUNT}, with the managed key", output)
        self.assertNotIn('Set the key type to "Authentication key"', output)

    def test_an_unreadable_public_list_leaves_the_signing_add_to_gh(self) -> None:
        adds = self.adds("signing-public-unreadable")
        self.assertEqual(len(adds), 1, adds)
        self.assertTrue(adds[0].endswith("--type signing"), adds)
        self.assertNotIn("gh could not list its keys", self.output("signing-public-unreadable"))

    def test_a_host_identity_answering_as_another_account_refuses_beside_a_key_on_gh_s(
        self,
    ) -> None:
        self.assertEqual(self.adds("host-other-key-on-gh"), [])
        self.assertIn(HOST_MISMATCH, self.output("host-other-key-on-gh"))

    def test_gh_listing_the_authentication_key_proves_whose_it_is(self) -> None:
        adds = self.adds("listed-auth-passphrase")
        self.assertEqual(len(adds), 1, adds)
        self.assertTrue(adds[0].endswith("--type signing"), adds)
        self.assertNotIn("Could not tell", self.output("listed-auth-passphrase"))

    def test_a_configured_host_is_not_told_to_paste(self) -> None:
        self.assertEqual(self.adds("configured-host"), [])
        self.assertNotIn("paste", self.output("configured-host"))

    def test_the_host_s_own_probe_is_asked_again_only_after_an_authentication_add(self) -> None:
        for name, probes in (
            ("host-refused-resolve", 1),
            ("host-added-resolve", 2),
            ("host-auth-done-resolve", 1),
        ):
            with self.subTest(name=name):
                self.assertEqual(len(self.listed(name, "host")), probes, self.results[name])

    def test_a_listing_without_the_signing_scope_asks_the_public_list(self) -> None:
        self.assertEqual(self.adds("signing-scope-missing"), [])
        output = self.output("signing-scope-missing")
        self.assertNotIn("WARNING", output)
        self.assertIn("Already registered as the signing key", output)

    def test_a_host_identity_answering_as_the_gh_account_is_registered(self) -> None:
        self.assertEqual(len(self.adds("host-same")), 2)
        self.assertNotIn("ssh identity answers as", self.output("host-same"))

    def test_a_denial_without_an_offer_of_this_key_registers_nothing(self) -> None:
        for state in CANNOT_TELL:
            with self.subTest(state=state):
                self.assertEqual(self.adds(f"tell-{state}"), [])
                self.assertIn(
                    "Could not tell which account the managed key belongs to",
                    self.output(f"tell-{state}"),
                )

    def test_only_the_evidence_probe_is_verbose_and_non_prompting(self) -> None:
        probes = self.listed("probe-once", "probes")
        self.assertEqual(len(probes), 2, probes)
        evidence = [p for p in probes if "-v" in p.split()]
        self.assertEqual(len(evidence), 1, probes)
        self.assertIn("BatchMode=yes", evidence[0])
        resolving = [p for p in probes if "-v" not in p.split()]
        self.assertNotIn("BatchMode=yes", resolving[0])

    def test_a_passphrase_key_still_resolves_as_the_managed_key_through_the_prompting_probe(
        self,
    ) -> None:
        self.assertEqual(self.adds("passphrase-resolve"), [])
        self.assertIn(f"user={ACCOUNT} managed=True", self.output("passphrase-resolve"))

    def test_a_gh_listing_that_fails_registers_nothing_and_the_public_list_answers(self) -> None:
        self.assertEqual(self.adds("list-fails"), [])
        self.assertIn("gh could not list its keys", self.output("list-fails"))
        self.assertIn(ADD_REFRESH, self.output("list-fails"))
        self.assertIn("state=registered", self.output("list-fails"))
        self.assertIn("state=missing", self.output("list-fails-empty"))

    def test_a_listing_gh_cannot_read_names_the_scope_remedy_before_any_probe(self) -> None:
        output = self.output("list-fails-passphrase")
        self.assertIn(ADD_REFRESH, output)
        self.assertNotIn("Could not tell", output)
        self.assertEqual(self.listed("list-fails-passphrase", "probes"), [])

    def test_a_login_with_only_the_authentication_scope_falls_back_to_the_public_list(self) -> None:
        self.assertEqual(len(self.adds("auth-scope-only")), 2)
        self.assertIn("state=missing", self.output("auth-scope-only"))
        self.assertIn("state=registered", self.output("auth-scope-only-listed"))

    def test_the_signing_check_names_an_unknown_apart_from_a_registered_key(self) -> None:
        body = function_body(
            WINDOWS_SCRIPT.read_text(encoding="utf-8"), "\nfunction Invoke-Configure {"
        )
        self.assertIn("default { warn 'GitHub could not be reached", body)

    def test_a_lagging_public_list_cannot_cause_a_false_paste_or_a_second_add(self) -> None:
        self.assertEqual(len(self.adds("lag")), 2)
        self.assertIn("state=registered", self.output("lag"))
        self.assertEqual(self.adds("listed-lag"), [])
        self.assertIn("state=registered", self.output("listed-lag"))

    def test_an_unreachable_github_at_the_final_check_reads_as_unknown(self) -> None:
        self.assertEqual(self.adds("fallback-then-unreachable"), [])
        self.assertIn("state=unknown", self.output("fallback-then-unreachable"))

    def test_help_no_longer_says_registration_cannot_be_automated(self) -> None:
        result = run_pwsh(["-File", str(WINDOWS_SCRIPT), "-Help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("cannot be automated", result.stdout)
        self.assertIn("gh ssh-key add", result.stdout)

    def test_configure_registers_before_it_checks_authentication(self) -> None:
        body = function_body(
            WINDOWS_SCRIPT.read_text(encoding="utf-8"), "\nfunction Invoke-Configure {"
        )
        self.assertIn("\n    Register-KeysThroughGh\n", body)
        self.assertLess(body.index("Register-KeysThroughGh"), body.index("Test-Authentication"))


def function_body(text: str, opener: str) -> str:
    """The text from `opener` to the closing brace at column 0 that ends the function it opens."""
    start = text.index(opener)
    return text[start : text.index("\n}\n", start)]


@unittest.skipUnless(sys.platform == "linux", "drives the Linux script's own functions")
class TestPackageInstalled(unittest.TestCase):
    """`setup-github.sh`'s installed-package check, with a stub `dpkg-query` first on PATH."""

    def check(self, want: str, status: str) -> int:
        """The exit status of `package_installed git` when dpkg holds git at `want` and `status`.

        The stub answers each status field the format names, so the definition reading the wrong one fails here.
        An empty `want` stands for a package dpkg does not know, which dpkg-query answers with exit 1.
        """
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        functions = directory / "functions.sh"
        functions.write_text(linux_functions(), encoding="utf-8")
        answer = (
            "exit 1"
            if not want
            else (
                f"case $2 in *Status-Want*) printf '{want}' ;; *Status-Status*) printf '{status}' ;;"
                f" *) printf '{want} ok {status}' ;; esac"
            )
        )
        stub = directory / "dpkg-query"
        stub.write_text(f"#!/bin/sh\n{answer}\n", encoding="ascii")
        stub.chmod(0o755)
        return run_sourced(functions, directory, "package_installed git").returncode

    def test_an_installed_package_reads_as_installed(self) -> None:
        self.assertEqual(self.check("install", "installed"), 0)

    def test_a_held_package_reads_as_installed(self) -> None:
        self.assertEqual(self.check("hold", "installed"), 0)

    def test_a_removed_package_reads_as_missing(self) -> None:
        self.assertNotEqual(self.check("deinstall", "config-files"), 0)

    def test_an_unknown_package_reads_as_missing(self) -> None:
        self.assertNotEqual(self.check("", ""), 0)

    def test_install_tools_carries_the_same_definition(self) -> None:
        installer = LINUX_SCRIPT.with_name("install-tools.sh").read_text(encoding="utf-8")
        self.assertEqual(
            function_body(installer, "\npackage_installed() {"),
            function_body(LINUX_SCRIPT.read_text(encoding="utf-8"), "\npackage_installed() {"),
        )


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
