#!/bin/bash

# Upgrades this host, on Debian and Ubuntu based hosts, Proxmox and WSL included.
# Two levels: the packages of the release the host is on, which is routine, and the release itself, which is not and so is a separate action behind its own flag.
#
# A release upgrade is refused on a host this script cannot carry across safely: Proxmox, whose major upgrades are a documented procedure with its own preconditions, and a distribution that is neither Debian nor Ubuntu, where the sources rewrite below has no meaning.
# Refusing is the whole point of running this rather than apt by hand: the package upgrade is the same everywhere, and the release upgrade is where hosts differ.
#
# One script rather than one per host: the package path, the WSL and reboot handling, and the command line are shared, and only the release path branches.

set -Eeuo pipefail

readonly BACKUP_ROOT="/var/backups/upgrade-host"

MODE="packages"
DRY_RUN=false
ASSUME_YES=false
DISTRO_ID=""
DISTRO_VERSION=""
CODENAME=""
IS_PROXMOX=false
IS_WSL=false
SUDO=()
APT_OPTS=()

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

usage() {
    cat <<'EOF'
Usage: upgrade-host.sh [options]

Upgrades this host. Packages within the current release are routine; the release itself is a
separate action, and is refused on Proxmox and on a distribution this script does not know.

Actions, name one, default --packages:
  -s, --status      Report the host, what is upgradable, and what release it could move to
  -p, --packages    Upgrade the packages of the current release
  -r, --release     Upgrade the packages, then upgrade to the next release
  -h, --help        Show this help

Options:
  -n, --dry-run     Print the commands instead of running them
  -y, --yes         Do not prompt, and keep the installed config file on a packaging conflict

A release upgrade moves one release at a time, which is what both distributions support: a host two
releases behind is upgraded by running this twice. Debian is carried by rewriting the codename in
the apt sources that point at Debian's own mirrors; a third party repository is left alone, named,
and is the operator's call. Ubuntu is carried by do-release-upgrade, which handles its own sources.

Examples:
  upgrade-host.sh                  Upgrade the packages of the current release
  upgrade-host.sh --status         Report, change nothing
  upgrade-host.sh --packages -y    Upgrade unattended
  upgrade-host.sh --release        Upgrade the packages, then move to the next release
  upgrade-host.sh --release -n     Show what a release upgrade would run
EOF
}

# --- Host ---

detect_host() {
    [[ -r /etc/os-release ]] || die "/etc/os-release is not readable, cannot identify this host"

    # shellcheck disable=SC1091  # Host file, not present at lint time.
    . /etc/os-release

    DISTRO_ID="${ID:-unknown}"
    DISTRO_VERSION="${VERSION_ID:-unknown}"
    CODENAME="${VERSION_CODENAME:-}"

    command -v apt-get >/dev/null || die "apt-get not found, this script upgrades apt based hosts"

    # Proxmox reports itself as its Debian base, so the marker is its own tooling.
    if command -v pveversion >/dev/null; then
        IS_PROXMOX=true
    fi

    # WSL has no kernel of its own to reboot into, and the shutdown is driven from Windows.
    if grep -qi microsoft /proc/version 2>/dev/null || [[ -n ${WSL_DISTRO_NAME:-} ]]; then
        IS_WSL=true
    fi

    if [[ $EUID -ne 0 ]]; then
        command -v sudo >/dev/null || die "Not running as root and sudo is not installed"
        SUDO=(sudo)
    fi

    if [[ $ASSUME_YES == true ]]; then
        # Keep the installed config file on a packaging conflict, since an unattended run has nobody to answer the prompt, and a replaced config is the harder half to notice afterwards.
        export DEBIAN_FRONTEND=noninteractive
        APT_OPTS=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
    fi
}

