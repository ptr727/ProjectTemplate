# Writes the host-wide agent rules to Codex's and opencode's global instruction files, by driving global_rules.ps1 in the tree this script sits in.
# The rules are the ones the Claude Code safety kit renders into the user CLAUDE.md.
# Python is its one dependency, which is why the bootstrap runs it after the tools step.
# An agent that is not installed for this user is skipped by the writer itself.

[CmdletBinding()]
param(
    [Alias('r')][switch]$Report,
    [Alias('n')][switch]$DryRun,
    [Alias('y')][switch]$Yes,
    [Alias('h')][switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$WANT_REPORT = [bool]$Report
$DRY_RUN = [bool]$DryRun
# -Yes is accepted for symmetry with the sibling scripts, since the writer never prompts.
$null = $Yes
$WANT_HELP = [bool]$Help

function die { param([string]$Message) [Console]::Error.WriteLine("ERROR: $Message"); exit 1 }

function usage {
    # The closing marker of a here-string has to sit at column 0, so this block is deliberately unindented.
    Write-Host @'
Usage: install-global-rules.ps1 [options]

Writes the fleet's host-wide agent rules to the global instruction file of each of Codex and
opencode that is installed for this user, by running host-setup\agent-safety\global_rules.ps1 from
this tree. It writes rules and registers no hook.

Actions, name one, default install:
  -r, -Report       Read-only: is each file current?
  -h, -Help         Show this help

Options:
  -n, -DryRun       Say what would be written, change nothing
  -y, -Yes          Accepted for symmetry with the sibling scripts, since the writer never prompts
'@
}

function main {
    if ($WANT_HELP) { usage; exit 0 }

    $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $writer = Join-Path $root 'host-setup' | Join-Path -ChildPath 'agent-safety' | Join-Path -ChildPath 'global_rules.ps1'
    if (-not (Test-Path -LiteralPath $writer)) {
        die 'This tree carries no host-setup\agent-safety\global_rules.ps1, so there is nothing to drive'
    }

    $arguments = @()
    if ($WANT_REPORT) { $arguments += '--report' }
    # A report is read-only already, and the writer refuses the two flags together.
    if ($DRY_RUN -and -not $WANT_REPORT) { $arguments += '--dry-run' }

    & $writer @arguments
    exit $LASTEXITCODE
}

main
