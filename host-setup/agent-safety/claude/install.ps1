# Thin wrapper: run the cross-platform installer with a Python 3 (Windows).
# All logic lives in install.py so every OS runs one tested code path.
# It is idempotent and safe to re-run.
#   .\install.ps1
#   $env:CLAUDE_HOME = "C:\path"; .\install.ps1   # override the target (testing)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "install.py"

# Try each candidate and take the first that runs and reports Python 3.11 or newer.
# A name being on PATH is not enough: the WindowsApps `python3` alias stub is found by Get-Command, prints a Store message, and installs nothing.
# A bare `python` is the last resort, since it is Python 2 on some systems.
# The probe's stderr stays visible so a candidate that fails says why, and a mismatch falls through to the next.
$probe = 'import sys; print("ok" if sys.version_info >= (3, 11) else "old")'
$candidates = @(
    @{ Name = "py"; Args = @("-3") }
    @{ Name = "python3"; Args = @() }
    @{ Name = "python"; Args = @() }
)
foreach ($candidate in $candidates) {
    if (-not (Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue)) { continue }
    $prefix = $candidate.Args
    $out = & $candidate.Name @prefix -c $probe
    if ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq "ok") {
        & $candidate.Name @prefix $script @args
        exit $LASTEXITCODE
    }
    [Console]::Error.WriteLine("Passed over $($candidate.Name): it did not report Python 3.11 or newer.")
}
Write-Error "Python 3.11+ is required and was not found on PATH (tried py -3, python3, python)."
exit 1
