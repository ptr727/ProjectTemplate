# Keeps the coding-agent tools a user installed current, and installs one only when it is named.
# These tools live in the user's own profile and each carries its own updater, so this script runs as that user, never elevated.
# For the same reason it drives each tool's updater rather than replacing it.
# An upgrade touches only a tool already present, so a host where nobody installed codex never gets codex from a scheduled run.
#
# A tool installed through a package manager (winget, scoop, choco, npm, bun) is reported and left to that manager.
# This is because running the tool's own updater over it would fight the manager for the same files.
# The winget packages are already upgraded by upgrade-host.ps1, so winget is not repeated here.
# Every step is idempotent, and a tool that fails is reported while the rest still run.

# CmdletBinding with an explicit position on the tool list is what keeps every other parameter named only.
# Without it a stray word binds to the first parameter that takes a value, and a mistyped tool name is reported against an option instead.
[CmdletBinding()]
param(
    [Alias('r')][switch]$Report,
    [Alias('u')][switch]$Upgrade,
    [Alias('i')][switch]$Install,
    [Alias('l')][switch]$List,
    [Alias('a')][switch]$All,
    [Alias('j')][switch]$Json,
    [Alias('n')][switch]$DryRun,
    [Alias('y')][switch]$Yes,
    [Alias('h')][switch]$Help,
    [Parameter(Position = 0, ValueFromRemainingArguments)][string[]]$Name
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# A non-zero exit from a tool is an answer here rather than a failure.
$PSNativeCommandUseErrorActionPreference = $false

# Seconds one updater or installer may run before it is treated as hung.
# The limit is long enough for a slow download and short enough that a scheduled run ends.
$STEP_TIMEOUT = 900
$READ_TIMEOUT = 30

# Every parameter is read into a variable here rather than from inside a function.
# A script parameter reached only from a nested scope reads as declared and never used.
# That is a finding on each one, and it hides a parameter that genuinely is unused.
$ACTIONS = [ordered]@{
    report  = [bool]$Report
    upgrade = [bool]$Upgrade
    install = [bool]$Install
    list    = [bool]$List
}
$WANT_HELP = [bool]$Help
$INSTALL_ALL = [bool]$All
$JSON_OUTPUT = [bool]$Json
$DRY_RUN = [bool]$DryRun
$ASSUME_YES = [bool]$Yes
$WANT_TOOLS = @($Name | Where-Object { $_ })

$MODE = 'report'
$SELECTED = @()
$NOTES = [System.Collections.Generic.List[string]]::new()
$NOTE_TEXTS = [System.Collections.Generic.List[string]]::new()
$FAILED = [System.Collections.Generic.List[string]]::new()
$CHANGED = [System.Collections.Generic.List[string]]::new()
$TMP_DIR = ''

# --- Output ---

function log { param([string]$Message = '') Write-Host $Message }
function info { param([string]$Message) Write-Host "  $Message" }
function warn { param([string]$Message) [Console]::Error.WriteLine("WARNING: $Message") }
function die { param([string]$Message) [Console]::Error.WriteLine("ERROR: $Message"); exit 1 }

function note {
    param([string]$Tool, [string]$Text)
    $script:NOTES.Add("${Tool}: $Text")
    $script:NOTE_TEXTS.Add($Text)
}

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
Usage: install-agents.ps1 [options] [tool ...]

Keeps the coding-agent tools this user installed current, through each tool's own updater, and
installs a tool only when it is named. Runs as the user who owns the tools, never elevated.

Actions, name one, default -Report:
  -r, -Report       Report each tool, where it lives, and what manages it, change nothing
  -u, -Upgrade      Upgrade each tool already installed, and install nothing
  -i, -Install      Install the named tools that are missing, leave an installed one alone
  -l, -List         List the managed tools and where each installs from
  -h, -Help         Show this help

Options:
  -a, -All          With -Install, name every managed tool
  -j, -Json         Write the report as JSON rather than a table, for a program to read
  -n, -DryRun       Print the commands instead of running them
  -y, -Yes          Do not prompt before changing anything

With no tool named, -Report and -Upgrade select every managed tool, while -Install selects
none, so an install always names what it installs, or says -All.

A tool reads as "self" when no package manager owns it, so this script runs its update, and as
the name of a package manager when one owns it, which -Upgrade leaves to that manager. herdr's
agent integrations are reinstalled after an upgrade wherever herdr reports one outdated, and a
herdr server still running the previous binary is left running and reported as needing a
restart, since stopping it would end every pane inside it.

codex and opencode document no installer on Windows other than a package manager, so -Install
refuses them here and names the documented route rather than inventing one.

-Json writes one object: "schema" (1), "platform" ("windows"), "tools", one entry per tool with
"tool", "installed", "path", "manager", "status" ("present" or "absent"), "restart_pending" (true,
false, or null where the tool cannot say) and that tool's own "notes", and a top-level "notes".

Examples:
  install-agents.ps1                        Report on every tool
  install-agents.ps1 -Upgrade -Yes          Upgrade every tool already installed, no prompt
  install-agents.ps1 -Upgrade herdr         Upgrade herdr, if it is installed
  install-agents.ps1 -Install herdr cswap   Install two tools
  install-agents.ps1 -Install -All          Install every tool that is missing
'@
}

