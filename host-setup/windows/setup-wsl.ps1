# Installs the WSL distributions this host runs, and reports how Docker Desktop is integrated with them.
# It is a fourth script rather than part of upgrade-host.ps1, because installing a distribution stands a new environment up where a platform update brings this one current, and those are different actions on different subjects.
#
# The Docker integration is reported and never written.
# Docker Desktop holds these settings in memory and rewrites its settings file from that copy while it runs, so an edit made here is discarded at Docker's next save and an edit made while it is stopped is undone by the next start.

[CmdletBinding()]
param(
    [Alias('s')][switch]$Status,
    [Alias('l')][switch]$List,
    [Alias('i')][switch]$Install,
    [switch]$Default,
    [Alias('n')][switch]$DryRun,
    [Alias('y')][switch]$Yes,
    [Alias('h')][switch]$Help,
    [Parameter(Position = 0, ValueFromRemainingArguments)][string[]]$Name
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# A non-zero exit from wsl is an answer here rather than a failure.
# Setting this keeps a profile that turned it on from turning every read into a terminating error.
$PSNativeCommandUseErrorActionPreference = $false

# Docker Desktop registers these itself, so they are not distributions the operator installed and a report that named them would count two as three.
$DOCKER_DISTRIBUTIONS = @('docker-desktop', 'docker-desktop-data')

$SETTINGS = Join-Path $env:APPDATA 'Docker\settings-store.json'

# Every parameter is read into a variable here rather than from inside a function.
# A script parameter reached only from a nested scope reads as declared and never used, which is a finding on each one and hides a parameter that genuinely is unused.
$ACTIONS = [ordered]@{
    status  = [bool]$Status
    list    = [bool]$List
    install = [bool]$Install
}
$WANT_HELP = [bool]$Help
$WANT_DEFAULT = [bool]$Default
# Filtered rather than wrapped, because wrapping an unset parameter yields a one element list holding nothing, which reads as one distribution named the empty string.
$WANT_NAMES = @($Name | Where-Object { $_ })

$MODE = 'status'
$DRY_RUN = [bool]$DryRun
$ASSUME_YES = [bool]$Yes

# --- Output ---

function log { param([string]$Message = '') Write-Host $Message }
function info { param([string]$Message) Write-Host "  $Message" }
function step { param([string]$Message) Write-Host "`n==> $Message" }
function warn { param([string]$Message) [Console]::Error.WriteLine("WARNING: $Message") }
function die { param([string]$Message) [Console]::Error.WriteLine("ERROR: $Message"); exit 1 }

function ok { param([string]$Message) Write-Host "  [ ok ] $Message" }
function missing { param([string]$Message) Write-Host "  [    ] $Message" }

function usage {
    # The closing marker of a here-string has to sit at column 0, so this block is deliberately unindented.
    Write-Host @'
Usage: setup-wsl.ps1 [options] [distribution]

Installs a WSL distribution, and reports the ones this host runs and how Docker Desktop is
integrated with them.

Actions, name one, default -Status:
  -s, -Status       Report the platform, the distributions, and the Docker integration
  -l, -List         List the distributions this host can install
  -i, -Install      Install the named distribution
  -h, -Help         Show this help

Options:
  -n, -DryRun       Print the commands instead of running them
  -y, -Yes          Do not prompt before changing the host
      -Default      Also make the installed distribution the default

An install does not launch the distribution, so it skips the first run account setup and is safe
unattended. The new distribution has no user until it is launched once, which is a person's step.

The Docker Desktop integration is reported and never written, because Docker rewrites its settings
file from memory while it runs, so an edit made here does not survive. Change it in Docker Desktop
under Settings, Resources, WSL integration.

Examples:
  setup-wsl.ps1                       Report the platform and the distributions
  setup-wsl.ps1 -List                 List what can be installed
  setup-wsl.ps1 -Install Debian       Install Debian, without launching it
  setup-wsl.ps1 -Install Ubuntu-24.04 -Default
  setup-wsl.ps1 -Install Debian -DryRun
'@
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

function Test-WslPresent {
    return [bool](Get-Command wsl.exe -ErrorAction SilentlyContinue)
}

function Get-WslPlatform {
    $platform = [ordered]@{}
    $text = Invoke-Wsl '--version'
    if ($LASTEXITCODE -ne 0) { return $platform }
    foreach ($label in 'WSL version', 'Kernel version', 'WSLg version', 'Windows version') {
        if ($text -match "(?m)^$label`:\s*(\S+)\s*$") { $platform[$label] = $Matches[1] }
    }
    return $platform
}

# The distributions this host has registered, with Docker's own excluded.
# The verbose listing marks the default with a leading asterisk, which is the only place that fact is published.
function Get-WslDistribution {
    $rows = @()
    $text = Invoke-Wsl '--list' '--verbose'
    if ($LASTEXITCODE -ne 0) { return , $rows }
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -notmatch '^(\*?)\s+(\S+)\s+(\S+)\s+(\d+)\s*$') { continue }
        $distribution = $Matches[2]
        if ($script:DOCKER_DISTRIBUTIONS -contains $distribution) { continue }
        $rows += @{
            Name      = $distribution
            State     = $Matches[3]
            Version   = $Matches[4]
            IsDefault = ($Matches[1] -eq '*')
        }
    }
    return , $rows
}

