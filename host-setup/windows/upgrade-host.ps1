# Upgrades this host, on native Windows.
# Two things are brought current: the packages winget manages, which is routine, and the WSL platform itself, which is the Windows peer of a kernel and moves on its own release schedule.
#
# A WSL platform update is refused while Docker Desktop is running, because Docker holds the WSL service open and the update then fails part way rather than declining.
# Refusing is the point of running this rather than the two commands by hand: each is one line, and the guard between them is not.
#
# There is no action for a Windows feature update, where the Linux peer moves a host to the next release.
# Windows Update owns that upgrade, it is not driven the way an apt sources rewrite is, and an action that pretended otherwise would be the one thing this script must not carry.

[CmdletBinding()]
param(
    [Alias('s')][switch]$Status,
    [Alias('p')][switch]$Packages,
    [Alias('w')][switch]$Wsl,
    [Alias('a')][switch]$All,
    [Alias('n')][switch]$DryRun,
    [Alias('y')][switch]$Yes,
    [Alias('h')][switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# A non-zero exit from winget or wsl is an answer here rather than a failure.
# Setting this keeps a profile that turned it on from turning every read into a terminating error.
$PSNativeCommandUseErrorActionPreference = $false

# The processes that mean Docker Desktop is up, and the reason each is here rather than the obvious alternative, is in Assert-DockerStopped.
$DOCKER_PROCESSES = @('Docker Desktop', 'com.docker.backend', 'com.docker.build')

# Where a package that winget will not move is actually upgraded from.
# One line per package rather than a rule, because the answer is the application's own and nothing about it can be worked out from winget.
$SELF_UPDATE_NOTE = @{
    'MSYS2.MSYS2' = 'Upgraded from inside msys2 with pacman, not from here.'
}

# Every parameter is read into a variable here rather than from inside a function.
# A script parameter reached only from a nested scope reads as declared and never used, which is a finding on each one and hides a parameter that genuinely is unused.
$ACTIONS = [ordered]@{
    status   = [bool]$Status
    packages = [bool]$Packages
    wsl      = [bool]$Wsl
    all      = [bool]$All
}
$WANT_HELP = [bool]$Help

$MODE = 'packages'
$DRY_RUN = [bool]$DryRun
$ASSUME_YES = [bool]$Yes
$ELEVATED = $false

# --- Output ---

function log { param([string]$Message = '') Write-Host $Message }
function info { param([string]$Message) Write-Host "  $Message" }
function step { param([string]$Message) Write-Host "`n==> $Message" }
function warn { param([string]$Message) [Console]::Error.WriteLine("WARNING: $Message") }
function die { param([string]$Message) [Console]::Error.WriteLine("ERROR: $Message"); exit 1 }

function usage {
    # The closing marker of a here-string has to sit at column 0, so this block is deliberately unindented.
    Write-Host @'
Usage: upgrade-host.ps1 [options]

Upgrades this host: the packages winget manages, and the WSL platform. A Windows feature update is
Windows Update's to make and has no action here.

Actions, name one, default -Packages:
  -s, -Status       Report the host, what is upgradable, and the WSL platform
  -p, -Packages     Upgrade every winget package that has an upgrade
  -w, -Wsl          Update the WSL platform only
  -a, -All          Upgrade the packages, then update the WSL platform
  -h, -Help         Show this help

Options:
  -n, -DryRun       Print the commands instead of running them
  -y, -Yes          Do not prompt before changing the host

A WSL platform update stops the WSL service, and Docker Desktop holds it open, so this refuses to
start one while Docker is running. Quit Docker from its tray icon first, since pausing it is not
enough. Updating WSL also restarts every distribution, so anything running inside one is stopped.

Some packages report a version winget cannot move, because the application updates itself and the
version it was installed at is the one recorded. Those are listed apart and left alone, and the
report names where each is actually upgraded from.

Examples:
  upgrade-host.ps1                 Upgrade the winget packages
  upgrade-host.ps1 -Status         Report, change nothing
  upgrade-host.ps1 -Packages -Yes  Upgrade unattended
  upgrade-host.ps1 -All            Upgrade the packages, then the WSL platform
  upgrade-host.ps1 -Wsl -DryRun    Show what a WSL update would run
'@
}

# --- Host ---

function Test-HostSupported {
    if ($PSVersionTable.PSVersion.Major -lt 7) {
        die "This script needs PowerShell 7 or later, and this is $($PSVersionTable.PSVersion). Install it with: winget install --id Microsoft.PowerShell --exact --source winget"
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        die 'winget not found, and this script upgrades winget packages. Install App Installer from the Microsoft Store, then run this again.'
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $script:ELEVATED = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-HostDescription {
    $caption = (Get-CimInstance Win32_OperatingSystem).Caption
    $wingetVersion = (& winget --version 2>&1 | Out-String).Trim()
    return "$caption $([Environment]::OSVersion.Version), pwsh $($PSVersionTable.PSVersion), winget $wingetVersion"
}

# --- Execution ---

# Run a command, or print it under -DryRun.
# A read used to decide what to do runs either way, and only a command that changes the host goes through here.
function run {
    param([Parameter(Mandatory)][string]$Command, [Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    if ($script:DRY_RUN) {
        Write-Host "  [dry run] $Command $($Arguments -join ' ')"
        return 0
    }
    # The command's own output goes to the console rather than down the pipeline.
    # A native command writes to this function's output stream, so without this the caller receives every line the command printed with the exit code appended, and a check against 0 then compares against the first line of output.
    & $Command @Arguments | Out-Host
    return $LASTEXITCODE
}

function confirm {
    param([Parameter(Mandatory)][string]$Question)
    if ($script:ASSUME_YES -or $script:DRY_RUN) { return $true }
    # Both are checked because a scheduled task reports one and not the other, and either alone misses a case.
    if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {
        die 'Not a terminal and -Yes was not given, refusing to change the host unattended'
    }
    return ((Read-Host "$Question [y/N]") -match '^(y|yes)$')
}

# --- winget ---

# What winget would upgrade, split into the packages it will move and the ones it will not.
# The two halves are counted here rather than read off winget's own closing count, which sums both and so reports a number that matches neither list.
# Each row is parsed by header offset, since a package name carries spaces and splitting on them moves the version into the name.
function Get-Upgradable {
    $result = @{ Ready = @(); Explicit = @() }
    $text = (& winget upgrade --include-unknown --disable-interactivity 2>&1 | Out-String -Width 500)
    if ($LASTEXITCODE -ne 0) { return $result }

    $marker = 'require explicit targeting for upgrade'
    $index = $text.IndexOf($marker)
    $ready = if ($index -ge 0) { $text.Substring(0, $index) } else { $text }
    $explicit = if ($index -ge 0) { $text.Substring($index) } else { '' }

    $result.Ready = Read-UpgradeTable -Text $ready
    $result.Explicit = Read-UpgradeTable -Text $explicit
    return $result
}

function Read-UpgradeTable {
    param([string]$Text)
    $rows = @()
    if (-not $Text) { return , $rows }
    $lines = $Text -split "`r?`n"
    $header = $lines | Where-Object { $_ -match '^Name\s+Id\s+Version\s+Available' } | Select-Object -First 1
    if (-not $header) { return , $rows }
    $idColumn = $header.IndexOf('Id')
    $versionColumn = $header.IndexOf('Version')
    $availableColumn = $header.IndexOf('Available')
    foreach ($line in $lines) {
        if ($line.Length -le $availableColumn) { continue }
        if ($line -match '^Name\s+Id\s+Version') { continue }
        if ($line -match '^-+$') { continue }
        $id = ($line.Substring($idColumn, $versionColumn - $idColumn)).Trim()
        # A row whose id column is blank is wrapped output or a progress line rather than a package.
        if (-not $id -or $id -notmatch '^\S+$') { continue }
        $rows += @{
            Id        = $id
            Version   = ($line.Substring($versionColumn, $availableColumn - $versionColumn)).Trim()
            Available = (($line.Substring($availableColumn) -split '\s+')[0]).Trim()
        }
    }
    return , $rows
}

# --- WSL ---

# Every wsl.exe call goes through here, because wsl.exe emits UTF-16 by default and its output then reads as NUL separated characters.
# WSL_UTF8 changes what wsl.exe emits, where setting the console encoding would only change how this process decodes it and would corrupt in-distribution output that is already UTF-8.
# A host whose WSL predates WSL_UTF8 still answers in UTF-16, so a result carrying a NUL is stripped rather than reported as unreadable.
function Invoke-Wsl {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    $previous = $env:WSL_UTF8
    try {
        $env:WSL_UTF8 = '1'
        $text = (& wsl.exe @Arguments 2>&1 | Out-String -Width 500)
        if ($text.Contains([char]0)) { $text = $text -replace "`0", '' }
        return $text
    } finally {
        if ($null -eq $previous) { Remove-Item Env:\WSL_UTF8 -ErrorAction SilentlyContinue }
        else { $env:WSL_UTF8 = $previous }
    }
}

function Get-WslVersion {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { return $null }
    $text = Invoke-Wsl '--version'
    if ($LASTEXITCODE -ne 0) { return $null }
    $parts = @()
    foreach ($label in 'WSL version', 'Kernel version', 'WSLg version') {
        if ($text -match "(?m)^$label`:\s*(\S+)\s*$") { $parts += "$label $($Matches[1])" }
    }
    if ($parts.Count -eq 0) { return $null }
    return ($parts -join ', ')
}

# --- Docker ---

# Whether Docker Desktop is running, which is what blocks a WSL platform update.
# The service is not the probe: com.docker.service reads Stopped on a host with Docker Desktop plainly running, since it is the elevated helper rather than the engine.
# Neither wslservice nor vmmemWSL is the probe either, since both run for any distribution and would report Docker on a host that has none.
function Get-DockerProcess {
    return , @(Get-Process -Name $script:DOCKER_PROCESSES -ErrorAction SilentlyContinue)
}

# A WSL platform update stops and restarts the WSL service, and Docker Desktop holds it open, so the update fails part way rather than declining.
# The guard runs under -DryRun too, since it is a read that decides what to do rather than a change to the host, and a dry run that printed the command would say the update was available when it is not.
function Assert-DockerStopped {
    $running = Get-DockerProcess
    if ($running.Count -eq 0) { return }
    info "Running: $((($running | ForEach-Object { $_.Name }) | Sort-Object -Unique) -join ', ')"
    die 'Docker Desktop is running, and a WSL platform update fails part way while it holds the WSL service open. Quit Docker Desktop from its tray icon and wait for it to report that it has stopped, then run this again. Pausing it is not enough.'
}

# --- Actions ---

function Show-Status {
    log "Host      : $(Get-HostDescription)"
    if ($script:ELEVATED) {
        log 'Elevation : elevated, and an unelevated run is the one to prefer'
    } else {
        log 'Elevation : not elevated, which is the state to prefer'
    }

    $upgradable = Get-Upgradable
    log "Upgradable: $($upgradable.Ready.Count) package(s)"
    if ($upgradable.Explicit.Count -gt 0) {
        log "Self-updating: $($upgradable.Explicit.Count) package(s), which winget does not move"
    }

    $wslVersion = Get-WslVersion
    log "WSL       : $(if ($wslVersion) { $wslVersion } else { 'not installed, or its version could not be read' })"

    $docker = Get-DockerProcess
    if ($docker.Count -gt 0) {
        $names = (($docker | ForEach-Object { $_.Name }) | Sort-Object -Unique) -join ', '
        log "Docker    : running ($names), so a WSL platform update is refused"
    } else {
        log 'Docker    : not running'
    }

    if ($upgradable.Explicit.Count -eq 0) { return }
    log ''
    log 'Self-updating, so winget reports the version it was installed at rather than the version it runs:'
    foreach ($row in $upgradable.Explicit) {
        info "$($row.Id)  installed at $($row.Version), source carries $($row.Available)"
        # No command is printed for these, deliberately: winget cannot move them, and offering one invites a full reinstall over a working copy in pursuit of a number that does not change.
        if ($script:SELF_UPDATE_NOTE.ContainsKey($row.Id)) {
            info "  $($script:SELF_UPDATE_NOTE[$row.Id])"
        } else {
            info '  This application updates itself, so upgrade it from its own tooling.'
        }
    }
}

function Invoke-PackageUpgrade {
    $upgradable = Get-Upgradable
    if ($upgradable.Ready.Count -eq 0) {
        step 'Upgrading winget packages'
        info 'Nothing to upgrade'
    } else {
        step "Upgrading $($upgradable.Ready.Count) winget package(s)"
        foreach ($row in $upgradable.Ready) { info "$($row.Id)  $($row.Version) -> $($row.Available)" }
        $code = run -Command 'winget' -Arguments 'upgrade', '--all', '--include-unknown', '--disable-interactivity',
        '--accept-source-agreements', '--accept-package-agreements', '--silent'
        if ($code -ne 0) { warn "winget exited $code, so one or more packages did not upgrade" }
    }

    # Named rather than silently skipped, because a package winget leaves alone reads as one it upgraded.
    if ($upgradable.Explicit.Count -gt 0) {
        step "Left alone, $($upgradable.Explicit.Count) package(s) that update themselves"
        foreach ($row in $upgradable.Explicit) { info $row.Id }
    }
}

function Invoke-WslUpdate {
    step 'Updating the WSL platform'
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        warn 'wsl.exe not found, so there is no WSL platform to update'
        return
    }
    Assert-DockerStopped
    info 'This restarts every distribution, so anything running inside one is stopped'
    $code = run -Command 'wsl.exe' -Arguments '--update'
    if ($code -ne 0) { warn "wsl --update exited $code" }
}

function Invoke-Upgrade {
    log "Host: $(Get-HostDescription)"
    log "Mode: $($script:MODE)$(if ($script:DRY_RUN) { ' (dry run)' })"

    # The guard runs before the prompt, so a run that cannot finish says so rather than asking first and refusing after.
    if ($script:MODE -in @('wsl', 'all')) { Assert-DockerStopped }

    if (-not (confirm 'Upgrade this host?')) { die 'Declined' }

    if ($script:MODE -in @('packages', 'all')) { Invoke-PackageUpgrade }
    if ($script:MODE -in @('wsl', 'all')) { Invoke-WslUpdate }

    step 'Done'
}

# --- Entry ---

# PowerShell records which switches were given and not the order they came in, so two actions is a refusal rather than the last one winning.
# Refusing is also the better answer: an action silently discarded is one the caller believes ran.
function Resolve-Mode {
    $given = @($script:ACTIONS.Keys | Where-Object { $script:ACTIONS[$_] })
    if ($given.Count -gt 1) { die "More than one action given ($($given -join ', ')), name one" }
    if ($given.Count -eq 0) { return 'packages' }
    return $given[0]
}

function main {
    if ($script:WANT_HELP) { usage; exit 0 }
    $script:MODE = Resolve-Mode
    Test-HostSupported

    if ($script:MODE -eq 'status') { Show-Status } else { Invoke-Upgrade }
}

main
