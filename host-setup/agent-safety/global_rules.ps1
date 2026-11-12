# Thin wrapper: run the cross-platform global rules writer with a Python 3 (Windows).
# All logic lives in global_rules.py so every OS runs one tested code path.
# It is idempotent and safe to re-run.
#   .\global_rules.ps1
#   .\global_rules.ps1 --report
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "global_rules.py"

# The same launcher order install.ps1 uses, so the kit and the rules writer run under one interpreter.
if (Get-Command "py" -ErrorAction SilentlyContinue) {
    & py -3 $script @args
} elseif (Get-Command "python3" -ErrorAction SilentlyContinue) {
    & python3 $script @args
} elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
    # Verify a bare `python` is Python 3 before handing it Python 3 syntax.
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

# Propagate the writer's exit code - a native command's non-zero exit does not stop the script.
exit $LASTEXITCODE