function Get-WslAvailable {
    $rows = @()
    $text = Invoke-Wsl '--list' '--online'
    if ($LASTEXITCODE -ne 0) { return , $rows }
    $started = $false
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -match '^NAME\s+FRIENDLY NAME') { $started = $true; continue }
        if (-not $started) { continue }
        if ($line -notmatch '^(\S+)\s\s+(.+?)\s*$') { continue }
        $rows += @{ Name = $Matches[1]; Friendly = $Matches[2] }
    }
    return , $rows
}

# --- Docker ---

# How Docker Desktop records its WSL integration, read from its settings file.
# Reported and never written: Docker holds these in memory and rewrites the file from that copy while it runs, so an edit here is discarded at its next save.
function Get-DockerIntegration {
    if (-not (Test-Path $script:SETTINGS)) { return $null }
    try {
        return (Get-Content $script:SETTINGS -Raw | ConvertFrom-Json)
    } catch {
        warn "Docker's settings file at $($script:SETTINGS) could not be read as JSON, so the integration is not reported: $($_.Exception.Message)"
        return $null
    }
}

function Get-JsonMember {
    param($Object, [string]$Member)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Member]
    if ($null -eq $property) { return $null }
    return $property.Value
}

# --- Actions ---

function Show-Platform {
    log 'Platform'
    $platform = Get-WslPlatform
    if ($platform.Count -eq 0) {
        info 'WSL is installed but did not report a version, so it may need "wsl --update"'
        return
    }
    foreach ($label in $platform.Keys) { info ("{0,-16}{1}" -f $label, $platform[$label]) }
}

function Show-Distribution {
    log ''
    log 'Distributions'
    $rows = Get-WslDistribution
    if ($rows.Count -eq 0) {
        info 'None registered, and -List names what this host can install'
        return , $rows
    }
    $format = '  {0,-16} {1,-12} {2,-9} {3}'
    log ($format -f 'NAME', 'STATE', 'VERSION', 'DEFAULT')
    foreach ($row in $rows) {
        log ($format -f $row.Name, $row.State, $row.Version, $(if ($row.IsDefault) { 'yes' } else { '' }))
    }
    return , $rows
}

function Show-Integration {
    param([array]$Distribution)
    log ''
    log 'Docker Desktop integration, as Docker records it'

    $settings = Get-DockerIntegration
    if ($null -eq $settings) {
        info 'Docker Desktop is not installed here, or has never been started, so there is no integration to report'
        return
    }

    $engine = Get-JsonMember $settings 'WslEngineEnabled'
    if ($engine) { ok 'WSL engine enabled' } else { missing 'WSL engine enabled' }

    $withDefault = Get-JsonMember $settings 'EnableIntegrationWithDefaultWslDistro'
    if ($withDefault) { ok 'integration with the default distribution' } else { missing 'integration with the default distribution' }

    $integrated = @(Get-JsonMember $settings 'IntegratedWslDistros')
    foreach ($row in $Distribution) {
        if ($integrated -contains $row.Name) { ok $row.Name } else { missing $row.Name }
    }

    if (Get-JsonMember $settings 'WslUpdateRequired') {
        warn 'Docker reports that WSL needs updating, which upgrade-host.ps1 -Wsl does once Docker is quit'
    }

    log ''
    info 'Change this in Docker Desktop under Settings, Resources, WSL integration.'
    info 'It is reported here and never written, because Docker rewrites its settings file from memory while it runs.'
}

