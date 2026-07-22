# Thin wrapper: run the cross-platform installer with a Python 3 (Windows).
# All logic lives in install.py so every OS runs one tested code path. Idempotent, safe to re-run.
#   .\install.ps1
#   $env:CLAUDE_HOME = "C:\path"; .\install.ps1   # override the target (testing)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "install.py"

# Prefer launchers that are unambiguously Python 3. install.py and the hook use Python 3 syntax, so a
# bare `python` (Python 2 on some systems) is the last resort.
if (Get-Command "py" -ErrorAction SilentlyContinue) {
    & py -3 $script @args
} elseif (Get-Command "python3" -ErrorAction SilentlyContinue) {
    & python3 $script @args
} elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
    # Verify a bare `python` is Python 3 before handing it Python 3 syntax - it is Python 2 on some setups,
    # which would fail to parse install.py. py -3 and python3 above are Python 3 by construction.
    & python -c "import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Found python on PATH but it is not Python 3 (tried py -3, python3, python). Install Python 3."
        exit 1
    }
    & python $script @args
} else {
    Write-Error "Python 3 is required and was not found on PATH (tried py -3, python3, python)."
    exit 1
}

# Propagate the installer's exit code - a native command's non-zero exit does not stop the script.
exit $LASTEXITCODE
