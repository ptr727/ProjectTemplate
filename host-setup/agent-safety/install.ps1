# Thin wrapper: run the cross-platform installer with a Python 3 (Windows).
# All logic lives in install.py so every OS runs one tested code path. Idempotent; safe to re-run.
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
    & python $script @args
} else {
    Write-Error "Python 3 is required and was not found on PATH (tried py -3, python3, python)."
    exit 1
}