function Show-Status {
    if (-not (Test-WslPresent)) {
        die 'wsl.exe not found, so WSL is not installed on this host. Install it with: wsl --install --no-distribution'
    }
    Show-Platform
    $distribution = Show-Distribution
    Show-Integration -Distribution $distribution
}

function Show-List {
    if (-not (Test-WslPresent)) {
        die 'wsl.exe not found, so WSL is not installed on this host. Install it with: wsl --install --no-distribution'
    }
    $rows = Get-WslAvailable
    if ($rows.Count -eq 0) {
        die 'WSL listed no installable distributions, which usually means it could not reach the distribution index'
    }
    $format = '{0,-32} {1}'
    log ($format -f 'NAME', 'FRIENDLY NAME')
    foreach ($row in $rows) { log ($format -f $row.Name, $row.Friendly) }
}

function Install-Distribution {
    if (-not (Test-WslPresent)) {
        die 'wsl.exe not found, so WSL is not installed on this host. Install it with: wsl --install --no-distribution'
    }
    if ($script:WANT_NAMES.Count -ne 1) {
        die 'Name one distribution to install, and -List names the ones this host can install'
    }
    $wanted = $script:WANT_NAMES[0]

    # Checked against the index rather than left to wsl, whose failure for an unknown name is a long help text rather than a message naming the problem.
    $available = Get-WslAvailable
    if ($available.Count -gt 0 -and ($available | ForEach-Object { $_.Name }) -notcontains $wanted) {
        die "`"$wanted`" is not a distribution this host can install, and -List names the ones it can"
    }

    $installed = Get-WslDistribution
    if (($installed | ForEach-Object { $_.Name }) -contains $wanted) {
        log "$wanted is already installed, leaving it alone"
        return
    }

    # The name is wrapped because a question mark is a legal character in a variable name, so "$wanted?" reads as a variable nobody set.
    if (-not (confirm "Install $($wanted)?")) { die 'Declined' }

    step "Installing $wanted"
    # The first run account setup is skipped, which is what makes this safe unattended, and it leaves the distribution with no user until somebody launches it.
    $code = run -Command 'wsl.exe' -Arguments '--install', $wanted, '--no-launch'
    if ($code -ne 0) { die "wsl --install exited $code" }

    if ($script:WANT_DEFAULT) {
        step "Making $wanted the default distribution"
        $code = run -Command 'wsl.exe' -Arguments '--set-default', $wanted
        if ($code -ne 0) { warn "wsl --set-default exited $code" }
    }

    step 'Done'
    info "Launch it once with `"wsl -d $wanted`" to create its user account, which this deliberately did not do"
    info 'Enable it for Docker in Docker Desktop under Settings, Resources, WSL integration'
}

# --- Entry ---

# PowerShell records which switches were given and not the order they came in, so two actions is a refusal rather than the last one winning.
# Refusing is also the better answer: an action silently discarded is one the caller believes ran.
function Resolve-Mode {
    $given = @($script:ACTIONS.Keys | Where-Object { $script:ACTIONS[$_] })
    if ($given.Count -gt 1) { die "More than one action given ($($given -join ', ')), name one" }
    if ($given.Count -eq 0) { return 'status' }
    return $given[0]
}

function main {
    if ($script:WANT_HELP) { usage; exit 0 }
    $script:MODE = Resolve-Mode

    switch ($script:MODE) {
        'list' { Show-List }
        'install' { Install-Distribution }
        default { Show-Status }
    }
}

main
