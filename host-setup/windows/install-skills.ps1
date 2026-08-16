# Installs the fleet skills for the current user, by driving the hub-hosted installer in the tree this script sits in.
# This is the one script here that reaches outside host-setup/, deliberately: the skills content lives at the tree root, so a copy fetched without the tree has nothing to install, and the independent-fetchability rule the sibling scripts follow cannot apply to it.
# Python is its one dependency, which is why the bootstrap runs it last: install-tools.ps1 provides the interpreter before this needs one.
#
# Every step is idempotent, because the installer it drives is.
# SKILLS_SOURCE_COMMIT, where the caller sets it, names the commit of a tree git cannot answer for, which is what a tarball fetched by the bootstrap is.

[CmdletBinding()]
param(
    [Alias('r')][switch]$Report,
    [Alias('n')][switch]$DryRun,
    [Alias('y')][switch]$Yes,
    [Alias('h')][switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# A non-zero exit from a probed interpreter is an answer here rather than a failure.
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
Usage: install-skills.ps1 [options]

Installs the fleet skills for the current user, by running scripts/skills_install.py from this
tree. The installer copies the skills to ~/.agents/skills/ for Codex and opencode, registers the
Claude Code plugin where the claude CLI is present and says so where it is not, and stamps what it
installed so a later report can answer whether this machine is current.

Actions, name one, default install:
  -r, -Report       Read-only: is this machine's skills install current against this tree?
  -h, -Help         Show this help

Options:
  -n, -DryRun       Print the command instead of running it
  -y, -Yes          Accepted for symmetry with the sibling scripts, since the installer never prompts
'@
}

# The first candidate that is a Python 3.7+, since the installer needs `from __future__ import annotations`.
# Each available launcher is version-checked before being committed to, because none guarantees 3.7+ by construction.
function Find-Python {
    $candidates = @(
        @{ Exe = 'py'; Arguments = @('-3') },
        @{ Exe = 'python3'; Arguments = @() },
        @{ Exe = 'python'; Arguments = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        & $candidate.Exe @($candidate.Arguments) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}

function main {
    if ($WANT_HELP) { usage; exit 0 }

    $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $installer = Join-Path $root 'scripts' | Join-Path -ChildPath 'skills_install.py'
    if (-not (Test-Path $installer)) {
        die 'This tree carries no scripts/skills_install.py, so there is nothing to drive'
    }

    $python = Find-Python
    if (-not $python) {
        die 'Python 3.7+ not found. install-tools.ps1 provides it, so run the tools step first and this one after.'
    }

    $arguments = @()
    if ($WANT_REPORT) { $arguments += '--report' }

    if ($DRY_RUN) {
        info "[dry run] $($python.Exe) $($python.Arguments -join ' ') $installer $($arguments -join ' ')"
        exit 0
    }

    & $python.Exe @($python.Arguments) $installer @arguments
    exit $LASTEXITCODE
}

main