host_description() {
    local description="$DISTRO_ID $DISTRO_VERSION"
    [[ -n $CODENAME ]] && description="$description ($CODENAME)"
    if [[ $IS_PROXMOX == true ]]; then
        description="$description, Proxmox VE $(pveversion | sed -n 's|^pve-manager/\([^/]*\)/.*|\1|p')"
    fi
    [[ $IS_WSL == true ]] && description="$description, WSL"
    printf '%s' "$description"
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

apt_get() { run_root apt-get "$@" "${APT_OPTS[@]}"; }

confirm() {
    [[ $ASSUME_YES == true || $DRY_RUN == true ]] && return 0
    [[ -t 0 ]] || die "Not a terminal and --yes was not given, refusing to change the host unattended"
    local reply
    read -r -p "$1 [y/N] " reply
    [[ $reply == [yY] || $reply == [yY][eE][sS] ]]
}

# A minimal install carries no curl, and without this guard the caller sees "command not found" from inside a command substitution, which reads as an answer rather than as a failure.
fetch() {
    command -v curl >/dev/null || return 1
    curl -fsSL --retry 2 --connect-timeout 10 "$@"
}

# --- Packages ---

upgrade_packages() {
    step "Updating package lists"
    apt_get update

    step "Upgrading packages"
    apt_get full-upgrade

    step "Completing any partial install"
    apt_get install -f

    step "Removing unused packages and old kernels"
    apt_get autoremove --purge

    step "Cleaning the package cache"
    run_root apt-get autoclean

    refresh_snaps
}

# Snap runs on an Ubuntu host with snapd active, which a container and a WSL distribution usually are not.
refresh_snaps() {
    command -v snap >/dev/null || return 0
    systemctl is-active --quiet snapd 2>/dev/null || return 0

    step "Refreshing snaps"
    run_root snap refresh
}

upgradable_count() {
    # A failed listing and a listing that genuinely found nothing upgradable both read as "no matches" through grep alone, so the two are told apart here rather than both printing 0.
    # This only ever backs a status report, so the answer here is "unknown" rather than a die: nothing downstream mutates on the strength of this count.
    local out status=0
    out=$(apt list --upgradable 2>/dev/null) || status=$?
    if [[ $status -ne 0 ]]; then
        # Re-run once more, stdout discarded this time, so the diagnostic comes from a pipe head bounds in memory rather than a file this would otherwise have to size-cap and clean up itself.
        printf 'unknown, apt list --upgradable failed (exit %s): %s' "$status" "$(apt list --upgradable 2>&1 >/dev/null | head -c 200 | tr '\n' ' ')"
        return 0
    fi
    printf '%s package(s), against the lists as they stand' "$(grep -c '/' <<<"$out" || true)"
}

# --- Reboot ---

reboot_required() {
    [[ -f /var/run/reboot-required ]] && return 0

    # A host with no /boot, which a container and a WSL distribution both are, carries no kernel of its own to compare against.
    # Saying so here rather than leaving it to the search failing keeps the answer from depending on this being called where set -e is suppressed.
    [[ -d /boot ]] || return 1

    # Debian does not always write the marker, so compare what is installed against what is running.
    local running latest
    running=$(uname -r)
    latest=$(find /boot -maxdepth 1 -name 'vmlinuz-*' -printf '%f\n' 2>/dev/null |
        sed 's/^vmlinuz-//' | sort -V | tail -1) || return 1
    [[ -n $latest && $latest != "$running" ]]
}

report_reboot() {
    step "Checking whether a restart is needed"

    if [[ $IS_WSL == true ]]; then
        # A WSL distribution runs the kernel Windows gives it, so only the userspace restarts.
        if [[ -f /var/run/reboot-required ]]; then
            info "Packages asked for a restart: run \"wsl --shutdown\" from Windows, then relaunch"
        else
            info "No restart needed"
        fi
        return 0
    fi

    if reboot_required; then
        info "Restart needed, running $(uname -r)"
        [[ -f /var/run/reboot-required.pkgs ]] && info "Asked for by: $(tr '\n' ' ' </var/run/reboot-required.pkgs)"
        info "Run \"sudo reboot\" when convenient"
    else
        info "No restart needed"
    fi
    return 0
}

# --- Release, shared ---

# Refuse a release upgrade this script cannot carry safely.
# A refusal names what to do instead, because the answer is a procedure rather than a different flag.
release_guard() {
    if [[ $IS_PROXMOX == true ]]; then
        die "Proxmox major upgrades are a documented procedure with their own preconditions, and this script will not drive one. See https://pve.proxmox.com/wiki/Upgrade_from_8_to_9 for the current path, and use --packages here."
    fi
    if [[ $DISTRO_ID != "debian" && $DISTRO_ID != "ubuntu" ]]; then
        die "Release upgrades are supported on Debian and Ubuntu only, and this host reports \"$DISTRO_ID\". Use --packages here."
    fi
    if [[ -z $CODENAME ]]; then
        die "/etc/os-release names no release codename, so the target release cannot be worked out"
    fi
}

# A release upgrade on a host with held or half-configured packages fails partway through, which is the worst place to find out.
release_preconditions() {
    local held
    held=$(apt-mark showhold 2>/dev/null | tr '\n' ' ')
    if [[ -n ${held// /} ]]; then
        die "Held packages block a release upgrade, unhold them first: $held"
    fi

    # A dpkg --audit that fails to run is not the same as one that runs and finds nothing, and only the second one clears the way into a release upgrade.
    local audit status=0
    audit=$("${SUDO[@]}" dpkg --audit 2>&1) || status=$?
    if [[ $status -ne 0 ]]; then
        die "dpkg --audit failed to run (exit $status), so half-configured packages cannot be ruled out before a release upgrade: $audit"
    fi
    if [[ -n $audit ]]; then
        die "dpkg reports half-configured packages, fix them first: $audit"
    fi

    local free
    free=$(df --output=avail -BG /var | tail -1 | tr -dc '0-9')
    if [[ -n $free && $free -lt 5 ]]; then
        warn "Only ${free}G free on /var, a release upgrade downloads the whole distribution"
        confirm "Continue anyway?" || die "Declined"
    fi
    return 0
}

# Debian's release index is read over https, which a minimal install cannot do yet.
# A status report says so rather than installing anything, and a release run is already changing the host, so it installs.
ensure_release_prerequisites() {
    [[ $DISTRO_ID == "debian" ]] || return 0
    command -v curl >/dev/null && return 0

    step "Installing curl, which the release index is read with"
    apt_get update
    apt_get install curl ca-certificates
}

backup_sources() {
    local stamp target
    stamp=$(date +%Y%m%d-%H%M%S)
    target="$BACKUP_ROOT/$stamp"

    step "Backing up the apt sources to $target"
    run_root install -d -m 0755 "$target"

    # A missing file is skipped, since a host on deb822 alone carries no sources.list.
    # A copy that fails for any other reason stops the upgrade: this backup is the only way back, and finding out it was never taken belongs before the sources are rewritten rather than after.
    if [[ -f /etc/apt/sources.list ]]; then
        run_root cp -a /etc/apt/sources.list "$target/"
    fi
    if [[ -d /etc/apt/sources.list.d ]]; then
        run_root cp -a /etc/apt/sources.list.d "$target/"
    fi
}

# --- Release, Debian ---

# The fetch is captured before it is parsed rather than piped into the parser.
# Under pipefail a reader that stops early, as a parser looking for one line does, leaves the fetch writing to a closed pipe, and a successful download then reports itself as a failure.
debian_suite_codename() {
    local release
    release=$(fetch "https://deb.debian.org/debian/dists/$1/Release" 2>/dev/null) || return 1
    awk '/^Codename:/ { print $2; exit }' <<<"$release"
}

# The release after the one this host is on.
# Both distributions support one step at a time, so this never reaches past the next one, and a host further behind is upgraded by running the script again.
# Returns 0 and prints the next release, 1 when the host is already on the newest, and 2 when the index could not be read at all.
# The last two are not the same answer: a host that cannot reach the index is not a host with nothing to upgrade to, and reporting it as one is the dangerous reading.
debian_next_codename() {
    local current="$1" suite previous="" codename read_any=false

    for suite in oldoldstable oldstable stable; do
        codename=$(debian_suite_codename "$suite") || codename=""
        [[ -z $codename ]] && continue
        read_any=true
        if [[ $previous == "$current" ]]; then
            printf '%s' "$codename"
            return 0
        fi
        previous="$codename"
    done

    [[ $read_any == true ]] || return 2
    return 1
}

# Sources that point somewhere other than Debian's own mirrors.
# The new release may not have a suite for them yet, so they are named rather than rewritten, and what to do about them is the operator's call.
# Each entry is judged on its own URI rather than by the file holding it, since one file can hold both a Debian mirror and a third party repository and the second one still breaks the update.
debian_third_party_sources() {
    local file

    for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
        [[ -f $file ]] || continue
        # shellcheck disable=SC2016  # The fields belong to awk, not to the shell.
        awk '
            $1 == "deb" || $1 == "deb-src" {
                uri = ""
                for (i = 2; i <= NF; i++) {
                    if ($i ~ /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//) { uri = $i; break }
                }
                if (uri != "" && uri !~ /\.debian\.org/) print FILENAME ": " uri
            }' "$file"
    done

    for file in /etc/apt/sources.list.d/*.sources; do
        [[ -f $file ]] || continue
        # shellcheck disable=SC2016  # The buffer belongs to awk, not to the shell.
        awk '
            function flush(   i, j, count, value, uris) {
                for (i = 1; i <= n; i++) {
                    if (buf[i] ~ /^URIs:/) {
                        value = buf[i]
                        sub(/^URIs:[[:space:]]*/, "", value)
                        # A stanza may list several URIs, so each is judged on its own rather than the line being read as one string.
                        count = split(value, uris, /[[:space:]]+/)
                        for (j = 1; j <= count; j++) {
                            if (uris[j] != "" && uris[j] !~ /\.debian\.org/) print FILENAME ": " uris[j]
                        }
                    }
                }
                n = 0
            }
            /^[[:space:]]*$/ { flush(); next }
            { buf[++n] = $0 }
            END { flush() }' "$file"
    done

    return 0
}

# Rewrite the release codename in the sources that point at Debian's own mirrors, in both the legacy one-line format and deb822.
# A deb822 stanza is rewritten as a unit, because the URI that decides whether it is Debian's sits on a different line from the suite that names the release.
debian_rewrite_sources() {
    local from="$1" to="$2" file staged rewritten=0

    for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
        [[ -f $file ]] || continue
        grep -q '\.debian\.org' "$file" || continue
        grep -qw "$from" "$file" || continue

        rewritten=$((rewritten + 1))
        if [[ $DRY_RUN == true ]]; then
            info "[dry run] rewrite $from to $to in $file"
            continue
        fi

        # Rewrite through a staging file rather than in place, so a failure part way leaves the original sources file whole.
        # The shell does the redirect, so it writes where the caller can write and root only installs the result.
        staged=$(mktemp "$TMP_DIR/source.XXXXXX")
        if [[ $file == *.sources ]]; then
            # shellcheck disable=SC2016  # $0 and the buffer belong to awk, not to the shell.
            awk -v from="$from" -v to="$to" '
                function flush(   i, j, count, value, uris, debian, line) {
                    # Rewrite a stanza only where every URI in it is a Debian mirror.
                    # A stanza holding one of each is left alone, since moving the suite would move the third party URI to a suite it may not carry.
                    debian = 0
                    for (i = 1; i <= n; i++) {
                        if (buf[i] ~ /^URIs:/) {
                            value = buf[i]
                            sub(/^URIs:[[:space:]]*/, "", value)
                            count = split(value, uris, /[[:space:]]+/)
                            debian = (count > 0)
                            for (j = 1; j <= count; j++) {
                                if (uris[j] != "" && uris[j] !~ /\.debian\.org/) debian = 0
                            }
                        }
                    }
                    for (i = 1; i <= n; i++) {
                        line = buf[i]
                        if (debian && line ~ /^Suites:/) gsub(from, to, line)
                        print line
                    }
                    n = 0
                }
                /^[[:space:]]*$/ { flush(); print ""; next }
                { buf[++n] = $0 }
                END { flush() }' "$file" >"$staged"
        else
            # shellcheck disable=SC2016  # $0 belongs to awk, not to the shell.
            awk -v from="$from" -v to="$to" \
                '{ if ($0 ~ /\.debian\.org/) gsub(from, to); print }' "$file" >"$staged"
        fi

        run_root install -m 0644 "$staged" "$file"
        info "Rewrote $file"
    done

    # Naming what was looked for rather than what was missing: a host on a mirror of its own still names the codename, and being told no source names it would send the reader looking for the wrong thing.
    [[ $rewritten -gt 0 ]] ||
        die "No apt source pointing at a Debian mirror names \"$from\", so there is nothing to rewrite. A host on a mirror other than one under debian.org, or one tracking \"stable\" rather than a codename, is moved by editing its sources itself."
}

debian_release_upgrade() {
    local next status=0
    next=$(debian_next_codename "$CODENAME") || status=$?
    if [[ $status -eq 2 ]]; then
        # The release prerequisites install curl before this runs, so reaching here means the fetch itself failed rather than the tool being absent.
        die "Cannot read the Debian release index at deb.debian.org, so the release to move to is unknown. Check this host's network, DNS, and TLS, then run this again."
    fi
    if [[ $status -ne 0 ]]; then
        log "\"$CODENAME\" is the current stable release, nothing to upgrade to"
        return 0
    fi

    step "Upgrading Debian $CODENAME to $next"
    release_preconditions

    local -a third_party=()
    readarray -t third_party < <(debian_third_party_sources)
    if [[ ${#third_party[@]} -gt 0 ]]; then
        info "These sources are not Debian's and are left as they are:"
        local file
        for file in "${third_party[@]}"; do
            info "  $file"
        done
        info "A repository with no suite for $next fails the update; disable it first if that is the case"
    fi
    info "A snapshot or backup taken before this point is the only way back"
    confirm "Upgrade $CODENAME to $next?" || die "Declined"

    backup_sources
    debian_rewrite_sources "$CODENAME" "$next"

    step "Reading the $next package lists"
    apt_get update

    # The release notes take the minimal upgrade first, so the packages that need no new dependencies land before the ones that pull in a changed dependency set.
    step "Upgrading without new packages"
    apt_get upgrade --without-new-pkgs

    step "Upgrading the rest of the distribution"
    apt_get full-upgrade

    step "Removing what $next no longer needs"
    apt_get autoremove --purge
    run_root apt-get autoclean
}

# --- Release, Ubuntu ---

ubuntu_release_upgrade() {
    step "Checking for a new Ubuntu release"

    if ! command -v do-release-upgrade >/dev/null; then
        apt_get install update-manager-core
    fi

    # Which release is offered, LTS or every release, is the host's own policy in /etc/update-manager/release-upgrades rather than something to decide here.
    if [[ $DRY_RUN == true ]]; then
        info "[dry run] do-release-upgrade"
        return 0
    fi
    if ! "${SUDO[@]}" do-release-upgrade -c; then
        log "No new release is offered under this host's upgrade policy (/etc/update-manager/release-upgrades)"
        return 0
    fi

    info "A snapshot or backup taken before this point is the only way back"
    confirm "Start the release upgrade?" || die "Declined"

    if [[ $ASSUME_YES == true ]]; then
        run_root do-release-upgrade -f DistUpgradeViewNonInteractive
    else
        run_root do-release-upgrade
    fi
}

# --- Actions ---

# What a release upgrade would move this host to, in one line.
# Shared by the status report and by the release run, which says where it is going before it spends time on the packages.
release_summary() {
    if [[ $IS_PROXMOX == true ]]; then
        printf 'refused, Proxmox major upgrades are their own documented procedure'
        return 0
    fi

    case "$DISTRO_ID" in
    debian)
        local next status=0
        next=$(debian_next_codename "$CODENAME") || status=$?
        case "$status" in
        0) printf '%s to %s' "$CODENAME" "$next" ;;
        2)
            # The two reasons have different remedies, so they are reported apart.
            if command -v curl >/dev/null; then
                printf 'unknown, the Debian release index at deb.debian.org could not be reached'
            else
                printf 'unknown, curl is not installed and the Debian release index is read with it'
            fi
            ;;
        *) printf '%s is the current stable release, nothing to move to' "$CODENAME" ;;
        esac
        ;;
    ubuntu)
        if ! command -v do-release-upgrade >/dev/null; then
            printf 'unknown until update-manager-core is installed'
            return 0
        fi
        # The check exits non-zero when nothing is offered, and prints its progress either way, so the exit code decides and the output only names the release.
        # It reads the same as any user, so it runs without sudo and a status report never asks for a password.
        local check summary
        if check=$(do-release-upgrade -c 2>&1); then
            summary=$(grep -i -m 1 "new release" <<<"$check" || true)
            printf '%s' "${summary:-a new release is offered}"
        else
            printf "none offered under this host's upgrade policy (/etc/update-manager/release-upgrades)"
        fi
        ;;
    *) printf 'not supported on this host' ;;
    esac
}

status() {
    log "Host      : $(host_description)"
    log "Upgradable: $(upgradable_count)"
    log "Release   : $(release_summary)"

    report_reboot
}

upgrade() {
    log "Host: $(host_description)"
    if [[ $DRY_RUN == true ]]; then
        log "Mode: $MODE (dry run)"
    else
        log "Mode: $MODE"
    fi

    if [[ $MODE == "release" ]]; then
        release_guard
        ensure_release_prerequisites
        log "Release: $(release_summary)"
    fi

    confirm "Upgrade this host?" || die "Declined"

    upgrade_packages

    if [[ $MODE == "release" ]]; then
        case "$DISTRO_ID" in
        debian) debian_release_upgrade ;;
        ubuntu) ubuntu_release_upgrade ;;
        esac
    fi

    report_reboot
    step "Done"
}

# --- Entry ---

parse_args() {
    local -a actions=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
        -s | --status) actions+=(status) ;;
        -p | --packages) actions+=(packages) ;;
        -r | --release) actions+=(release) ;;
        -n | --dry-run) DRY_RUN=true ;;
        -y | --yes) ASSUME_YES=true ;;
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
    detect_host

    case "$MODE" in
    status) status ;;
    packages | release) upgrade ;;
    esac
}

main "$@"
