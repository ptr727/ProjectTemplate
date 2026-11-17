# Pulls exactly the buckets it is given, and derives none from the host name or a file prefix.
# It reaches outside host-setup\, so a copy fetched without the tree has nothing to drive.

[CmdletBinding()]
param(
    [Alias('b')][string[]]$Bucket,
    [Alias('n')][switch]$DryRun,
    [Alias('y')][switch]$Yes,
    [Alias('h')][switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

# Names arrive as separate words or as one comma separated word, since -File hands a comma list over as a single string.
$BUCKET_NAMES = @($Bucket | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$DRY_RUN = [bool]$DryRun
$ASSUME_YES = [bool]$Yes
$WANT_HELP = [bool]$Help

function die { param([string]$Message) [Console]::Error.WriteLine("ERROR: $Message"); exit 1 }

function usage {
    # The closing marker of a here-string has to sit at column 0, so this block is deliberately unindented.
    Write-Host @'
Usage: restore-secrets.ps1 -Bucket NAME[,NAME ...] [options]

Pulls the named secret buckets into this user's profile, by running scripts\secrets_sync.py pull
from this tree. A bucket name takes lowercase letters, digits, and hyphens, and starts with a letter
or digit. Only the buckets named are pulled. The sync prompts before it replaces a file, and it needs
a terminal on stdin to write.

Run it as yourself and not elevated, since the files belong in your own profile. It needs op, which
install-tools.ps1 -Install op provides, and Python 3.11 or newer.

Options:
  -b, -Bucket LIST  Pull these buckets, LIST being names separated by commas
  -n, -DryRun       Print the plan and the op writes, send none
  -y, -Yes          Answer the sync's confirmation prompt yes
  -h, -Help         Show this help
'@
}

# Only Windows has the elevation the check reads, and a test host elsewhere has nothing to refuse.
function Test-Elevated {
    if (-not $IsWindows) { return $false }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# The first candidate that is Python 3.11 or newer, which the sync needs.
function Find-Python {
    $candidates = @(
        @{ Exe = 'py'; Arguments = @('-3') },
        @{ Exe = 'python3'; Arguments = @() },
        @{ Exe = 'python'; Arguments = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        & $candidate.Exe @($candidate.Arguments) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}

function main {
    if ($WANT_HELP) { usage; exit 0 }
    if ($PSVersionTable.PSVersion.Major -lt 7) {
        die "This script needs PowerShell 7 or later, and this is $($PSVersionTable.PSVersion). Install it with: winget install --id Microsoft.PowerShell --exact --source winget"
    }
    if ($BUCKET_NAMES.Count -eq 0) { die 'Name at least one bucket with -Bucket' }
    foreach ($name in $BUCKET_NAMES) {
        if ($name -cnotmatch '^[a-z0-9][a-z0-9-]*$') { die "-Bucket takes lowercase letters, digits, and hyphens that start with a letter or digit, not '$name'" }
    }
    if (Test-Elevated) {
        die 'The files belong in your own profile, so this runs as that user and not elevated, run it from a normal PowerShell window'
    }

    $sync = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'scripts' | Join-Path -ChildPath 'secrets_sync.py'
    if (-not (Test-Path -LiteralPath $sync)) {
        die 'This tree carries no scripts\secrets_sync.py, so there is nothing to drive'
    }

    $arguments = @('pull')
    foreach ($name in $BUCKET_NAMES) { $arguments += @('--bucket', $name) }
    if ($DRY_RUN) { $arguments += '--dry-run' }
    if ($ASSUME_YES) { $arguments += '--yes' }

    # A dry run on a host that has not installed op yet prints the command rather than failing on the missing tool.
    $hasOp = [bool](Get-Command op -ErrorAction SilentlyContinue)
    if ($DRY_RUN -and -not $hasOp) {
        Write-Host "Would run: python $sync $($arguments -join ' ')"
        exit 0
    }
    if (-not $hasOp) { die 'op is not installed. install-tools.ps1 -Install op provides it, so run that first.' }

    $python = Find-Python
    if (-not $python) {
        die 'Python 3.11 or newer not found, and the sync needs one.'
    }

    & $python.Exe @($python.Arguments) $sync @arguments
    exit $LASTEXITCODE
}

main
