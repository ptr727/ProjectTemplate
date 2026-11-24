#!/usr/bin/env python3
"""Tests for `upgrade-host.ps1 -CleanPath`, driven through the script's own functions.

The registry read and write, the directory probe, the session-user check, and the broadcast are stubbed.
So no test reads or changes the host's real `Path`, and the same cases run wherever `pwsh` does.

Run as `python3 tests/test_upgrade_host.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parent.parent
UPGRADER = ROOT / "host-setup" / "windows" / "upgrade-host.ps1"

FUNCTIONS = (
    "Get-PathEntryTarget",
    "Get-PathEntryKey",
    "Get-PathEntryState",
    "Get-PathCleanup",
    "Get-PathWriteScope",
    "Show-PathFinding",
    "Invoke-PathCleanup",
    "Show-Status",
    "Test-StaleVariable",
)

HARNESS = r"""
param([string]$Script, [string]$Cases)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$case = Get-Content -Raw -LiteralPath $Cases | ConvertFrom-Json -AsHashtable
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$null)
$wanted = $case.functions
foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
foreach ($assignment in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -eq '$PATH_KEPT_REASON' }, $false)) {
    . ([scriptblock]::Create($assignment.Extent.Text))
}

$LINES = [Collections.Generic.List[string]]::new()
$WRITES = [Collections.Generic.List[object]]::new()
$CONFIRMS = [Collections.Generic.List[string]]::new()
$BROADCASTS = 0
function log { param([string]$Message = '') $script:LINES.Add($Message) }
function info { param([string]$Message) $script:LINES.Add("  $Message") }
function step { param([string]$Message) $script:LINES.Add("==> $Message") }
function warn { param([string]$Message) $script:LINES.Add("WARNING: $Message") }
function die { param([string]$Message) throw "DIE $Message" }
function confirm { param([string]$Question) $script:CONFIRMS.Add($Question); return $script:case.confirm }
$READS = @{ Machine = 0; User = 0 }
function Read-PathScope {
    param([string]$Scope)
    $script:READS[$Scope]++
    $raw = $script:case.stored[$Scope].raw
    $kind = $script:case.stored[$Scope].kind
    if ($script:READS[$Scope] -gt 1 -and $script:case.changedOnReread -contains $Scope) { $raw += ';C:\installed-meanwhile' }
    if ($script:READS[$Scope] -gt 1 -and $script:case.kindChangedOnReread -contains $Scope) { $kind = 'String' }
    @{ Raw = $raw; Kind = $kind }
}
function Read-StoredVariable {
    param([string]$Name)
    if ($script:case.storedVariables.ContainsKey($Name)) { return $script:case.storedVariables[$Name] }
    return $null
}
function Write-PathScope {
    param([string]$Scope, [string]$Value, $Kind)
    if ($script:case.failWrite -eq $Scope) { throw 'access denied' }
    $script:WRITES.Add([ordered]@{ scope = $Scope; value = $Value; kind = "$Kind" })
}
function Send-EnvironmentChange { $script:BROADCASTS++; return $true }
function Get-DirectoryState {
    param([string]$Path)
    if ($script:case.directories.ContainsKey($Path)) { return $script:case.directories[$Path] }
    return 'missing'
}
function Get-SessionUserMatch { return $script:case.sessionUser }
function Write-PathLengthWarning { }
function Get-HostDescription { 'a host' }
function Get-Upgradable { @{ Ready = @(); Explicit = @() } }
function Get-WslVersion { '2.0' }
function Get-DockerProcess { , @() }

