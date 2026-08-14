#!/bin/bash

# Installs and upgrades the host tools the fleet's repositories expect, on Debian and Ubuntu based hosts, Proxmox included.
# A tool comes from the distro where the distro package keeps up, and from upstream where it does not: an apt repository where upstream publishes one, a released binary where it does not.
# No version is written into this script, and each upstream is asked what it carries now, so the script does not go stale between releases.
#
# Every step is idempotent.
# A keyring or sources file is written only when its content differs, a binary is fetched only when the installed version differs, and apt is refreshed only when a sources file changed.
# Re-running repairs drift rather than assuming a clean host.

set -Eeuo pipefail

readonly KEYRING_DIR="/etc/apt/keyrings"
readonly SOURCES_DIR="/etc/apt/sources.list.d"
readonly BIN_DIR="/usr/local/bin"

# Managed tools, in dependency order: node asks jq to read the upstream release index.
readonly TOOLS=(git gh jq git-restore-mtime node python uv dotnet)

# Package sets.
# The default set is what a tool needs to be useful, and the optional set is what is useful often enough to name but not always wanted, installed only with --optional.
readonly PYTHON_PACKAGES=(build-essential ca-certificates curl python3 python3-dev python3-venv python3-pip)
readonly PYTHON_OPTIONAL=(python-is-python3 python-dev-is-python3 pipx python3-setuptools python3-wheel)

MODE="report"
DRY_RUN=false
ASSUME_YES=false
WITH_OPTIONAL=false
APT_REFRESHED=false
APT_DIRTY=false
DISTRO_ID=""
DISTRO_VERSION=""
ARCH=""
SUDO=()
SELECTED=()
NOTES=()
FAILED=()
CHANGED=()

TMP_DIR=$(mktemp -d)
readonly TMP_DIR
trap 'rm -rf "$TMP_DIR"' EXIT

# --- Output ---

log() { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

note() { NOTES+=("$1: $2"); }

usage() {
    cat <<'EOF'
Usage: install-tools.sh [options] [tool ...]

Installs the host tools the fleet's repositories expect, from upstream where the distro package
trails upstream. With no tool named, every managed tool is selected.

Actions, the last one given wins, default --report:
  -r, --report      Report installed and available versions, change nothing
  -i, --install     Install what is missing, leave an installed tool at its version
  -u, --upgrade     Install what is missing and upgrade what is behind
  -l, --list        List the managed tools and their package sets
  -h, --help        Show this help

Options:
  -n, --dry-run     Print the commands instead of running them
  -y, --yes         Do not prompt before changing the host
  -o, --optional    Include the optional package set, where a tool has one

Versions read as apt versions for an apt-managed tool and as upstream versions for a standalone
binary, so a column compares like with like. A report reads the apt cache as it stands and does
not refresh it, so an available version is as current as the last apt update.

Examples:
  install-tools.sh                       Report on every tool
  install-tools.sh --install             Install what is missing
  install-tools.sh --upgrade --yes       Bring every tool current, no prompt
  install-tools.sh --upgrade node jq     Bring two tools current
  install-tools.sh --install --optional python dotnet
  install-tools.sh --upgrade --dry-run   Show what an upgrade would run
EOF
}

# --- Host ---

# Read the host identity, and refuse a host this script cannot install for.
# A Proxmox host reports itself as its Debian base, so it needs no case of its own.
detect_host() {
    [[ -r /etc/os-release ]] || die "/etc/os-release is not readable, cannot identify this host"

    # shellcheck disable=SC1091  # Host file, not present at lint time.
    . /etc/os-release

    DISTRO_ID="${ID:-}"
    DISTRO_VERSION="${VERSION_ID:-}"
    if [[ $DISTRO_ID != "debian" && $DISTRO_ID != "ubuntu" ]]; then
        [[ " ${ID_LIKE:-} " == *" debian "* ]] ||
            die "Unsupported distribution \"${DISTRO_ID:-unknown}\", this script installs on Debian and Ubuntu based hosts"
        warn "Distribution \"$DISTRO_ID\" is Debian based but untested, treating it as Debian"
        DISTRO_ID="debian"
    fi

    command -v apt-get > /dev/null || die "apt-get not found, this script installs apt packages"
    ARCH=$(dpkg --print-architecture)

    if [[ $EUID -ne 0 ]]; then
        command -v sudo > /dev/null || die "Not running as root and sudo is not installed"
        SUDO=(sudo)
    fi
}

# --- Execution ---

# Run a command, or print it under --dry-run.
# A read used to decide what to do runs either way, and only a command that changes the host goes through here.
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

# A minimal install carries no curl, and without this guard the caller sees "command not found" from inside a command substitution, which reads as an answer rather than as a failure.
fetch() {
    command -v curl > /dev/null || return 1
    curl -fsSL --retry 2 --connect-timeout 10 "$@"
}

# --- apt ---

apt_installed_version() {
    local version
    version=$(apt-cache policy "$1" 2> /dev/null | awk '/Installed:/ { print $2 }')
    [[ $version == "(none)" ]] && version=""
    printf '%s' "$version"
}

apt_candidate_version() {
    local version
    version=$(apt-cache policy "$1" 2> /dev/null | awk '/Candidate:/ { print $2 }')
    [[ $version == "(none)" ]] && version=""
    printf '%s' "$version"
}

# Refresh the apt lists, once per run, and again after a sources file changes.
apt_refresh() {
    [[ $APT_REFRESHED == true && $APT_DIRTY == false ]] && return 0
    run_root apt-get update
    APT_REFRESHED=true
    APT_DIRTY=false
}

# Install packages.
# Installing leaves an installed package at its version, so an apt upgrade of a tool the caller did not ask to upgrade cannot ride along, and upgrading passes everything to apt.
apt_install() {
    local -a wanted=()
    local package
    for package in "$@"; do
        if [[ $MODE == "install" ]] && [[ -n $(apt_installed_version "$package") ]]; then
            continue
        fi
        wanted+=("$package")
    done
    [[ ${#wanted[@]} -eq 0 ]] && return 0

    apt_refresh
    run_root apt-get install -y "${wanted[@]}"
}

# Packages from a set that are not installed, one per line.
# Whether a package is installed is asked of dpkg rather than of apt, here and in the sibling scripts, because dpkg answers about the installed state while apt-cache also carries a candidate, and the two disagree on a host whose lists are stale.
# The candidate is still what apt_candidate_version reports, since that is the question the report asks.
package_installed() {
    dpkg-query -W -f='${Status}' "$1" 2> /dev/null | grep -q "^install ok installed"
}

apt_missing() {
    local package
    for package in "$@"; do
        package_installed "$package" || printf '%s\n' "$package"
    done
    return 0
}

# Install a package that displaces distro packages, asking apt what it would remove and putting that in front of the operator first.
# Asking apt beats naming the conflicts here, because the conflict set belongs to the upstream package and changes without notice.
# Orphaned dependencies are left for a later apt autoremove rather than swept here, since autoremove reaches the whole host.
apt_install_displacing() {
    local package="$1"
    apt_refresh

    if [[ $DRY_RUN == true ]]; then
        info "[dry run] apt-get install -y $package, removals cannot be previewed until the repository is read"
        return 0
    fi

    local -a removals=()
    readarray -t removals < <(apt-get -s install "$package" 2> /dev/null | awk '/^Remv / { print $2 }')
    if [[ ${#removals[@]} -gt 0 ]]; then
        log "  Installing $package removes ${#removals[@]} package(s): ${removals[*]}"
        log "  Their dependencies are left installed, for a later apt autoremove to clean up"
        confirm "  Continue?" || die "Declined, $package left as it is"
    fi

    run_root apt-get install -y "$package"
}

# --- Repositories ---

# Install an apt signing keyring, and prove it is the key that signs the repository metadata rather than trusting the download on its own.
# An upstream that rotates or adds a signing key breaks a pinned fingerprint list but not this check.
# Returns 0 when the keyring changed.
install_keyring() {
    local url="$1" path="$2" release_url="$3" armored="$4"
    local staged
    staged="$TMP_DIR/$(basename "$path")"

    if [[ $armored == true ]]; then
        fetch "$url" | gpg --dearmor > "$staged"
    else
        fetch -o "$staged" "$url"
    fi

    # The check is the reason to trust the key at all, so a host without gpgv stops here rather than installing the key unverified.
    # Prerequisites install it before any of this runs.
    command -v gpgv > /dev/null ||
        die "gpgv is not installed, so the key at $url cannot be checked against the repository it signs"
    fetch -o "$TMP_DIR/InRelease" "$release_url" ||
        die "Cannot read the repository metadata at $release_url"
    gpgv --keyring "$staged" "$TMP_DIR/InRelease" > /dev/null 2>&1 ||
        die "The key at $url does not sign the repository at $release_url, refusing to trust it"

    if [[ -f $path ]] && cmp -s "$staged" "$path"; then
        return 1
    fi

    run_root install -d -m 0755 "$KEYRING_DIR"
    run_root install -m 0644 "$staged" "$path"
    return 0
}

# Write a deb822 sources file, matching the format the rest of sources.list.d uses.
# Returns 0 when the file changed.
write_sources() {
    local name="$1" uris="$2" suites="$3" components="$4" keyring="$5"
    local path="$SOURCES_DIR/$name.sources"
    local content
    content=$(printf 'Types: deb\nURIs: %s\nSuites: %s\nComponents: %s\nArchitectures: %s\nSigned-By: %s\n' \
        "$uris" "$suites" "$components" "$ARCH" "$keyring")

    if [[ -f $path ]] && [[ "$(cat "$path")" == "$content" ]]; then
        return 1
    fi

    if [[ $DRY_RUN == true ]]; then
        info "[dry run] write $path"
        return 0
    fi

    printf '%s' "$content" | "${SUDO[@]}" tee "$path" > /dev/null
    return 0
}

# Drop a superseded repository file, so apt does not read the same repository twice.
remove_stale() {
    local path
    for path in "$@"; do
        [[ -e $path ]] || continue
        run_root rm -f "$path"
        APT_DIRTY=true
    done
}

ensure_prerequisites() {
    local -a missing=()
    # The gpgv package is named on its own rather than left to gnupg, since the keyring check depends on it.
    readarray -t missing < <(apt_missing ca-certificates curl gnupg gpgv)
    [[ ${#missing[@]} -eq 0 ]] && return 0
    apt_refresh
    run_root apt-get install -y "${missing[@]}"
}

# --- Upstream releases ---

# The tag of a repository's current release.
# GitHub's "latest" slug redirects to it, which costs no API quota and needs no token.
github_latest_tag() {
    local repo="$1" url
    url=$(curl -fsSLI -o /dev/null -w '%{url_effective}' --retry 2 --connect-timeout 10 \
        "https://github.com/$repo/releases/latest") || return 1
    printf '%s' "${url##*/}"
}

verify_sha256() {
    local file="$1" name="$2" sums="$3" expected actual
    expected=$(awk -v name="$name" '$2 == name { print $1 }' "$sums")
    [[ -n $expected ]] || die "No checksum published for $name"
    actual=$(sha256sum "$file" | cut -d ' ' -f 1)
    [[ $actual == "$expected" ]] || die "Checksum mismatch for $name"
}

# --- git ---

git_source() { printf 'distro'; }
git_version() { apt_installed_version git; }
git_target() { apt_candidate_version git; }
git_install() { apt_install git; }

# --- gh ---

gh_source() { printf 'cli.github.com'; }
gh_version() { apt_installed_version gh; }
gh_target() { apt_candidate_version gh; }

gh_install() {
    ensure_prerequisites

    # The keyring moved to /etc/apt/keyrings and the sources file to deb822, so both predecessors are cleared.
    remove_stale "$SOURCES_DIR/github-cli.list" "/usr/share/keyrings/githubcli-archive-keyring.gpg"

    if install_keyring "https://cli.github.com/packages/githubcli-archive-keyring.gpg" \
        "$KEYRING_DIR/githubcli-archive-keyring.gpg" \
        "https://cli.github.com/packages/dists/stable/InRelease" false; then
        APT_DIRTY=true
    fi

    if write_sources "github-cli" "https://cli.github.com/packages" "stable" "main" \
        "$KEYRING_DIR/githubcli-archive-keyring.gpg"; then
        APT_DIRTY=true
    fi

    apt_install gh
}

# --- jq ---

jq_source() { printf 'jqlang/jq'; }

jq_version() {
    command -v jq > /dev/null || return 0
    jq --version 2> /dev/null | sed 's/^jq-//'
}

jq_target() {
    local tag
    tag=$(github_latest_tag "jqlang/jq") || return 1
    printf '%s' "${tag#jq-}"
}

# The distro package stays installed and stays shadowed: /usr/local/bin precedes /usr/bin, so the upstream binary wins without removing a package another package may depend on.
jq_install() {
    local asset="jq-linux-$ARCH"
    fetch -o "$TMP_DIR/$asset" "https://github.com/jqlang/jq/releases/latest/download/$asset"
    fetch -o "$TMP_DIR/jq-sha256sum.txt" "https://github.com/jqlang/jq/releases/latest/download/sha256sum.txt"
    verify_sha256 "$TMP_DIR/$asset" "$asset" "$TMP_DIR/jq-sha256sum.txt"
    run_root install -m 0755 "$TMP_DIR/$asset" "$BIN_DIR/jq"
}

# --- git-restore-mtime ---

git_restore_mtime_source() { printf 'MestreLion/git-tools'; }

# Read the version out of the installed script rather than by running it, so a host that is missing the python3 the script runs on still reports what it has.
git_restore_mtime_version() {
    local path version
    path=$(command -v git-restore-mtime) || return 0
    version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$path" | head -1)
    if [[ -z $version ]]; then
        version=$(git restore-mtime --version 2> /dev/null | awk '{ print $NF }')
    fi
    printf '%s' "$version"
}

# Upstream attaches no asset to a release, so the script is taken from the tree at the latest release tag rather than from the default branch: a tag is a fixed revision that installs the same bytes twice, where the branch is whatever it holds at the moment of the fetch.
# The version is read from the file that gets installed, so the reported version is the installed one either way.
git_restore_mtime_fetch() {
    local staged="$TMP_DIR/git-restore-mtime" tag
    [[ -s $staged ]] && return 0

    tag=$(github_latest_tag "MestreLion/git-tools") || return 1
    fetch -o "$staged" "https://raw.githubusercontent.com/MestreLion/git-tools/$tag/git-restore-mtime"
}

git_restore_mtime_target() {
    git_restore_mtime_fetch || return 1
    sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$TMP_DIR/git-restore-mtime" | head -1
}

# Git runs any executable named git-* on the PATH as a subcommand, so this is all "git restore-mtime" needs.
git_restore_mtime_install() {
    # Upstream ships a python3 script, and a minimal server image carries no python at all.
    apt_install python3

    git_restore_mtime_fetch
    run_root install -m 0755 "$TMP_DIR/git-restore-mtime" "$BIN_DIR/git-restore-mtime"
}

# --- node ---

node_source() { printf 'deb.nodesource.com'; }
node_version() { apt_installed_version nodejs; }
node_target() { apt_candidate_version nodejs; }

# The major release line upstream currently carries as LTS.
# Upstream's index is newest first, so the first entry naming an LTS codename belongs to the current LTS line.
node_lts_major() {
    local index="$TMP_DIR/node-index.json" version
    [[ -s $index ]] || fetch -o "$index" "https://nodejs.org/dist/index.json" || return 1

    if command -v jq > /dev/null; then
        version=$(jq -r 'first(.[] | select(.lts != false)) | .version' "$index")
    else
        # One pass over the file rather than a pipeline: upstream serves this index without spaces today and nothing promises that, so the whitespace JSON allows is tolerated, and a reader that stopped early would leave its producer writing to a closed pipe under pipefail.
        version=$(awk -v RS='}' '
            !found && /"lts"[[:space:]]*:[[:space:]]*"/ {
                if (match($0, /"version"[[:space:]]*:[[:space:]]*"v?[0-9]+/)) {
                    field = substr($0, RSTART, RLENGTH)
                    if (match(field, /[0-9]+/)) {
                        print substr(field, RSTART, RLENGTH)
                        found = 1
                    }
                }
            }' "$index")
    fi

    version="${version#v}"
    version="${version%%.*}"
    [[ $version =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$version"
}

node_install() {
    ensure_prerequisites

    local major
    major=$(node_lts_major) || die "Cannot read the current LTS line from the upstream release index"

    if install_keyring "https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key" \
        "$KEYRING_DIR/nodesource.gpg" \
        "https://deb.nodesource.com/node_$major.x/dists/nodistro/InRelease" true; then
        APT_DIRTY=true
    fi

    if write_sources "nodesource" "https://deb.nodesource.com/node_$major.x" "nodistro" "main" \
        "$KEYRING_DIR/nodesource.gpg"; then
        APT_DIRTY=true
    fi

    # The upstream package carries npm itself and conflicts with the distro npm and nodejs-doc, so installing it displaces them.
    apt_install_displacing nodejs
}

# --- python ---

python_source() { printf 'distro'; }
python_version() { apt_installed_version python3; }
python_target() { apt_candidate_version python3; }

python_packages() {
    local -a packages=("${PYTHON_PACKAGES[@]}")
    [[ $WITH_OPTIONAL == true ]] && packages+=("${PYTHON_OPTIONAL[@]}")
    printf '%s\n' "${packages[@]}"
}

python_install() {
    local -a packages=()
    readarray -t packages < <(python_packages)
    apt_install "${packages[@]}"
}

# --- uv ---

uv_source() { printf 'astral-sh/uv'; }

uv_version() {
    command -v uv > /dev/null || return 0
    uv --version 2> /dev/null | awk '{ print $2 }'
}

# Upstream tags without a leading v today, and the installed version never carries one, so the tag is normalized rather than trusted: a tag that gains a prefix would otherwise read as permanently outdated and re-download on every run.
uv_target() {
    local tag
    tag=$(github_latest_tag "astral-sh/uv") || return 1
    printf '%s' "${tag#v}"
}

# Upstream names its assets by rust target triple rather than by dpkg architecture.
uv_triple() {
    case "$ARCH" in
        amd64) printf 'x86_64-unknown-linux-gnu' ;;
        arm64) printf 'aarch64-unknown-linux-gnu' ;;
        armhf) printf 'armv7-unknown-linux-gnueabihf' ;;
        i386) printf 'i686-unknown-linux-gnu' ;;
        *) return 1 ;;
    esac
}

# Both binaries ship in one archive: uv resolves and runs projects, and uvx runs a tool without installing it into the project.
uv_install() {
    local triple asset
    triple=$(uv_triple) || die "Upstream publishes no uv build for $ARCH"
    asset="uv-$triple.tar.gz"

    fetch -o "$TMP_DIR/$asset" "https://github.com/astral-sh/uv/releases/latest/download/$asset"
    fetch -o "$TMP_DIR/$asset.sha256" "https://github.com/astral-sh/uv/releases/latest/download/$asset.sha256"
    verify_sha256 "$TMP_DIR/$asset" "$asset" "$TMP_DIR/$asset.sha256"

    tar -xzf "$TMP_DIR/$asset" -C "$TMP_DIR"
    run_root install -m 0755 "$TMP_DIR/uv-$triple/uv" "$BIN_DIR/uv"
    run_root install -m 0755 "$TMP_DIR/uv-$triple/uvx" "$BIN_DIR/uvx"
}

# --- dotnet ---

dotnet_source() {
    if [[ -f "$SOURCES_DIR/microsoft-prod.sources" || -f "$SOURCES_DIR/microsoft-prod.list" ]]; then
        printf 'packages.microsoft.com'
    else
        printf 'distro'
    fi
}

# SDK packages apt can see, lowest major first.
# An empty result means no feed carries one yet.
dotnet_sdk_packages() {
    apt-cache search --names-only '^dotnet-sdk-[0-9]+\.[0-9]+$' 2> /dev/null | awk '{ print $1 }' | sort -V
}

dotnet_sdk_latest() { dotnet_sdk_packages | tail -1; }

dotnet_version() {
    local package
    package=$(dotnet_sdk_latest)
    [[ -n $package ]] || return 0
    apt_installed_version "$package"
}

dotnet_target() {
    local package
    package=$(dotnet_sdk_latest)
    [[ -n $package ]] || return 0
    apt_candidate_version "$package"
}

# Microsoft's feed is the fallback rather than the default: where the distro carries .NET, mixing the two feeds is what breaks a host, and Microsoft's feed carries amd64 only.
dotnet_feed() {
    # The decision rests on what the distro feed carries, so it is made against lists that have been read at least once.
    # A host whose lists were never populated reports no SDK at all, and adding Microsoft's feed to a distribution that ships its own is the mixing this exists to avoid.
    apt_refresh

    [[ -n $(dotnet_sdk_latest) ]] && return 0

    if [[ $ARCH != "amd64" ]]; then
        warn "No .NET SDK in the distro feed and Microsoft's feed carries amd64 only, skipping dotnet on $ARCH"
        return 1
    fi

    local deb="$TMP_DIR/packages-microsoft-prod.deb"
    fetch -o "$deb" "https://packages.microsoft.com/config/$DISTRO_ID/$DISTRO_VERSION/packages-microsoft-prod.deb" ||
        die "Microsoft publishes no feed for $DISTRO_ID $DISTRO_VERSION"
    run_root dpkg -i "$deb"
    APT_DIRTY=true
    apt_refresh
}

# The default set is the newest SDK, and the optional set is every other SDK line the feed carries, for a host that has to build against more than one.
dotnet_install() {
    dotnet_feed || return 0

    local latest
    latest=$(dotnet_sdk_latest)
    [[ -n $latest ]] || die "No .NET SDK package is available for $DISTRO_ID $DISTRO_VERSION"

    apt_install "$latest"

    [[ $WITH_OPTIONAL == true ]] || return 0
    local package
    while read -r package; do
        if [[ $package != "$latest" ]]; then
            apt_install "$package"
        fi
    done < <(dotnet_sdk_packages)
}

# --- Tool dispatch ---

tool_function() {
    local tool="$1" suffix="$2"
    printf '%s_%s' "${tool//-/_}" "$suffix"
}

# Whether a tool's upstream repository is in place.
# A tool installed from the distro while its upstream repository is unconfigured reads as current against the distro's own version, which is the one thing the report must not say, so it reads as unmanaged instead.
tool_configured() {
    case "$1" in
        gh) [[ -f "$SOURCES_DIR/github-cli.sources" ]] ;;
        node) [[ -f "$SOURCES_DIR/nodesource.sources" ]] ;;
        *) return 0 ;;
    esac
}

tool_effective_status() {
    local tool="$1" status
    status=$(tool_status "$2" "$3")
    if [[ $status == "current" || $status == "outdated" ]] && ! tool_configured "$tool"; then
        status="unmanaged"
    fi
    printf '%s' "$status"
}

tool_status() {
    local installed="$1" target="$2"

    if [[ -z $target ]]; then
        [[ -n $installed ]] && printf 'unknown' || printf 'unavailable'
        return 0
    fi
    if [[ -z $installed ]]; then
        printf 'missing'
        return 0
    fi
    if [[ $installed == "$target" ]] || dpkg --compare-versions "$installed" ge "$target" 2> /dev/null; then
        printf 'current'
    else
        printf 'outdated'
    fi
}

# Where PATH currently resolves $1 to, when that is not $BIN_DIR/$1, or empty when it already is.
# Shared by the report, which only names the shadow, and --install/--upgrade, which removes it.
# "type -P" skips aliases and shell functions, which "command -v" answers for with no file behind them.
tool_shadow_path() {
    local name="$1" resolved
    resolved=$(type -P "$name" 2> /dev/null || true)
    [[ -n $resolved && $resolved != "$BIN_DIR/$name" ]] && printf '%s' "$resolved"
    return 0
}

# Per-tool detail worth a line under the report: a package set that is part installed, or a feed that is not configured yet.
tool_note() {
    local tool="$1"
    local -a missing=()

    case "$tool" in
        python)
            local -a packages=()
            readarray -t packages < <(python_packages)
            readarray -t missing < <(apt_missing "${packages[@]}")
            if [[ ${#missing[@]} -gt 0 ]]; then
                note "python" "${#missing[@]} package(s) not installed: ${missing[*]}"
            fi
            if [[ $WITH_OPTIONAL == false ]]; then
                note "python" "optional set not selected: ${PYTHON_OPTIONAL[*]}"
            fi
            ;;
        dotnet)
            local -a sdks=()
            readarray -t sdks < <(dotnet_sdk_packages)
            if [[ ${#sdks[@]} -eq 0 ]]; then
                note "dotnet" "no SDK package available, Microsoft's feed is not configured"
            else
                readarray -t missing < <(apt_missing "${sdks[@]}")
                if [[ ${#missing[@]} -gt 0 ]]; then
                    note "dotnet" "SDK line(s) not installed: ${missing[*]}"
                fi
            fi
            ;;
        jq | uv | git-restore-mtime)
            # A copy earlier on the PATH keeps answering after this script installs a newer one, which reads as an upgrade that did not take.
            # --install/--upgrade removes it, so a report naming one here is stale the moment either runs.
            local resolved
            resolved=$(tool_shadow_path "$tool")
            if [[ -n $resolved ]]; then
                if [[ -x "$BIN_DIR/$tool" ]]; then
                    note "$tool" "$resolved comes first on the PATH and shadows the managed copy at $BIN_DIR/$tool"
                else
                    note "$tool" "$resolved is installed outside $BIN_DIR and keeps answering once the managed copy is installed"
                fi
            fi
            ;;
        gh | node)
            local name="github-cli"
            if [[ $tool == "node" ]]; then
                name="nodesource"
            fi
            if [[ ! -f "$SOURCES_DIR/$name.sources" ]]; then
                note "$tool" "upstream repository not configured, the available version is the distro's"
            fi
            ;;
        *) ;;
    esac

    return 0
}

report() {
    # Wide enough for an Ubuntu backport version, which is the longest of these in practice.
    local format="%-18s %-26s %-26s %-22s %s\n"
    # shellcheck disable=SC2059  # Format string is a constant defined above.
    printf "$format" "TOOL" "INSTALLED" "AVAILABLE" "SOURCE" "STATUS"

    if ! command -v curl > /dev/null; then
        note "report" "curl is not installed, so an upstream that is not an apt repository cannot be read yet"
    fi

    local tool installed target
    for tool in "${SELECTED[@]}"; do
        installed=$("$(tool_function "$tool" version)" 2> /dev/null || true)
        target=$("$(tool_function "$tool" target)" 2> /dev/null || true)
        # shellcheck disable=SC2059  # Format string is a constant defined above.
        printf "$format" "$tool" "${installed:--}" "${target:--}" \
            "$("$(tool_function "$tool" source)")" "$(tool_effective_status "$tool" "$installed" "$target")"
        tool_note "$tool"
    done

    [[ ${#NOTES[@]} -eq 0 ]] && return 0
    log ""
    log "Notes:"
    local entry
    for entry in "${NOTES[@]}"; do
        info "$entry"
    done
}

# Remove a copy of a managed tool found earlier on PATH than $BIN_DIR, so the managed copy is what PATH resolves to afterward.
# Only jq, uv, and git-restore-mtime install as loose binaries outside apt, and uv's companion uvx is unshadowed alongside it.
tool_unshadow() {
    local tool="$1"
    local -a names=()
    case "$tool" in
        jq | git-restore-mtime) names=("$tool") ;;
        uv) names=(uv uvx) ;;
        *) return 0 ;;
    esac

    # A loop, not one check, since PATH can stack more than one shadow ahead of $BIN_DIR.
    # Removing only the nearest would still leave $BIN_DIR shadowed by the next one.
    local name resolved
    for name in "${names[@]}"; do
        while true; do
            resolved=$(tool_shadow_path "$name")
            [[ -n $resolved ]] || break

            # A distro package's own file, found only when PATH puts it ahead of $BIN_DIR, which this script does not set up.
            # Removing it directly would desync dpkg's database from the filesystem, so it stays, and the fix is the PATH order.
            if dpkg-query -S "$resolved" > /dev/null 2>&1; then
                warn "$tool: $resolved belongs to a distro package and stays, put $BIN_DIR ahead of it on PATH instead"
                break
            fi

            log "$tool: $resolved shadows $BIN_DIR/$name, removing it"
            if ! confirm "  Remove $resolved?"; then
                warn "$tool: left $resolved in place, it will keep shadowing $BIN_DIR/$name"
                break
            fi
            run_root rm -f "$resolved"
            # Under --dry-run nothing is actually removed, so the same path would resolve again forever.
            [[ $DRY_RUN == true ]] && break
        done
    done
}

# Install or upgrade one tool.
# A tool whose install returns non-zero is collected rather than fatal, so one failure does not strand the rest of the run.
# A refusal is not a failure and does end the run: an unverifiable keyring, a checksum mismatch, or a declined prompt stops everything rather than being collected, because continuing past one would install something nobody vouched for.
# Some upstream lookups also end the run today where collecting them would match the intent above, which TODO.md records rather than changes here.
apply_tool() {
    local tool="$1" installed target status

    # Unshadowing first is safe only when nothing at $BIN_DIR could be made worse by it.
    # --upgrade brings $BIN_DIR current regardless, and --install with nothing there yet has nothing to protect.
    # --install with a managed copy already in place leaves it at its version by design, so removing a newer shadow first would downgrade what PATH resolves to.
    if [[ $MODE == "upgrade" || ! -x "$BIN_DIR/$tool" ]]; then
        tool_unshadow "$tool"
    elif [[ -n $(tool_shadow_path "$tool") ]]; then
        log "$tool: still shadowed on PATH, --upgrade removes it, --install leaves it to avoid downgrading what's shadowing it"
    fi

    installed=$("$(tool_function "$tool" version)" 2> /dev/null || true)
    target=$("$(tool_function "$tool" target)" 2> /dev/null || true)
    status=$(tool_effective_status "$tool" "$installed" "$target")

    # A package set can be part installed while the tool that names it reads as current, so python and dotnet always carry on into the install and the mode decides what apt is asked to do.
    local package_set=false
    if [[ $tool == "python" || $tool == "dotnet" ]]; then
        package_set=true
    fi

    if [[ $package_set == false ]]; then
        if [[ $status == "current" ]]; then
            log "$tool: current at ${installed}, leaving it alone"
            return 0
        fi
        if [[ $MODE == "install" && $status == "outdated" ]]; then
            log "$tool: at ${installed}, upstream carries ${target}, --upgrade moves it"
            return 0
        fi
    fi

    if [[ $status == "current" ]]; then
        log "$tool: current at ${installed}, checking the package set"
    elif [[ $status == "unmanaged" ]]; then
        # The available version is the distro's until the upstream repository is read, so quoting it here would name the wrong upstream.
        log "$tool: installed from the distro, moving it to $("$(tool_function "$tool" source)")"
    else
        log "$tool: ${status}${target:+, upstream carries $target}"
    fi
    if ! "$(tool_function "$tool" install)"; then
        warn "$tool failed"
        FAILED+=("$tool")
        return 0
    fi

    local now
    now=$("$(tool_function "$tool" version)" 2> /dev/null || true)
    if [[ $now != "$installed" ]]; then
        CHANGED+=("$tool ${installed:--} -> ${now:--}")
    fi
}

apply() {
    log "Selected: ${SELECTED[*]}"
    if [[ $DRY_RUN == true ]]; then
        log "Mode: $MODE (dry run)"
    else
        log "Mode: $MODE"
    fi
    log ""
    confirm "Change this host?" || die "Declined"
    log ""

    # Every tool reads an upstream over https, and a minimal image carries none of this.
    ensure_prerequisites

    local tool
    for tool in "${SELECTED[@]}"; do
        apply_tool "$tool"
    done

    log ""
    if [[ ${#CHANGED[@]} -gt 0 ]]; then
        log "Changed:"
        local entry
        for entry in "${CHANGED[@]}"; do
            info "$entry"
        done
    else
        log "Nothing changed"
    fi

    if [[ ${#FAILED[@]} -gt 0 ]]; then
        log ""
        warn "Failed: ${FAILED[*]}"
        return 1
    fi
}

list_tools() {
    log "Managed tools:"
    local tool
    for tool in "${TOOLS[@]}"; do
        printf '  %-18s %s\n' "$tool" "$("$(tool_function "$tool" source)")"
    done
    log ""
    log "Package sets:"
    info "python default  : ${PYTHON_PACKAGES[*]}"
    info "python optional : ${PYTHON_OPTIONAL[*]}"
    info "dotnet default  : the newest SDK line the feed carries"
    info "dotnet optional : every other SDK line the feed carries"
}

# --- Entry ---

parse_args() {
    local -a requested=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -r | --report) MODE="report" ;;
            -i | --install) MODE="install" ;;
            -u | --upgrade) MODE="upgrade" ;;
            -l | --list) MODE="list" ;;
            -n | --dry-run) DRY_RUN=true ;;
            -y | --yes) ASSUME_YES=true ;;
            -o | --optional) WITH_OPTIONAL=true ;;
            -h | --help)
                usage
                exit 0
                ;;
            -*) die "Unknown option \"$1\", --help lists the options" ;;
            *)
                local known=false tool
                for tool in "${TOOLS[@]}"; do
                    if [[ $tool == "$1" ]]; then
                        known=true
                    fi
                done
                [[ $known == true ]] || die "Unknown tool \"$1\", --list names the managed tools"
                requested+=("$1")
                ;;
        esac
        shift
    done

    if [[ ${#requested[@]} -gt 0 ]]; then
        # Keep the registry's dependency order rather than the order given on the command line.
        local tool requested_tool
        for tool in "${TOOLS[@]}"; do
            for requested_tool in "${requested[@]}"; do
                if [[ $tool == "$requested_tool" ]]; then
                    SELECTED+=("$tool")
                    break
                fi
            done
        done
    else
        SELECTED=("${TOOLS[@]}")
    fi
}

main() {
    parse_args "$@"

    if [[ $MODE == "list" ]]; then
        list_tools
        exit 0
    fi

    detect_host

    case "$MODE" in
        report) report ;;
        install | upgrade) apply ;;
    esac
}

main "$@"
