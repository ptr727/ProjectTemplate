# Thin wrapper: run the cross-platform installer with the available python (Windows).
# All logic lives in install.py so every OS runs one tested code path. Idempotent; safe to re-run.
#   .\install.ps1
#   $env:CLAUDE_HOME = "C:\path"; .\install.ps1   # override the target (testing)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "install.py"

if (Get-Command "python" -ErrorAction SilentlyContinue) {
    & python $script @args
} elseif (Get-Command "python3" -ErrorAction SilentlyContinue) {
    & python3 $script @args
} elseif (Get-Command "py" -ErrorAction SilentlyContinue) {
    & py -3 $script @args
} else {
    Write-Error "Python is required and was not found on PATH (tried python, python3, py)."
    exit 1
}
