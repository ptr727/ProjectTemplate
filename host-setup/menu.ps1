# A human-facing front end over the scripts this fleet otherwise authors for an agent following instructions: the host tooling in host-setup\windows\, and the repo-level tools in scripts\ and spec\ that ptr727/ProjectTemplate hosts and every other repo reaches rather than carries.
# Menu options rather than a command a human has to already know, and a forcing function on the tools it fronts: a task with no discoverable menu entry is a gap in the tools themselves.
#
# Fetchable on its own, like bootstrap.ps1: run from a hub checkout directly, or download this one file into a downstream repo and it clones the hub itself.
# Where bootstrap.ps1 stands a host up and stops, this loops so a human answers more than one question in a sitting, and it knows the difference between "the hub" and "a repo this host happens to be sitting in" so it can offer each their own tasks.
#
# Runs under Windows PowerShell 5.1, the one shell a fresh Windows host guarantees, and hands off to PowerShell 7 the same way bootstrap.ps1 does, since every host-setup\windows script it drives requires that version.

[CmdletBinding()]
param(
    [Alias('n')][switch]$DryRun,
    [Alias('y')][switch]$Yes,
    [string]$Ref,
    [string]$Dir,
    [switch]$Keep,
    [Alias('h')][switch]$Help
)

# Captured before anything else touches scope, so the pwsh handoff below can rebuild the argument list this process was bound with, rather than reaching for an automatic variable from inside a nested function.
$SCRIPT_BOUND_PARAMETERS = $PSBoundParameters

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# A non-zero exit from git or a driven script is an answer here rather than a failure.
$PSNativeCommandUseErrorActionPreference = $false

$HUB_REPO = 'ptr727/ProjectTemplate'
$HUB_URL = "https://github.com/$HUB_REPO"
$DEFAULT_REF = 'main'

# Every parameter is read into a variable here rather than from inside a function, matching bootstrap.ps1: a parameter reached only from a nested scope reads as declared and never used.
$DRY_RUN = [bool]$DryRun
$ASSUME_YES = [bool]$Yes
$REF = if ($Ref) { $Ref } else { $DEFAULT_REF }
$KEEP = [bool]$Keep
$WANT_HELP = [bool]$Help

# No placeholder for $DIR here, matching bootstrap.ps1: PowerShell variable names are case-insensitive, so $DIR and the -Dir parameter $Dir are the same variable, and Resolve-Directory's own assignment in main is what gives it its resolved value.
$HUB_ROOT = ''
$HUB_FETCHED = $false
$IS_HUB_CHECKOUT = $false
$DOWNSTREAM_ROOT = ''
$DOWNSTREAM_NAME = ''
$PWSH_PATH = ''
$QUIT = $false

# --- Output ---

function log { param([string]$Message = '') Write-Host $Message }
function info { param([string]$Message) Write-Host "  $Message" }
function step { param([string]$Message) Write-Host "`n==> $Message" }
function warn { param([string]$Message) [Console]::Error.WriteLine("WARNING: $Message") }
function die { param([string]$Message) [Console]::Error.WriteLine("ERROR: $Message"); exit 1 }
# Reports a task-time error without ending the process, unlike die: a dispatched action's failure returns to the menu, and only a startup failure (bad arguments, no git) is fatal.
function fail { param([string]$Message) [Console]::Error.WriteLine("ERROR: $Message") }

function usage {
    # The closing marker of a here-string has to sit at column 0, so this block is deliberately unindented.
    Write-Host @'
Usage: menu.ps1 [options]

An interactive menu over this fleet's host and repo tooling: update the host tools, upgrade the
OS packages, install the fleet skills, audit a cataloged repo, and pull the hub's verbatim-owned
files into a downstream repo's own worktree. Run from a hub checkout or from any other repo. The
menu shows each the tasks that apply to it.

Options:
  -y, -Yes          Pass -Yes to each tool this menu runs, so a tool does not prompt. The menu's
                    own choice, confirmation, and repo-name prompts still ask.
  -n, -DryRun       Print what each step would run, change nothing
      -Ref REF      Hub branch, tag, pull request ref, or commit to run from, default main
      -Dir PATH     Where a fetched hub checkout is cloned, default %LOCALAPPDATA%\host-setup
      -Keep         Leave a fetched hub checkout in place, which is removed by default
  -h, -Help         Show this help

With no console to ask on, this prints the same reminder bootstrap.ps1 does and exits, since a
redirected run is not a place to answer a menu.
'@
}

