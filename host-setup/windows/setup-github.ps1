# Sets up git and GitHub on a Windows host: the SSH key, the git configuration, and commit signing.
# Every step is idempotent, so a re-run repairs a half configured host rather than duplicating what is already there.
#
# Two steps cannot be automated, because they happen in a browser: registering the public key as an authentication key, and registering the same key as a signing key.
# Both are gates rather than suggestions.
# The script stops at each, prints the key to paste and where to paste it, and checks afterwards that the registration took, by reading the keys GitHub publishes for the account.
#
# The path settings are written in the tilde form the Linux peer writes, because git expands it on Windows too, and a home shared with a WSL distribution then carries one value rather than two that disagree.

[CmdletBinding()]
param(
    [Alias('s')][switch]$Status,
    [Alias('c')][switch]$Configure,
    [Alias('n')][switch]$DryRun,
    [Alias('y')][switch]$Yes,
    [string]$Name,
    [string]$Email,
    [string]$SharedCheckout,
    [Alias('h')][switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# A non-zero exit from git or ssh is an answer here rather than a failure.
# Setting this keeps a profile that turned it on from turning every read into a terminating error.
$PSNativeCommandUseErrorActionPreference = $false

$KEY = Join-Path $HOME '.ssh\id_ed25519'
$ALLOWED_SIGNERS = Join-Path $HOME '.config\git\allowed_signers'

# Git expands a leading tilde in a path setting on Windows as well, and the hosts already configured by hand hold the tilde form, so writing that form leaves an already configured host untouched.
# File operations use the expanded paths above, since only git expands the tilde.
$KEY_SETTING = '~/.ssh/id_ed25519.pub'
$ALLOWED_SIGNERS_SETTING = '~/.config/git/allowed_signers'
$KEY_SETTINGS_URL = 'https://github.com/settings/ssh/new'

# The identity the maintainer's commits carry, used only where the host names none of its own.
$DEFAULT_NAME = 'Pieter Viljoen'
$DEFAULT_EMAIL = 'ptr727@users.noreply.github.com'

# Every parameter is read into a variable here rather than from inside a function.
# A script parameter reached only from a nested scope reads as declared and never used, which is a finding on each one and hides a parameter that genuinely is unused.
$ACTIONS = [ordered]@{
    status    = [bool]$Status
    configure = [bool]$Configure
}
$WANT_HELP = [bool]$Help

$MODE = 'status'
$DRY_RUN = [bool]$DryRun
$ASSUME_YES = [bool]$Yes
$WANT_NAME = $Name
$WANT_EMAIL = $Email
$SHARED = $SharedCheckout
$GITHUB_USER = ''
$MANAGED_KEY_AUTHENTICATES = $false

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
Usage: setup-github.ps1 [options]

Sets up git and GitHub on this host: the SSH key, the git configuration, and commit signing.

Actions, name one, default -Status:
  -s, -Status       Report what is set up and what is not, change nothing
  -c, -Configure    Create the key, apply the configuration, and check both registrations
  -h, -Help         Show this help

Options:
  -n, -DryRun       Print the commands instead of running them
  -y, -Yes          Do not prompt before changing the host
      -Name         Name for git commits
      -Email        Email for git commits
      -SharedCheckout PATH
                    Configure a checkout that several accounts share, at PATH. This turns off git's
                    ownership check for that path, so it is named rather than assumed: a host one
                    account uses needs it for nothing. "*" applies it to every path on the host,
                    which is the broadest form and is reported as such.

The identity comes from -Name and -Email, or from what this host already carries, or from the
default the maintainer commits under, in that order. A host configured for somebody else keeps its
own identity rather than being quietly rewritten.

Registering the key is done in a browser and cannot be automated, so -Configure stops at each
registration and prints what to paste. The same key is registered twice, once as an authentication
key and once as a signing key: authentication reaches private repositories, signing is what marks a
commit verified. Each is checked against the keys GitHub publishes for the account.

Two host settings are reported and never written, because both need administrator and rewriting a
working host is worse than naming what to change: the ssh-agent service, and the key file's ACL.

Examples:
  setup-github.ps1                     Report, change nothing
  setup-github.ps1 -Configure          Set the host up
  setup-github.ps1 -Configure -DryRun  Show what it would do
  setup-github.ps1 -Configure -Email you@users.noreply.github.com
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

# --- Prerequisites ---

# Both git and ssh ship with Git for Windows, and neither is installed from here.
# Installing them belongs to install-tools.ps1, so a gap is named with the command that closes it rather than closed twice in two scripts.
# Every executable this script calls is named, rather than the three it is most obviously about.
# One left out fails partway through with whatever that command says when it is missing, in place of the one message here that names the remedy.
function Test-Prerequisite {
    $absent = @()
    foreach ($tool in 'git', 'ssh', 'ssh-keygen', 'ssh-keyscan', 'ssh-add') {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { $absent += $tool }
    }
    if ($absent.Count -eq 0) { return }
    die "Not found on this host: $($absent -join ', '). All of these ship with Git for Windows, which install-tools.ps1 -Install git installs."
}

# --- Key ---

function Get-KeyBody {
    if (-not (Test-Path "$script:KEY.pub")) { return $null }
    $text = (Get-Content "$script:KEY.pub" -Raw).Trim()
    if (-not $text) { return $null }
    return $text
}

function New-KeyIfAbsent {
    step 'Checking the SSH key'
    if (Test-Path $script:KEY) {
        info "Already at $($script:KEY)"
        return
    }
    $directory = Split-Path -Parent $script:KEY
    if (-not (Test-Path $directory)) {
        if ($script:DRY_RUN) { info "[dry run] create $directory" } else { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    }
    info "Creating $($script:KEY)"
    # An empty passphrase is not chosen here, so ssh-keygen asks, which is the one question only the operator can answer.
    $code = run -Command 'ssh-keygen' -Arguments '-t', 'ed25519', '-C', $script:WANT_EMAIL, '-f', $script:KEY
    if ($code -ne 0) { die "ssh-keygen exited $code" }
}

# The ACL is reported and never rewritten.
# Creating the key with ssh-keygen already sets it correctly on Windows, and rewriting an ACL is hard to undo and hard to preview under -DryRun, so a wrong one is named with the command that repairs it.
function Test-KeyAcl {
    if (-not (Test-Path $script:KEY)) { return $null }
    $acl = Get-Acl $script:KEY
    $others = @($acl.Access | Where-Object { $_.IdentityReference.Value -ne $acl.Owner -and $_.IdentityReference.Value -notmatch 'NT AUTHORITY\\SYSTEM|BUILTIN\\Administrators' })
    return ($others.Count -eq 0)
}

function Add-KnownHost {
    step 'Checking that github.com is a known host'
    $known = Join-Path $HOME '.ssh\known_hosts'
    if ((& ssh-keygen -F github.com 2>&1 | Out-String) -match 'found') {
        info 'Already known'
        return
    }
    info 'Adding the github.com host keys'
    if ($script:DRY_RUN) {
        info "[dry run] check the github.com host key against the fingerprints GitHub publishes, then record it in $known"
        return
    }

    # Comment lines carry the banner rather than a key, and ssh-keygen refuses a file holding one.
    $scanned = @(& ssh-keyscan github.com 2> $null | Where-Object { $_ -and $_ -notmatch '^\s*#' })
    if ($scanned.Count -eq 0) {
        die 'ssh-keyscan returned no host key for github.com. The OpenSSH under %SystemRoot%\System32 is older than the key exchange github.com offers and fails with "unsupported KEX method", where the copy shipped with Git for Windows under %ProgramFiles%\Git\usr\bin succeeds. Put that one first on PATH and run this again.'
    }

    # What was offered is checked against what GitHub publishes before it is recorded, matching the Linux peer.
    # Recording whatever answered would persist a substituted key on the one run where nothing yet pins the real one, and every later connection would then verify against it.
    $temp = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString() + '.pub')
    try {
        [IO.File]::WriteAllText($temp, ($scanned -join "`n") + "`n")
        $offered = @(& ssh-keygen -lf $temp 2> $null |
                ForEach-Object { ($_ -split '\s+')[1] -replace '^SHA256:', '' } |
                Where-Object { $_ } | Sort-Object -Unique)
    } finally {
        Remove-Item $temp -Force -ErrorAction SilentlyContinue
    }
    if ($offered.Count -eq 0) { die 'The host key github.com offered could not be fingerprinted, so it is not being recorded' }

    $published = @()
    try {
        $meta = Invoke-RestMethod -Uri 'https://api.github.com/meta' -TimeoutSec 10
        $published = @($meta.ssh_key_fingerprints.PSObject.Properties |
                Where-Object { $_.Name -like 'SHA256*' } | ForEach-Object { $_.Value })
    } catch {
        $published = @()
    }
    if ($published.Count -eq 0) {
        die 'Cannot read the host key fingerprints GitHub publishes, so the key it offered cannot be checked. Compare it by hand against https://docs.github.com/authentication/keeping-your-account-secure/githubs-ssh-key-fingerprints and record it with ssh-keyscan.'
    }

    foreach ($fingerprint in $offered) {
        if ($published -notcontains $fingerprint) {
            die "github.com offered a host key GitHub does not publish (SHA256:$fingerprint), so it is not being recorded"
        }
    }
    info 'The offered host key matches what GitHub publishes'

    $directory = Split-Path -Parent $known
    if (-not (Test-Path $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    Add-Content -Path $known -Value $scanned
}

# --- GitHub, read only ---

# Every probe refuses to write a host key, so asking whether authentication works cannot enroll github.com behind the reader's back and a status run stays a read-only action.
# A status run also refuses to prompt, since it may be run unattended and a passphrase prompt would hang it.
function Get-SshGreeting {
    param([switch]$ManagedKeyOnly)
    $options = @('-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10')
    if ($script:MODE -eq 'status') { $options += @('-o', 'BatchMode=yes') }
    if ($ManagedKeyOnly) {
        if (-not (Test-Path $script:KEY)) { return '' }
        # The ssh client offers the default identity files and an agent's keys besides whatever is named, so a host carrying another account's key authenticates as that account.
        # An empty config file is what isolates the managed key, since the default identity files count as configured identities on their own.
        $options += @('-F', 'NUL', '-o', 'IdentitiesOnly=yes', '-i', $script:KEY)
    }
    return (& ssh @options -T 'git@github.com' 2>&1 | Out-String)
}

function Get-GreetingUser {
    param([string]$Greeting)
    if ($Greeting -match '(?m)^Hi ([^!]+)!') { return $Matches[1] }
    return ''
}

# Prefer the account the managed key belongs to, since that is the one the registration checks are about, and say so when the host answers as somebody else.
function Resolve-GitHubUser {
    $general = Get-GreetingUser (Get-SshGreeting)
    $managed = Get-GreetingUser (Get-SshGreeting -ManagedKeyOnly)

    $script:MANAGED_KEY_AUTHENTICATES = [bool]$managed
    if ($managed) {
        $script:GITHUB_USER = $managed
        if ($general -and $general -ne $managed) {
            warn "This host authenticates as $general with another key, while the managed key belongs to $managed"
        }
        return
    }
    $script:GITHUB_USER = $general
}

# GitHub publishes both key lists for an account, so a registration can be checked from the host without a token and without the browser that made it.
# Each returns registered, missing, or unknown, and the third is not the second: a momentary failure to reach GitHub reported as "not registered" sends the reader to register a key that is already there.
# PowerShell separates those two by itself, since an account with no signing key returns an empty list where an unreachable GitHub throws.
function Test-KeyRegistered {
    param([ValidateSet('auth', 'signing')][string]$Kind)
    $body = Get-KeyBody
    if (-not $body -or -not $script:GITHUB_USER) { return 'unknown' }
    # The comment field is not part of what GitHub publishes, so only the type and the key itself are compared.
    $wanted = (($body -split '\s+') | Select-Object -First 2) -join ' '
    try {
        if ($Kind -eq 'auth') {
            $keys = (Invoke-RestMethod -Uri "https://github.com/$($script:GITHUB_USER).keys" -TimeoutSec 10) -split "`n"
        } else {
            $keys = @(Invoke-RestMethod -Uri "https://api.github.com/users/$($script:GITHUB_USER)/ssh_signing_keys" -TimeoutSec 10 | ForEach-Object { $_.key })
        }
    } catch {
        return 'unknown'
    }
    foreach ($key in $keys) {
        if ($key.Trim() -eq $wanted) { return 'registered' }
    }
    return 'missing'
}

# A registration is a browser step, so this prints what to paste and where, then stops.
function Show-RegistrationNeeded {
    param([string]$Kind, [string]$Type)
    log ''
    log "The key is not registered for $Kind. This step happens in a browser:"
    info "1. Open $($script:KEY_SETTINGS_URL)"
    info "2. Set the key type to `"$Type key`""
    info "3. Paste the key below, and give it this host's name"
    log ''
    # A dry run reports what it would create rather than creating it, so the key this block exists to print may not be there.
    $body = Get-KeyBody
    if ($body) { log $body } else { info "No key at $($script:KEY).pub yet, so there is nothing to paste. A run that is not a dry run creates it." }
    log ''
}

# --- git configuration ---

function Get-GitConfig {
    param([string]$Key)
    $value = (& git config --global --get $Key 2>$null | Out-String).Trim()
    return $value
}

# Set a value only where it differs, so a re-run is silent rather than rewriting the same file.
function Set-GitConfig {
    param([string]$Key, [string]$Value)
    if ((Get-GitConfig $Key) -eq $Value) { return $false }
    run -Command 'git' -Arguments 'config', '--global', $Key, $Value | Out-Null
    return $true
}

# The safe.directory setting is multi valued, so setting it again appends a duplicate rather than replacing it.
function Add-GitConfigOnce {
    param([string]$Key, [string]$Value)
    $current = @(& git config --global --get-all $Key 2>$null)
    if ($current -contains $Value) { return $false }
    run -Command 'git' -Arguments 'config', '--global', '--add', $Key, $Value | Out-Null
    return $true
}

# The flag wins, then whatever the host already carries, then the default.
# Reading the host first is what keeps a machine configured for somebody else from being rewritten by a run meant to be safe to repeat.
function Resolve-Identity {
    if (-not $script:WANT_NAME) {
        $existing = Get-GitConfig 'user.name'
        $script:WANT_NAME = if ($existing) { $existing } else { $script:DEFAULT_NAME }
    }
    if (-not $script:WANT_EMAIL) {
        $existing = Get-GitConfig 'user.email'
        $script:WANT_EMAIL = if ($existing) { $existing } else { $script:DEFAULT_EMAIL }
    }
}

function Set-GitIdentity {
    step 'Configuring git'
    info "Identity: $($script:WANT_NAME) <$($script:WANT_EMAIL)>"

    $changed = 0
    if (Set-GitConfig 'user.name' $script:WANT_NAME) { $changed++ }
    if (Set-GitConfig 'user.email' $script:WANT_EMAIL) { $changed++ }
    # Git Credential Manager ships with Git for Windows and is what gh expects there, where the Linux peer writes a cache helper because no manager exists.
    # Written only where nothing is set, so a host already carrying a helper keeps it.
    if (-not (Get-GitConfig 'credential.helper')) {
        if (Set-GitConfig 'credential.helper' 'manager') { $changed++ }
    }

    if ($changed -eq 0) { info 'Already configured' } else { info "$changed setting(s) written" }
}

# A checkout several accounts share needs two settings that are relaxations rather than defaults, so they are applied only for a path the caller names.
function Set-SharedCheckout {
    if (-not $script:SHARED) { return }
    step "Configuring the shared checkout at $($script:SHARED)"

    $changed = 0
    if (Set-GitConfig 'core.sharedRepository' 'group') { $changed++ }
    if (Add-GitConfigOnce 'safe.directory' $script:SHARED) { $changed++ }

    if ($script:SHARED -eq '*') {
        warn 'safe.directory is set to every path on this host, which turns the ownership check off everywhere'
    } elseif (-not (Test-Path $script:SHARED)) {
        info "$($script:SHARED) does not exist yet, and the setting waits for it"
    }

    if ($changed -eq 0) { info 'Already configured' } else { info "$changed setting(s) written" }
}

function Set-Signing {
    step 'Configuring commit signing'

    $changed = 0
    if (Set-GitConfig 'gpg.format' 'ssh') { $changed++ }
    if (Set-GitConfig 'user.signingkey' $script:KEY_SETTING) { $changed++ }
    if (Set-GitConfig 'commit.gpgsign' 'true') { $changed++ }
    if (Set-GitConfig 'tag.gpgsign' 'true') { $changed++ }
    if (Set-GitConfig 'gpg.ssh.allowedSignersFile' $script:ALLOWED_SIGNERS_SETTING) { $changed++ }

    # The allowed signers file is what verifies a signature locally, and it is appended to rather than rewritten, since it can carry other identities.
    $body = Get-KeyBody
    if ($body) {
        $entry = "$($script:WANT_EMAIL) namespaces=`"git`" $body"
        $existing = if (Test-Path $script:ALLOWED_SIGNERS) { @(Get-Content $script:ALLOWED_SIGNERS) } else { @() }
        if ($existing -notcontains $entry) {
            if ($script:DRY_RUN) {
                info "[dry run] append this host's key to $($script:ALLOWED_SIGNERS)"
            } else {
                $directory = Split-Path -Parent $script:ALLOWED_SIGNERS
                if (-not (Test-Path $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
                Add-Content -Path $script:ALLOWED_SIGNERS -Value $entry
            }
            $changed++
        }
    }

    if ($changed -eq 0) { info 'Already configured' } else { info "$changed setting(s) written" }
}

# Sign a commit in a throwaway repository and verify it.
# This proves the configuration end to end, which reading the settings back cannot: a wrong allowed signers entry reads as correct and fails only when a signature is checked.
function Test-Signing {
    $repository = Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString())
    try {
        New-Item -ItemType Directory -Path $repository -Force | Out-Null
        & git init -q $repository 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
        & git -C $repository commit -q --allow-empty -m 'signing check' 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
        & git -C $repository verify-commit HEAD 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        Remove-Item $repository -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --- Agent ---

# On Windows the agent is a service rather than a socket, and both starting it and setting it to start automatically need administrator.
# Reported with the command that fixes it rather than run, on the same rule as the key ACL: this script does not elevate, and naming the change is better than half applying it.
function Show-AgentStatus {
    log ''
    log 'SSH agent'
    $service = Get-Service ssh-agent -ErrorAction SilentlyContinue
    if (-not $service) {
        missing 'the ssh-agent service exists, and OpenSSH is installed as a Windows optional feature'
        return
    }
    if ($service.Status -eq 'Running') {
        ok 'the ssh-agent service is running'
    } else {
        missing "the ssh-agent service is running, it is $($service.Status), start it with: Set-Service ssh-agent -StartupType Automatic; Start-Service ssh-agent"
        return
    }

    $body = Get-KeyBody
    $loaded = (& ssh-add -L 2>&1 | Out-String)
    if ($body -and $loaded -match [regex]::Escape((($body -split '\s+') | Select-Object -First 2) -join ' ')) {
        ok 'the managed key is loaded in the agent'
    } else {
        missing "the managed key is loaded in the agent, add it with: ssh-add $($script:KEY)"
    }
}

# --- Actions ---

function Show-Status {
    log 'Host identity'
    foreach ($tool in 'git', 'ssh') {
        if (Get-Command $tool -ErrorAction SilentlyContinue) { ok "$tool installed" } else { missing "$tool installed, which install-tools.ps1 -Install git provides" }
    }
    if (Test-Path $script:KEY) { ok "SSH key at $($script:KEY)" } else { missing "SSH key at $($script:KEY)" }

    $acl = Test-KeyAcl
    if ($null -eq $acl) { missing 'the key file ACL, unknown until the key exists' }
    elseif ($acl) { ok 'the key file is readable only by its owner' }
    else { missing "the key file is readable only by its owner, repair it with: icacls `"$($script:KEY)`" /inheritance:r /grant:r `"`$env:USERNAME:F`"" }

    $known = (& ssh-keygen -F github.com 2>&1 | Out-String) -match 'found'
    Resolve-GitHubUser
    if ($script:GITHUB_USER) { ok "SSH authentication to GitHub, as $($script:GITHUB_USER)" }
    elseif (-not $known) { missing 'SSH authentication to GitHub, unchecked because github.com is not in known_hosts, which -Configure adds' }
    else { missing 'SSH authentication to GitHub' }

    if ($script:MANAGED_KEY_AUTHENTICATES) { ok 'The managed key is the one that authenticates' }
    elseif ($script:GITHUB_USER) { missing 'The managed key authenticates, so the account above answered with another key or through this host ssh config' }
    else { missing 'The managed key authenticates' }

    log ''
    log 'Registration, as GitHub publishes it'
    # Which account to ask about comes from authentication, so with none there is no question to ask, and that reads differently from having asked and failed.
    if (-not $script:GITHUB_USER -or -not (Get-KeyBody)) {
        missing 'Authentication key registered, unknown until authentication works'
        missing 'Signing key registered, unknown until authentication works'
    } else {
        foreach ($pair in @(@('auth', 'Authentication'), @('signing', 'Signing'))) {
            switch (Test-KeyRegistered -Kind $pair[0]) {
                'registered' { ok "$($pair[1]) key registered" }
                'unknown' { missing "$($pair[1]) key registered, GitHub could not be reached to check" }
                default { missing "$($pair[1]) key registered" }
            }
        }
    }

    log ''
    log 'git configuration'
    foreach ($key in 'user.name', 'user.email', 'credential.helper', 'gpg.format',
        'user.signingkey', 'commit.gpgsign', 'tag.gpgsign', 'gpg.ssh.allowedSignersFile') {
        $value = Get-GitConfig $key
        if ($value) { ok "$key = $value" } else { missing $key }
    }

    # The shared checkout settings are reported apart, because absent is the right state for a host one account uses and listing them as missing would read as two gaps to close.
    $shared = @(& git config --global --get-all safe.directory 2>$null)
    if ($shared.Count -gt 0) {
        $unique = @($shared | Sort-Object -Unique)
        ok "safe.directory = $($unique -join ' ')"
        if ($shared.Count -gt $unique.Count) {
            info "$($shared.Count) entries for $($unique.Count) path(s), so this setting was added more than once"
        }
    } else {
        info '[    ] safe.directory not set, which -SharedCheckout <path> sets where a checkout is shared'
    }

    Show-AgentStatus

    log ''
    log 'GitHub CLI'
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        missing 'gh installed, which install-tools.ps1 -Install gh provides'
    } else {
        $auth = (& gh auth status 2>&1 | Out-String)
        if ($auth -match 'Logged in to \S+ account (\S+)') { ok "authenticated as $($Matches[1])" } else { missing 'authenticated, log in with: gh auth login --hostname github.com --git-protocol ssh' }
        # Reported and never set, matching the Linux peer, which touches gh nowhere.
        # An https protocol makes a checkout made through gh authenticate by token where every other checkout on the host authenticates by key.
        if ($auth -match 'Git operations protocol: (\S+)') {
            if ($Matches[1] -eq 'ssh') { ok 'git protocol is ssh' }
            else { missing "git protocol is ssh, it is $($Matches[1]), set it with: gh config set git_protocol ssh" }
        }
    }

    log ''
    log 'Signing'
    if (Test-Signing) { ok 'A commit signs and verifies on this host' } else { missing 'A commit signs and verifies on this host' }
}

function Invoke-Configure {
    Test-Prerequisite
    Resolve-Identity
    New-KeyIfAbsent
    Add-KnownHost

    # The local configuration comes first, because none of it needs GitHub.
    # A host that cannot authenticate yet, which is every host between creating its key and registering it, still ends this run with its identity, its signing configuration, and a commit that verifies locally.
    Set-GitIdentity
    Set-SharedCheckout
    Set-Signing

    step 'Signing a commit to check the configuration'
    if ($script:DRY_RUN) {
        info '[dry run] sign and verify a commit in a throwaway repository'
    } elseif (Test-Signing) {
        info 'A commit signs and verifies'
    } else {
        die "A commit did not verify. Check $($script:ALLOWED_SIGNERS) holds this host key against $($script:WANT_EMAIL)."
    }

    step 'Checking the key registrations at GitHub'
    Resolve-GitHubUser
    if (-not $script:GITHUB_USER) {
        Show-RegistrationNeeded -Kind 'authentication' -Type 'Authentication'
        warn 'Authentication does not work yet, so nothing that reaches GitHub will work until the key above is registered'
        return
    }
    info "Authenticated as $($script:GITHUB_USER)"

    if ((Test-KeyRegistered -Kind 'signing') -eq 'missing') {
        Show-RegistrationNeeded -Kind 'signing' -Type 'Signing'
        warn 'Commits sign and verify locally, and show as unverified on GitHub until the key above is registered as a signing key'
    } else {
        info 'The signing key is registered'
    }

    Show-AgentStatus
    step 'Done'
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
    Test-Prerequisite

    if ($script:MODE -eq 'status') { Show-Status } else { Invoke-Configure }
}

main
