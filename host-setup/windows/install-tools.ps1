# Installs and upgrades the host tools the fleet's repositories expect, on native Windows, through winget.
# Every tool in the contract has a winget package, so winget is the only source here, where the Linux script needs three because an apt feed trails upstream on half its tools.
# No version is written into this script, and winget is asked what each package carries now, so the script does not go stale between releases.
#
# Every step is idempotent.
# A package is installed only where winget reports none, and upgraded only where the installed version differs from what the source carries.
# Re-running repairs drift rather than assuming a clean host.
#
# Nothing here elevates, and no scope is passed unless the caller names one.
# An installer that needs administrator raises its own prompt, which is the path with the fewest failures: forcing user scope installs a second copy beside a machine wide one, and some installers fail outright when launched from an already elevated process.

# CmdletBinding with an explicit position on the tool list is what keeps every other parameter named only.
# Without it a stray word binds to the first parameter that takes a value, and a mistyped tool name is reported against -Scope instead.
[CmdletBinding()]
param(
    [Alias('r')][switch]$Report,
    [Alias('i')][switch]$Install,
    [Alias('u')][switch]$Upgrade,
    [switch]$Reinstall,
    [Alias('l')][switch]$List,
    [Alias('n')][switch]$DryRun,
    [Alias('y')][switch]$Yes,
    [Alias('o')][switch]$Optional,
    [ValidateSet('user', 'machine')][string]$Scope,
    [Alias('h')][switch]$Help,
    [Parameter(Position = 0, ValueFromRemainingArguments)][string[]]$Name
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# A non-zero exit from winget is an answer here rather than a failure.
# Setting this keeps a profile that turned it on from turning every read into a terminating error.
$PSNativeCommandUseErrorActionPreference = $false

# Returned by winget when nothing matches the query, which is an answer rather than a failure.
$NOT_FOUND = -1978335212

# Every parameter is read into a variable here rather than from inside a function.
# A script parameter reached only from a nested scope reads as declared and never used, which is a finding on each one and hides a parameter that genuinely is unused.
$ACTIONS = [ordered]@{
    report    = [bool]$Report
    install   = [bool]$Install
    upgrade   = [bool]$Upgrade
    reinstall = [bool]$Reinstall
    list      = [bool]$List
}
$WANT_HELP = [bool]$Help
# Filtered rather than wrapped, because wrapping an unset parameter yields a one element list holding nothing, which reads as one tool named the empty string.
$WANT_TOOLS = @($Name | Where-Object { $_ })

$MODE = 'report'
$DRY_RUN = [bool]$DryRun
$ASSUME_YES = [bool]$Yes
$WITH_OPTIONAL = [bool]$Optional
$WANT_SCOPE = $Scope
$ELEVATED = $false
$SELECTED = @()
$NOTES = @()
$FAILED = @()
$CHANGED = @()
$EXPLICIT = $null

# --- Output ---

function log { param([string]$Message = '') Write-Host $Message }
function info { param([string]$Message) Write-Host "  $Message" }
function step { param([string]$Message) Write-Host "`n==> $Message" }
function warn { param([string]$Message) [Console]::Error.WriteLine("WARNING: $Message") }
function die { param([string]$Message) [Console]::Error.WriteLine("ERROR: $Message"); exit 1 }

function note { param([string]$Tool, [string]$Message) $script:NOTES += "${Tool}: $Message" }

# A path with the home directory replaced by the variable that names it.
# A report is written to be pasted into an issue or a pull request, so a path it prints carries the account name into wherever it is pasted, and the comments here already avoid writing one for the same reason.
# The variable is what a reader expands themselves, so the path stays as actionable as it was.
function Hide-Home {
    param([string]$Path)
    if (-not $Path -or -not $HOME) { return $Path }
    if ($Path.StartsWith($HOME, [StringComparison]::OrdinalIgnoreCase)) {
        return '%USERPROFILE%' + $Path.Substring($HOME.Length)
    }
    return $Path
}

function usage {
    # The closing marker of a here-string has to sit at column 0, so this block is deliberately unindented.
    Write-Host @'
Usage: install-tools.ps1 [options] [tool ...]

Installs the host tools the fleet's repositories expect, from winget, which is the only source
every one of them has. With no tool named, every managed tool is selected.

Actions, name one, default -Report:
  -r, -Report       Report installed and available versions, change nothing
  -i, -Install      Install what is missing, leave an installed tool at its version
  -u, -Upgrade      Install what is missing and upgrade what is behind
      -Reinstall    Remove the installed copy, then install it again
  -l, -List         List the managed tools and their winget package ids
  -h, -Help         Show this help

Options:
  -n, -DryRun       Print the commands instead of running them
  -y, -Yes          Do not prompt before changing the host
  -o, -Optional     Include the optional package set, where a tool has one
      -Scope        Name a scope, either user or machine, for the copy to act on

Run this without elevation. No scope is passed unless -Scope names one, so winget acts on the copy
it finds and an installer that needs administrator asks for it itself. Naming a scope that
disagrees with the installed copy would add a second copy beside the first rather than replacing
it, so an upgrade refuses that case and names -Reinstall, which removes the old copy first.

An install or upgrade of docker also brings the WSL platform itself up to Docker Desktop's own
floor where it is behind, and stops Docker Desktop first either way, since Docker Desktop holds
the WSL service open and both a platform update and its own WSL integration otherwise need a
restart to recover from an engine bump. wsl --update raises its own administrator prompt, which
this refuses to start unattended where nothing could answer it, rather than hang.

Examples:
  install-tools.ps1                       Report on every tool
  install-tools.ps1 -Install              Install what is missing
  install-tools.ps1 -Upgrade -Yes         Bring every tool current, no prompt
  install-tools.ps1 -Upgrade uv jq        Bring two tools current
  install-tools.ps1 -Install -Optional dotnet
  install-tools.ps1 -Upgrade -DryRun      Show what an upgrade would run
  install-tools.ps1 -Reinstall jq -Scope machine
'@
}

# --- Host ---

# Read the host identity, and refuse a host this script cannot install for.
# A host that cannot run this script cannot be repaired by it, so each refusal prints the one command that fixes it.
function Test-HostSupported {
    if ($PSVersionTable.PSVersion.Major -lt 7) {
        die "This script needs PowerShell 7 or later, and this is $($PSVersionTable.PSVersion). Install it with: winget install --id Microsoft.PowerShell --exact --source winget"
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        die 'winget not found, and this script installs winget packages. Install App Installer from the Microsoft Store, then run this again.'
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $script:ELEVATED = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
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

# The version column of every row winget printed for one id.
# The offsets come from the header rather than from a whitespace split, because a package name carries spaces and splitting on them moves the version into the name.
# A row is kept only where the id column holds the id that was asked for, which drops the trailing count line without a list of strings to ignore.
# Every list returning function here returns with a leading comma, which is what stops PowerShell unrolling a one element array into the element.
# Without it a single installed version arrives as a string, and asking a string for its Count is an error under Set-StrictMode rather than the 1 the caller expects.
function Read-WingetTable {
    param([string]$Text, [string]$Id)
    $rows = @()
    $lines = $Text -split "`r?`n"
    $header = $lines | Where-Object { $_ -match '^Name\s+Id\s+Version' } | Select-Object -First 1
    if (-not $header) { return , $rows }
    $idColumn = $header.IndexOf('Id')
    $versionColumn = $header.IndexOf('Version')
    foreach ($line in $lines) {
        if ($line.Length -le $versionColumn) { continue }
        if (-not $line.Substring($idColumn).StartsWith($Id)) { continue }
        $rows += ($line.Substring($versionColumn) -split '\s+')[0]
    }
    return , $rows
}

# The id and version of every row winget printed for a source search, unfiltered by id.
# Shares the header driven offsets with Read-WingetTable, but keeps the id column instead of assuming the caller already knows it, which is what Resolve-ToolPackage needs from a family search.
function Read-WingetSearchTable {
    param([string]$Text, [string]$Prefix)
    $rows = @()
    $lines = $Text -split "`r?`n"
    $header = $lines | Where-Object { $_ -match '^Name\s+Id\s+Version' } | Select-Object -First 1
    if (-not $header) { return , $rows }
    $idColumn = $header.IndexOf('Id')
    $versionColumn = $header.IndexOf('Version')
    foreach ($line in $lines) {
        if ($line.Length -le $versionColumn) { continue }
        $id = ($line.Substring($idColumn, $versionColumn - $idColumn)).Trim()
        if (-not $id.StartsWith($Prefix)) { continue }
        $version = ($line.Substring($versionColumn) -split '\s+')[0]
        $rows += [PSCustomObject]@{ Id = $id; Version = $version }
    }
    return , $rows
}

# The installed versions of one package id, or an empty list where none is installed.
# The exit code decides rather than the output text, since a missing id and an unreadable source both print prose and only the code tells them apart.
# A null answer means the question could not be answered, which is not the same as none installed.
function Get-WingetInstalled {
    param([Parameter(Mandatory)][string]$Id, [string]$InScope)
    $arguments = @('list', '--id', $Id, '--exact', '--source', 'winget', '--disable-interactivity')
    if ($InScope) { $arguments += @('--scope', $InScope) }
    $text = (& winget @arguments 2>&1 | Out-String -Width 500)
    if ($LASTEXITCODE -eq $script:NOT_FOUND) { return , @() }
    if ($LASTEXITCODE -ne 0) { return $null }
    return , (Read-WingetTable -Text $text -Id $Id)
}

# One version for a package winget lists more than once, or nothing where the rows do not describe one product.
# Rows sharing a major version are side by side builds of one product and the newest is the answer, which is what a dotnet SDK line looks like.
# Rows whose majors differ are two products sharing an id, which is what the legacy WSL installer looks like beside WSL itself, and there no single version compares.
function Resolve-InstalledVersion {
    param([string[]]$Version)
    if (-not $Version -or $Version.Count -eq 0) { return $null }
    if ($Version.Count -eq 1) { return $Version[0] }
    $majors = @($Version | ForEach-Object { ($_ -split '\.')[0] } | Sort-Object -Unique)
    if ($majors.Count -ne 1) { return $null }

    # Compared component by component rather than sorted, because Sort-Object orders a version as text and 10.0.9 then outranks 10.0.10.
    # Three side by side dotnet builds hid this, since 110, 204 and 302 are all three digits and sort the same either way.
    $newest = $Version[0]
    foreach ($candidate in $Version) {
        if ((Compare-HostVersion $candidate $newest) -gt 0) { $newest = $candidate }
    }
    return $newest
}

# What the source carries now, read without installing anything.
# The show command prints one Version line even for an id the list command answers with several rows, which is what makes it the reader for the target rather than a second list call.
function Get-WingetAvailable {
    param([Parameter(Mandatory)][string]$Id)
    $text = (& winget show --id $Id --exact --source winget --disable-interactivity 2>&1 | Out-String -Width 500)
    if ($LASTEXITCODE -ne 0) { return $null }
    if ($text -match '(?m)^Version:\s+(\S+)\s*$') {
        if ($Matches[1] -eq 'Unknown') { return $null }
        return $Matches[1]
    }
    return $null
}

# Which scopes carry a copy, as a sorted list.
# Two probes rather than one reading, because winget reports no scope column and a package installed in both scopes is the case worth catching.
function Get-WingetScope {
    param([Parameter(Mandatory)][string]$Id)
    $found = @()
    foreach ($candidate in 'user', 'machine') {
        $rows = Get-WingetInstalled -Id $Id -InScope $candidate
        if ($null -ne $rows -and $rows.Count -gt 0) { $found += $candidate }
    }
    return , $found
}

# Whether winget wrote the uninstall entry for this package itself, which it does for a portable or an archive package and not for an exe or an msi.
# This is the only positive evidence of provenance available, and its absence proves nothing: winget runs the vendor's own installer for an exe or an msi, so that entry is identical whether winget invoked it or a person did.
# Reported where present and silent where not, rather than being turned into a claim it cannot support.
function Test-WingetOwnedEntry {
    param([Parameter(Mandatory)][string]$Id)
    $suffix = '_Microsoft.Winget.Source_8wekyb3d8bbwe'
    $roots = @(
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    foreach ($root in $roots) {
        if (Test-Path (Join-Path $root ($Id + $suffix))) { return $true }
    }
    return $false
}

# The package ids winget refuses to move as part of an upgrade of everything.
# A package lands here because its manifest asks for it, which is the author saying the application updates itself, and the version winget reports is then the one the installer first wrote rather than the one the application runs.
# Read once and cached, since it costs a full upgrade query.
function Get-ExplicitUpgrade {
    if ($null -ne $script:EXPLICIT) { return , $script:EXPLICIT }
    $script:EXPLICIT = @()
    $text = (& winget upgrade --include-unknown --disable-interactivity 2>&1 | Out-String -Width 500)
    if ($LASTEXITCODE -ne 0) { return $script:EXPLICIT }
    $marker = 'require explicit targeting for upgrade'
    $index = $text.IndexOf($marker)
    if ($index -lt 0) { return $script:EXPLICIT }
    $tail = $text.Substring($index)
    $lines = $tail -split "`r?`n"
    $header = $lines | Where-Object { $_ -match '^Name\s+Id\s+Version' } | Select-Object -First 1
    if (-not $header) { return $script:EXPLICIT }
    $idColumn = $header.IndexOf('Id')
    $versionColumn = $header.IndexOf('Version')
    foreach ($line in $lines) {
        if ($line.Length -le $versionColumn) { continue }
        if ($line -match '^Name\s+Id\s+Version') { continue }
        $script:EXPLICIT += ($line.Substring($idColumn, $versionColumn - $idColumn)).Trim()
    }
    return , $script:EXPLICIT
}

# The package id winget's own catalog says produced the version this tool reports as installed.
# `winget list --id X --exact` answers yes for a package sharing this tool's family even where X itself is not what is installed (#693: a host running OpenJS.NodeJS, the Current channel, reads as OpenJS.NodeJS.LTS because the two share a publisher and a name), but the version it reports comes straight from the installed entry regardless of which id was asked about, so matching that version against every id in the family, read from a plain source search rather than an id scoped one, is what tells the ids apart without trusting the id column winget answered with at all.
# A tool naming no family resolves to its own package unchanged and costs nothing extra, and a version matching none of the family's ids does the same, since a family search that cannot confirm anything is no worse than not asking.
# A version matching more than one id falls back the same way rather than picking the first: two ids sharing a version happens right at a channel handoff, when a major that just left Current has not yet lost its old id from the search results, and picking between them by list order would be a guess wearing the shape of an answer.
function Resolve-ToolPackage {
    param([Parameter(Mandatory)][hashtable]$Tool, [string]$Installed)
    if (-not $Tool.Family -or -not $Installed) { return $Tool.Package }
    $text = (& winget search --query $Tool.Family --source winget --disable-interactivity 2>&1 | Out-String -Width 500)
    if ($LASTEXITCODE -ne 0) { return $Tool.Package }
    $found = @((Read-WingetSearchTable -Text $text -Prefix $Tool.Family) | Where-Object { $_.Version -eq $Installed })
    if ($found.Count -eq 1) { return $found[0].Id }
    return $Tool.Package
}

# Install, upgrade and remove, named here so the flags appear once in this file.
# No scope is passed unless the caller named one, because winget then acts on the copy it found and naming a different scope adds a copy rather than replacing one.
function Invoke-WingetInstall {
    param([Parameter(Mandatory)][string]$Id)
    $arguments = @('install', '--id', $Id, '--exact', '--source', 'winget', '--disable-interactivity',
        '--accept-source-agreements', '--accept-package-agreements', '--silent')
    if ($script:WANT_SCOPE) { $arguments += @('--scope', $script:WANT_SCOPE) }
    return (run 'winget' @arguments)
}

# An upgrade takes the named scope too, on the same rule as an install.
# Reaching here with a scope that disagrees with the installed copy is already refused, so the scope named here is one a copy sits in, and passing it is what says which copy to move where a tool is installed in both.
function Invoke-WingetUpgrade {
    param([Parameter(Mandatory)][string]$Id)
    $arguments = @('upgrade', '--id', $Id, '--exact', '--source', 'winget', '--disable-interactivity',
        '--accept-source-agreements', '--accept-package-agreements', '--silent', '--include-unknown')
    if ($script:WANT_SCOPE) { $arguments += @('--scope', $script:WANT_SCOPE) }
    return (run 'winget' @arguments)
}

# The scope to remove from is the one the copy was found in, which the caller passes, rather than the one the caller asked to end up in.
# Removing in the requested scope finds nothing where the copy sits in the other one, so the old copy survives and the install that follows adds a second beside it, which is the state -Reinstall exists to clear.
function Invoke-WingetRemove {
    param([Parameter(Mandatory)][string]$Id, [string]$InScope)
    $arguments = @('uninstall', '--id', $Id, '--exact', '--disable-interactivity', '--silent')
    if ($InScope) { $arguments += @('--scope', $InScope) }
    return (run -Command 'winget' -Arguments $arguments)
}

# --- Tools ---

# Managed tools, in the order a report lists them.
# Every one is a single winget package, which is what makes this a table where the Linux script needs four functions per tool.
# Probe names the executable that proves the tool is present when winget knows no package for it, and it is py for python because that is the name a correctly set up Windows host carries.
# Family names the id prefix winget's catalog shares across every channel and pinned major a tool ships under, and is empty everywhere but node: node alone ships as more than one id (Current, LTS, and one per pinned major back to 4), any of which is fine on a host as long as its version clears Available, and Resolve-ToolPackage is what finds which one that is.
$TOOLS = @(
    @{ Name = 'git'; Package = 'Git.Git'; Probe = 'git'; Optional = @(); Family = '' }
    @{ Name = 'gh'; Package = 'GitHub.cli'; Probe = 'gh'; Optional = @(); Family = '' }
    @{ Name = 'jq'; Package = 'jqlang.jq'; Probe = 'jq'; Optional = @(); Family = '' }
    @{ Name = 'python'; Package = 'Python.Python.3.13'; Probe = 'py'; Optional = @(); Family = '' }
    @{ Name = 'uv'; Package = 'astral-sh.uv'; Probe = 'uv'; Optional = @(); Family = '' }
    @{ Name = 'docker'; Package = 'Docker.DockerDesktop'; Probe = 'docker'; Optional = @(); Family = '' }
    @{ Name = 'node'; Package = 'OpenJS.NodeJS.LTS'; Probe = 'node'; Optional = @(); Family = 'OpenJS.NodeJS' }
    @{ Name = 'dotnet'; Package = 'Microsoft.DotNet.SDK.10'; Probe = 'dotnet'; Optional = @('Microsoft.DotNet.SDK.9', 'Microsoft.DotNet.SDK.8'); Family = '' }
)

function Get-Tool {
    param([Parameter(Mandatory)][string]$ToolName)
    return ($script:TOOLS | Where-Object { $_.Name -eq $ToolName } | Select-Object -First 1)
}

# --- Status ---

# A version as a comparable list of integers, with anything non numeric dropped.
# Mirrors the gate's own comparison so a host reads the same either side of it.
function Get-VersionKey {
    param([string]$Version)
    $parts = @()
    foreach ($part in ($Version -split '[._-]')) {
        if ($part -match '^\d+$') { $parts += [int]$part } else { break }
    }
    if ($parts.Count -eq 0) { return , @(0) }
    return , $parts
}

# Compare two versions, padding the shorter with zeros so more components alone does not read as newer.
function Compare-HostVersion {
    param([string]$Left, [string]$Right)
    $a = Get-VersionKey $Left
    $b = Get-VersionKey $Right
    for ($i = 0; $i -lt [Math]::Max($a.Count, $b.Count); $i++) {
        $x = if ($i -lt $a.Count) { $a[$i] } else { 0 }
        $y = if ($i -lt $b.Count) { $b[$i] } else { 0 }
        if ($x -lt $y) { return -1 }
        if ($x -gt $y) { return 1 }
    }
    return 0
}

# Everything a report and an apply both need about one tool, read once.
function Get-ToolState {
    param([Parameter(Mandatory)][hashtable]$Tool)
    $rows = Get-WingetInstalled -Id $Tool.Package
    $state = @{
        # The id acted on from here down.
        # Reading rows above already answered whether something from this tool's family is installed and at what version, and this may still change below, to the id that version actually belongs to, before anything here is reported or acted on.
        Package   = $Tool.Package
        Installed = $null
        Rows      = @()
        # Whether the installed state was read at all, kept apart from what it said.
        # Folding a failed read into an empty list would report a tool whose state is unknown as one that is not installed, and an install would then run against a host nobody measured.
        Readable  = ($null -ne $rows)
        Available = (Get-WingetAvailable -Id $Tool.Package)
        Scope     = @()
        Status    = 'unknown'
    }
    if ($state.Readable) {
        $state.Rows = $rows
        $state.Installed = Resolve-InstalledVersion -Version $rows
        $state.Package = Resolve-ToolPackage -Tool $Tool -Installed $state.Installed
        if ($rows.Count -gt 0) { $state.Scope = Get-WingetScope -Id $state.Package }
    }
    $state.Status = Get-ToolStatus -Tool $Tool -State $state
    return $state
}

# One word for what a tool needs.
# The three Windows meanings sit beside the five the Linux peer carries, and each names a different reason a version comparison would mislead.
function Get-ToolStatus {
    param([Parameter(Mandatory)][hashtable]$Tool, [Parameter(Mandatory)][hashtable]$State)
    if (-not $State.Readable) { return 'unreadable' }
    if ($State.Rows.Count -eq 0) {
        # A tool answering on PATH that winget knows no package for is one winget cannot manage at all.
        if (Get-Command $Tool.Probe -ErrorAction SilentlyContinue) { return 'unmanaged' }
        if (-not $State.Available) { return 'unavailable' }
        return 'missing'
    }
    if (-not $State.Installed) { return 'multiple' }
    if (-not $State.Available) { return 'unknown' }
    if ((Get-ExplicitUpgrade) -contains $State.Package) { return 'self-updating' }
    if ((Compare-HostVersion $State.Installed $State.Available) -ge 0) { return 'current' }
    return 'outdated'
}

# Per tool detail worth a line under the report, rather than a column of its own.
function Add-ToolNote {
    param([Parameter(Mandatory)][hashtable]$Tool, [Parameter(Mandatory)][hashtable]$State)
    if ($Tool.Name -eq 'python') {
        # Written unexpanded, because the expanded form names a real account and the prose gate rejects that.
        note 'python' 'python3 resolves to the Microsoft Store alias stub under %LOCALAPPDATA%\Microsoft\WindowsApps, so py -3 is the name this contract uses here'
        $resolved = Get-Command python -ErrorAction SilentlyContinue
        if ($resolved -and $resolved.Source -notmatch 'Python\d') {
            note 'python' "python resolves to $(Hide-Home $resolved.Source), which is not the interpreter winget installed"
        }
    }
    if ($State.Scope.Count -gt 1) {
        note $Tool.Name 'installed in both scopes, so one copy shadows the other on PATH, and -Reinstall removes one'
    }
    if ($State.Rows.Count -gt 0 -and (Test-WingetOwnedEntry -Id $State.Package)) {
        note $Tool.Name 'winget wrote this uninstall entry, so winget installed it'
    }
    if ($State.Status -eq 'multiple') {
        note $Tool.Name "winget lists $($State.Rows -join ', ') under one id, and their major versions differ, so no single installed version compares"
    }
    if ($State.Status -eq 'unmanaged') {
        note $Tool.Name 'answers on PATH and winget knows no package for it, so winget cannot upgrade it and -Reinstall does not apply'
    }
    if ($State.Status -eq 'unreadable') {
        note $Tool.Name 'winget did not answer what is installed, so this row reports nothing rather than reporting it as absent'
    }
    if ($State.Status -eq 'self-updating') {
        note $Tool.Name 'updates itself, so the version winget reports is the one it was installed at rather than the one it runs'
    }
    if ($Tool.Name -eq 'dotnet' -and -not $script:WITH_OPTIONAL) {
        note 'dotnet' "optional set not selected: $($Tool.Optional -join ', ')"
    }
    if ($Tool.Name -eq 'docker') {
        $wslProblem = Test-WslReadyForDocker
        if ($wslProblem) { note 'docker' $wslProblem }
    }
}

# --- WSL ---

# Docker Desktop's own documented floor for the WSL platform, per docs.docker.com/desktop/features/wsl.
$DOCKER_WSL_FLOOR = '2.1.5'

# Every wsl.exe call goes through here, because wsl.exe emits UTF-16 by default and its output then reads as NUL separated characters.
# Mirrored from upgrade-host.ps1 rather than shared with it, on the same rule as the rest of this directory: a script here has to stay independently fetchable.
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

# Just the "WSL version" line, as a value Compare-HostVersion can read.
# Get-WslVersion in upgrade-host.ps1 concatenates WSL, kernel and WSLg into one display string instead, which serves a report rather than a comparison.
function Get-WslPlatformVersion {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { return $null }
    $text = Invoke-Wsl '--version'
    if ($LASTEXITCODE -ne 0) { return $null }
    if ($text -match '(?m)^WSL version:\s*(\S+)\s*$') { return $Matches[1] }
    return $null
}

# What stands between this host and installing or upgrading docker, or $null where nothing does.
# A pure report either way, used as-is for -Report's note and as Enter-DockerMaintenance's own pre-check below, which is what actually drives the WSL platform update this used to only name.
# Standing up WSL from nothing with wsl --install stays a person's own step regardless, since that is a different action on a different subject from bringing an existing platform current, the same reasoning that keeps distribution installs in setup-wsl.ps1 rather than folded in here.
function Test-WslReadyForDocker {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        return "wsl.exe was not found, and Docker Desktop needs WSL2. Install it with: wsl --install --no-distribution"
    }
    $version = Get-WslPlatformVersion
    if (-not $version) {
        return "the WSL version could not be read, and Docker Desktop needs WSL $($script:DOCKER_WSL_FLOOR) or later. Update it with: host-setup\windows\upgrade-host.ps1 -Wsl"
    }
    if ((Compare-HostVersion $version $script:DOCKER_WSL_FLOOR) -lt 0) {
        return "WSL is at $version, and Docker Desktop needs $($script:DOCKER_WSL_FLOOR) or later. Update it with: host-setup\windows\upgrade-host.ps1 -Wsl"
    }
    return $null
}

# --- Docker ---

# Whether Docker Desktop answers as running right now, through its own CLI rather than a process probe.
# A vmmem process surviving a crash is not the same as the backend actually being up, and the CLI is what agrees with what the tray icon shows.
function Test-DockerDesktopRunning {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
    $text = (& docker desktop status --format json 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { return $false }
    return ($text -match '"Status"\s*:\s*"running"')
}

function Stop-DockerDesktop {
    info 'Stopping Docker Desktop'
    $code = run 'docker' @('desktop', 'stop', '--timeout', '90')
    if ($code -ne 0) { warn "docker desktop stop exited $code" }
    return $code
}

function Start-DockerDesktop {
    info 'Starting Docker Desktop'
    $code = run 'docker' @('desktop', 'start', '--timeout', '180')
    if ($code -ne 0) { warn 'docker desktop start did not exit cleanly, start Docker Desktop by hand and check WSL integration per distro' }
    return $code
}

# Everything a docker install, upgrade or reinstall needs from Docker Desktop and WSL before winget touches the package is folded into one window rather than run twice: Docker Desktop holds the WSL service open, so a platform update fails part way while it is running, the same reason upgrade-host.ps1 -Wsl refuses outright.
# Docker Desktop's own per distro WSL integration also goes stale across an engine bump often enough that it has a name on Docker's own tracker, surfacing as "WSL integration with distro '<name>' unexpectedly stopped" the next time anything in that distro touches docker, and the in app "Restart the WSL integration" button on that dialog does not clear it, since it retries the proxy inside the distro that is already running against the same stale state.
# Both are fixed the same way: Docker Desktop stopped, WSL brought current, every distro shut down with it so each one's integration remounts fresh rather than being patched in place, and Docker Desktop started again once the docker package itself is also settled, by Exit-DockerMaintenance below.
# Boundary note: this script used to be read-only on WSL by design, naming upgrade-host.ps1 -Wsl as a person's own step rather than running it.
# The maintainer moved that boundary once a docker version bump on its own already needed this same stop, then restart, window for its own integration to recover, since a platform update asks for nothing more than that same window with Docker Desktop already stopped inside it.
# Returns whether docker is clear to proceed, and whether this stopped Docker Desktop, which is what Exit-DockerMaintenance needs to know whether it owes a restart.
function Enter-DockerMaintenance {
    param([Parameter(Mandatory)][bool]$WasRunning, [string]$WslProblem)

    $result = @{ Proceed = $true; Stopped = $false }
    if (-not $WasRunning -and -not $WslProblem) { return $result }

    $question = if ($WslProblem) {
        "docker needs WSL updated first ($WslProblem). Stop Docker Desktop, update WSL, and restart Docker Desktop to continue?"
    } else {
        "docker is about to change version, and Docker Desktop's own WSL integration commonly goes stale across an engine bump. Stop Docker Desktop first, and restart it after, to avoid that?"
    }
    if (-not (confirm $question)) {
        if ($WslProblem) { $result.Proceed = $false; return $result }
        note 'docker' 'WSL integration may report a stale per-distro error until Docker Desktop and WSL are both restarted (docker desktop stop; wsl --shutdown; docker desktop start); declined the automatic cycle'
        return $result
    }

    if ($WasRunning) {
        Stop-DockerDesktop | Out-Null
        $result.Stopped = $true
    }
    if ($WslProblem) {
        # Installing through wsl --update's own MSI raises a UAC prompt when this pwsh is not itself elevated, the state the rest of this script deliberately stays in (running elevated trades this one prompt for winget installers elsewhere that fail pre-elevated instead).
        # Nothing can answer that prompt without a person at the console, so a run with neither would hang rather than fail, which -Yes is supposed to rule out, not walk into unattended.
        if (-not $script:ELEVATED -and (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected)) {
            warn "wsl --update needs administrator and raises its own prompt, which nothing can answer unattended; it would hang rather than fail, so this refuses to start it. Run interactively once so the prompt has someone to answer, or update WSL first with: host-setup\windows\upgrade-host.ps1 -Wsl"
            if ($result.Stopped) { Start-DockerDesktop | Out-Null; $result.Stopped = $false }
            $result.Proceed = $false
            return $result
        }
        info 'Updating the WSL platform'
        info 'This restarts every distribution, so anything running inside one is stopped'
        $updateCode = run 'wsl.exe' @('--update')
        if ($updateCode -ne 0) { warn "wsl --update exited $updateCode" }
        $stillBroken = Test-WslReadyForDocker
        if ($stillBroken) {
            warn "docker skipped, still $stillBroken after the update"
            if ($result.Stopped) { Start-DockerDesktop | Out-Null; $result.Stopped = $false }
            $result.Proceed = $false
            return $result
        }
    }
    return $result
}

# The other half of Enter-DockerMaintenance, run once the docker package itself is settled: shuts every WSL distro down so each one's integration remounts fresh, then starts Docker Desktop again.
# A no-op where Enter-DockerMaintenance never stopped anything, so a run that changed nothing about Docker Desktop does not restart it for no reason, and a run that was declined does not either.
function Exit-DockerMaintenance {
    param([Parameter(Mandatory)][bool]$Stopped)
    if (-not $Stopped) { return }
    info 'Shutting down WSL'
    $shutdownCode = run 'wsl.exe' @('--shutdown')
    if ($shutdownCode -ne 0) { warn "wsl --shutdown exited $shutdownCode" }
    Start-DockerDesktop | Out-Null
}

# --- Actions ---

function Show-List {
    $format = '{0,-10} {1,-24} {2}'
    log ($format -f 'TOOL', 'PACKAGE', 'OPTIONAL')
    foreach ($tool in $script:SELECTED) {
        $record = Get-Tool $tool
        $optional = if ($record.Optional.Count -gt 0) { $record.Optional -join ', ' } else { '-' }
        log ($format -f $record.Name, $record.Package, $optional)
    }
}

function Show-Report {
    $format = '{0,-10} {1,-16} {2,-16} {3,-24} {4,-13} {5}'
    log ($format -f 'TOOL', 'INSTALLED', 'AVAILABLE', 'SOURCE', 'SCOPE', 'STATUS')

    foreach ($tool in $script:SELECTED) {
        $record = Get-Tool $tool
        $state = Get-ToolState -Tool $record
        # Every row is printed only where they did not resolve to one version, since a dotnet line carrying three side by side builds resolves cleanly and listing all three would overflow the column for nothing.
        $installed = if ($state.Status -eq 'multiple') { $state.Rows -join ',' } elseif ($state.Installed) { $state.Installed } else { '-' }
        $available = if ($state.Available) { $state.Available } else { '-' }
        $scope = if ($state.Scope.Count -gt 0) { $state.Scope -join '+' } else { '-' }
        log ($format -f $record.Name, $installed, $available, $state.Package, $scope, $state.Status)
        Add-ToolNote -Tool $record -State $state
    }

    if ($script:ELEVATED) {
        note 'report' 'this pwsh is elevated, and some installers fail when launched from an elevated process, so an unelevated run is the one to prefer'
    }

    if ($script:NOTES.Count -eq 0) { return }
    log ''
    log 'Notes:'
    foreach ($entry in $script:NOTES) { info $entry }
}

# Install, upgrade or reinstall one tool.
# A tool whose install returns non-zero is collected rather than fatal, so one failure does not strand the rest of the run.
# A refusal is not a failure and does end the run: a declined prompt, or a scope that disagrees with the installed copy, stops everything rather than being collected, because continuing past either would install a copy nobody asked for.
function Invoke-ToolApply {
    param([Parameter(Mandatory)][string]$ToolName)
    $record = Get-Tool $ToolName
    $state = Get-ToolState -Tool $record
    # The id fresh work goes to, which is the installed id itself except in exactly one case: an outdated tool resolved to an id outside the tool's own default, which for node means a pinned major that is frozen and can never itself clear Available, so the default is the only id capable of fixing it.
    $target = if ($state.Status -eq 'outdated' -and $state.Package -ne $record.Package) { $record.Package } else { $state.Package }

    if ($state.Status -eq 'unmanaged') {
        log "${ToolName}: answers on PATH and winget knows no package for it, leaving it alone"
        return
    }

    # Collected rather than fatal, on the same rule as a failed install, and never installed past.
    # Installing against a state nobody could read is how a second copy lands beside a first one that was there all along.
    if ($state.Status -eq 'unreadable') {
        warn "$ToolName skipped, winget did not answer what is installed and this will not install against an unknown state"
        $script:FAILED += $ToolName
        return
    }

    # Naming a scope the installed copy does not sit in would add a second copy beside it, so the removal is asked for rather than done on the way past.
    if ($script:WANT_SCOPE -and $state.Rows.Count -gt 0 -and $state.Scope.Count -gt 0 -and
        $state.Scope -notcontains $script:WANT_SCOPE -and $script:MODE -ne 'reinstall') {
        die "${ToolName}: installed $($state.Scope -join ' and ') wide at $($state.Installed), and -Scope $($script:WANT_SCOPE) was given. Installing would add a second copy beside it. Remove the existing copy first with: install-tools.ps1 -Reinstall $ToolName -Scope $($script:WANT_SCOPE)"
    }

    if ($script:MODE -eq 'reinstall') {
        if ($state.Rows.Count -eq 0) {
            log "${ToolName}: not installed, so there is nothing to remove"
        } else {
            $where = if ($state.Scope.Count -gt 0) { " installed $($state.Scope -join ' and ') wide" } else { '' }
            $again = if ($target -eq $state.Package) { 'install it again' } else { "install $target instead" }
            if (-not (confirm "Remove $($state.Package) at $($state.Rows -join ', ')$where and $($again)?")) {
                die 'Declined'
            }
            # Every copy is removed, each in the scope it was found in, since a tool present in both scopes is exactly the shadowing this action exists to clear.
            # An empty scope means winget reported none, and there the removal names none either and lets winget act on what it finds.
            $found = if ($state.Scope.Count -gt 0) { $state.Scope } else { @('') }
            foreach ($scope in $found) {
                if ((Invoke-WingetRemove -Id $state.Package -InScope $scope) -ne 0) {
                    warn "$ToolName failed to uninstall$(if ($scope) { " the $scope wide copy" })"
                    $script:FAILED += $ToolName
                    return
                }
            }
        }
    } elseif ($state.Status -eq 'current') {
        log "${ToolName}: current at $($state.Installed), leaving it alone"
        return
    } elseif ($state.Status -eq 'self-updating') {
        log "${ToolName}: updates itself, and winget does not move it"
        return
    } elseif ($state.Status -eq 'multiple') {
        log "${ToolName}: winget lists $($state.Rows -join ', ') under one id, so -Reinstall is the action that resolves it"
        return
    } elseif (($script:MODE -eq 'upgrade' -or $script:MODE -eq 'install') -and $state.Status -eq 'outdated' -and $target -ne $state.Package) {
        # Named ahead of the plain install-mode branch below, since -Upgrade would hit this same fixed-release wall, so pointing -Install at -Upgrade here would only trade one dead end for another.
        log "${ToolName}: $($state.Package) at $($state.Installed) is a fixed release and cannot advance under that id, -Reinstall $ToolName replaces it with $target"
        return
    } elseif ($script:MODE -eq 'install' -and $state.Status -eq 'outdated') {
        log "${ToolName}: at $($state.Installed), the source carries $($state.Available), -Upgrade moves it"
        return
    }

    # Reached only once real work is about to happen: every "leave it alone" status above already returned.
    # That is deliberate here, not incidental: an already current docker with a stale WSL platform has nothing this run needs to fix, and gating on WSL readiness any earlier would skip a tool that was never going to change anyway.
    $dockerMaintenance = $null
    if ($ToolName -eq 'docker') {
        $wasRunning = Test-DockerDesktopRunning
        $wslProblem = Test-WslReadyForDocker
        if ($wslProblem -and -not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
            warn "docker skipped, $wslProblem"
            $script:FAILED += $ToolName
            return
        }
        $dockerMaintenance = Enter-DockerMaintenance -WasRunning $wasRunning -WslProblem $wslProblem
        if (-not $dockerMaintenance.Proceed) {
            warn "docker skipped, $wslProblem"
            $script:FAILED += $ToolName
            return
        }
    }

    if ($script:MODE -ne 'reinstall') {
        log "${ToolName}: $($state.Status)$(if ($state.Available) { ", the source carries $($state.Available)" })"
    }

    $packages = @($target)
    if ($script:WITH_OPTIONAL) { $packages += $record.Optional }

    foreach ($package in $packages) {
        $code = if ($state.Rows.Count -gt 0 -and $script:MODE -eq 'upgrade' -and $package -eq $target) {
            Invoke-WingetUpgrade -Id $package
        } else {
            Invoke-WingetInstall -Id $package
        }
        if ($code -ne 0) {
            warn "$ToolName failed on $package, winget exited $code"
            # Windows will not replace a file that is open, and winget reports that as an access denial naming the file rather than whatever holds it.
            # The holder is usually the tool itself, left running by an editor or a language server, so the process is named here and the reader is spared guessing at a permission problem that is not one.
            $running = @(Get-Process -Name $record.Probe -ErrorAction SilentlyContinue)
            if ($running.Count -gt 0) {
                info "$($record.Probe) is running as process $($running.Id -join ', '), and Windows cannot replace a running executable"
                info 'Close whatever is running it, then run this again'
            }
            $script:FAILED += $ToolName
            # Owed regardless of how this returns: Enter-DockerMaintenance stopped Docker Desktop for this attempt, and a failed install is not a reason to leave it down.
            if ($dockerMaintenance) { Exit-DockerMaintenance -Stopped $dockerMaintenance.Stopped }
            return
        }
    }

    $now = Resolve-InstalledVersion -Version (Get-WingetInstalled -Id $target)
    if ($now -ne $state.Installed) {
        $before = if ($state.Installed) { $state.Installed } else { '-' }
        $after = if ($now) { $now } else { '-' }
        $script:CHANGED += "$ToolName $before -> $after"
    }
    if ($dockerMaintenance) { Exit-DockerMaintenance -Stopped $dockerMaintenance.Stopped }
}

function Invoke-Apply {
    log "Selected: $($script:SELECTED -join ' ')"
    if ($script:ELEVATED) {
        warn 'This pwsh is elevated, and some installers fail when launched from an elevated process. An unelevated run lets each installer ask for administrator only where it needs it.'
    }
    if (-not (confirm "$($script:MODE) these tools?")) { die 'Declined' }

    foreach ($tool in $script:SELECTED) {
        step $tool
        Invoke-ToolApply -ToolName $tool
    }

    log ''
    if ($script:CHANGED.Count -gt 0) {
        log 'Changed:'
        foreach ($entry in $script:CHANGED) { info $entry }
    } else {
        log 'Nothing changed'
    }

    if ($script:FAILED.Count -gt 0) {
        warn "Failed: $($script:FAILED -join ' ')"
        return 1
    }
    return 0
}

# --- Entry ---

# PowerShell records which switches were given and not the order they came in, so two actions is a refusal rather than the last one winning.
# Refusing is also the better answer: an action silently discarded is one the caller believes ran.
function Resolve-Mode {
    $given = @($script:ACTIONS.Keys | Where-Object { $script:ACTIONS[$_] })
    if ($given.Count -gt 1) { die "More than one action given ($($given -join ', ')), name one" }
    if ($given.Count -eq 0) { return 'report' }
    return $given[0]
}

function Resolve-Selection {
    $names = @($script:TOOLS | ForEach-Object { $_.Name })
    if ($script:WANT_TOOLS.Count -eq 0) { return , $names }
    foreach ($candidate in $script:WANT_TOOLS) {
        if ($names -notcontains $candidate) {
            die "Unknown tool `"$candidate`", -List names the managed tools"
        }
    }
    # Sorted into registry order rather than the order they were typed, so a run reads the same however it was asked for.
    return , @($names | Where-Object { $script:WANT_TOOLS -contains $_ })
}

function main {
    if ($script:WANT_HELP) { usage; exit 0 }
    $script:MODE = Resolve-Mode
    Test-HostSupported
    $script:SELECTED = Resolve-Selection

    switch ($script:MODE) {
        'list' { Show-List; exit 0 }
        'report' { Show-Report; exit 0 }
        default { exit (Invoke-Apply) }
    }
}

main
