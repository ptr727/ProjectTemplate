# Writes the host-wide agent rules to Codex's and opencode's global instruction files, by driving global_rules.py in the tree this script sits in.
# The rules are the ones the Claude Code safety kit renders into the user CLAUDE.md.
# Python 3 is its one dependency, which is why the bootstrap runs it after the tools step.
# An agent that is not installed for this user is skipped by the writer itself, on any Python 3.
# The writer asks for 3.11 only once an agent is present, and says so then.

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
opencode that is installed for this user, by running host-setup\agent-safety\global_rules.py from
this tree. It writes rules and registers no hook.

Actions, name one, default install:
  -r, -Report       Read-only: is each file current?
  -h, -Help         Show this help

Options:
  -n, -DryRun       Say what would be written, change nothing
  -y, -Yes          Accepted for symmetry with the sibling scripts, since the writer never prompts
'@
}

# The first candidate that is a Python 3, since the writer holds its own 3.11 floor for a host that has an agent.
function Find-Python {
    $candidates = @(
        @{ Exe = 'py'; Arguments = @('-3') },
        @{ Exe = 'python3'; Arguments = @() },
        @{ Exe = 'python'; Arguments = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        & $candidate.Exe @($candidate.Arguments) -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}

function main {
    if ($WANT_HELP) { usage; exit 0 }

    $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $writer = Join-Path $root 'host-setup' | Join-Path -ChildPath 'agent-safety' | Join-Path -ChildPath 'global_rules.py'
    if (-not (Test-Path -LiteralPath $writer)) {
        die 'This tree carries no host-setup\agent-safety\global_rules.py, so there is nothing to drive'
    }

    $arguments = @()
    if ($WANT_REPORT) { $arguments += '--report' }
    # A report is read-only already, and the writer refuses the two flags together.
    if ($DRY_RUN -and -not $WANT_REPORT) { $arguments += '--dry-run' }

    $python = Find-Python
    if (-not $python) {
        die 'Python 3 not found. install-tools.ps1 provides it, so run the tools step first and this one after.'
    }

    & $python.Exe @($python.Arguments) $writer @arguments
    exit $LASTEXITCODE
}

main
