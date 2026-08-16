# Stands a host up from nothing, by fetching this repository and handing control to the host tooling inside it.
# It is the one file fetched on its own, because a host with no git and no checkout is what it exists to fix.
# It reads no payload, no table, and no sibling module: it obtains a tree and runs one entry point inside that tree.
#
# A tarball rather than a clone, because a clone needs git on a host that may not have it, and because a tarball of a resolved commit cannot be stale.
# The commit it resolved is printed before anything runs, so a run says which revision of the fleet's tooling it used.
#
# An unverified loader is worse than none, which is why this one does more than its Linux peer before it trusts anything.
# It pins TLS 1.2 itself rather than assume a fresh host's default reaches GitHub, it checks a fetched tree for the marker it wrote before removing anything under -Dir, and it hands off to PowerShell 7 explicitly rather than assume the console it started in already carries it.
#
# Windows PowerShell 5.1 is the one shell a fresh Windows host guarantees, the way bash is guaranteed on a fresh Debian host, so everything up to and including finding or installing PowerShell 7 is written to run under it.
# Once pwsh is confirmed, this file hands the rest of the run to itself under pwsh, and every script under host-setup/windows it goes on to drive requires that version too.

# The Host action binds to StandUpHost rather than Host, because Host is PowerShell's own automatic variable for the host program ($Host.Name, $Host.UI), and a parameter named Host would shadow it and fail PSAvoidAssignmentToAutomaticVariable.
# The alias keeps -Host as the spelling a caller types, while leaving the automatic variable free for the console-detection checks below.
[CmdletBinding()]
param(
    [Alias('r')][switch]$Report,
    [Alias('Host')][switch]$StandUpHost,
    [switch]$Dev,
    [switch]$Upgrade,
    [switch]$Tools,
    [switch]$Github,
    [switch]$Skills,
    [Alias('w')][switch]$Wsl,
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
# A non-zero exit from winget, tar or a driven script is an answer here rather than a failure.
# Setting this keeps a profile that turned it on from turning every read into a terminating error.
$PSNativeCommandUseErrorActionPreference = $false

# A fresh Windows host's default TLS floor can predate 1.2, which raw.githubusercontent.com and the GitHub API both require.
# Set once, unconditionally, since it is a no-op where the runtime already defaults higher.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$REPO = 'ptr727/ProjectTemplate'
$DEFAULT_REF = 'main'

# --- Output ---

function log { param([string]$Message = '') Write-Host $Message }
function info { param([string]$Message) Write-Host "  $Message" }
function step { param([string]$Message) Write-Host "`n==> $Message" }
function warn { param([string]$Message) [Console]::Error.WriteLine("WARNING: $Message") }
function die { param([string]$Message) [Console]::Error.WriteLine("ERROR: $Message"); exit 1 }

# Every parameter is read into a variable here rather than from inside a function.
# A script parameter reached only from a nested scope reads as declared and never used, which is a finding on each one and hides a parameter that genuinely is unused.
$ACTIONS = [ordered]@{
    report  = [bool]$Report
    upgrade = [bool]$Upgrade
    tools   = [bool]$Tools
    github  = [bool]$Github
    skills  = [bool]$Skills
    wsl     = [bool]$Wsl
    host    = [bool]$StandUpHost
    dev     = [bool]$Dev
}
$WANT_HELP = [bool]$Help
$DRY_RUN = [bool]$DryRun
$ASSUME_YES = [bool]$Yes
$KEEP = [bool]$Keep
$REF = if ($Ref) { $Ref } else { $DEFAULT_REF }

$MODE = ''
# No placeholder for $DIR here, unlike the four lines around it: PowerShell variable names are case-insensitive, so $DIR and the -Dir parameter $Dir are the same variable, and resetting it here would silently discard whatever -Dir the caller passed before Resolve-Directory ever reads it.
$RESOLVED = ''
$TREE = ''
$PWSH_PATH = ''

function usage {
    # The closing marker of a here-string has to sit at column 0, so this block is deliberately unindented.
    Write-Host @'
Usage: bootstrap.ps1 [action] [options]

Stands a host up: upgrades its packages, installs the host tools, and configures git and GitHub.
Fetches this repository and runs the tooling from that tree, so the tools and the rules that
describe them come from one revision rather than from whatever a host happens to hold.

Actions, name one, default -Report:
  -r, -Report       Report what each tool would do, change nothing
      -Host         Upgrade packages, install the tools, configure git and GitHub, install the skills
      -Dev          As -Host, and add the tools a development machine needs
      -Upgrade      Upgrade the packages winget manages, only
      -Tools        Install the host tools, only
      -Github       Configure git, the SSH key, and commit signing, only
      -Skills       Install the fleet skills for the current user, only
  -w, -Wsl          Report the WSL platform and the distributions installed, only
  -h, -Help         Show this help

Options:
  -y, -Yes          Do not prompt, and pass the same to each tool
  -n, -DryRun       Print what each step would run, change nothing
      -Ref REF      Branch, tag, pull request ref, or commit to run from, default main
      -Dir PATH     Where the tree is extracted, default %LOCALAPPDATA%\host-setup
      -Keep         Leave the extracted tree in place, which is removed by default

With no action on a console, the menu asks. With no action and no console, the report runs, since a
redirected run is not a place to answer a question.

This runs under Windows PowerShell 5.1, the version every fresh Windows host guarantees, and hands
control to PowerShell 7 once it has found or installed it: every script under host-setup\windows
requires it and refuses to run without it.

Examples:
  bootstrap.ps1                        Ask what to do
  bootstrap.ps1 -Report                Report only
  bootstrap.ps1 -Host -Yes             Stand a host up unattended
  bootstrap.ps1 -Ref develop -Report   Report using the tooling on develop
  bootstrap.ps1 -Tools -DryRun         Show what installing the tools would run
'@
}

# --- pwsh handoff ---
#
# Everything above this point, and the four functions below, run under Windows PowerShell 5.1: no ternary or null-coalescing operator, no multi-argument Join-Path, nothing newer than that runtime parses.
# Everything past the handoff may use whatever pwsh 7 accepts, though it mostly does not need to.

function Resolve-Pwsh {
    $command = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    # ProgramFiles(x86) carries no value on a host with no WOW64 layer, and Join-Path on a null path is a terminating error under Set-StrictMode, not an empty match to fall through.
    $candidates = @((Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'))
    if (${env:ProgramFiles(x86)}) { $candidates += (Join-Path ${env:ProgramFiles(x86)} 'PowerShell\7\pwsh.exe') }
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

# This is the one place that install-tools.ps1's own rule does not hold: pwsh is deliberately not part of any tool registry, because a host that cannot run these scripts cannot be repaired by them.
# Standing up pwsh is the whole reason this file exists rather than starting from a checkout.
function Install-Pwsh {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        die 'pwsh (PowerShell 7) is not installed, and winget is not on this host to install it. Install "App Installer" from the Microsoft Store, or install PowerShell 7 directly from https://aka.ms/PSWindows, then run this again.'
    }
    step 'Installing PowerShell 7'
    & winget install --id Microsoft.PowerShell --exact --source winget --accept-source-agreements --accept-package-agreements --silent --disable-interactivity | Out-Host
    $wingetExit = $LASTEXITCODE
    $found = Resolve-Pwsh
    if (-not $found) {
        # This names winget's own exit code even though pwsh's absence, not the code, is what decides this die: a non-zero code explains why, where "reported installing" alone does not, and winget answering 0 while pwsh is still missing is worth saying too.
        die "winget exited $wingetExit installing PowerShell 7, and pwsh could still not be found. Close this console and paste the setup lines again, or install it from https://aka.ms/PSWindows."
    }
    return $found
}

# Rebuilds the arguments this process was bound with, since a param() bound script has no raw $args left to forward: -Report arrives as $Report = $true, not as a string in a list.
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

# --- Fetch ---

# Resolve the ref to the commit it names, so the run reports a revision rather than a moving name.
# The plain-text accept header returns the commit alone, which keeps this free of a JSON parser on a host that has none.
function Resolve-Ref {
    try {
        $sha = Invoke-RestMethod -Uri "https://api.github.com/repos/$script:REPO/commits/$script:REF" -Headers @{ Accept = 'application/vnd.github.sha' } -TimeoutSec 15
    } catch {
        $sha = $null
    }
    if ($sha) {
        $script:RESOLVED = "$sha".Trim()
        return
    }
    # An unauthenticated request is rate limited per address, so a busy network can lose the lookup while the download itself is fine.
    warn "Could not resolve $script:REF to a commit, so this run cannot be attributed to one"
    $script:RESOLVED = ''
}

# The paths this loader creates under DIR, named in one place so the cleanup and the download agree.
# DIR itself is never removed, since -Dir may name a directory the caller owns and put other things in.
function Get-TreePath { Join-Path $script:DIR 'tree' }
function Get-ArchivePath { Join-Path $script:DIR 'tree.tar.gz' }
function Get-MarkerPath { Join-Path (Get-TreePath) '.bootstrap-owned' }

# A tree carries a marker this loader wrote, and a tree without one is somebody else's.
# DIR is a caller-supplied path, so 'tree' under it is not necessarily ours: pointing -Dir at a directory that already holds one would otherwise have this remove it, both before extracting and again on exit.
function Test-TreeOwnership { Test-Path (Get-MarkerPath) }

# Refuses to remove a tree this run did not create, rather than trusting the name.
function Remove-Tree {
    $tree = Get-TreePath
    if (-not (Test-Path $tree)) { return }
    if (-not (Test-TreeOwnership)) { die "$tree exists and this loader did not create it, so it will not be removed. Choose another -Dir." }
    Remove-Item -Recurse -Force $tree
}

function Get-Tree {
    $archive = Get-ArchivePath
    $want = if ($script:RESOLVED) { $script:RESOLVED } else { $script:REF }

    step "Fetching $script:REPO at $script:REF"
    if ($script:RESOLVED) { info "Commit: $script:RESOLVED" }

    New-Item -ItemType Directory -Path $script:DIR -Force | Out-Null
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "https://codeload.github.com/$script:REPO/tar.gz/$want" -OutFile $archive -TimeoutSec 120
    } catch {
        die "Could not download $script:REPO at $script:REF. Check the ref exists and that this host reaches codeload.github.com."
    }

    # The archive holds one top-level directory named for the repository and the revision.
    # Extracting into a directory of our own keeps a second run from reading the first one's tree.
    $tree = Get-TreePath
    Remove-Tree
    New-Item -ItemType Directory -Path $tree -Force | Out-Null
    New-Item -ItemType File -Path (Get-MarkerPath) -Force | Out-Null
    & tar -xzf $archive -C $tree --strip-components=1
    if ($LASTEXITCODE -ne 0) { die 'Could not extract the downloaded archive' }
    Remove-Item -Force $archive -ErrorAction SilentlyContinue

    $script:TREE = $tree
    info "Extracted to $script:TREE"
}

# A tree that is not ours was already refused where it mattered, at the download.
# Refusing again from here would print the same error a second time, after the one that actually stopped the run.
# Removes what this run created rather than what it finished, because TREE is set only once extraction has succeeded.
# A failed extract leaves both the archive and a part-written tree, so keying this on TREE left a tarball in the cache on every failed attempt.
function Invoke-Cleanup {
    if ($script:KEEP) { return }
    $tree = Get-TreePath
    if ((Test-Path $tree) -and -not (Test-TreeOwnership)) {
        Remove-Item -Force (Get-ArchivePath) -ErrorAction SilentlyContinue
        return
    }
    Remove-Tree
    Remove-Item -Force (Get-ArchivePath) -ErrorAction SilentlyContinue
}

# --- Handoff ---

# Every tool runs from inside the fetched tree, and this is the only place a path inside it is named.
# Written as one interpolated, forward-slashed string against the bare $TREE rather than $script:TREE or Join-Path, so this loader's one entry point into the tree reads as the literal pattern the Linux loader is checked by, and stays checkable by that same pattern.
# A read resolves $TREE up to script scope on its own, and only a write needs the script: prefix, which is why the assignment in Get-Tree still carries it.
function Invoke-Tool {
    param([Parameter(Mandatory)][string]$Tool, [switch]$ToleratesFailure, [Parameter(ValueFromRemainingArguments)][string[]]$Arguments)

    $path = "$TREE/host-setup/windows/$Tool"
    if (-not (Test-Path $path)) {
        die "The fetched tree carries no $Tool at host-setup/windows, so this ref is not one to bootstrap from"
    }

    $flags = @()
    if ($script:ASSUME_YES) { $flags += '-Yes' }
    if ($script:DRY_RUN) { $flags += '-DryRun' }

    & $script:PWSH_PATH -NoProfile -ExecutionPolicy Bypass -File $path @Arguments @flags
    if ($LASTEXITCODE -ne 0 -and -not $ToleratesFailure) { die "$Tool exited $LASTEXITCODE" }
}

function Show-Report {
    Invoke-Tool -Tool 'upgrade-host.ps1' -Arguments '-Status'
    Invoke-Tool -Tool 'install-tools.ps1' -Arguments '-Report'
    Invoke-Tool -Tool 'setup-github.ps1' -Arguments '-Status'
    # Tolerated rather than fatal, since a missing install is a finding for a report to name and not a reason to stop naming the rest.
    Invoke-Tool -Tool 'install-skills.ps1' -ToleratesFailure -Arguments '-Report'
    if ($LASTEXITCODE -ne 0) { info 'The fleet skills install is missing or stale, and -Host or -Skills lands it' }
}

# The order is fixed rather than chosen.
# Packages come first so install-tools.ps1 and setup-github.ps1 act on a host winget has just brought current, and GitHub comes last because it is the only step that waits on a person in a browser.
# The skills step runs after the tools, because install-tools.ps1 provides the interpreter it needs.
function Invoke-StandUp {
    # Named Kind rather than Profile, which is PowerShell's own automatic variable for the current user's profile script.
    param([string]$Kind)

    Invoke-Tool -Tool 'upgrade-host.ps1' -Arguments '-Packages'
    if ($Kind -eq 'dev') {
        Invoke-Tool -Tool 'install-tools.ps1' -Arguments '-Install', '-Optional'
    } else {
        Invoke-Tool -Tool 'install-tools.ps1' -Arguments '-Install'
    }
    Invoke-Tool -Tool 'setup-github.ps1' -Arguments '-Configure'
    Invoke-Tool -Tool 'install-skills.ps1'
}

# Names the host in the menu heading.
function Get-HostDescription {
    try {
        return (Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).Caption
    } catch {
        return 'this host'
    }
}

# Both are checked because a scheduled task reports one and not the other, and either alone misses a case.
# The ISE is checked apart, because it answers both of those as interactive and then blocks on a Read-Host it does not render usably.
function Test-Interactive {
    if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) { return $false }
    if ($Host.Name -eq 'Windows PowerShell ISE Host') { return $false }
    return $true
}

function Show-Menu {
    log "Standing up $(Get-HostDescription)"
    log ''
    log '  1  Report only, change nothing'
    log '  2  Upgrade the packages winget manages'
    log '  3  Install the host tools'
    log '  4  Configure git and GitHub'
    log '  5  Install the fleet skills'
    log '  6  Report the WSL platform and the distributions installed'
    log '  7  All of the above but WSL, which is a host stood up'
    log '  8  All of the above but WSL, plus the development tools'
    log '  q  Quit'
    log ''

    $choice = Read-Host 'Choose'
    switch ($choice) {
        '1' { $script:MODE = 'report' }
        '2' { $script:MODE = 'upgrade' }
        '3' { $script:MODE = 'tools' }
        '4' { $script:MODE = 'github' }
        '5' { $script:MODE = 'skills' }
        '6' { $script:MODE = 'wsl' }
        '7' { $script:MODE = 'host' }
        '8' { $script:MODE = 'dev' }
        'q' { exit 0 }
        'Q' { exit 0 }
        default { die 'Not one of the choices' }
    }
}

# --- Entry ---

# Windows has shipped tar.exe under %SystemRoot%\System32 since Windows 10 1803 and Windows Server 2019, and it reads a .tar.gz archive directly.
# That is why this loader does not reach for Expand-Archive, which cannot.
function Test-Prerequisite {
    if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
        die 'tar.exe not found under %SystemRoot%\System32. This assumes Windows 10 1803, Windows Server 2019, or later, all of which ship it.'
    }
}

