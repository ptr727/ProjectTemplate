# Installs the Claude Code safety kit for the current user, by driving the installer in the tree this script sits in.
# Like install-skills.ps1 it reaches outside host-setup\, deliberately, since the kit lives beside the other agent-safety content.
# A host without Claude Code is skipped with a line saying so, because installing would create the .claude directory for an agent nobody installed.
# The kit installer takes no dry-run flag, so a dry run here prints the command and runs nothing.
#
# AGENT_SAFETY_SOURCE_COMMIT, where the caller sets it, names the commit of a tree git cannot answer for.
# A tarball fetched by the bootstrap is such a tree.

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
# -Yes is accepted for symmetry with the sibling scripts, since the installer never prompts.
$null = $Yes
$WANT_HELP = [bool]$Help

function info { param([string]$Message) Write-Host "  $Message" }
function die { param([string]$Message) [Console]::Error.WriteLine("ERROR: $Message"); exit 1 }

function usage {
    # The closing marker of a here-string has to sit at column 0, so this block is deliberately unindented.
    Write-Host @'
Usage: install-safety-kit.ps1 [options]

Installs the Claude Code safety kit for the current user: the write guard, the model tier guard,
the stray-process sweep, the containment prefix, and the host-wide rules in the user CLAUDE.md. It
runs host-setup\agent-safety\claude\install.ps1 from this tree and does nothing where Claude Code
is not installed for this user.

Actions, name one, default install:
  -r, -Report       Read-only: does the installed kit match this tree?
  -h, -Help         Show this help

Options:
  -n, -DryRun       Print the command instead of running it
  -y, -Yes          Accepted for symmetry with the sibling scripts, since the installer never prompts
'@
}

function Test-ClaudePresent {
    if (Get-Command claude -ErrorAction SilentlyContinue) { return $true }
    $claudeHome = if ($env:CLAUDE_HOME) { $env:CLAUDE_HOME } else { Join-Path $HOME '.claude' }
    return (Test-Path -LiteralPath $claudeHome -PathType Container)
}

function main {
    if ($WANT_HELP) { usage; exit 0 }

    $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $installer = Join-Path $root 'host-setup' | Join-Path -ChildPath 'agent-safety' | Join-Path -ChildPath 'claude' | Join-Path -ChildPath 'install.ps1'
    if (-not (Test-Path -LiteralPath $installer)) {
        die 'This tree carries no host-setup\agent-safety\claude\install.ps1, so there is nothing to drive'
    }

    if (-not (Test-ClaudePresent)) {
        info 'Claude Code is not installed for this user, so the safety kit is skipped'
        exit 0
    }

    $arguments = @()
    if ($WANT_REPORT) { $arguments += '--report' }

    if ($DRY_RUN) {
        info "[dry run] $installer $($arguments -join ' ')"
        exit 0
    }

    & $installer @arguments
    exit $LASTEXITCODE
}

main