# --- Execution ---

# Runs one command with standard input closed and the time bounded.
# An updater that stops to ask a question therefore fails rather than hanging a scheduled run.
# A read used to decide what to do and a change both go through this, and only Invoke-Step honors -DryRun.
function Invoke-Captured {
    param([Parameter(Mandatory)][string]$Command, [string[]]$Arguments = @(), [int]$TimeoutSec = 30)
    $resolved = $Command
    if (-not [IO.Path]::IsPathRooted($Command)) {
        $found = Get-Command -Name $Command -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $found) { return [pscustomobject]@{ ExitCode = 127; Output = "$Command not found"; TimedOut = $false } }
        $resolved = $found.Source
    }
    # A batch shim, the form npm installs a command as, cannot be started directly, so cmd.exe runs it.
    if ($resolved -match '\.(cmd|bat)$') {
        $psi = [Diagnostics.ProcessStartInfo]::new($(if ($env:ComSpec) { $env:ComSpec } else { 'cmd.exe' }))
        foreach ($argument in @('/d', '/c', $resolved)) { $psi.ArgumentList.Add($argument) }
    } else {
        $psi = [Diagnostics.ProcessStartInfo]::new($resolved)
    }
    foreach ($argument in $Arguments) { $psi.ArgumentList.Add($argument) }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    try {
        $process = [Diagnostics.Process]::Start($psi)
    } catch {
        return [pscustomobject]@{ ExitCode = 127; Output = $_.Exception.Message; TimedOut = $false }
    }
    $process.StandardInput.Close()
    # Both streams are read asynchronously, since a full pipe on one would block the tool while this waits on the other.
    $outTask = $process.StandardOutput.ReadToEndAsync()
    $errTask = $process.StandardError.ReadToEndAsync()
    $timedOut = -not $process.WaitForExit($TimeoutSec * 1000)
    if ($timedOut) {
        try { $process.Kill($true) } catch { warn "Could not stop $resolved after its time ran out: $($_.Exception.Message)" }
        $null = $process.WaitForExit(5000)
    }
    $text = ''
    $stderr = ''
    if ($outTask.Wait(5000)) { $text = $outTask.Result }
    if ($errTask.Wait(5000)) { $stderr = $errTask.Result }
    $code = if ($timedOut) { 124 } else { $process.ExitCode }
    return [pscustomobject]@{ ExitCode = $code; Output = $text; Error = $stderr; TimedOut = $timedOut }
}

# Run a changing command, or print it under -DryRun, and return its exit code.
function Invoke-Step {
    param([Parameter(Mandatory)][string]$Command, [string[]]$Arguments = @())
    if ($script:DRY_RUN) {
        Write-Host "  [dry run] $Command $($Arguments -join ' ')"
        return 0
    }
    $result = Invoke-Captured -Command $Command -Arguments $Arguments -TimeoutSec $script:STEP_TIMEOUT
    if ($result.Output) { Write-Host $result.Output.TrimEnd() }
    if ($result.PSObject.Properties['Error'] -and $result.Error) { [Console]::Error.WriteLine($result.Error.TrimEnd()) }
    if ($result.TimedOut) { warn "$Command ran past $script:STEP_TIMEOUT seconds and was stopped" }
    return $result.ExitCode
}

function confirm {
    param([Parameter(Mandatory)][string]$Question)
    if ($script:ASSUME_YES -or $script:DRY_RUN) { return $true }
    # Both are checked because a scheduled task reports one and not the other, and either alone misses a case.
    if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {
        die 'Not a terminal and -Yes was not given, refusing to change anything unattended'
    }
    return ((Read-Host "$Question [y/N]") -match '^(y|yes)$')
}

