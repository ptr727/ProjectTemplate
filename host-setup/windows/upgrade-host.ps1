# Upgrades this host, on native Windows.
# Two things are brought current:
#   - The packages winget manages, which is routine.
#   - The WSL platform itself, which is the Windows peer of a kernel and moves on its own release schedule.
# A third action removes the dead, duplicate and empty entries those installs leave in Path.
#
# A WSL platform update is refused while Docker Desktop is running.
# The reason is that Docker holds the WSL service open, so the update then fails part way rather than declining.
# Refusing is the point of running this rather than the two commands by hand: each is one line, and the guard between them is not.
#
# There is no action for a Windows feature update, where the Linux peer moves a host to the next release.
# Windows Update owns that upgrade.
# It is not driven the way an apt sources rewrite is.
# An action that pretended otherwise would be the one thing this script must not carry.

[CmdletBinding()]
param(
    [Alias('s')][switch]$Status,
    [Alias('p')][switch]$Packages,
    [Alias('w')][switch]$Wsl,
    [Alias('a')][switch]$All,
    [Alias('c')][switch]$CleanPath,
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
# A script parameter reached only from a nested scope reads as declared and never used.
# That is a finding on each one, and it hides a parameter that genuinely is unused.
$ACTIONS = [ordered]@{
    status   = [bool]$Status
    packages = [bool]$Packages
    wsl      = [bool]$Wsl
    all       = [bool]$All
    cleanpath = [bool]$CleanPath
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
Windows Update's to make and has no action here. Also removes the dead and duplicate Path entries
installs leave behind.

Actions, name one, default -Packages:
  -s, -Status       Report the host, what is upgradable, and the WSL platform
  -p, -Packages     Upgrade every winget package that has an upgrade
  -w, -Wsl          Update the WSL platform only
  -a, -All          Upgrade the packages, then update the WSL platform
  -c, -CleanPath    Remove dead, duplicate and empty Path entries
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

-CleanPath removes an entry whose directory does not exist, an entry repeating an earlier one, and
an empty entry. An entry whose drive or share does not answer is kept, since its state could not be
read. Run unelevated, it cleans the user Path and lists the machine findings, which an elevated run
such as the same command under gsudo removes. Run elevated, it cleans both. Either way it prints
each previous value before writing it, so a cleanup can be undone by hand. -Status lists what it
would remove.

Examples:
  upgrade-host.ps1                 Upgrade the winget packages
  upgrade-host.ps1 -Status         Report, change nothing
  upgrade-host.ps1 -Packages -Yes  Upgrade unattended
  upgrade-host.ps1 -All            Upgrade the packages, then the WSL platform
  upgrade-host.ps1 -Wsl -DryRun    Show what a WSL update would run
  upgrade-host.ps1 -CleanPath -DryRun
                                   Show what a Path cleanup would remove
  gsudo upgrade-host.ps1 -CleanPath
                                   Clean the machine Path too
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
    # A native command writes to this function's output stream.
    # So without this the caller receives every line the command printed with the exit code appended.
    # A check against 0 then compares against the first line of output.
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
# The two halves are counted here rather than read off winget's own closing count.
# That count sums both and so reports a number that matches neither list.
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
# WSL_UTF8 changes what wsl.exe emits.
# Setting the console encoding instead would only change how this process decodes it, and would corrupt in-distribution output that is already UTF-8.
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
# The service is not the probe.
# The com.docker.service entry reads Stopped on a host with Docker Desktop plainly running, since it is the elevated helper rather than the engine.
# Neither wslservice nor vmmemWSL is the probe either, since both run for any distribution and would report Docker on a host that has none.
function Get-DockerProcess {
    return , @(Get-Process -Name $script:DOCKER_PROCESSES -ErrorAction SilentlyContinue)
}

# A WSL platform update stops and restarts the WSL service, and Docker Desktop holds it open, so the update fails part way rather than declining.
# The guard runs under -DryRun too, because of the following.
#   - It is a read that decides what to do rather than a change to the host.
#   - A dry run that printed the command would say the update was available when it is not.
function Assert-DockerStopped {
    $running = Get-DockerProcess
    if ($running.Count -eq 0) { return }
    info "Running: $((($running | ForEach-Object { $_.Name }) | Sort-Object -Unique) -join ', ')"
    die 'Docker Desktop is running, and a WSL platform update fails part way while it holds the WSL service open. Quit Docker Desktop from its tray icon and wait for it to report that it has stopped, then run this again. Pausing it is not enough.'
}

# --- Actions ---

# The expanded length of the machine and user Path together, which is the length Explorer's rebuild of its environment measures.
# Reading each scope from the registry rather than $env:Path matters, because this process may have inherited a Path that was already rebuilt.
function Get-HostPathLength {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    return "$machine;$user".Length
}

# The warning for a Path this long, or nothing below the margin.
# Past about 4095 characters Explorer drops the user half of Path and reports no error.
# So the warning has to come from the install that grows it, because nothing else on the host says so.
function Get-PathLengthWarning {
    param([int]$Length)
    $margin = 3700
    if ($Length -le $margin) { return $null }
    return "the machine and user Path together expand to $Length characters, above the warning margin of $margin. Past about 4095, Explorer silently drops the user half of Path from everything it launches, so an editor stops finding per-user tools while a new terminal tab still finds them. Remove entries, since shortening one with a variable leaves its expanded length the same, then sign out and back in."
}

function Write-PathLengthWarning {
    $warning = Get-PathLengthWarning -Length (Get-HostPathLength)
    if ($warning) { warn $warning }
}

# --- Path cleanup ---

# Each half of Path is read and written as the registry stores it rather than through [Environment]::GetEnvironmentVariable.
# That call expands the value, and writing an expanded value back freezes %USERPROFILE% and its like into literal paths.
function Open-PathKey {
    param([Parameter(Mandatory)][ValidateSet('Machine', 'User')][string]$Scope, [bool]$Writable = $false)
    if ($Scope -eq 'Machine') {
        return [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager\Environment', $Writable)
    }
    return [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $Writable)
}

# One half of Path as stored, its variables unexpanded, with the value's registry kind so a write can keep it.
# A half with no Path value reads as empty, which a cleanup leaves alone.
function Read-PathScope {
    param([Parameter(Mandatory)][ValidateSet('Machine', 'User')][string]$Scope)
    $none = @{ Raw = ''; Kind = $null }
    $key = Open-PathKey -Scope $Scope
    if ($null -eq $key) { return $none }
    try {
        if ($key.GetValueNames() -notcontains 'Path') { return $none }
        return @{
            Raw  = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            Kind = $key.GetValueKind('Path')
        }
    } finally {
        $key.Dispose()
    }
}

function Write-PathScope {
    param(
        [Parameter(Mandatory)][ValidateSet('Machine', 'User')][string]$Scope,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Value,
        [Parameter(Mandatory)][Microsoft.Win32.RegistryValueKind]$Kind
    )
    $key = Open-PathKey -Scope $Scope -Writable $true
    if ($null -eq $key) { throw "the $($Scope.ToLower()) Path key could not be opened for writing" }
    try { $key.SetValue('Path', $Value, $Kind) } finally { $key.Dispose() }
}

# Tells running programs the environment changed, so a process started after the cleanup sees it without a sign-out.
# This is the broadcast the System Properties dialog sends after an edit.
function Send-EnvironmentChange {
    if (-not ('UpgradeHost.NativeMethods' -as [type])) {
        # The closing marker of a here-string has to sit at column 0, so this block is deliberately unindented.
        Add-Type -Namespace UpgradeHost -Name NativeMethods -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
    }
    $HWND_BROADCAST = [IntPtr]0xffff
    $WM_SETTINGCHANGE = 0x1A
    $SMTO_ABORTIFHUNG = 2
    $answer = [UIntPtr]::Zero
    $sent = [UpgradeHost.NativeMethods]::SendMessageTimeout($HWND_BROADCAST, $WM_SETTINGCHANGE, [UIntPtr]::Zero, 'Environment', $SMTO_ABORTIFHUNG, 5000, [ref]$answer)
    return ($sent -ne [IntPtr]::Zero)
}

# The directory an entry names, expanded and without the quotes cmd accepts around one holding a space.
# Both matter, because Test-Path on the quoted or unexpanded text reads a working entry as missing.
function Get-PathEntryTarget {
    param([Parameter(Mandatory)][string]$Entry)
    $text = $Entry.Trim()
    if ($text.Length -ge 2 -and $text.StartsWith('"') -and $text.EndsWith('"')) { $text = $text.Substring(1, $text.Length - 2) }
    return [Environment]::ExpandEnvironmentVariables($text)
}

# Whether a directory is there, missing, or could not be read.
# Only a not-found answer is missing.
# Anything else, such as a drive that is not ready or a share that refuses, is unreadable.
# A state that could not be read is never reported as an absence.
function Get-DirectoryState {
    param([Parameter(Mandatory)][string]$Path)
    try {
        $null = [IO.File]::GetAttributes($Path)
        return 'present'
    } catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] {
        return 'missing'
    } catch {
        return 'unreadable'
    }
}

# Whether an entry is present, dead, or one this cleanup does not judge.
# An entry is dead only when its drive or share root answers and the directory does not.
# A root that does not answer is an unmounted drive or an offline share, and the entry may well come back with it.
# The root is matched by shape rather than with [IO.Path].
# Off Windows that reads a drive letter as relative, and so would misjudge every entry under a test there.
function Get-PathEntryState {
    param([Parameter(Mandatory)][string]$Target)
    if ($Target.Contains('%')) { return 'unexpanded' }
    $root = if ($Target -match '^[A-Za-z]:\\') { $Target.Substring(0, 3) }
    elseif ($Target -match '^\\\\[^\\]+\\[^\\]+') { $Matches[0] }
    else { $null }
    if (-not $root) { return 'relative' }
    if ((Get-DirectoryState -Path $root) -ne 'present') { return 'root' }
    switch (Get-DirectoryState -Path $Target) {
        'present' { return 'present' }
        'missing' { return 'dead' }
        default { return 'unreadable' }
    }
}

# A variable's value as the registry stores it, expanded, or nothing where neither scope stores it.
# The user scope is read first, since it overrides the machine one.
function Read-StoredVariable {
    param([Parameter(Mandatory)][string]$Name)
    foreach ($scope in 'User', 'Machine') {
        $key = Open-PathKey -Scope $scope
        if ($null -eq $key) { continue }
        try {
            $value = $key.GetValue($Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        } finally {
            $key.Dispose()
        }
        if ($null -ne $value) { return [Environment]::ExpandEnvironmentVariables([string]$value) }
    }
    return $null
}

# Whether a variable an entry names now stores a value other than the one this process inherited.
# An install that repoints a variable, such as a JDK upgrade moving JAVA_HOME, leaves a running terminal on the old value.
# Judging the entry by that value would read a live entry as dead.
function Test-StaleVariable {
    param([Parameter(Mandatory)][string]$Entry)
    foreach ($match in [regex]::Matches($Entry, '%([^%;]+)%')) {
        $name = $match.Groups[1].Value
        $stored = Read-StoredVariable -Name $name
        if ($null -ne $stored -and $stored -ne [Environment]::GetEnvironmentVariable($name)) { return $true }
    }
    return $false
}

# Why an entry that was not judged is kept, by its state.
$PATH_KEPT_REASON = @{
    stale      = 'a variable in it changed after this terminal started, so it was not judged. Run this again from a new terminal'
    unexpanded = 'a variable in it does not expand here, so it was not judged'
    relative   = 'it is not a full path, so it was not judged'
    root       = 'its drive or share did not answer, so it was not judged'
    unreadable = 'its directory could not be read, so it was not judged'
}

# What a cleanup would remove from each half, and what it keeps without judging.
# Machine entries come first because that is the order of the effective Path.
# So of two equal entries the machine one is kept, and a user entry repeating a machine one is removed from the user half.
# Entries compare after expansion, case folding, and trimming a trailing \ or \., which name the same directory.
# Every kept entry keeps its stored text, unexpanded, and its place.
function Get-PathCleanup {
    param([AllowEmptyString()][string]$Machine, [AllowEmptyString()][string]$User)
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $plan = [ordered]@{}
    foreach ($scope in 'Machine', 'User') {
        $raw = if ($scope -eq 'Machine') { $Machine } else { $User }
        $kept = [Collections.Generic.List[string]]::new()
        $removed = [Collections.Generic.List[object]]::new()
        $unjudged = [Collections.Generic.List[object]]::new()
        $entries = if ($raw) { $raw -split ';' } else { @() }
        foreach ($entry in $entries) {
            if (-not $entry.Trim()) {
                $removed.Add(@{ Entry = $entry; Reason = 'from a doubled or trailing ;' })
                continue
            }
            # Kept out of the duplicate keys too, since its stale expansion could match an entry it does not name.
            if (Test-StaleVariable -Entry $entry) {
                $kept.Add($entry)
                $unjudged.Add(@{ Entry = $entry; Reason = $script:PATH_KEPT_REASON.stale })
                continue
            }
            $target = Get-PathEntryTarget -Entry $entry
            if (-not $seen.Add(($target -replace '(\\\.?)+$', ''))) {
                $removed.Add(@{ Entry = $entry; Reason = 'repeats an earlier entry' })
                continue
            }
            $state = Get-PathEntryState -Target $target
            if ($state -eq 'dead') {
                $removed.Add(@{ Entry = $entry; Reason = 'does not exist' })
                continue
            }
            $kept.Add($entry)
            if ($state -ne 'present') { $unjudged.Add(@{ Entry = $entry; Reason = $script:PATH_KEPT_REASON[$state] }) }
        }
        $plan[$scope] = @{ Value = ($kept -join ';'); Removed = $removed.ToArray(); Unjudged = $unjudged.ToArray() }
    }
    return $plan
}

# Whether this process runs as the user signed in to its session: same, other, or unknown.
# An elevation as another account opens that account's HKCU, so its user Path is not the signed-in user's.
# The owner of this session's explorer.exe is the signed-in user.
# A session with no Explorer, such as one over SSH, has no answer.
function Get-SessionUserMatch {
    $self = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $session = (Get-Process -Id $PID).SessionId
    $shells = @(Get-CimInstance Win32_Process -Filter "Name = 'explorer.exe' AND SessionId = $session" -ErrorAction SilentlyContinue)
    if ($shells.Count -eq 0) { return 'unknown' }
    foreach ($shell in $shells) {
        $owner = Invoke-CimMethod -InputObject $shell -MethodName GetOwnerSid -ErrorAction SilentlyContinue
        if ($owner -and $owner.Sid -eq $self) { return 'same' }
    }
    return 'other'
}

# Which halves this run may write, and why a half it may not is left alone.
# Elevation is detected and never required, so neither answer is a refusal.
function Get-PathWriteScope {
    if (-not $script:ELEVATED) {
        return @{
            Machine = $false
            User    = $true
            Reason  = @{ Machine = 'Removing the machine entries needs an elevated run, such as this same command under gsudo.' }
        }
    }
    $match = Get-SessionUserMatch
    if ($match -eq 'same') { return @{ Machine = $true; User = $true; Reason = @{} } }
    $reason = switch ($match) {
        'other' { 'This elevated process does not run as the user signed in to this session, so the user Path it reads is another account''s and is left alone. Run unelevated to clean the signed-in user''s.' }
        default { 'No Explorer runs in this session to name its signed-in user, so this elevated run cannot tell whose user Path it reads and leaves it alone. Remove the user entries listed above by hand, or run this from a desktop session.' }
    }
    return @{ Machine = $true; User = $false; Reason = @{ User = $reason } }
}

function Show-PathFinding {
    param([Parameter(Mandatory)]$Plan, [Parameter(Mandatory)]$Write)
    foreach ($scope in 'Machine', 'User') {
        $half = $Plan[$scope]
        $label = $scope.ToLower()
        if ($half.Removed.Count -eq 0 -and $half.Unjudged.Count -eq 0) {
            info "$label Path: nothing to remove"
            continue
        }
        info "$label Path: $($half.Removed.Count) to remove"
        foreach ($row in $half.Removed) {
            $shown = if ($row.Entry.Trim()) { $row.Entry } else { '(empty)' }
            info "  remove  $shown  ($($row.Reason))"
        }
        foreach ($row in $half.Unjudged) { info "  keep    $($row.Entry)  ($($row.Reason))" }
        if ($half.Removed.Count -gt 0 -and -not $Write[$scope]) { info "  $($Write.Reason[$scope])" }
    }
}

function Invoke-PathCleanup {
    log "Host: $(Get-HostDescription)"
    log "Mode: $($script:MODE)$(if ($script:DRY_RUN) { ' (dry run)' })"
    $stored = @{ Machine = Read-PathScope -Scope Machine; User = Read-PathScope -Scope User }
    $plan = Get-PathCleanup -Machine $stored.Machine.Raw -User $stored.User.Raw
    $write = Get-PathWriteScope

    step 'Path cleanup'
    Show-PathFinding -Plan $plan -Write $write
    $scopes = @('Machine', 'User' | Where-Object { $write[$_] -and $plan[$_].Removed.Count -gt 0 })
    if ($scopes.Count -eq 0) {
        info 'Nothing this run can remove'
        step 'Done'
        return
    }

    if (-not (confirm "Remove these entries from the $(($scopes | ForEach-Object { $_.ToLower() }) -join ' and ') Path?")) { die 'Declined' }

    # Read again after the prompt, which can wait indefinitely.
    # Writing the plan over a Path an install changed meanwhile would silently revert that change.
    foreach ($scope in $scopes) {
        if ((Read-PathScope -Scope $scope).Raw -cne $stored[$scope].Raw) {
            die "The $($scope.ToLower()) Path changed after it was read, so nothing was written. Run this again."
        }
    }

    foreach ($scope in $scopes) {
        $label = $scope.ToLower()
        # Printed before the write rather than after, so a cleanup that goes wrong can still be undone by hand.
        info "Previous $label Path, to restore by hand: $($stored[$scope].Raw)"
        if ($script:DRY_RUN) {
            info "[dry run] write the $label Path as: $($plan[$scope].Value)"
            continue
        }
        try {
            Write-PathScope -Scope $scope -Value $plan[$scope].Value -Kind $stored[$scope].Kind
        } catch {
            die "Writing the $label Path failed, and it is as it was: $($_.Exception.Message)"
        }
        info "Wrote the $label Path"
    }

    if (-not $script:DRY_RUN -and -not (Send-EnvironmentChange)) {
        warn 'The environment change was not broadcast, so a program started from Explorer sees the new Path only after a sign-out'
    }

    Write-PathLengthWarning
    step 'Done'
}

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

    Write-PathLengthWarning

    $plan = Get-PathCleanup -Machine (Read-PathScope -Scope Machine).Raw -User (Read-PathScope -Scope User).Raw
    $removable = $plan.Machine.Removed.Count + $plan.User.Removed.Count
    log "Path      : $(if ($removable -gt 0) { "$removable entr$(if ($removable -eq 1) { 'y' } else { 'ies' }) that -CleanPath removes" } else { 'nothing for -CleanPath to remove' })"
    if ($removable -gt 0 -or $plan.Machine.Unjudged.Count -gt 0 -or $plan.User.Unjudged.Count -gt 0) {
        Show-PathFinding -Plan $plan -Write (Get-PathWriteScope)
    }

    if ($upgradable.Explicit.Count -eq 0) { return }
    log ''
    log 'Self-updating, so winget reports the version it was installed at rather than the version it runs:'
    foreach ($row in $upgradable.Explicit) {
        info "$($row.Id)  installed at $($row.Version), source carries $($row.Available)"
        # No command is printed for these, deliberately, because of the following.
        #   - winget cannot move them.
        #   - Offering one invites a full reinstall over a working copy in pursuit of a number that does not change.
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

    # After the packages rather than before, because a package move is what grows Path.
    Write-PathLengthWarning

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

    switch ($script:MODE) {
        'status' { Show-Status }
        'cleanpath' { Invoke-PathCleanup }
        default { Invoke-Upgrade }
    }
}

main
