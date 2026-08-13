# Thin wrapper: run the cross-platform installer with a Python 3 (Windows).
# All logic lives in skills_install.py so every OS runs one tested code path.
# It is idempotent and safe to re-run.
#   .\skills_install.ps1
#   $env:AGENTS_HOME = "C:\path"; .\skills_install.ps1   # override the target (testing)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "skills_install.py"

# Prefer launchers that are unambiguously Python 3.
# The installer uses Python 3 syntax, so a bare `python`, which is Python 2 on some systems, is the last resort.
if (Get-Command "py" -ErrorAction SilentlyContinue) {
    & py -3 $script @args
} elseif (Get-Command "python3" -ErrorAction SilentlyContinue) {
    & python3 $script @args
} elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
    # Verify a bare `python` is Python 3 before handing it Python 3 syntax, since it is Python 2 on some setups and would fail to parse skills_install.py.
    # The two branches above are Python 3 by construction, so only this one needs the check.
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