# An absolute path, and never a drive root, since everything below it is created and removed under it.
function Resolve-Directory {
    if (-not $script:Dir) { return (Join-Path $env:LOCALAPPDATA 'host-setup') }
    if (-not [IO.Path]::IsPathRooted($script:Dir)) { die "-Dir takes an absolute path, and `"$($script:Dir)`" is relative" }
    $trimmed = $script:Dir.TrimEnd('\', '/')
    if ((-not $trimmed) -or ($trimmed -match '^[A-Za-z]:$')) { die '-Dir may not be a drive root' }
    return $trimmed
}

# PowerShell records which switches were given and not the order they came in, so two actions is a refusal rather than the last one winning.
# Unlike the four scripts this loader drives, zero given is not this loader's own default: main tells the menu and the piped-in report apart, the way bootstrap.sh's own entry point does.
function Resolve-Mode {
    $given = @($script:ACTIONS.Keys | Where-Object { $script:ACTIONS[$_] })
    if ($given.Count -gt 1) { die "More than one action given ($($given -join ', ')), name one" }
    if ($given.Count -eq 0) { return '' }
    return $given[0]
}

function main {
    if ($script:WANT_HELP) { usage; exit 0 }

    if ($PSVersionTable.PSVersion.Major -lt 7) {
        Invoke-PwshHandoff
    }

    # Reached only under a confirmed pwsh 7, either started that way or handed off to above.
    $script:PWSH_PATH = (Get-Process -Id $PID).Path
    Test-Prerequisite
    $script:DIR = Resolve-Directory
    $script:MODE = Resolve-Mode

    # A run with no action and no console reports rather than guessing, which is what a redirected run is.
    # The remedy is printed rather than assumed, since somebody reaching this has just pasted a one-line install.
    if (-not $script:MODE) {
        if (Test-Interactive) {
            Show-Menu
        } else {
            $script:MODE = 'report'
            warn 'No action given and no console to ask on, so this is a report'
            info 'Download the file and run it, rather than piping it, to reach the menu:'
            info '  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12'
            info "  Invoke-WebRequest -UseBasicParsing -Uri https://raw.githubusercontent.com/$script:REPO/$script:DEFAULT_REF/host-setup/bootstrap.ps1 -OutFile bootstrap.ps1"
            info '  powershell -ExecutionPolicy Bypass -File bootstrap.ps1'
        }
    }

    try {
        Resolve-Ref
        # The commit the resolve produced is handed to the skills installer, since the tarball tree it runs from has no .git to answer for it.
        $env:SKILLS_SOURCE_COMMIT = $script:RESOLVED
        Get-Tree
        switch ($script:MODE) {
            'report' { Show-Report }
            'upgrade' { Invoke-Tool -Tool 'upgrade-host.ps1' -Arguments '-Packages' }
            'tools' { Invoke-Tool -Tool 'install-tools.ps1' -Arguments '-Install' }
            'github' { Invoke-Tool -Tool 'setup-github.ps1' -Arguments '-Configure' }
            'skills' { Invoke-Tool -Tool 'install-skills.ps1' }
            # Only setup-wsl.ps1's -Status runs here: its -Install needs a distribution name, which no flag here collects, so choosing a default distro nobody asked for is exactly what -Wsl staying out of -Host and -Dev already exists to avoid.
            # Installing one by name is a checkout away, once this run has fetched it.
            'wsl' { Invoke-Tool -Tool 'setup-wsl.ps1' -Arguments '-Status' }
            'host' { Invoke-StandUp -Kind 'host' }
            'dev' { Invoke-StandUp -Kind 'dev' }
        }
    } finally {
        Invoke-Cleanup
    }

    step 'Done'
    if ($script:KEEP) { info "The fetched tree is at $script:TREE" }
}

main
