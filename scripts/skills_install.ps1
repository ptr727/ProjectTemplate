# Thin wrapper: run the cross-platform installer with a Python 3.7+ (Windows).
# All logic lives in skills_install.py so every OS runs one tested code path.
# It is idempotent and safe to re-run.
#   Run: scripts\skills_install.ps1
#   Or: $env:AGENTS_HOME = "C:\path"; scripts\skills_install.ps1   # override the target (testing)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "skills_install.py"

# Try each candidate in order, version-checking it before committing to it.
# None of these launchers guarantees 3.7+ by construction.
# `py -3` and `python3` can both resolve to an old 3.6 interpreter on some systems.
# Picking the first *available* launcher and checking only that one would fail the whole install on a machine where an earlier candidate is stale but a later one is fine.
$candidates = @(
    @{ Exe = "py"; Args = @("-3") },
    @{ Exe = "python3"; Args = @() },
    @{ Exe = "python"; Args = @() }
)

$pyExe = $null
$pyArgs = @()
foreach ($c in $candidates) {
    if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) {
        continue
    }
    # The installer uses `from __future__ import annotations`, which needs 3.7+.
    # A bare `python` may be Python 2 on some systems.
    & $c.Exe @($c.Args) -c "import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $pyExe = $c.Exe
        $pyArgs = $c.Args
        break
    }
}

if (-not $pyExe) {
    Write-Error "Python 3.7+ is required and was not found on PATH (tried py -3, python3, python)."
    exit 1
}

# $pyArgs is splatted rather than sliced from a combined array.
# PowerShell's `1..0` range operator returns a descending @(1, 0) rather than empty.
# That would corrupt a single-element launcher's argument list.
& $pyExe @pyArgs $script @args

# Propagate the installer's exit code - a native command's non-zero exit does not stop the script.
exit $LASTEXITCODE