# --- pwsh handoff ---
#
# Everything above this point, and the four functions below, run under Windows PowerShell 5.1: no ternary or null-coalescing operator, no multi-argument Join-Path, nothing newer than that runtime parses.
# Everything past the handoff may use whatever pwsh 7 accepts, though it mostly does not need to.
# Identical in shape to bootstrap.ps1's own handoff, kept as a separate copy rather than a shared module: a script fetched alone would otherwise fail on a missing sibling file.

function Resolve-Pwsh {
    $command = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @((Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'))
    if (${env:ProgramFiles(x86)}) { $candidates += (Join-Path ${env:ProgramFiles(x86)} 'PowerShell\7\pwsh.exe') }
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

# PowerShell 7 is deliberately not a managed host tool: a host that cannot run these scripts cannot be repaired by them.
function Install-Pwsh {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        die 'pwsh (PowerShell 7) is not installed, and winget is not on this host to install it. Install "App Installer" from the Microsoft Store, or install PowerShell 7 directly from https://aka.ms/PSWindows, then run this again.'
    }
    step 'Installing PowerShell 7'
    & winget install --id Microsoft.PowerShell --exact --source winget --accept-source-agreements --accept-package-agreements --silent --disable-interactivity | Out-Host
    $wingetExit = $LASTEXITCODE
    $found = Resolve-Pwsh
    if (-not $found) {
        die "winget exited $wingetExit installing PowerShell 7, and pwsh could still not be found. Close this console and paste the setup lines again, or install it from https://aka.ms/PSWindows."
    }
    return $found
}

# Rebuilds the arguments this process was bound with, since a param() bound script has no raw $args left to forward.
function Get-ForwardedArgument {
    $forward = @()
    foreach ($key in $script:SCRIPT_BOUND_PARAMETERS.Keys) {
        $value = $script:SCRIPT_BOUND_PARAMETERS[$key]
        if ($value -is [switch]) {
            if ($value.IsPresent) { $forward += "-$key" }
        } else {
            $forward += "-$key"
            $forward += "$value"
        }
    }
    return , $forward
}

function Invoke-PwshHandoff {
    $pwshPath = Resolve-Pwsh
    if (-not $pwshPath) { $pwshPath = Install-Pwsh }
    $forward = Get-ForwardedArgument
    & $pwshPath -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath @forward
    exit $LASTEXITCODE
}

# --- Hub resolution ---

# The owner/name from a remote.origin.url in any of the shapes git or the GitHub UI hand out, or empty where the checkout carries no origin at all.
function Get-OriginSlug {
    param([Parameter(Mandatory)][string]$Root)
    $url = (& git -C $Root config --get remote.origin.url 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $url) { return '' }
    $slug = "$url".Trim()
    $slug = $slug -replace '^git@github\.com:', ''
    $slug = $slug -replace '^ssh://git@github\.com/', ''
    $slug = $slug -replace '^https://github\.com/', ''
    $slug = $slug -replace '\.git$', ''
    return $slug
}

# A marker sitting beside the clone rather than inside it, so a -Dir pointed at a directory this run does not own is never the one removed on exit.
function Get-MarkerPath { Join-Path $script:DIR 'hub.owned' }

# Answers whether an existing $DIR\hub may be removed: absent, or created by this run.
function Test-HubRemovable {
    $hubPath = Join-Path $script:DIR 'hub'
    if (-not (Test-Path $hubPath)) { return $true }
    if (Test-Path (Get-MarkerPath)) { return $true }
    fail "$hubPath exists and this run did not create it, so it will not be removed. Pass -Dir to choose another cache location."
    return $false
}

# Windows has no peer to flock, so a session-scoped named mutex serializes a fetch against a second menu.ps1 sharing this -Dir instead.
function Get-HubMutexName {
    $safe = ($script:DIR -replace '[^A-Za-z0-9]', '_')
    return "ProjectTemplateHostSetupMenu_$safe"
}

function Invoke-FetchHub {
    # -DryRun promises to change nothing, and fetching is the one real change this whole script makes to the host.
    if ($script:DRY_RUN) {
        fail "This task needs a fetched hub checkout, and fetching one is itself a change -DryRun does not make. Run without -DryRun, or from inside a hub checkout already on $script:DEFAULT_REF."
        return $false
    }
    New-Item -ItemType Directory -Path $script:DIR -Force | Out-Null
    $mutex = New-Object System.Threading.Mutex($false, (Get-HubMutexName))
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne()
        } catch [System.Threading.AbandonedMutexException] {
            # A prior holder crashed mid-fetch, which leaves the tree it was writing rather than the lock itself in a bad state, so ownership passes to this run.
            $acquired = $true
        }
        return (Invoke-FetchHubLocked)
    } finally {
        # Released here, once the fetch itself finishes, rather than held for the rest of this session: Invoke-InteractiveMenu's loop keeps a session alive well past its one fetch, and holding the lock that long would block every other menu.ps1 sharing this -Dir until this session quits.
        # A second session starting its own fetch while this one is still reading the tree it just cloned is the accepted residual race left by that choice, the same one menu.sh's own flock accepts for the same reason.
        if ($acquired) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function Invoke-FetchHubLocked {
    step "Fetching $script:HUB_REPO at $script:REF"
    if (-not (Test-HubRemovable)) { return $false }
    $hubPath = Join-Path $script:DIR 'hub'
    if (Test-Path $hubPath) { Remove-Item -Recurse -Force $hubPath }
    # Marked as ours before git can create anything under $hubPath, not only once the clone also succeeds: git can leave a partial directory behind on a failed or interrupted clone, and an unmarked one would then block every retry until removed by hand.
    New-Item -ItemType File -Path (Get-MarkerPath) -Force | Out-Null
    # A full clone of the default branch first, whatever -Ref names: spec\audit.py walks the hub's own history to judge whether a carried copy is trailing the file it was copied from, and a shallow clone would read every file as changed at the truncation boundary.
    # Piped to Out-Host rather than left bare: an unassigned native call's stdout otherwise joins this function's own return value, which return $true/$false below would then be appended to instead of replacing.
    & git clone --quiet --branch $script:DEFAULT_REF --single-branch $script:HUB_URL $hubPath | Out-Host
    if ($LASTEXITCODE -ne 0) {
        fail "Could not clone $script:HUB_REPO. Check that this host reaches github.com."
        Remove-Item -Recurse -Force $hubPath -ErrorAction SilentlyContinue
        return $false
    }
    if ($script:REF -ne $script:DEFAULT_REF) {
        & git -C $hubPath fetch --quiet origin $script:REF | Out-Host
        if ($LASTEXITCODE -ne 0) {
            fail "Could not fetch $script:REF from $script:HUB_REPO. Check the ref exists."
            return $false
        }
        & git -C $hubPath checkout --quiet FETCH_HEAD | Out-Host
        if ($LASTEXITCODE -ne 0) {
            fail "Could not check out $script:REF"
            return $false
        }
    }
    $script:HUB_ROOT = $hubPath
    $script:HUB_FETCHED = $true
    info "Cloned to $script:HUB_ROOT"
    return $true
}

# Whether the current checkout is the hub, by origin identity alone, independent of -Ref or of whether that checkout is fresh enough to reuse.
function Test-HubCheckout {
    $top = (& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $top) { return }
    $top = "$top".Trim()
    if ((Get-OriginSlug $top) -ne $script:HUB_REPO) { return }
    $script:IS_HUB_CHECKOUT = $true
    # Only for the default ref: naming any other -Ref always fetches fresh, even from inside the hub itself.
    if ($script:REF -eq $script:DEFAULT_REF) { $script:HUB_ROOT = $top }
}

# Confirms a tentative local HUB_ROOT still matches a clean, freshly fetched origin/main before any tool reads it.
# A local checkout that has moved on falls back to a real fetch rather than being trusted.
function Confirm-HubRoot {
    if ($script:HUB_ROOT) {
        # -DryRun trusts an already-known checkout (this hub checkout, or one this run already fetched) as is, rather than fetching to confirm it is still fresh: confirming means fetching, and fetching is a change -DryRun does not make.
        if ($script:DRY_RUN) { return $true }
        & git -C $script:HUB_ROOT fetch --quiet origin $script:DEFAULT_REF | Out-Host
        if ($LASTEXITCODE -eq 0) {
            $status = & git -C $script:HUB_ROOT status --porcelain
            if (-not $status) {
                $head = "$(& git -C $script:HUB_ROOT rev-parse HEAD)".Trim()
                $originHead = "$(& git -C $script:HUB_ROOT rev-parse "origin/$script:DEFAULT_REF")".Trim()
                if ($head -eq $originHead) { return $true }
            }
        }
        $script:HUB_ROOT = ''
    }
    if ($script:DRY_RUN) {
        fail "This task needs a fetched hub checkout, and fetching one is itself a change -DryRun does not make. Run without -DryRun, or from inside a hub checkout already on $script:DEFAULT_REF."
        return $false
    }
    return (Invoke-FetchHub)
}

# A downstream repo is whatever git repo the menu is run from, when that repo is not the hub itself.
# Gated on IS_HUB_CHECKOUT rather than HUB_ROOT: the hub is never a downstream repo, even when -Ref left HUB_ROOT unset.
function Test-DownstreamCheckout {
    if ($script:IS_HUB_CHECKOUT) { return }
    $top = (& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $top) { return }
    $top = "$top".Trim()
    $script:DOWNSTREAM_ROOT = $top
    $slug = Get-OriginSlug $top
    # A checkout with no origin falls back to its own directory name, more useful than an empty slug's basename.
    $script:DOWNSTREAM_NAME = if ($slug) { Split-Path -Leaf $slug } else { Split-Path -Leaf $top }
}

function Invoke-Cleanup {
    if ($script:KEEP -or -not $script:HUB_FETCHED) { return }
    if (-not (Test-Path (Get-MarkerPath))) { return }
    Remove-Item -Recurse -Force (Join-Path $script:DIR 'hub') -ErrorAction SilentlyContinue
    Remove-Item -Force (Get-MarkerPath) -ErrorAction SilentlyContinue
}

# --- Running a tool ---

# Every host tool runs from inside the hub tree, and this is the only place a path inside it is named.
# Spawned as its own pwsh process rather than dot-sourced, since every host-setup\windows script ends its own main with exit, which would otherwise end this menu too.
function Invoke-HostTool {
    param([Parameter(Mandatory)][string]$Tool, [Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    if (-not (Confirm-HubRoot)) { return 1 }
    $path = Join-Path $script:HUB_ROOT 'host-setup\windows' | Join-Path -ChildPath $Tool
    if (-not (Test-Path $path)) {
        fail "$script:HUB_ROOT carries no $Tool at host-setup\windows, so this ref is not one to run tasks from"
        return 1
    }
    $flags = @()
    if ($script:ASSUME_YES) { $flags += '-Yes' }
    if ($script:DRY_RUN) { $flags += '-DryRun' }
    # Out-Host again, for the same reason as the git calls above: bare, this would join the exit code below into one leaked return value.
    & $script:PWSH_PATH -NoProfile -ExecutionPolicy Bypass -File $path @Arguments @flags | Out-Host
    return $LASTEXITCODE
}

# The first candidate that is a Python 3.7+, the same probe install-skills.ps1 uses: none of py, python3 or python guarantees that version by construction on Windows.
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

# The Python tools under scripts\ and spec\ resolve their own root from __file__ rather than the working directory, so they are called by absolute path from wherever this script runs.
# Checked here rather than upfront in main: a host with no interpreter yet can still use every host action, and only the actions that need one name it as their own prerequisite.
function Invoke-HubPython {
    param([Parameter(Mandatory)][string]$ScriptPath, [Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    $python = Find-Python
    if (-not $python) {
        fail 'Python 3.7+ is required for this task. host-setup\windows\install-tools.ps1 provides it.'
        return 127
    }
    # Out-Host again, for the same reason as Invoke-HostTool.
    & $python.Exe @($python.Arguments) (Join-Path $script:HUB_ROOT $ScriptPath) @Arguments | Out-Host
    return $LASTEXITCODE
}

# --- Actions ---

function Invoke-AuditRepo {
    $default = if ($script:DOWNSTREAM_NAME) { $script:DOWNSTREAM_NAME } else { $script:HUB_REPO }
    $default = $default.Split('/')[-1]
    $name = Read-Host "Repo to audit [$default]"
    if (-not $name) { $name = $default }
    if (-not (Confirm-HubRoot)) { return 1 }
    return (Invoke-HubPython -ScriptPath 'spec/audit.py' -Arguments $name)
}

function Invoke-CheckSkillsDist {
    if (-not (Confirm-HubRoot)) { return 1 }
    $rc = Invoke-HubPython -ScriptPath 'scripts/build_dist.py' -Arguments '--check'
    # Only 0 (clean) and 1 (stale) are outcomes scripts\build_dist.py --check documents for itself, so only those two read as a check result.
    # Anything else is this task failing to run rather than a finding.
    switch ($rc) {
        0 { info 'Every generated Skills distribution matches .agents/skills/'; return 0 }
        1 { info 'A generated Skills distribution is stale. This menu does not regenerate it from a fetched checkout, since the result has to be committed in the hub itself.'; return 0 }
        default {
            fail "scripts/build_dist.py --check did not run to completion (exit $rc)"
            return 1
        }
    }
}

function Invoke-CarryAction {
    param([Parameter(Mandatory)][string]$Mode)
    if (-not $script:DOWNSTREAM_ROOT) {
        fail "No downstream repo checkout found. Run this menu from inside the target repo's own worktree."
        return 1
    }
    # A non-default -Ref checks out something other than the origin/main scripts/carry.py's hub argument requires, so this is refused here with the actual reason rather than failing deep inside that tool.
    if ($script:REF -ne $script:DEFAULT_REF) {
        fail "Pulling hub files needs the hub's $script:DEFAULT_REF branch, and this session was started with -Ref $script:REF. Run without -Ref, or start a separate session on $script:DEFAULT_REF for this task."
        return 1
    }
    $default = $script:DOWNSTREAM_NAME
    $name = Read-Host "Repo name as cataloged in registry/repos.json [$default]"
    if (-not $name) { $name = $default }
    if (-not (Confirm-HubRoot)) { return 1 }
    return (Invoke-HubPython -ScriptPath 'scripts/carry.py' -Arguments $Mode, $name, '--target', $script:DOWNSTREAM_ROOT)
}

# --- Menu ---

function Show-MenuHeading {
    $hubSuffix = if ($script:HUB_ROOT) { " ($script:HUB_ROOT)" } else { '' }
    log "Hub:        $script:HUB_REPO$hubSuffix"
    if ($script:DOWNSTREAM_ROOT) {
        log "Downstream: $script:DOWNSTREAM_NAME ($script:DOWNSTREAM_ROOT)"
    } else {
        log "Downstream: none (run from inside a repo's own checkout for the pull-from-hub tasks)"
    }
}

function Show-Menu {
    log ''
    Show-MenuHeading
    log ''
    log 'Host, on this machine:'
    log '   1  Report installed host tools'
    log '   2  Install missing host tools'
    log '   3  Upgrade the host tools that trail upstream'
    log '   4  Report the host OS upgrade status'
    log '   5  Upgrade the host OS packages'
    log '   6  Report git and GitHub setup'
    log '   7  Configure git and GitHub'
    # Only setup-wsl.ps1's -Status runs here: its -Install needs a distribution name, which no menu entry collects, and installing one nobody asked for is exactly what this stays out of.
    log '   8  Report the WSL platform and the distributions installed'
    log '   9  Report fleet Skills install status'
    log '  10  Install or update the fleet Skills'
    log ''
    log 'Hub, ptr727/ProjectTemplate:'
    log '  11  Audit a cataloged repo'
    log '  12  Check the generated Skills distributions are current'
    # Also gated on REF: scripts/carry.py always rejects a hub checkout that is not exactly on the default ref.
    if ($script:DOWNSTREAM_ROOT -and $script:REF -eq $script:DEFAULT_REF) {
        log ''
        log 'Downstream, the repo this menu is run from:'
        log '  13  Check what the hub would change here, change nothing'
        log "  14  Pull the hub's verbatim-owned files into this repo"
    }
    log ''
    log '   q  Quit'
    log ''
}

# A failing task is reported and returns to the menu rather than ending the session, so this cannot double as "quit": QUIT is a separate flag the q/Q case sets, read by the loop after every dispatch regardless of whether the task it ran succeeded.
function Invoke-Dispatch {
    param([string]$Choice)
    switch ($Choice) {
        '1' { return (Invoke-HostTool -Tool 'install-tools.ps1' -Arguments '-Report') }
        '2' { return (Invoke-HostTool -Tool 'install-tools.ps1' -Arguments '-Install') }
        '3' { return (Invoke-HostTool -Tool 'install-tools.ps1' -Arguments '-Upgrade') }
        '4' { return (Invoke-HostTool -Tool 'upgrade-host.ps1' -Arguments '-Status') }
        '5' { return (Invoke-HostTool -Tool 'upgrade-host.ps1' -Arguments '-Packages') }
        '6' { return (Invoke-HostTool -Tool 'setup-github.ps1' -Arguments '-Status') }
        '7' { return (Invoke-HostTool -Tool 'setup-github.ps1' -Arguments '-Configure') }
        '8' { return (Invoke-HostTool -Tool 'setup-wsl.ps1' -Arguments '-Status') }
        '9' { return (Invoke-HostTool -Tool 'install-skills.ps1' -Arguments '-Report') }
        '10' { return (Invoke-HostTool -Tool 'install-skills.ps1') }
        '11' { return (Invoke-AuditRepo) }
        '12' { return (Invoke-CheckSkillsDist) }
        '13' { return (Invoke-CarryAction 'check') }
        '14' {
            # -DryRun changes nothing, and scripts/carry.py itself has no dry-run mode, so a dry-run apply reads as its own check instead of silently mutating the downstream worktree.
            if ($script:DRY_RUN) { return (Invoke-CarryAction 'check') }
            return (Invoke-CarryAction 'apply')
        }
        { $_ -in @('q', 'Q') } { $script:QUIT = $true; return 0 }
        default {
            warn 'Not one of the choices'
            return 2
        }
    }
}

function Invoke-InteractiveMenu {
    while ($true) {
        Show-Menu
        $choice = Read-Host 'Choose'
        $script:QUIT = $false
        $rc = Invoke-Dispatch $choice
        if ($script:QUIT) { break }
        # An unrecognized choice is rc 2, already warned by Invoke-Dispatch, so this loops straight back rather than reading a pointless confirmation.
        if ($rc -eq 2) { continue }
        if ($rc -eq 0) {
            step 'Done'
        } else {
            warn 'That task ended with an error'
        }
        Read-Host 'Press Enter to return to the menu' | Out-Null
    }
}

# --- Entry ---

# Both are checked because a scheduled task reports one and not the other, and either alone misses a case.
# The ISE is checked apart, because it answers both of those as interactive and then blocks on a Read-Host it does not render usably.
function Test-Interactive {
    if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) { return $false }
    if ($Host.Name -eq 'Windows PowerShell ISE Host') { return $false }
    return $true
}

# An absolute path, and never a drive or UNC share root, since everything below it is created and removed under it.
function Resolve-Directory {
    if (-not $script:Dir) { return (Join-Path $env:LOCALAPPDATA 'host-setup') }
    if (-not [IO.Path]::IsPathRooted($script:Dir)) { die "-Dir takes an absolute path, and `"$($script:Dir)`" is relative" }
    # Canonicalized before the root check, since a lexically rooted "C:\temp\.." is not the string "C:\" but resolves to it the moment anything below opens a path under it, matching menu.sh's own readlink -m step.
    $canonical = [IO.Path]::GetFullPath($script:Dir)
    $trimmed = $canonical.TrimEnd('\', '/')
    if ((-not $trimmed) -or ($trimmed -match '^[A-Za-z]:$') -or ($trimmed -match '^\\\\[^\\]+\\[^\\]+$')) { die '-Dir may not be a drive or share root' }
    return $trimmed
}

function main {
    if ($script:WANT_HELP) { usage; exit 0 }

    if ($PSVersionTable.PSVersion.Major -lt 7) {
        Invoke-PwshHandoff
    }

    # Reached only under a confirmed pwsh 7, either started that way or handed off to above.
    $script:PWSH_PATH = (Get-Process -Id $PID).Path
    $script:DIR = Resolve-Directory

    if (-not (Test-Interactive)) {
        warn 'No console to ask on, so there is no menu to show'
        info 'Download the file and run it, rather than piping it, to reach the menu:'
        info '  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12'
        info "  Invoke-WebRequest -UseBasicParsing -Uri https://raw.githubusercontent.com/$script:HUB_REPO/$script:DEFAULT_REF/host-setup/menu.ps1 -OutFile menu.ps1"
        info '  powershell -ExecutionPolicy Bypass -File menu.ps1'
        exit 0
    }

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { die 'git is required' }

    try {
        Test-HubCheckout
        Test-DownstreamCheckout
        Invoke-InteractiveMenu
    } finally {
        Invoke-Cleanup
    }
}

main