foreach ($name in $case.environment.Keys) { Set-Item -LiteralPath "Env:\$name" -Value $case.environment[$name] }
$ELEVATED = $case.elevated
$DRY_RUN = $case.dryRun
$MODE = 'cleanpath'
$outcome = 'ok'
try {
    switch ($case.action) {
        'plan' { $plan = Get-PathCleanup -Machine $case.stored.Machine.raw -User $case.stored.User.raw }
        default { & $case.action }
    }
} catch {
    if ("$_" -notlike 'DIE *') { throw }
    $outcome = "$_"
}
[ordered]@{
    outcome    = $outcome
    lines      = $LINES
    writes     = $WRITES
    confirms   = $CONFIRMS
    broadcasts = $BROADCASTS
    plan       = if ($case.action -eq 'plan') { $plan } else { $null }
} | ConvertTo-Json -Depth 6
"""

LIVE_ROOT = {"C:\\": "present"}


def drive(
    action: str,
    machine: str = "",
    user: str = "",
    directories: dict[str, str] | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Run `action` against stubbed registry halves, returning what it printed and wrote."""
    case = {
        "functions": list(FUNCTIONS),
        "action": action,
        "stored": {
            "Machine": {"raw": machine, "kind": "ExpandString"},
            "User": {"raw": user, "kind": "ExpandString"},
        },
        "directories": {**LIVE_ROOT, **(directories or {})},
        "elevated": options.get("elevated", False),
        "sessionUser": options.get("session_user", "same"),
        "storedVariables": options.get("stored_variables", {}),
        "changedOnReread": options.get("changed_on_reread", []),
        "kindChangedOnReread": options.get("kind_changed_on_reread", []),
        "failWrite": options.get("fail_write", ""),
        "dryRun": options.get("dry_run", False),
        "confirm": options.get("confirm", True),
        "environment": options.get("environment", {}),
    }
    with tempfile.TemporaryDirectory() as directory:
        harness = Path(directory, "harness.ps1")
        harness.write_text(HARNESS, encoding="utf-8")
        data = Path(directory, "case.json")
        data.write_text(json.dumps(case), encoding="utf-8")
        result = subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(harness),
                "-Script",
                str(UPGRADER),
                "-Cases",
                str(data),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=120,
        )
    if result.returncode != 0:
        raise AssertionError(f"harness failed ({result.returncode}): {result.stderr}")
    output: dict[str, Any] = json.loads(result.stdout)
    return output


def removed(plan: dict[str, Any], scope: str) -> list[tuple[str, str]]:
    """The entries a plan removes from one half, each with its reason."""
    return [(row["Entry"], row["Reason"]) for row in plan[scope]["Removed"]]