# Fetches an installer to a file and runs it from there rather than piping it into a shell.
# A truncated download then fails before it runs rather than part way through.
function Invoke-Installer {
    param([Parameter(Mandatory)][string]$Url)
    $pwsh = (Get-Process -Id $PID).Path
    if ($script:DRY_RUN) {
        Write-Host "  [dry run] download $Url and run it with $pwsh -File"
        return 0
    }
    $file = Join-Path $script:TMP_DIR (([Uri]$Url).Host + '.installer.ps1')
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $file -TimeoutSec $script:STEP_TIMEOUT
    } catch {
        warn "Could not fetch ${Url}: $($_.Exception.Message)"
        return 1
    }
    return (Invoke-Step -Command $pwsh -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $file))
}

# A scheduled run starts with the PATH its account was given.
# That PATH lacks the per-user directories these installers write to and any change an installer just made to the user PATH.
# Each is appended rather than prepended, so a caller whose PATH already orders them keeps its own order.
function Update-SessionPath {
    $separator = [IO.Path]::PathSeparator
    $present = @($env:PATH -split [regex]::Escape($separator))
    $wanted = @(Join-Path $HOME '.local/bin'; Join-Path $HOME '.opencode/bin')
    if ($IsWindows) { $wanted += @([Environment]::GetEnvironmentVariable('Path', 'User') -split ';') }
    foreach ($dir in $wanted) {
        if ($dir -and (Test-Path -LiteralPath $dir -PathType Container) -and ($present -notcontains $dir)) {
            $env:PATH = "$env:PATH$separator$dir"
            $present += $dir
        }
    }
}

# --- Detection ---

function Get-ToolPath {
    param([string]$Tool)
    $found = Get-Command -Name $Tool -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    return ''
}

# The first dotted version in the tool's own banner, since each prints it with different words around it.
function Get-ToolVersion {
    param([string]$Path)
    $result = Invoke-Captured -Command $Path -Arguments @('--version') -TimeoutSec $script:READ_TIMEOUT
    if ($result.Output -match '([0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*)') { return $Matches[1] }
    return ''
}

