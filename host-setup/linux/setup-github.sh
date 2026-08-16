#!/bin/bash

# Sets up git and GitHub on a host: the SSH key, the git configuration, and commit signing.
# Every step is idempotent, so a re-run repairs a half configured host rather than duplicating what is already there.
#
# Two steps cannot be automated, because they happen in a browser: registering the public key as an authentication key, and registering the same key as a signing key.
# Both are gates rather than suggestions.
# The script stops at each, prints the key to paste and where to paste it, and checks afterwards that the registration took, by reading the keys GitHub publishes for the account.

set -Eeuo pipefail

readonly KEY="$HOME/.ssh/id_ed25519"
readonly ALLOWED_SIGNERS="$HOME/.config/git/allowed_signers"

# Git expands a leading tilde in a path setting, and the hosts already configured by hand hold the tilde form, so writing that form leaves an already configured host untouched.
# File operations use the expanded paths above, since the shell does not expand a tilde inside a variable.
# shellcheck disable=SC2088  # The tilde is meant to stay literal here, git is what expands it.
readonly KEY_SETTING="~/.ssh/id_ed25519.pub"
# shellcheck disable=SC2088  # As above.
readonly ALLOWED_SIGNERS_SETTING="~/.config/git/allowed_signers"
readonly KEY_SETTINGS_URL="https://github.com/settings/ssh/new"

# The identity the maintainer's commits carry, used only where the host names none of its own.
readonly DEFAULT_NAME="Pieter Viljoen"
readonly DEFAULT_EMAIL="ptr727@users.noreply.github.com"

NAME=""
EMAIL=""

MODE="status"
DRY_RUN=false
ASSUME_YES=false
GITHUB_USER=""
MANAGED_KEY_AUTHENTICATES=false
SHARED_CHECKOUT=""
SUDO=()

TMP_DIR=$(mktemp -d)
readonly TMP_DIR
trap 'rm -rf "$TMP_DIR"' EXIT

# --- Output ---

log() { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

ok() { printf '  [ ok ] %s\n' "$*"; }
missing() { printf '  [    ] %s\n' "$*"; }

usage() {
    cat <<'EOF'
Usage: setup-github.sh [options]

Sets up git and GitHub on this host: the SSH key, the git configuration, and commit signing.

Actions, name one, default --status:
  -s, --status      Report what is set up and what is not, change nothing
  -c, --configure   Create the key, apply the configuration, and check both registrations
  -h, --help        Show this help

Options:
  -n, --dry-run     Print the commands instead of running them
  -y, --yes         Do not prompt before changing the host
      --name NAME   Name for git commits
      --email MAIL  Email for git commits
      --shared-checkout PATH
                    Configure a checkout that several accounts share, at PATH. This turns off git's
                    ownership check for that path, so it is named rather than assumed: a host one
                    account uses needs it for nothing. "*" applies it to every path on the host,
                    which is the broadest form and is reported as such.

The identity comes from --name and --email, or from what this host already carries, or from the
default the maintainer commits under, in that order. A host configured for somebody else keeps its
own identity rather than being quietly rewritten.

Registering the key is done in a browser and cannot be automated, so --configure stops at each
registration and prints what to paste. The same key is registered twice, once as an authentication
key and once as a signing key: authentication reaches private repositories, signing is what marks a
commit verified. Each is checked against the keys GitHub publishes for the account, so a run says
whether the registration took rather than assuming it.
EOF
}

# --- Execution ---

run() {
    if [[ $DRY_RUN == true ]]; then
        printf '  [dry run] %s\n' "$*"
        return 0
    fi
    "$@"
}

run_root() { run "${SUDO[@]}" "$@"; }

confirm() {
    [[ $ASSUME_YES == true || $DRY_RUN == true ]] && return 0
    [[ -t 0 ]] || die "Not a terminal and --yes was not given, refusing to change the host unattended"
    local reply
    read -r -p "$1 [y/N] " reply
    [[ $reply == [yY] || $reply == [yY][eE][sS] ]]
}

fetch() {
    command -v curl > /dev/null || return 1
    curl -fsSL --retry 2 --connect-timeout 10 "$@"
}

# --- Prerequisites ---

package_installed() {
    dpkg-query -W -f='${Status}' "$1" 2> /dev/null | grep -q "^install ok installed"
}

prerequisites_missing() {
    local package
    for package in git openssh-client ca-certificates curl; do
        package_installed "$package" || printf '%s\n' "$package"
    done
    return 0
}

ensure_prerequisites() {
    local -a packages=()
    readarray -t packages < <(prerequisites_missing)
    [[ ${#packages[@]} -eq 0 ]] && return 0

    # Everything above this point works anywhere, and installing is the first step that does not, so a host without apt is told that rather than meeting a missing command part way through.
    command -v apt-get > /dev/null ||
        die "apt-get not found, and installing ${packages[*]} needs it. Install them with this host's package manager, then run this again."

    step "Installing ${packages[*]}"
    run_root apt-get update
    run_root apt-get install -y "${packages[@]}"
}

# --- Key ---

# The key as its type and body, without the trailing comment.
# That is the form both of GitHub's published lists carry: the .keys endpoint publishes two fields per line, and the signing keys API keeps the comment in a separate title field rather than in the key.
# A local public key carries three fields, so a comparison that skipped this would never match.
key_body() {
    local file="${1:-$KEY.pub}"
    [[ -f $file ]] || return 1
    awk '{ print $1" "$2 }' "$file"
}

ensure_key() {
    if [[ -f $KEY && -f $KEY.pub ]]; then
        return 0
    fi

    # A private key with no public half is the harder case, because everything downstream reads the public one: the registration paste, the allowed signers entry, and both registration checks.
    # The public key is derived from the private key rather than the pair being regenerated, since regenerating would invalidate a key that is already registered.
    if [[ -f $KEY ]]; then
        step "Deriving the missing public key from the private key"
        if [[ $DRY_RUN == true ]]; then
            info "[dry run] ssh-keygen -y -f $KEY > $KEY.pub"
            return 0
        fi
        ssh-keygen -y -f "$KEY" > "$KEY.pub"
        chmod 644 "$KEY.pub"
        return 0
    fi

    step "Creating an SSH key"
    info "Generating one asks for a passphrase"
    run mkdir -p "$HOME/.ssh"
    run chmod 700 "$HOME/.ssh"
    run ssh-keygen -t ed25519 -f "$KEY"
}

# The first enrollment is the one moment a substituted host key would be accepted for good, and every later connection trusts whatever was recorded here.
# So what github.com offers is checked against the fingerprints GitHub publishes before it is written down, and a check that cannot run is a refusal rather than a warning, since recording an unchecked key is what this prevents.
ensure_known_host() {
    ssh-keygen -F github.com > /dev/null 2>&1 && return 0

    step "Trusting the github.com host key"

    local scanned offered published fingerprint
    scanned=$(ssh-keyscan github.com 2> /dev/null) || die "github.com did not answer a host key scan"
    [[ -n $scanned ]] || die "github.com offered no host key"

    offered=$(ssh-keygen -lf - <<< "$scanned" | awk '{ print $2 }' | sed 's/^SHA256://' | sort -u)
    published=$(fetch "https://api.github.com/meta" |
        tr ',' '\n' | sed -n 's/.*"SHA256_[A-Z0-9]*": *"\([^"]*\)".*/\1/p' | sort -u) || published=""
    [[ -n $published ]] ||
        die "Cannot read the host key fingerprints GitHub publishes, so the key it offered cannot be checked. Compare it by hand against https://docs.github.com/authentication/keeping-your-account-secure/githubs-ssh-key-fingerprints and record it with ssh-keyscan."

    while read -r fingerprint; do
        grep -qxF "$fingerprint" <<< "$published" ||
            die "github.com offered a host key GitHub does not publish (SHA256:$fingerprint), so it is not being recorded"
    done <<< "$offered"
    info "The offered host key matches what GitHub publishes"

    run mkdir -p "$HOME/.ssh"
    # The shell owns the redirect, and this file belongs to the user, so no elevation is involved.
    if [[ $DRY_RUN == true ]]; then
        info "[dry run] record the checked host key in $HOME/.ssh/known_hosts"
        return 0
    fi
    printf '%s\n' "$scanned" >> "$HOME/.ssh/known_hosts"
}

# --- GitHub, read only ---

# Every probe refuses to write a host key, so asking whether authentication works cannot enroll github.com behind the reader's back and a status run stays a read-only action.
# Enrolling the host key is ensure_known_host's job, in configure mode, where changing the host is the point.
#
# A status run also refuses to prompt, since it may be run unattended and a passphrase prompt would hang it.
# Configure may prompt, because a passphrase for a key with no agent is a question only the operator can answer, and it is already an interactive action.
ssh_probe() {
    local -a opts=(-o StrictHostKeyChecking=yes -o ConnectTimeout=10)
    if [[ $MODE == "status" ]]; then
        opts+=(-o BatchMode=yes)
    fi
    # shellcheck disable=SC2029  # These are ssh's own options and its target, not a remote command.
    ssh "${opts[@]}" "$@"
}

# Authentication over SSH, which also names the account answering.
# GitHub always closes the session, so the greeting is the result rather than the exit code.
github_auth_user() {
    local greeting
    greeting=$(ssh_probe -T git@github.com 2>&1 || true)
    printf '%s' "$greeting" | sed -n 's/^Hi \([^!]*\)!.*/\1/p'
}

# The same question asked of the managed key alone.
# The ssh client offers the default identity files and an agent's keys besides whatever is named, so a host carrying another account's key authenticates as that account, and every check below would then be asking about the wrong one.
# IdentitiesOnly is not enough by itself, since the default files count as configured identities.
#
# That isolation is also why this is a second probe rather than a replacement: a host reaching GitHub through a proxy or over port 443 needs the config this probe discards, so a failure here means the managed key did not answer rather than that the host cannot reach GitHub.
github_auth_user_managed() {
    [[ -f $KEY ]] || return 1

    local greeting
    greeting=$(ssh_probe -F /dev/null -o IdentitiesOnly=yes -i "$KEY" -T git@github.com 2>&1 || true)
    printf '%s' "$greeting" | sed -n 's/^Hi \([^!]*\)!.*/\1/p'
}

# Prefer the account the managed key belongs to, since that is the one the registration checks are about, and say so when the host answers as somebody else.
resolve_github_user() {
    local general managed
    general=$(github_auth_user)
    managed=$(github_auth_user_managed || true)

    MANAGED_KEY_AUTHENTICATES=false
    if [[ -n $managed ]]; then
        MANAGED_KEY_AUTHENTICATES=true
        GITHUB_USER="$managed"
        if [[ -n $general && $general != "$managed" ]]; then
            warn "This host authenticates as $general with another key, while the managed key belongs to $managed"
        fi
        return 0
    fi

    GITHUB_USER="$general"
    return 0
}

# GitHub publishes both key lists for an account, so a registration can be checked from the host without a token and without the browser that made it.
# Each fetch is captured before it is searched rather than piped into the search.
# Under pipefail a search that stops at its first match leaves the fetch writing to a closed pipe, so a key that is registered reports itself as missing on any list long enough for the timing to matter.
# Each returns 0 registered, 1 not registered, and 2 when the answer could not be read at all.
# The third is not the second: a momentary failure to reach GitHub reported as "not registered" sends the reader to register a key that is already there.
github_auth_key_registered() {
    local body keys
    body=$(key_body) || return 2
    keys=$(fetch "https://github.com/$GITHUB_USER.keys") || return 2
    grep -qxF "$body" <<< "$keys"
}

github_signing_key_registered() {
    local body payload matches keys rc=0
    body=$(key_body) || return 2
    payload=$(fetch "https://api.github.com/users/$GITHUB_USER/ssh_signing_keys") || return 2

    # An account with no signing key returns an empty list, so grep matches nothing and exits 1.
    # Under pipefail that became the pipeline's status and mapped to "could not be read", which reported a definite no as a network problem and skipped the registration prompt.
    # An account with no signing key is exactly the account this function exists to prompt.
    # Grep's 1 is therefore an empty list and only a higher status is a failure to read.
    matches=$(grep -oE '"key": *"[^"]*"' <<< "$payload") || rc=$?
    ((rc <= 1)) || return 2
    keys=$(sed 's/"key": *"//; s/"$//' <<< "$matches")

    grep -qxF "$body" <<< "$keys"
}

# A registration is a browser step, so this prints what to paste and where, then stops.
# The consequence of not registering differs per caller, so each says its own rather than this saying one for all.
# Authentication failing stops everything that reaches GitHub, signing not being registered stops nothing locally, and a host authenticating with another key is working and not managed.
registration_needed() {
    local kind="$1" type="$2"
    log ""
    log "The key is not registered for $kind. This step happens in a browser:"
    info "1. Open $KEY_SETTINGS_URL"
    info "2. Set the key type to \"$type key\""
    info "3. Paste the key below, and give it this host's name"
    log ""
    # A dry run reports what it would create rather than creating it, so the key this block exists to print may not be there.
    # Reaching for it anyway ends the run on a cat error, at the one line the reader came for.
    if [[ -f $KEY.pub ]]; then
        cat "$KEY.pub"
    else
        info "No key at $KEY.pub yet, so there is nothing to paste. A run that is not a dry run creates it."
    fi
    log ""
}

# --- git configuration ---

git_config_get() { git config --global --get "$1" 2> /dev/null || true; }

# Set a value only where it differs, so a re-run is silent rather than rewriting the same file.
git_config_set() {
    local key="$1" value="$2" current
    current=$(git_config_get "$key")
    [[ $current == "$value" ]] && return 1

    run git config --global "$key" "$value"
    return 0
}

# The safe.directory setting is multi valued, so setting it again appends a duplicate rather than replacing it.
git_config_add_once() {
    local key="$1" value="$2"
    git config --global --get-all "$key" 2> /dev/null | grep -qxF "$value" && return 1

    run git config --global --add "$key" "$value"
    return 0
}

# The flag wins, then whatever the host already carries, then the default.
# Reading the host first is what keeps a machine configured for somebody else from being rewritten by a run meant to be safe to repeat, and it is why the defaults are not written into the variables at the top.
resolve_identity() {
    local existing

    if [[ -z $NAME ]]; then
        existing=$(git_config_get "user.name")
        NAME="${existing:-$DEFAULT_NAME}"
    fi
    if [[ -z $EMAIL ]]; then
        existing=$(git_config_get "user.email")
        EMAIL="${existing:-$DEFAULT_EMAIL}"
    fi
}

configure_git() {
    step "Configuring git"

    resolve_identity
    info "Identity: $NAME <$EMAIL>"

    local changed=0
    git_config_set "user.name" "$NAME" && changed=$((changed + 1))
    git_config_set "user.email" "$EMAIL" && changed=$((changed + 1))
    git_config_set "credential.helper" "cache --timeout=3600" && changed=$((changed + 1))

    if [[ $changed -eq 0 ]]; then
        info "Already configured"
    else
        info "$changed setting(s) written"
    fi
    return 0
}

# A checkout several accounts share needs two settings that are relaxations rather than defaults, so they are applied only for a path the caller names.
# The safe.directory setting turns off the ownership check git added to stop a repository owned by someone else running its hooks as you, and widening it to every path on a host is a different decision from widening it to one directory that is known to be shared.
# A host with one account needs neither, which is why nothing here runs without the flag.
configure_shared_checkout() {
    [[ -n $SHARED_CHECKOUT ]] || return 0

    step "Configuring the shared checkout at $SHARED_CHECKOUT"

    local changed=0
    git_config_set "core.sharedRepository" "group" && changed=$((changed + 1))
    git_config_add_once "safe.directory" "$SHARED_CHECKOUT" && changed=$((changed + 1))

    if [[ $SHARED_CHECKOUT == "*" ]]; then
        warn "safe.directory is set to every path on this host, which turns the ownership check off everywhere"
    elif [[ ! -d $SHARED_CHECKOUT ]]; then
        info "$SHARED_CHECKOUT does not exist yet, and the setting waits for it"
    fi

    if [[ $changed -eq 0 ]]; then
        info "Already configured"
    else
        info "$changed setting(s) written"
    fi
    return 0
}

configure_signing() {
    step "Configuring commit signing"

    local changed=0
    git_config_set "gpg.format" "ssh" && changed=$((changed + 1))
    git_config_set "user.signingkey" "$KEY_SETTING" && changed=$((changed + 1))
    git_config_set "commit.gpgsign" "true" && changed=$((changed + 1))
    git_config_set "tag.gpgsign" "true" && changed=$((changed + 1))
    git_config_set "gpg.ssh.allowedSignersFile" "$ALLOWED_SIGNERS_SETTING" && changed=$((changed + 1))

    # The allowed signers file is what verifies a signature locally, and it is appended to rather than rewritten, since it can carry other identities.
    local entry
    entry="$EMAIL namespaces=\"git\" $(cat "$KEY.pub" 2> /dev/null || true)"
    if ! grep -qxF "$entry" "$ALLOWED_SIGNERS" 2> /dev/null; then
        run mkdir -p "$(dirname "$ALLOWED_SIGNERS")"
        if [[ $DRY_RUN == true ]]; then
            info "[dry run] append this host's key to $ALLOWED_SIGNERS"
        else
            printf '%s\n' "$entry" >> "$ALLOWED_SIGNERS"
        fi
        changed=$((changed + 1))
    fi

    if [[ $changed -eq 0 ]]; then
        info "Already configured"
    else
        info "$changed setting(s) written"
    fi
    return 0
}

# Sign a commit in a throwaway repository and verify it.
# This proves the configuration end to end, which reading the settings back cannot: a wrong allowed signers entry reads as correct and fails only when a signature is checked.
signing_works() {
    local repo="$TMP_DIR/signing-check"
    rm -rf "$repo"
    mkdir -p "$repo"
    git init -q "$repo" 2> /dev/null || return 1
    git -C "$repo" commit -q --allow-empty -m "signing check" > /dev/null 2>&1 || return 1
    git -C "$repo" verify-commit HEAD > /dev/null 2>&1
}

# --- Actions ---

status() {
    log "Host identity"
    if command -v git > /dev/null; then
        ok "git installed"
    else
        missing "git installed, --configure installs it, so the settings below read as unset"
    fi
    if [[ -f $KEY ]]; then
        ok "SSH key at $KEY"
    else
        missing "SSH key at $KEY"
    fi

    local known=true
    ssh-keygen -F github.com > /dev/null 2>&1 || known=false

    resolve_github_user
    if [[ -n $GITHUB_USER ]]; then
        ok "SSH authentication to GitHub, as $GITHUB_USER"
    elif [[ $known == false ]]; then
        missing "SSH authentication to GitHub, unchecked because github.com is not in known_hosts, which --configure adds"
    else
        missing "SSH authentication to GitHub"
    fi
    if [[ $MANAGED_KEY_AUTHENTICATES == true ]]; then
        ok "The managed key is the one that authenticates"
    elif [[ -n $GITHUB_USER ]]; then
        missing "The managed key authenticates, so the account above answered with another key or through this host's ssh config"
    else
        missing "The managed key authenticates"
    fi

    log ""
    log "Registration, as GitHub publishes it"
    if [[ -z $GITHUB_USER || ! -f $KEY.pub ]]; then
        missing "Authentication key registered, unknown until authentication works"
        missing "Signing key registered, unknown until authentication works"
    else
        local rc=0
        github_auth_key_registered || rc=$?
        case "$rc" in
            0) ok "Authentication key registered" ;;
            2) missing "Authentication key registered, GitHub could not be reached to check" ;;
            *) missing "Authentication key registered" ;;
        esac

        rc=0
        github_signing_key_registered || rc=$?
        case "$rc" in
            0) ok "Signing key registered" ;;
            2) missing "Signing key registered, GitHub could not be reached to check" ;;
            *) missing "Signing key registered, commits show as unverified on GitHub" ;;
        esac
    fi

    log ""
    log "git configuration"
    local key value
    for key in user.name user.email credential.helper gpg.format \
        user.signingkey commit.gpgsign tag.gpgsign gpg.ssh.allowedSignersFile; do
        value=$(git_config_get "$key")
        if [[ -n $value ]]; then
            ok "$key = $value"
        else
            missing "$key"
        fi
    done

    # The shared checkout settings are reported apart, because absent is the right state for a host one account uses and listing them as missing would read as two gaps to close.
    local -a shared=()
    readarray -t shared < <(git config --global --get-all safe.directory 2> /dev/null || true)
    if [[ ${#shared[@]} -gt 0 ]]; then
        local -a unique=()
        readarray -t unique < <(printf '%s\n' "${shared[@]}" | sort -u)
        ok "safe.directory = ${unique[*]}"
        if [[ ${#shared[@]} -gt ${#unique[@]} ]]; then
            info "${#shared[@]} entries for ${#unique[@]} path(s), so this setting was added more than once"
        fi
    else
        info "[    ] safe.directory not set, which --shared-checkout <path> sets where a checkout is shared"
    fi
    value=$(git_config_get "core.sharedRepository")
    if [[ -n $value ]]; then
        ok "core.sharedRepository = $value"
    else
        info "[    ] core.sharedRepository not set, optional as above"
    fi

    log ""
    log "Signing"
    if signing_works; then
        ok "A commit signs and verifies on this host"
    else
        missing "A commit signs and verifies on this host"
    fi
}

configure() {
    ensure_prerequisites
    ensure_key
    ensure_known_host

    # The local configuration comes first, because none of it needs GitHub.
    # A host that cannot authenticate yet, which is every host between creating its key and registering it, still ends this run with its identity, its signing configuration, and a commit that verifies locally.
    configure_git
    configure_shared_checkout
    configure_signing

    step "Signing a commit to check the configuration"
    if [[ $DRY_RUN == true ]]; then
        info "[dry run] sign and verify a commit in a throwaway repository"
    elif signing_works; then
        info "A commit signs and verifies"
    else
        die "A commit did not verify. Check $ALLOWED_SIGNERS holds this host's key against $EMAIL."
    fi

    # Authentication is the gate for everything GitHub answers: without it this host reaches no private repository, and the registration check below has no account to read.
    step "Checking SSH authentication to GitHub"
    resolve_github_user
    if [[ -z $GITHUB_USER ]]; then
        registration_needed "authentication" "Authentication"
        die "SSH authentication to GitHub failed. Nothing that reaches GitHub works until the key is registered."
    fi
    if [[ $MANAGED_KEY_AUTHENTICATES == true ]]; then
        info "Authenticated as $GITHUB_USER, with the managed key"
    else
        # The host reaches GitHub, so this is not a failure, and the key this script manages is still not the one doing it.
        # Reporting it as an ordinary line let a run end in "Done" on a host whose managed key was registered nowhere, which is the state this script exists to leave behind.
        registration_needed "authentication" "Authentication"
        warn "This host authenticates with a key other than the managed one, so the managed key is registered nowhere and revoking this host alone would not cut its access"
    fi

    # Signing is the second gate.
    # The configuration above is what signs and verifies locally, and the registration is what makes GitHub show the commit as verified, so only one of those two can be done from here.
    step "Checking the signing key registration"
    local signing_rc=0
    github_signing_key_registered || signing_rc=$?
    case "$signing_rc" in
        0) info "Registered as a signing key" ;;
        2) warn "GitHub could not be reached, so whether the signing key is registered is unknown" ;;
        *)
            registration_needed "signing" "Signing"
            warn "Commits sign locally but show as unverified on GitHub until the key is registered"
            ;;
    esac

    step "Done"
}

# --- Entry ---

parse_args() {
    local -a actions=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -s | --status) actions+=(status) ;;
            -c | --configure) actions+=(configure) ;;
            -n | --dry-run) DRY_RUN=true ;;
            -y | --yes) ASSUME_YES=true ;;
            --name)
                [[ $# -ge 2 ]] || die "--name takes a value"
                NAME="$2"
                shift
                ;;
            --email)
                [[ $# -ge 2 ]] || die "--email takes a value"
                EMAIL="$2"
                shift
                ;;
            --shared-checkout)
                [[ $# -ge 2 ]] || die "--shared-checkout takes the path of the shared checkout"
                SHARED_CHECKOUT="$2"
                shift
                ;;
            -h | --help)
                usage
                exit 0
                ;;
            *) die "Unknown option \"$1\", --help lists the options" ;;
        esac
        shift
    done

    # The order the actions were given is not a contract, so more than one is a refusal rather than the last one winning.
    if ((${#actions[@]} > 1)); then
        die "More than one action given (${actions[*]}), name one"
    fi
    [[ ${#actions[@]} -eq 1 ]] && MODE="${actions[0]}"
    return 0
}

main() {
    parse_args "$@"

    if [[ $EUID -ne 0 ]]; then
        command -v sudo > /dev/null || die "Not running as root and sudo is not installed"
        SUDO=(sudo)
    fi

    case "$MODE" in
        status) status ;;
        configure)
            confirm "Configure git and GitHub on this host?" || die "Declined"
            configure
            ;;
    esac
}

main "$@"