@unittest.skipUnless(shutil.which("pwsh"), "needs pwsh to drive upgrade-host.ps1's own functions")
class TestPathCleanupPlan(unittest.TestCase):
    """Which entries a cleanup removes, which it keeps, and the value it would write."""

    def test_the_first_of_two_equal_entries_is_kept_in_effective_order(self) -> None:
        live = {"C:\\a": "present", "C:\\b": "present"}
        plan = drive(
            "plan",
            machine="C:\\a;c:\\A\\;C:\\b",
            user="C:\\b\\.;C:\\a;C:\\B",
            directories=live,
        )["plan"]
        self.assertEqual(plan["Machine"]["Value"], "C:\\a;C:\\b")
        self.assertEqual([entry for entry, _ in removed(plan, "Machine")], ["c:\\A\\"])
        self.assertEqual(plan["User"]["Value"], "")
        self.assertEqual(
            removed(plan, "User"),
            [(entry, "repeats an earlier entry") for entry in ("C:\\b\\.", "C:\\a", "C:\\B")],
        )

    def test_a_user_entry_repeated_later_in_the_user_half_keeps_the_first(self) -> None:
        live = {"C:\\tools": "present", "C:\\other": "present"}
        plan = drive("plan", user="C:\\tools;C:\\other;C:\\TOOLS\\", directories=live)["plan"]
        self.assertEqual(plan["User"]["Value"], "C:\\tools;C:\\other")

    def test_an_entry_is_dead_only_under_a_root_that_answers(self) -> None:
        directories = {
            "C:\\present": "present",
            "C:\\locked": "unreadable",
            "Z:\\": "missing",
            "\\\\server\\share": "unreadable",
        }
        entries = [
            "C:\\present",
            "C:\\gone",
            "Z:\\tools",
            "\\\\server\\share\\bin",
            "C:\\locked",
            "bin",
        ]
        plan = drive("plan", user=";".join(entries), directories=directories)["plan"]
        self.assertEqual(removed(plan, "User"), [("C:\\gone", "does not exist")])
        self.assertEqual(
            plan["User"]["Value"], "C:\\present;Z:\\tools;\\\\server\\share\\bin;C:\\locked;bin"
        )
        unjudged = {row["Entry"]: row["Reason"] for row in plan["User"]["Unjudged"]}
        self.assertEqual(
            set(unjudged), {"Z:\\tools", "\\\\server\\share\\bin", "C:\\locked", "bin"}
        )
        self.assertIn("did not answer", unjudged["Z:\\tools"])
        self.assertIn("did not answer", unjudged["\\\\server\\share\\bin"])
        self.assertIn("could not be read", unjudged["C:\\locked"])
        self.assertIn("not a full path", unjudged["bin"])

    def test_a_drive_root_is_not_a_duplicate_of_the_bare_drive(self) -> None:
        plan = drive(
            "plan",
            user="C:\\;C:;C:\\.;c:\\\\",
            directories={"C:\\": "present", "c:\\\\": "present"},
        )["plan"]
        self.assertEqual(plan["User"]["Value"], "C:\\;C:")
        self.assertEqual(
            removed(plan, "User"),
            [("C:\\.", "repeats an earlier entry"), ("c:\\\\", "repeats an earlier entry")],
        )
        self.assertIn("not a full path", plan["User"]["Unjudged"][0]["Reason"])

    def test_a_literal_percent_in_a_directory_name_is_judged(self) -> None:
        plan = drive(
            "plan",
            user="C:\\tools\\50%;C:\\gone\\100%",
            directories={"C:\\tools\\50%": "present"},
        )["plan"]
        self.assertEqual(plan["User"]["Value"], "C:\\tools\\50%")
        self.assertEqual(removed(plan, "User"), [("C:\\gone\\100%", "does not exist")])
        self.assertEqual(plan["User"]["Unjudged"], [])

    def test_an_entry_naming_a_file_is_removed(self) -> None:
        plan = drive(
            "plan",
            user="C:\\tools\\tool.exe;C:\\tools",
            directories={"C:\\tools\\tool.exe": "file", "C:\\tools": "present"},
        )["plan"]
        self.assertEqual(
            removed(plan, "User"), [("C:\\tools\\tool.exe", "names a file rather than a directory")]
        )
        self.assertEqual(plan["User"]["Value"], "C:\\tools")

    def test_empty_entries_are_removed(self) -> None:
        plan = drive(
            "plan", user=";C:\\a;; ;C:\\b;", directories={"C:\\a": "present", "C:\\b": "present"}
        )["plan"]
        self.assertEqual(plan["User"]["Value"], "C:\\a;C:\\b")
        self.assertEqual(len(removed(plan, "User")), 4)

    def test_a_quoted_entry_is_judged_by_the_directory_inside_the_quotes(self) -> None:
        plan = drive(
            "plan",
            user='"C:\\Program Files\\x";C:\\Program Files\\x',
            directories={"C:\\Program Files\\x": "present"},
        )["plan"]
        self.assertEqual(plan["User"]["Value"], '"C:\\Program Files\\x"')

    def test_kept_entries_keep_their_unexpanded_text(self) -> None:
        plan = drive(
            "plan",
            user="%T3862%\\bin;%T3862%\\BIN\\;%NOT_SET_3862%\\x",
            directories={"C:\\home\\bin": "present"},
            environment={"T3862": "C:\\home"},
        )["plan"]
        self.assertEqual(plan["User"]["Value"], "%T3862%\\bin;%NOT_SET_3862%\\x")
        self.assertEqual(removed(plan, "User"), [("%T3862%\\BIN\\", "repeats an earlier entry")])
        self.assertIn("does not expand", plan["User"]["Unjudged"][0]["Reason"])

    def test_an_entry_whose_variable_changed_since_the_terminal_started_is_kept(self) -> None:
        plan = drive(
            "plan",
            user="%J3862%\\bin;C:\\jdk-new\\bin",
            directories={"C:\\jdk-new\\bin": "present"},
            environment={"J3862": "C:\\jdk-old"},
            stored_variables={"J3862": "C:\\jdk-new"},
        )["plan"]
        self.assertEqual(removed(plan, "User"), [])
        self.assertEqual(plan["User"]["Value"], "%J3862%\\bin;C:\\jdk-new\\bin")
        self.assertIn("changed after this terminal started", plan["User"]["Unjudged"][0]["Reason"])

    def test_an_unchanged_stored_variable_is_judged(self) -> None:
        plan = drive(
            "plan",
            user="%J3862%\\bin",
            environment={"J3862": "C:\\jdk-old"},
            stored_variables={"J3862": "C:\\jdk-old"},
        )["plan"]
        self.assertEqual(removed(plan, "User"), [("%J3862%\\bin", "does not exist")])