# What owns the installed copy, read from where the command resolves to rather than from how it was meant to be installed.
function Get-ToolManager {
    param([string]$Path)
    # Separators are unified so one set of patterns reads a Windows path and the stub path of a test on another host alike.
    $p = $Path -replace '/', '\'
    $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($item -and $item.LinkTarget) { $p = "$p`n" + (($item.LinkTarget -replace '/', '\')) }
    foreach ($candidate in ($p -split "`n")) {
        if ($candidate -like '*\WinGet\Packages\*' -or $candidate -like '*\Microsoft\WinGet\Links\*') { return 'winget' }
        if ($candidate -like '*\scoop\*') { return 'scoop' }
        if ($candidate -like '*\chocolatey\*') { return 'choco' }
        if ($candidate -like '*\node_modules\*' -or $candidate -like '*\npm\*') { return 'npm' }
        if ($candidate -like '*\.bun\*') { return 'bun' }
    }
    return 'self'
}

# --- herdr ---

# The integrations herdr reports outdated, or $null where the status could not be read.
function Get-HerdrOutdated {
    $result = Invoke-Captured -Command 'herdr' -Arguments @('integration', 'status', '--outdated-only') -TimeoutSec $script:READ_TIMEOUT
    if ($result.ExitCode -ne 0) { return $null }
    $names = @()
    foreach ($line in ($result.Output -split "`r?`n")) {
        if ($line -match '^([A-Za-z0-9_-]+):\s') { $names += $Matches[1] }
    }
    return , $names
}

# Read from herdr's own status rather than from whether a session is open.
# This is because a session on the current binary needs no restart however many panes it holds.
# True, false, or $null where herdr could not say.
function Get-HerdrRestartPending {
    $result = Invoke-Captured -Command 'herdr' -Arguments @('status', '--json') -TimeoutSec $script:READ_TIMEOUT
    if ($result.ExitCode -ne 0) { return $null }
    try {
        $status = $result.Output | ConvertFrom-Json
    } catch {
        return $null
    }
    if ($status -isnot [pscustomobject] -or -not $status.PSObject.Properties['update']) { return $null }
    $update = $status.update
    if ($update -isnot [pscustomobject] -or -not $update.PSObject.Properties['restart_needed']) { return $null }
    if ($update.restart_needed -is [bool]) { return $update.restart_needed }
    return $null
}

function Invoke-HerdrPost {
    $names = Get-HerdrOutdated
    if ($null -eq $names) {
        warn 'herdr: could not read the integration status'
        return 1
    }
    $failed = 0
    foreach ($integration in $names) {
        log "herdr: reinstalling the outdated $integration integration"
        if ((Invoke-Step -Command 'herdr' -Arguments @('integration', 'install', $integration)) -ne 0) { $failed = 1 }
    }
    return $failed
}

function Add-HerdrNote {
    $names = Get-HerdrOutdated
    if ($null -eq $names) {
        note 'herdr' 'could not read the integration status'
    } elseif ($names.Count -gt 0) {
        note 'herdr' "integrations outdated: $($names -join ' '), -Upgrade reinstalls them"
    }
    $pending = Get-HerdrRestartPending
    if ($pending -eq $true) {
        note 'herdr' 'a running server is on the previous binary, restart it when no pane needs it: herdr server stop'
    } elseif ($null -eq $pending) {
        note 'herdr' 'could not read whether a running server needs a restart'
    }
}

# --- The tools ---

# Each tool names where it installs from, how it updates, and how it installs.
# Neither codex nor opencode documents a Windows installer outside a package manager, so their install refuses and says which route their documentation names.
$TOOLS = @(
    @{
        Name    = 'claude'
        Source  = 'https://claude.ai/install.ps1'
        Upgrade = { Invoke-Step -Command 'claude' -Arguments @('update') }
        Install = { Invoke-Installer -Url 'https://claude.ai/install.ps1' }
    }
    @{
        Name    = 'codex'
        Source  = 'https://learn.chatgpt.com/docs/codex/cli'
        Upgrade = { Invoke-Step -Command 'codex' -Arguments @('update') }
        Install = {
            warn 'codex documents no Windows installer outside a package manager, and the route for Windows is on https://learn.chatgpt.com/docs/codex/cli, install it that way'
            return 1
        }
    }
    @{
        Name    = 'opencode'
        Source  = 'https://opencode.ai/docs/'
        Upgrade = { Invoke-Step -Command 'opencode' -Arguments @('upgrade') }
        Install = {
            warn 'opencode documents no Windows installer outside a package manager, install it with scoop install opencode, choco install opencode, or npm install -g opencode-ai (https://opencode.ai/docs/)'
            return 1
        }
    }
    @{
        Name    = 'herdr'
        Source  = 'https://herdr.dev/install.ps1'
        # A plain update rather than a handoff, so a live server keeps its panes on the old binary until someone restarts it.
        Upgrade = { Invoke-Step -Command 'herdr' -Arguments @('update') }
        Install = { Invoke-Installer -Url 'https://herdr.dev/install.ps1' }
    }
    @{
        Name    = 'cswap'
        Source  = 'uv tool install claude-swap'
        # Run through uv directly, because cswap upgrade on Windows only prints this command, its launcher being locked while it runs.
        Upgrade = { Invoke-Step -Command 'uv' -Arguments @('tool', 'upgrade', 'claude-swap') }
        Install = {
            if (-not $script:DRY_RUN -and -not (Get-ToolPath 'uv')) {
                warn 'cswap installs as a uv tool, and uv is not installed, install-tools.ps1 installs it'
                return 1
            }
            return (Invoke-Step -Command 'uv' -Arguments @('tool', 'install', 'claude-swap'))
        }
    }
)

function Get-Tool {
    param([string]$ToolName)
    return ($script:TOOLS | Where-Object { $_.Name -ceq $ToolName } | Select-Object -First 1)
}

# --- Report ---

function Get-RestartPending {
    param([string]$Tool)
    if ($Tool -ne 'herdr') { return $null }
    return (Get-HerdrRestartPending)
}

function Show-Report {
    $format = '{0,-10} {1,-16} {2,-8} {3}'
    $rows = @()
    if (-not $script:JSON_OUTPUT) { log ($format -f 'TOOL', 'INSTALLED', 'MANAGER', 'PATH') }

    foreach ($tool in $script:SELECTED) {
        $first = $script:NOTES.Count
        $path = Get-ToolPath $tool
        $installed = $null
        $manager = $null
        $status = 'absent'
        $restart = $null
        if ($path) {
            $version = Get-ToolVersion $path
            if ($version) { $installed = $version }
            $manager = Get-ToolManager $path
            $status = 'present'
            if ($manager -ne 'self') {
                note $tool "installed through $manager, which upgrades it, so -Upgrade leaves it alone"
            } elseif ($tool -eq 'herdr') {
                Add-HerdrNote
            }
            $restart = Get-RestartPending $tool
        }
        $shown = if ($path) { Hide-Home $path } else { $null }
        if ($script:JSON_OUTPUT) {
            $own = @()
            for ($i = $first; $i -lt $script:NOTE_TEXTS.Count; $i++) { $own += $script:NOTE_TEXTS[$i] }
            $rows += [ordered]@{
                tool            = $tool
                installed       = $installed
                path            = $shown
                manager         = $manager
                status          = $status
                restart_pending = $restart
                notes           = $own
            }
        } else {
            $v = if ($installed) { $installed } else { '-' }
            $m = if ($manager) { $manager } else { '-' }
            $s = if ($shown) { $shown } else { '-' }
            log ($format -f $tool, $v, $m, $s)
        }
    }

    if ($script:JSON_OUTPUT) {
        $document = [ordered]@{
            schema   = 1
            platform = 'windows'
            tools    = $rows
            notes    = @()
        }
        ConvertTo-Json -InputObject $document -Depth 6 -EscapeHandling EscapeNonAscii
        return
    }

    if ($script:NOTES.Count -eq 0) { return }
    log ''
    log 'Notes:'
    foreach ($entry in $script:NOTES) { info $entry }
}

# --- Apply ---

function Invoke-ToolUpgrade {
    param([string]$ToolName)
    $tool = Get-Tool $ToolName
    $path = Get-ToolPath $ToolName
    if (-not $path) {
        log "${ToolName}: not installed, leaving it absent"
        return
    }
    $manager = Get-ToolManager $path
    $installed = Get-ToolVersion $path
    $shownVersion = if ($installed) { $installed } else { 'an unreadable version' }
    if ($manager -ne 'self') {
        log "${ToolName}: installed through $manager, leaving it to $manager"
        return
    }

    # The herdr updater refuses to run from inside one of its own panes.
    # A run there therefore skips that one step rather than failing on a refusal it already knows is coming.
    if ($ToolName -eq 'herdr' -and $env:HERDR_PANE_ID) {
        log "herdr: at $shownVersion, and this run is inside a herdr pane, which herdr will not update from, run this outside herdr to update it"
        if ((Invoke-HerdrPost) -ne 0) { $script:FAILED.Add('herdr integrations') }
        return
    }

    log "${ToolName}: at $shownVersion, running its updater"
    if ((& $tool.Upgrade) -ne 0) {
        warn "${ToolName}: its updater failed"
        $script:FAILED.Add($ToolName)
        return
    }
    # Resolved again, since an updater may put the new binary somewhere that now comes first on PATH.
    $path = Get-ToolPath $ToolName
    $now = if ($path) { Get-ToolVersion $path } else { '' }
    if ($now -ne $installed) {
        $before = if ($installed) { $installed } else { '-' }
        $after = if ($now) { $now } else { '-' }
        $script:CHANGED.Add("$ToolName $before -> $after")
    }

    # Run whether or not the version moved, since an integration can be outdated against a herdr that was already current.
    if ($ToolName -eq 'herdr') {
        if ((Invoke-HerdrPost) -ne 0) { $script:FAILED.Add('herdr integrations') }
        if (-not $script:DRY_RUN) {
            $pending = Get-HerdrRestartPending
            if ($pending -eq $true) {
                log 'herdr: a running server is on the previous binary and is left running, restart it when no pane needs it'
            } elseif ($null -eq $pending) {
                log 'herdr: could not read whether a running server needs a restart'
            }
        }
    }
}

function Invoke-ToolInstall {
    param([string]$ToolName)
    $tool = Get-Tool $ToolName
    $path = Get-ToolPath $ToolName
    if ($path) {
        log "${ToolName}: already installed at $(Hide-Home $path), leaving it alone, -Upgrade updates it"
        return
    }
    log "${ToolName}: installing from $($tool.Source)"
    if ((& $tool.Install) -ne 0) {
        warn "${ToolName}: its installer failed"
        $script:FAILED.Add($ToolName)
        return
    }
    if ($script:DRY_RUN) { return }
    Update-SessionPath
    $path = Get-ToolPath $ToolName
    if (-not $path) {
        warn "${ToolName}: its installer finished, but no $ToolName is on PATH"
        $script:FAILED.Add($ToolName)
        return
    }
    $now = Get-ToolVersion $path
    $after = if ($now) { $now } else { '-' }
    $script:CHANGED.Add("$ToolName - -> $after")
}

function Invoke-Apply {
    log "Selected: $($script:SELECTED -join ' ')"
    if ($script:DRY_RUN) { log "Mode: $script:MODE (dry run)" } else { log "Mode: $script:MODE" }
    log ''
    if (-not (confirm "Change the agent tools of $([Environment]::UserName)?")) { die 'Declined' }
    log ''

    foreach ($tool in $script:SELECTED) {
        if ($script:MODE -eq 'install') { Invoke-ToolInstall $tool } else { Invoke-ToolUpgrade $tool }
    }

    log ''
    if ($script:CHANGED.Count -gt 0) {
        log 'Changed:'
        foreach ($entry in $script:CHANGED) { info $entry }
        info 'A session already running keeps its version until it is restarted'
    } else {
        log 'Nothing changed'
    }

    if ($script:FAILED.Count -gt 0) {
        log ''
        warn "Failed: $($script:FAILED -join ' ')"
        return 1
    }
    return 0
}

function Show-List {
    log 'Managed agent tools:'
    foreach ($tool in $script:TOOLS) { log ('  {0,-10} {1}' -f $tool.Name, $tool.Source) }
}

# --- Entry ---

function Resolve-Selection {
    $names = @($script:TOOLS | ForEach-Object { $_.Name })
    foreach ($candidate in $script:WANT_TOOLS) {
        if ($names -cnotcontains $candidate) { die "Unknown tool `"$candidate`", -List names the managed tools" }
    }
    if ($script:WANT_TOOLS.Count -eq 0) {
        if ($script:MODE -eq 'install' -and -not $script:INSTALL_ALL) {
            die '-Install names the tools it installs, or takes -All, so nothing is installed by default'
        }
        return , $names
    }
    # Sorted into registry order rather than the order they were typed, so a run reads the same however it was asked for.
    return , @($names | Where-Object { $script:WANT_TOOLS -ccontains $_ })
}

# The order the actions were given is not a contract, so more than one is a refusal rather than the last one winning.
function Resolve-Mode {
    $given = @($script:ACTIONS.Keys | Where-Object { $script:ACTIONS[$_] })
    if ($given.Count -gt 1) { die "More than one action given ($($given -join ', ')), name one" }
    $mode = if ($given.Count -eq 0) { 'report' } else { $given[0] }
    if ($script:JSON_OUTPUT -and $mode -ne 'report') {
        die "-Json changes how a report is written, so it applies only to -Report, and the $mode action was given"
    }
    if ($script:INSTALL_ALL -and $mode -ne 'install') {
        die "-All names every tool for -Install, and the $mode action was given"
    }
    if ($script:INSTALL_ALL -and $script:WANT_TOOLS.Count -gt 0) {
        die '-All and a tool name were both given, name one or the other'
    }
    return $mode
}

# Agent tools live in the user's profile, so an elevated run would read an administrator's profile and install into the wrong one.
# Only Windows has the elevation the check reads, and a test host elsewhere has nothing to refuse.
function Test-Elevated {
    if (-not $IsWindows) { return $false }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function main {
    if ($script:WANT_HELP) { usage; exit 0 }
    $unknown = @($script:WANT_TOOLS | Where-Object { $_ -like '-*' })
    if ($unknown.Count -gt 0) { die "Unknown option `"$($unknown[0])`", -Help lists the options" }
    if ($PSVersionTable.PSVersion.Major -lt 7) {
        die "This script needs PowerShell 7 or later, and this is $($PSVersionTable.PSVersion). Install it with: winget install --id Microsoft.PowerShell --exact --source winget"
    }
    $script:MODE = Resolve-Mode
    $script:SELECTED = Resolve-Selection

    if ($script:MODE -eq 'list') { Show-List; exit 0 }

    if (Test-Elevated) {
        die 'Agent tools live in a user profile, so this runs as that user and not elevated, run it from a normal PowerShell window'
    }
    if (-not $HOME -or -not (Test-Path -LiteralPath $HOME -PathType Container)) {
        die 'HOME is not set to a directory, so there is no user profile to read the tools from'
    }
    Update-SessionPath

    if ($script:MODE -eq 'report') { Show-Report; exit 0 }

    $script:TMP_DIR = Join-Path ([IO.Path]::GetTempPath()) ("install-agents-" + [guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $script:TMP_DIR
    try {
        $code = Invoke-Apply
    } finally {
        Remove-Item -LiteralPath $script:TMP_DIR -Recurse -Force -ErrorAction SilentlyContinue
    }
    exit $code
}

main
