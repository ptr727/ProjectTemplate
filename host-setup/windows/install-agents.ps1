# Keeps the coding-agent tools a user installed current, and installs one only when it is named.
# These tools live in the user's own profile and each carries its own updater, so this script runs as that user, never elevated.
# For the same reason it drives each tool's updater rather than replacing it.
# An upgrade touches only a tool already present, so a host where nobody installed codex never gets codex from a scheduled run.
#
# A tool installed through a package manager is never given its own updater, which would fight the manager for the same files.
# An upgrade runs that manager's own upgrade for that one package instead: npm, bun, and scoop.
# The winget packages are already upgraded by upgrade-host.ps1, so winget is not repeated here.
# A choco package needs an elevated shell and this script never elevates, so it is reported with its command and nothing is run.
# The same holds for an npm prefix under Program Files or ProgramData that a normal user cannot write, once a link there is followed.
# The npm on PATH owns a tool only where the prefix it reports holds it, rather than where a path merely looks like npm's.
# The package name is a per-tool record in this file (Get-NpmPackage), because a manager's shim does not always carry it.
# An npm or bun package must own the tool's files, so a fork providing the same command is reported and not replaced.
# A herdr upgraded by a manager gets the same integration and restart step the self-updater path runs.
# The scoop roots come from SCOOP and SCOOP_GLOBAL, else from scoop's own config.json, else from the defaults.
# A Node runtime is never upgraded for an agent inside it, and a global scoop app needs an elevated shell, so each is reported.
# The scoop app is read from under the scoop root's own apps directory, and the choco name from the choco lib directory.
# A tool recording a Managed copy (opencode) is upgraded through that copy, and another copy ahead of it is reported for a fix by hand.
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
$NPM_PREFIX = $null

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
    # The match ends at a path component, so a sibling profile such as the same name with a suffix is shown as it is.
    $profileRoot = $HOME.TrimEnd('\', '/')
    if ($Path -ieq $profileRoot -or $Path.StartsWith("$profileRoot\", [StringComparison]::OrdinalIgnoreCase) -or $Path.StartsWith("$profileRoot/", [StringComparison]::OrdinalIgnoreCase)) {
        return '%USERPROFILE%' + $Path.Substring($profileRoot.Length)
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
the name of a package manager when one owns it. -Upgrade then runs that manager's upgrade for
that one package: npm or bun install the latest tag, and scoop updates the app. winget is
upgraded by upgrade-host.ps1. choco needs an elevated shell and an npm directory under Program
Files or ProgramData needs one too, so each is reported with its command and nothing is run. herdr's
agent integrations are reinstalled after an upgrade wherever herdr reports one outdated, and a
herdr server still running the previous binary is left running and reported as needing a
restart, since stopping it would end every pane inside it.

opencode is the one tool with a managed copy, the npm package @opencode/cli, since it ships from
several sources, opencode 2 replaced opencode 1's npm package, and its installer script is bash
only. Another copy PATH reaches first, an npm opencode-ai among them, is reported with the command
that removes it and is never removed here. -Upgrade then upgrades the managed package where one is
installed, and -Install installs it only once no other copy is in the way.

-Json writes one object: "schema" (1), "platform" ("windows"), "tools", one entry per tool with
"tool", "installed", "path", "manager", "status" ("present" or "absent"), "restart_pending" (true,
false, or null where the tool cannot say) and that tool's own "notes", and a top-level "notes".

It exits 0 on success, 3 where a tool failed or was refused while the others still ran, and 1 where the run itself refused or stopped.

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

# The npm registry name of a tool, which npm and bun both install from, or nothing where none is recorded.
# These are the names each tool's own documentation gives, and herdr and cswap are not published there.
function Get-NpmPackage {
    param([string]$Tool)
    switch ($Tool) {
        'claude' { return '@anthropic-ai/claude-code' }
        'codex' { return '@openai/codex' }
        'opencode' { return '@opencode/cli' }
    }
    return $null
}

# Every npm registry name a tool has shipped under, current first.
# A copy under an earlier name is still that tool's, so its removal is named by that name, as opencode 1's opencode-ai is.
function Get-KnownNpmPackage {
    param([string]$Tool)
    $names = @()
    $current = Get-NpmPackage -Tool $Tool
    if ($current) { $names += $current }
    if ($Tool -eq 'opencode') { $names += 'opencode-ai' }
    return , $names
}

# A path with every link along it followed to its final target, or the path as given where it cannot be read.
# Each component is resolved, since a link can sit above the last one, as scoop's current junction does.
# It is the one link resolver here, so ownership, writability, and the manager read all see the same real path.
function Resolve-FinalPath {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return $Path }
    if ($item.LinkTarget) {
        $target = $item.ResolveLinkTarget($true)
        if ($target) { return Resolve-FinalPath -Path $target.FullName }
    }
    $parent = Split-Path -Parent $item.FullName
    if (-not $parent -or $parent -eq $item.FullName) { return $item.FullName }
    return Join-Path (Resolve-FinalPath -Path $parent) $item.Name
}

# Whether a path is the root or sits under it, with both sides read with either separator and without a trailing one.
# The match ends at a path component, so a sibling such as Program FilesPortable is not read as under Program Files.
# Below leaves out the root itself, for a caller that needs something inside it.
function Test-PathUnder {
    param([string]$Path, [string]$Root, [switch]$Below)
    $where = ($Path -replace '/', '\').TrimEnd('\')
    $base = ($Root -replace '/', '\').TrimEnd('\')
    if (-not $base) { return $false }
    if (-not $Below -and $where -ieq $base) { return $true }
    return $where.StartsWith("$base\", [StringComparison]::OrdinalIgnoreCase)
}

# Whether a directory sits under one of the roots a normal user cannot write.
function Test-ProtectedPath {
    param([string]$Directory, [string[]]$Roots)
    foreach ($root in ($Roots | Where-Object { $_ })) {
        if (Test-PathUnder -Path $Directory -Root $root) { return $true }
    }
    return $false
}

# Whether this user can write a directory, without elevating to find out.
# Windows keeps a per-user directory writable, and one under Program Files or ProgramData needs an elevated shell.
# The nvm-windows install points C:\Program Files\nodejs into a user-writable directory, so the link target decides, not the link.
function Test-DirectoryWritable {
    param([string]$Directory)
    $real = Resolve-FinalPath -Path $Directory
    if ($IsWindows) {
        return -not (Test-ProtectedPath -Directory $real -Roots @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData))
    }
    & test -w $real
    return ($LASTEXITCODE -eq 0)
}

# The prefix the npm on PATH installs global packages and their shims into, or nothing where npm cannot say.
function Get-NpmPrefix {
    if ($null -eq $script:NPM_PREFIX) {
        $script:NPM_PREFIX = ''
        if (Get-Command npm -CommandType Application -ErrorAction SilentlyContinue) {
            $result = Invoke-Captured -Command 'npm' -Arguments @('prefix', '-g') -TimeoutSec $script:READ_TIMEOUT
            if ($result.ExitCode -eq 0 -and $result.Output) { $script:NPM_PREFIX = $result.Output.Trim() }
        }
    }
    return $script:NPM_PREFIX
}

# Whether a tool's files sit where the npm on PATH installs: its shims directly in the prefix, or the package under the prefix's node_modules.
function Test-NpmOwnership {
    param([string]$Path)
    $prefix = Get-NpmPrefix
    if (-not $prefix) { return $false }
    # Both sides are resolved through every link first, since a manager can put a linked path on PATH.
    # Scoop's Node, for one, lists apps\nodejs\current\bin, which leads to the persist directory npm names as its prefix.
    $prefix = (Resolve-FinalPath -Path $prefix) -replace '/', '\'
    $prefix = $prefix.TrimEnd('\')
    $parent = Split-Path -Parent $Path
    $leaf = Split-Path -Leaf $Path
    $resolved = if ($parent) { Join-Path (Resolve-FinalPath -Path $parent) $leaf } else { $Path }
    $where = ($resolved -replace '/', '\')
    $cut = $where.LastIndexOf('\')
    $directory = if ($cut -gt 0) { $where.Substring(0, $cut).TrimEnd('\') } else { '' }
    return ($directory -ieq $prefix -or (Test-PathUnder -Path $where -Root "$prefix\node_modules" -Below))
}

# The scoop roots this script knows, the user's and the global one.
# SCOOP else the profile's scoop, and SCOOP_GLOBAL else the scoop under ProgramData.
function Get-ScoopRoot {
    # Scoop's own config names the roots when the environment does not, as the scoop command itself reads it.
    $config = $null
    $configHome = if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path $HOME '.config' }
    $configFile = Join-Path (Join-Path $configHome 'scoop') 'config.json'
    if (Test-Path -LiteralPath $configFile -PathType Leaf) {
        try { $config = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json } catch { $config = $null }
    }
    $fromConfig = {
        param([string]$Key)
        if ($config -and $config.PSObject.Properties[$Key] -and $config.$Key -is [string] -and $config.$Key) { return $config.$Key }
        return $null
    }
    $user = if ($env:SCOOP) { $env:SCOOP } elseif (& $fromConfig 'root_path') { & $fromConfig 'root_path' } else { Join-Path $HOME 'scoop' }
    $roots = @([pscustomobject]@{ Root = $user; Global = $false })
    $global = if ($env:SCOOP_GLOBAL) { $env:SCOOP_GLOBAL } elseif (& $fromConfig 'global_path') { & $fromConfig 'global_path' } elseif ($env:ProgramData) { Join-Path $env:ProgramData 'scoop' } else { $null }
    if ($global) { $roots += [pscustomobject]@{ Root = $global; Global = $true } }
    return $roots
}

# Whether a path sits under the shims or apps directory of a scoop root this script knows.
function Test-UnderScoop {
    param([string]$Path)
    foreach ($entry in (Get-ScoopRoot)) {
        foreach ($sub in 'shims', 'apps') {
            # Joined as text rather than with Join-Path, which fails for a root on a drive that is not present.
            # The root's trailing separator is trimmed first, as Get-ScoopApp does, so a root written as D:\Tools\ still matches.
            $base = ($entry.Root -replace '/', '\').TrimEnd('\')
            if (Test-PathUnder -Path $Path -Root "$base\$sub" -Below) { return $true }
        }
    }
    return $false
}

# The scoop app a path belongs to, read from directly under a scoop root's own apps directory, or nothing for any other path.
# A first apps segment anywhere in the path would take a directory a user happened to name Apps.
# Global says whether the app sits under the global root, which needs an elevated shell to update.
function Get-ScoopApp {
    param([string]$Path)
    # Scoop prints a per-user path with a leading tilde, so it is expanded to the profile before the roots are compared.
    if ($Path -match '^~[\\/]') { $Path = Join-Path $HOME $Path.Substring(2) }
    $where = $Path -replace '/', '\'
    foreach ($entry in (Get-ScoopRoot)) {
        $apps = (($entry.Root -replace '/', '\').TrimEnd('\')) + '\apps\'
        if (Test-PathUnder -Path $where -Root $apps -Below) {
            $name = ($where.Substring($apps.Length) -split '\\')[0]
            if ($name) { return [pscustomobject]@{ App = $name; Global = $entry.Global } }
        }
    }
    return $null
}

# The directory an npm package installs into under the prefix of the npm on PATH, or nothing where npm cannot say.
function Get-NpmPackageDirectory {
    param([string]$Package)
    $prefix = Get-NpmPrefix
    if (-not $prefix) { return $null }
    $prefix = (Resolve-FinalPath -Path $prefix).TrimEnd('\', '/')
    return (Join-Path (Join-Path $prefix 'node_modules') ($Package -replace '/', [IO.Path]::DirectorySeparatorChar))
}

# Whether the npm package a tool is recorded under is the one that owns its files.
# The package must be installed under the prefix, and the file must sit in it or a shim must name its directory.
# This keeps a fork that provides the same command from being replaced by the recorded package.
function Test-NpmPackageOwnership {
    param([string]$Path, [string]$Package)
    $directory = Get-NpmPackageDirectory -Package $Package
    if (-not $directory -or -not (Test-Path -LiteralPath (Join-Path $directory 'package.json') -PathType Leaf)) { return $false }
    $resolved = (Resolve-FinalPath -Path $Path) -replace '/', '\'
    if (Test-PathUnder -Path $resolved -Root $directory -Below) { return $true }
    # A shim is a script beside the package rather than a link into it, so its text must name the package's directory.
    return (Test-NpmShimName -Path $Path -Package $Package)
}

# Whether a shim's text names an npm package's directory.
function Test-NpmShimName {
    param([string]$Path, [string]$Package)
    $text = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
    if (-not $text) { return $false }
    $needle = 'node_modules\' + ($Package -replace '/', '\') + '\'
    return (($text -replace '/', '\').IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0)
}

# Whether the tool is bun's global install of the recorded package.
# On Windows bun copies a shim into its bin directory rather than linking into the package, so the shim alone names no package.
# There the shim must sit in bun's bin directory and the recorded package must be installed globally, as the npm branch checks package.json.
# The .bunx file beside the shim records the target, but its format is undocumented, so it is not read.
# Bun's bin directory, where its copied shims sit, and its global package directory, each resolved and without a trailing separator.
# BUN_INSTALL and BUN_INSTALL_GLOBAL_DIR are honored, and the install.globalDir in bunfig.toml is not read.
function Get-BunDirectory {
    $bun = if ($env:BUN_INSTALL) { $env:BUN_INSTALL } else { Join-Path $HOME '.bun' }
    $globalDir = if ($env:BUN_INSTALL_GLOBAL_DIR) { $env:BUN_INSTALL_GLOBAL_DIR } else { Join-Path (Join-Path $bun 'install') 'global' }
    return [pscustomobject]@{
        Bin    = ((Resolve-FinalPath -Path (Join-Path $bun 'bin')) -replace '/', '\').TrimEnd('\')
        Global = ((Resolve-FinalPath -Path $globalDir) -replace '/', '\').TrimEnd('\')
    }
}

function Test-BunPackageOwnership {
    param([string]$Path, [string]$Package)
    $bun = Get-BunDirectory
    $directory = Join-Path (Join-Path $bun.Global 'node_modules') ($Package -replace '/', '\')
    $resolved = (Resolve-FinalPath -Path $Path) -replace '/', '\'
    $inside = (Resolve-FinalPath -Path $directory) -replace '/', '\'
    if (Test-PathUnder -Path $resolved -Root $inside -Below) { return $true }
    $bin = $bun.Bin
    $cut = $resolved.LastIndexOf('\')
    $shimDirectory = if ($cut -gt 0) { $resolved.Substring(0, $cut) } else { '' }
    return ($shimDirectory -ieq $bin -and (Test-Path -LiteralPath (Join-Path $directory 'package.json')))
}

# The package among a tool's recorded npm names that owns its files under a manager, npm or bun, or nothing where none does.
# An upgrade and a stray's removal share it, so a copy under a name the tool shipped under earlier is named by that name.
function Get-OwningNpmPackage {
    param([string]$Tool, [string]$Path, [string]$Manager)
    foreach ($pkg in (Get-KnownNpmPackage -Tool $Tool)) {
        $owned = if ($Manager -eq 'npm') { Test-NpmPackageOwnership -Path $Path -Package $pkg } else { Test-BunPackageOwnership -Path $Path -Package $pkg }
        if ($owned) { return $pkg }
    }
    return $null
}

# The recorded npm name a tool's path or its shim's text shows, read without asking npm, or nothing where neither shows one.
function Get-NamedNpmPackage {
    param([string]$Tool, [string]$Path)
    $resolved = (Resolve-FinalPath -Path $Path) -replace '/', '\'
    foreach ($pkg in (Get-KnownNpmPackage -Tool $Tool)) {
        $needle = 'node_modules\' + ($pkg -replace '/', '\') + '\'
        if ($resolved.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $pkg }
        if (Test-NpmShimName -Path $Path -Package $pkg) { return $pkg }
    }
    return $null
}

# What an npm or bun step, upgrade or remove, runs for the package owning a tool's files, as Command and Arguments, or Why none runs.
# An upgrade and a stray's removal share it, so a copy under a name the tool shipped under earlier is named by that name.
# One of the tool's recorded names must own the files, so a fork providing the same command is never replaced or removed.
function Get-PackageStep {
    param([string]$Step, [string]$Tool, [string]$Path, [string]$Manager)
    $plan = [pscustomobject]@{ Command = $null; Arguments = @(); Why = '' }
    $action = if ($Step -eq 'upgrade') { 'upgrade' } else { 'remove' }
    $recorded = Get-NpmPackage -Tool $Tool
    if (-not $recorded) {
        $plan.Why = "no $Manager package name is recorded for $Tool, so $action it with $Manager by hand"
        return $plan
    }
    $pkg = Get-OwningNpmPackage -Tool $Tool -Path $Path -Manager $Manager
    # Where npm or bun cannot say which package owns the files, the command names the one the path or its shim shows.
    $name = if ($pkg) { $pkg } else { Get-NamedNpmPackage -Tool $Tool -Path $Path }
    if (-not $name) { $name = $recorded }
    # Installing the latest tag rather than npm update, which keeps a global package inside the range it was installed at.
    $arguments = if ($Step -eq 'upgrade') { @('install', '-g', "$name@latest") } elseif ($Manager -eq 'bun') { @('remove', '-g', $name) } else { @('uninstall', '-g', $name) }
    $shown = "$Manager $($arguments -join ' ')"
    if (-not (Get-Command $Manager -CommandType Application -ErrorAction SilentlyContinue)) {
        $plan.Why = "$Manager is not on PATH, so run: $shown"
    } elseif ($Manager -eq 'npm' -and -not (Test-NpmOwnership -Path $Path)) {
        $plan.Why = "its files at $(Hide-Home -Path $Path) are not under the prefix of the npm on PATH, so that npm does not own it, $action it where it was installed"
    } elseif ($Manager -eq 'npm' -and -not $pkg) {
        $plan.Why = "its files at $(Hide-Home -Path $Path) are not the npm package $recorded, so that package does not own it, $action it where it was installed"
    } elseif ($Manager -eq 'bun' -and -not $pkg) {
        $plan.Why = "its files at $(Hide-Home -Path $Path) are not under bun's global package $recorded, so that package does not own it, $action it where it was installed"
    } elseif ($Manager -eq 'npm' -and -not (Test-DirectoryWritable -Directory (Get-NpmPrefix))) {
        $plan.Why = "the npm prefix $(Hide-Home -Path (Get-NpmPrefix)) is not writable by this user, so run in an elevated shell: $shown"
    } else {
        $plan.Command = $Manager
        $plan.Arguments = $arguments
    }
    return $plan
}

# What a scoop step, update or uninstall, runs for a tool's app, as Command and Arguments, or Why none runs.
# An upgrade and a stray's removal share it, so both read the app from scoop and both leave scoop's Node and a global app alone.
function Get-ScoopStep {
    param([string]$Tool, [string]$Verb)
    $plan = [pscustomobject]@{ Command = $null; Arguments = @(); Why = '' }
    $action = if ($Verb -eq 'update') { 'upgrade' } else { 'remove' }
    $app = $null
    if (Get-Command scoop -CommandType Application -ErrorAction SilentlyContinue) {
        $which = Invoke-Captured -Command 'scoop' -Arguments @('which', $Tool) -TimeoutSec $script:READ_TIMEOUT
        if ($which.ExitCode -eq 0) { $app = Get-ScoopApp -Path $which.Output.Trim() }
    }
    if ($app -and $app.App -in 'nodejs', 'nodejs-lts') {
        $plan.Why = "it sits inside scoop's Node, and the npm on PATH does not own it, so $action it where it was installed"
    } elseif ($app -and $app.Global) {
        $plan.Why = "scoop's global apps need an elevated shell and this script never elevates, so run in one: scoop $Verb $($app.App) --global"
    } elseif ($app) {
        $plan.Command = 'scoop'
        $plan.Arguments = @($Verb, $app.App)
    } else {
        $plan.Why = "scoop's name for $Tool could not be read, so run scoop $Verb with it by hand"
    }
    return $plan
}

# The choco package holding a tool, read from the choco lib directory, or a placeholder where it cannot be read.
function Get-ChocoPackage {
    param([string]$Tool)
    $lib = if ($env:ChocolateyInstall) { Join-Path $env:ChocolateyInstall 'lib' } else { $null }
    if ($lib -and (Test-Path -LiteralPath $lib -PathType Container)) {
        $hit = Get-ChildItem -LiteralPath $lib -Recurse -Filter "$Tool.exe" -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return ($hit.FullName.Substring($lib.Length).TrimStart('\', '/') -split '[\\/]')[0] }
    }
    return '<package>'
}

# Why npm cannot install a package globally for this user, or nothing where it can.
# The install of the managed package reads it, and so does its upgrade while another copy sits ahead of it.
function Get-NpmInstallRefusal {
    param([string]$Package)
    if (-not (Get-ToolPath 'npm')) { return 'npm is not installed' }
    $prefix = Get-NpmPrefix
    if ($prefix -and -not (Test-DirectoryWritable -Directory $prefix)) {
        return "the npm prefix $(Hide-Home -Path $prefix) is not writable by this user, so run in an elevated shell: npm install -g $Package"
    }
    return ''
}

# What an upgrade does for a tool a package manager owns.
# Command and Arguments hold the command to run, or Why holds the reason none runs, and never both.
# The reason names the command where one exists, so the person can run it where this script may not.
function Get-ManagerPlan {
    param([string]$Tool, [string]$Path, [string]$Manager)
    $plan = [pscustomobject]@{ Command = $null; Arguments = @(); Why = '' }
    switch ($Manager) {
        'winget' { $plan.Why = 'winget owns it and upgrade-host upgrades it' }
        { $_ -in 'npm', 'bun' } { $plan = Get-PackageStep -Step 'upgrade' -Tool $Tool -Path $Path -Manager $Manager }
        'scoop' { $plan = Get-ScoopStep -Tool $Tool -Verb 'update' }
        'choco' { $plan.Why = "choco needs an elevated shell and this script never elevates, so run in one: choco upgrade $(Get-ChocoPackage -Tool $Tool) -y" }
    }
    return $plan
}

# What owns the installed copy, read from where the command resolves to rather than from how it was meant to be installed.
function Get-ToolManager {
    param([string]$Path)
    # Separators are unified so one set of patterns reads a Windows path and the stub path of a test on another host alike.
    $p = $Path -replace '/', '\'
    $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($item) {
        $final = Resolve-FinalPath -Path $Path
        if ($final -ne $Path) { $p = "$p`n" + ($final -replace '/', '\') }
    }
    foreach ($candidate in ($p -split "`n")) {
        # The npm on PATH is asked first where it installs, since its shims sit directly in the prefix, as in Program Files\nodejs.
        # It comes before every path shape, because a Node that scoop, choco, or winget installed puts npm's prefix under that manager's tree.
        # Read by shape there, an npm global would upgrade Node itself rather than the tool.
        # The path shape stays as a fallback so a copy npm cannot confirm is still never given the tool's own updater.
        if (Test-NpmOwnership -Path $candidate) { return 'npm' }
        if ($candidate -like '*\WinGet\Packages\*' -or $candidate -like '*\Microsoft\WinGet\Links\*') { return 'winget' }
        if ((Test-UnderScoop -Path $candidate) -or $candidate -like '*\scoop\*') { return 'scoop' }
        if ($candidate -like '*\chocolatey\*') { return 'choco' }
        # On Windows bun's shim is a copy in its bin directory, so bun is recognized by that directory and its global one.
        # A relocated bun carries no .bun segment, so BUN_INSTALL and BUN_INSTALL_GLOBAL_DIR name them.
        $bunDirectory = Get-BunDirectory
        $where = $candidate -replace '/', '\'
        foreach ($bunRoot in @($bunDirectory.Bin, $bunDirectory.Global)) {
            if (Test-PathUnder -Path $where -Root $bunRoot -Below) { return 'bun' }
        }
        # A bun global package also sits under a node_modules, so bun is read first, as on Linux.
        if ($candidate -like '*\.bun\*') { return 'bun' }
        if ($candidate -like '*\node_modules\*' -or $candidate -like '*\npm\*') { return 'npm' }
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
# A tool can record a Managed copy, the one this script installs and upgrades.
# The opencode tool records one, since it ships from several sources and opencode 2 replaced opencode 1's npm package.
# Its installer script is bash only, so on Windows the managed copy is its documented npm package.
$TOOLS = @(
    @{
        Name    = 'claude'
        Source  = 'https://claude.ai/install.ps1'
        Upgrade = { Invoke-Step -Command 'claude' -Arguments @('update') }
        Install = { Invoke-Installer -Url 'https://claude.ai/install.ps1' }
    }
    @{
        Name    = 'codex'
        Source  = 'https://chatgpt.com/codex/install.ps1'
        Upgrade = { Invoke-Step -Command 'codex' -Arguments @('update') }
        Install = { Invoke-Installer -Url 'https://chatgpt.com/codex/install.ps1' }
    }
    @{
        Name    = 'opencode'
        Source  = 'npm install -g @opencode/cli'
        Managed = @{ Manager = 'npm'; Package = '@opencode/cli' }
        # Run only while another copy sits ahead of the managed package, since the managed one is otherwise upgraded through npm's plan.
        Upgrade = {
            $refusal = if ($script:DRY_RUN) { '' } else { Get-NpmInstallRefusal -Package '@opencode/cli' }
            if ($refusal) {
                warn "opencode: $refusal"
                return 1
            }
            return (Invoke-Step -Command 'npm' -Arguments @('install', '-g', '@opencode/cli@latest'))
        }
        Install = {
            $refusal = if ($script:DRY_RUN) { '' } else { Get-NpmInstallRefusal -Package '@opencode/cli' }
            if ($refusal) {
                warn "opencode: $refusal (https://opencode.ai/docs/)"
                return 1
            }
            return (Invoke-Step -Command 'npm' -Arguments @('install', '-g', '@opencode/cli'))
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

# --- Managed Copies ---

# A tool can record a Managed copy, the one this script installs and upgrades.
# Another copy PATH reaches first is reported with the commands that fix it, and never removed here, since the fix is a one-off.

# The tool's Managed record, or nothing for a tool recording none, read by key since strict mode refuses a missing one.
function Get-ManagedRecord {
    param([string]$ToolName)
    $tool = Get-Tool $ToolName
    if ($tool -and $tool.ContainsKey('Managed')) { return $tool.Managed }
    return $null
}

# How the managed copy reads in a message.
function Get-ManagedLabel {
    param([string]$ToolName)
    $managed = Get-ManagedRecord $ToolName
    return "the $($managed.Manager) package $($managed.Package)"
}

# Whether the managed copy is installed, whatever PATH resolves to first.
function Test-ManagedPresent {
    param([string]$ToolName)
    $managed = Get-ManagedRecord $ToolName
    if (-not $managed) { return $false }
    $directory = Get-NpmPackageDirectory -Package $managed.Package
    return [bool]($directory -and (Test-Path -LiteralPath (Join-Path $directory 'package.json') -PathType Leaf))
}

# Whether a copy PATH resolves to is another copy than the tool's managed one.
function Test-StrayCopy {
    param([string]$ToolName, [string]$Path)
    $managed = Get-ManagedRecord $ToolName
    if (-not $managed) { return $false }
    if ((Get-ToolManager $Path) -ne $managed.Manager) { return $true }
    return -not (Test-NpmPackageOwnership -Path $Path -Package $managed.Package)
}

# The command that removes a stray copy through whatever owns it, as Command and Arguments, or Why where no one command does.
function Get-StrayPlan {
    param([string]$ToolName, [string]$Path, [string]$Manager)
    $plan = [pscustomobject]@{ Command = $null; Arguments = @(); Why = '' }
    switch ($Manager) {
        { $_ -in 'npm', 'bun' } { $plan = Get-PackageStep -Step 'remove' -Tool $ToolName -Path $Path -Manager $Manager }
        'scoop' { $plan = Get-ScoopStep -Tool $ToolName -Verb 'uninstall' }
        'choco' { $plan.Why = "choco needs an elevated shell and this script never elevates, so run in one: choco uninstall $(Get-ChocoPackage -Tool $ToolName) -y" }
        'winget' { $plan.Why = "winget owns it, so remove it by hand: winget uninstall $ToolName" }
        default { $plan.Why = "no package manager owns it, so delete it by hand: Remove-Item $(Hide-Home -Path $Path)" }
    }
    return $plan
}

# How to fix a stray copy by hand, naming its removal and then what makes the managed copy the one PATH reaches.
function Get-StrayFixup {
    param([string]$ToolName, [string]$Path)
    $manager = Get-ToolManager $Path
    $plan = Get-StrayPlan -ToolName $ToolName -Path $Path -Manager $manager
    $removal = if ($plan.Command) { "$($plan.Command) $($plan.Arguments -join ' ')" } else { $plan.Why }
    $next = if (Test-ManagedPresent $ToolName) { 'the managed package is then what PATH reaches' } else { "then run: install-agents.ps1 -Install $ToolName" }
    return "$(Hide-Home -Path $Path) is the $manager copy rather than $(Get-ManagedLabel $ToolName), so remove it by hand: $removal, and $next"
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
            $upgrades = $true
            $stray = $false
            if (Test-StrayCopy -ToolName $tool -Path $path) {
                note $tool (Get-StrayFixup -ToolName $tool -Path $path)
                $stray = $true
                $upgrades = $false
            }
            if (-not $stray -and $manager -ne 'self') {
                $plan = Get-ManagerPlan -Tool $tool -Path $path -Manager $manager
                if ($plan.Command) {
                    note $tool "installed through $manager, -Upgrade runs: $($plan.Command) $($plan.Arguments -join ' ')"
                } else {
                    note $tool "installed through $manager, -Upgrade does not run it: $($plan.Why)"
                    $upgrades = $false
                }
            }
            # The post step runs after the self-updater and after a manager upgrade alike, so the notes about it do too.
            # A manager that does not run leaves the post step unrun, so the notes would promise what -Upgrade does not do.
            if ($tool -eq 'herdr' -and $upgrades) { Add-HerdrNote }
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

function Add-VersionChange {
    param([string]$ToolName, [string]$Installed)
    $path = Get-ToolPath $ToolName
    $now = if ($path) { Get-ToolVersion $path } else { '' }
    if ($now -eq $Installed) { return }
    $before = if ($Installed) { $Installed } else { '-' }
    $after = if ($now) { $now } else { '-' }
    $script:CHANGED.Add("$ToolName $before -> $after")
}

function Get-CswapLauncherHolder {
    param([string]$Path)
    $directory = Split-Path -Parent $Path
    $extension = [IO.Path]::GetExtension($Path)
    $launchers = @('cswap', 'claude-swap') | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $directory "$_$extension")) }
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $running = $_.Path
            $running -and ($launchers | Where-Object { [string]::Equals($_, [IO.Path]::GetFullPath($running), [StringComparison]::OrdinalIgnoreCase) })
        } | ForEach-Object { $_.Id })
}

function Invoke-ToolUpgrade {
    param([string]$ToolName)
    $tool = Get-Tool $ToolName
    $path = Get-ToolPath $ToolName
    if (-not $path) {
        log "${ToolName}: not installed, leaving it absent"
        return
    }
    if (Test-StrayCopy -ToolName $ToolName -Path $path) {
        warn "${ToolName}: $(Get-StrayFixup -ToolName $ToolName -Path $path)"
        # The managed copy is still upgraded where one is installed, since that is the copy this script keeps.
        if (-not (Test-ManagedPresent $ToolName)) { return }
        log "${ToolName}: upgrading $(Get-ManagedLabel $ToolName)"
        if ((& $tool.Upgrade) -ne 0) {
            warn "${ToolName}: its upgrade failed"
            $script:FAILED.Add($ToolName)
        }
        return
    }
    $manager = Get-ToolManager $path
    $installed = Get-ToolVersion $path
    $shownVersion = if ($installed) { $installed } else { 'an unreadable version' }
    if ($manager -ne 'self') {
        Invoke-ManagerUpgrade -ToolName $ToolName -Path $path -Manager $manager -Installed $installed
        return
    }

    # The herdr updater refuses to run from inside one of its own panes.
    # A run there therefore skips that one step rather than failing on a refusal it already knows is coming.
    if ($ToolName -eq 'herdr' -and $env:HERDR_PANE_ID) {
        log "herdr: at $shownVersion, and this run is inside a herdr pane, which herdr will not update from, run this outside herdr to update it"
        if ((Invoke-HerdrPost) -ne 0) { $script:FAILED.Add('herdr integrations') }
        return
    }

    if ($ToolName -eq 'cswap') {
        $holders = @(Get-CswapLauncherHolder $path)
        if ($holders.Count -gt 0) {
            log "cswap: at $shownVersion, and a running cswap (process $($holders -join ', ')) holds its launcher, which uv cannot replace while it runs, close it and run this again to update it"
            return
        }
    }

    log "${ToolName}: at $shownVersion, running its updater"
    if ((& $tool.Upgrade) -ne 0) {
        warn "${ToolName}: its updater failed"
        $script:FAILED.Add($ToolName)
        Add-VersionChange -ToolName $ToolName -Installed $installed
        return
    }
    Add-VersionChange -ToolName $ToolName -Installed $installed

    if ($ToolName -eq 'herdr') { Invoke-HerdrAfterUpgrade }
}

# Run whether or not the version moved, since an integration can be outdated against a herdr that was already current.
# One path for the self-updater and for a manager, so both reinstall the integrations and give the same restart guidance.
function Invoke-HerdrAfterUpgrade {
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

# Upgrades a tool through the package manager that owns it, or says why nothing runs.
# A failing manager is recorded and the other tools still run, as a failing updater is.
function Invoke-ManagerUpgrade {
    param([string]$ToolName, [string]$Path, [string]$Manager, [string]$Installed)
    $plan = Get-ManagerPlan -Tool $ToolName -Path $Path -Manager $Manager
    if (-not $plan.Command) {
        log "${ToolName}: installed through $Manager, not run here: $($plan.Why)"
        return
    }
    $shown = if ($Installed) { $Installed } else { 'an unreadable version' }
    log "${ToolName}: at $shown, installed through $Manager, running: $($plan.Command) $($plan.Arguments -join ' ')"
    if ((Invoke-Step -Command $plan.Command -Arguments $plan.Arguments) -ne 0) {
        warn "${ToolName}: $Manager failed to upgrade it"
        $script:FAILED.Add($ToolName)
        Add-VersionChange -ToolName $ToolName -Installed $Installed
        return
    }
    Add-VersionChange -ToolName $ToolName -Installed $Installed
    if ($ToolName -eq 'herdr') { Invoke-HerdrAfterUpgrade }
}

function Invoke-ToolInstall {
    param([string]$ToolName)
    $tool = Get-Tool $ToolName
    $path = Get-ToolPath $ToolName
    if ($path -and (Test-StrayCopy -ToolName $ToolName -Path $path)) {
        warn "${ToolName}: $(Get-StrayFixup -ToolName $ToolName -Path $path)"
        return
    }
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
        return 3
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