@unittest.skipUnless(shutil.which("pwsh"), "needs pwsh to drive upgrade-host.ps1's own functions")
class TestPathCleanupWrite(unittest.TestCase):
    """Which halves a run writes for its elevation, and what it prints and sends around the write."""

    MACHINE = "C:\\keep;C:\\gone-machine"
    USER = "%T3862%\\bin;C:\\gone-user;C:\\keep"
    LIVE: ClassVar[dict[str, str]] = {"C:\\keep": "present", "C:\\home\\bin": "present"}

    def clean(self, **options: Any) -> dict[str, Any]:
        return drive(
            "Invoke-PathCleanup",
            machine=self.MACHINE,
            user=self.USER,
            directories=self.LIVE,
            environment={"T3862": "C:\\home"},
            **options,
        )

    def test_unelevated_cleans_the_user_half_and_names_the_machine_findings(self) -> None:
        result = self.clean(elevated=False)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(
            result["writes"], [{"scope": "User", "value": "%T3862%\\bin", "kind": "ExpandString"}]
        )
        text = "\n".join(result["lines"])
        self.assertIn("remove  C:\\gone-machine", text)
        self.assertIn("needs an elevated run, such as this same command under gsudo", text)
        self.assertEqual(result["broadcasts"], 1)

    def test_elevated_as_the_session_user_cleans_both_halves(self) -> None:
        result = self.clean(elevated=True, session_user="same")
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(
            [(write["scope"], write["value"]) for write in result["writes"]],
            [("Machine", "C:\\keep"), ("User", "%T3862%\\bin")],
        )
        self.assertEqual(result["broadcasts"], 1)
        self.assertNotIn("elevated run", "\n".join(result["lines"]))

    def test_elevated_as_another_account_leaves_the_user_half_alone(self) -> None:
        for match, reason in (("other", "another account's"), ("unknown", "No Explorer runs")):
            with self.subTest(match=match):
                result = self.clean(elevated=True, session_user=match)
                self.assertEqual(result["outcome"], "ok")
                self.assertEqual([write["scope"] for write in result["writes"]], ["Machine"])
                text = "\n".join(result["lines"])
                self.assertIn("remove  C:\\gone-user", text)
                self.assertIn(reason, text)

    def test_a_half_changed_while_the_prompt_waited_is_not_written(self) -> None:
        for changed in (["User"], ["Machine"]):
            with self.subTest(changed=changed):
                result = self.clean(elevated=True, changed_on_reread=changed)
                self.assertIn("Path changed after it was read", result["outcome"])
                self.assertEqual(result["writes"], [])
                self.assertEqual(result["broadcasts"], 0)

    def test_a_machine_change_stops_an_unelevated_user_write(self) -> None:
        result = self.clean(elevated=False, changed_on_reread=["Machine"])
        self.assertIn("machine Path changed after it was read", result["outcome"])
        self.assertEqual(result["writes"], [])

    def test_a_kind_changed_while_the_prompt_waited_is_not_written(self) -> None:
        result = self.clean(elevated=False, kind_changed_on_reread=["User"])
        self.assertIn("user Path changed after it was read", result["outcome"])
        self.assertEqual(result["writes"], [])

    def test_a_failed_second_write_names_the_first_and_still_broadcasts(self) -> None:
        result = self.clean(elevated=True, fail_write="User")
        self.assertIn("but the machine Path was already written", result["outcome"])
        self.assertEqual([write["scope"] for write in result["writes"]], ["Machine"])
        self.assertEqual(result["broadcasts"], 1)

    def test_a_failed_first_write_says_nothing_was_written(self) -> None:
        result = self.clean(elevated=True, fail_write="Machine")
        self.assertIn("so nothing was written", result["outcome"])
        self.assertEqual(result["writes"], [])
        self.assertEqual(result["broadcasts"], 0)

    def test_the_previous_value_is_printed_before_its_write(self) -> None:
        result = self.clean(elevated=False)
        previous = [line for line in result["lines"] if "Previous user Path" in line]
        self.assertEqual(len(previous), 1)
        self.assertTrue(previous[0].endswith(self.USER), previous[0])

    def test_a_dry_run_writes_and_sends_nothing(self) -> None:
        result = self.clean(elevated=True, dry_run=True)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["writes"], [])
        self.assertEqual(result["broadcasts"], 0)
        text = "\n".join(result["lines"])
        self.assertIn("[dry run] write the machine Path as: C:\\keep", text)
        self.assertIn("[dry run] write the user Path as: %T3862%\\bin", text)

    def test_a_declined_prompt_writes_nothing(self) -> None:
        result = self.clean(elevated=False, confirm=False)
        self.assertEqual(result["outcome"], "DIE Declined")
        self.assertEqual(result["writes"], [])

    def test_the_prompt_names_only_the_halves_this_run_writes(self) -> None:
        result = drive(
            "Invoke-PathCleanup",
            machine="C:\\keep;C:\\gone",
            user="C:\\keep\\",
            directories=self.LIVE,
        )
        self.assertEqual(result["confirms"], ["Remove these entries from the user Path?"])
        self.assertEqual([write["value"] for write in result["writes"]], [""])

    def test_nothing_to_remove_asks_nothing(self) -> None:
        result = drive("Invoke-PathCleanup", machine="C:\\keep", directories=self.LIVE)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["confirms"], [])
        self.assertEqual(result["writes"], [])

    def test_only_machine_findings_unelevated_asks_nothing_and_succeeds(self) -> None:
        result = drive("Invoke-PathCleanup", machine="C:\\keep;C:\\gone", directories=self.LIVE)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["confirms"], [])
        self.assertEqual(result["writes"], [])
        self.assertIn("needs an elevated run", "\n".join(result["lines"]))

    def test_status_lists_what_a_cleanup_removes(self) -> None:
        status = drive(
            "Show-Status",
            machine=self.MACHINE,
            user=self.USER,
            directories=self.LIVE,
            environment={"T3862": "C:\\home"},
        )
        self.assertEqual(status["writes"], [])
        text = "\n".join(status["lines"])
        self.assertIn("Path      : 3 entries that -CleanPath removes", text)
        self.assertIn("remove  C:\\gone-machine", text)
        self.assertIn("remove  C:\\gone-user", text)


if __name__ == "__main__":
    unittest.main()
