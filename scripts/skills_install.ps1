# Thin wrapper: run the cross-platform installer with a Python 3.7+ (Windows).
# All logic lives in skills_install.py so every OS runs one tested code path.
# It is idempotent and safe to re-run.
#   Run: scripts\skills_install.ps1
#   Or: $env:AGENTS_HOME = "C:\path"; scripts\skills_install.ps1   # override the target (testing)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "skills_install.py"

# Prefer launchers that are unambiguously Python 3, in the order tried below.
# $pyArgs is splatted rather than sliced from a combined array.
# PowerShell's `1..0` range operator returns a descending @(1, 0) rather than empty.
# That would corrupt a single-element launcher's argument list.
# None of these launchers guarantees 3.7+ by construction.
# `py -3` and `python3` can both resolve to an old 3.6 interpreter on some systems.
# The version is checked once, after selection, covering every branch.
$pyExe = $null
$pyArgs = @()
if (Get-Command "py" -ErrorAction SilentlyContinue) {
    $pyExe = "py"
    $pyArgs = @("-3")
} elseif (Get-Command "python3" -ErrorAction SilentlyContinue) {
    $pyExe = "python3"
} elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
    $pyExe = "python"
}

if (-not $pyExe) {
    Write-Error "Python 3.7+ is required and was not found on PATH (tried py -3, python3, python)."
    exit 1
}

# The installer uses `from __future__ import annotations`, which needs 3.7+.
# A bare `python` may be Python 2 on some systems.
& $pyExe @pyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Found $pyExe on PATH but it is not Python 3.7+. Install Python 3.7 or newer."
    exit 1
}

& $pyExe @pyArgs $script @args

# Propagate the installer's exit code - a native command's non-zero exit does not stop the script.
exit $LASTEXITCODE
