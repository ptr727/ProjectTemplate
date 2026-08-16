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

# The sudo credential cache drop-in, named for the tooling that writes it, and numbered to sort after a drop-in that arrived with a package.
readonly SUDOERS_FILE="/etc/sudoers.d/90-host-setup-sudo-timestamp"

# Minutes the cache stays valid, long enough to cover a run started from another terminal and short enough that a host left alone does not stay elevated.
readonly SUDO_TIMESTAMP_TIMEOUT=60

# Managed tools, in dependency order: node asks jq to read the upstream release index.
readonly TOOLS=(git gh jq git-restore-mtime node python uv docker dotnet)

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
CODENAME=""
IS_WSL=false
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
      --sudo-timestamp
                    Share one sudo credential cache across this user's terminals
  -h, --help        Show this help

Options:
  -n, --dry-run     Print the commands instead of running them
  -y, --yes         Do not prompt before changing the host
  -o, --optional    Include the optional package set, where a tool has one

Versions read as apt versions for an apt-managed tool and as upstream versions for a standalone
binary, so a column compares like with like. A report reads the apt cache as it stands and does
not refresh it, so an available version is as current as the last apt update.

--sudo-timestamp writes a sudoers drop-in for the invoking user alone, so one "sudo -v" covers
every terminal that user has open rather than only the one it ran in. It touches no tool.
Removing the file it names undoes it, and "update-alternatives --auto sudo" undoes the
implementation switch it asks for on a host whose sudo parses no timestamp_type.

Examples:
  install-tools.sh                       Report on every tool
  install-tools.sh --install             Install what is missing
  install-tools.sh --upgrade --yes       Bring every tool current, no prompt
  install-tools.sh --upgrade node jq     Bring two tools current
  install-tools.sh --install --optional python dotnet
  install-tools.sh --upgrade --dry-run   Show what an upgrade would run
  install-tools.sh --sudo-timestamp      Share one sudo credential cache across terminals
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
    CODENAME="${VERSION_CODENAME:-}"
    if [[ $DISTRO_ID != "debian" && $DISTRO_ID != "ubuntu" ]]; then
        [[ " ${ID_LIKE:-} " == *" debian "* ]] ||
            die "Unsupported distribution \"${DISTRO_ID:-unknown}\", this script installs on Debian and Ubuntu based hosts"
        warn "Distribution \"$DISTRO_ID\" is Debian based but untested, treating it as Debian"
        DISTRO_ID="debian"
    fi

    command -v apt-get > /dev/null || die "apt-get not found, this script installs apt packages"
    ARCH=$(dpkg --print-architecture)

    # WSL has no kernel of its own, and docker there comes only from Docker Desktop's own WSL integration, never a native install.
    if grep -qi microsoft /proc/version 2> /dev/null || [[ -n ${WSL_DISTRO_NAME:-} ]]; then
        IS_WSL=true
    fi

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

# --- docker ---

# Matches docker_install: a WSL distribution never reaches download.docker.com, so the report says where docker actually comes from there instead.
docker_source() {
    if [[ $IS_WSL == true ]]; then
        printf "Docker Desktop's WSL integration"
    else
        printf 'download.docker.com'
    fi
}

# Read directly from the CLI rather than from apt_installed_version docker-ce, unlike gh and node.
# On a WSL distribution using Docker Desktop's own WSL integration, docker is a working command with no docker-ce apt package behind it at all, and reading the apt package version would misreport that working install as absent.
# This also matches exactly what scripts/host_gate.py's own probes and pattern read, in the same order, so the two never disagree about one host.
# The daemon is asked first and its own banner is the fallback, because the docker on PATH inside a WSL distribution can be a separately packaged client talking to Docker Desktop's engine, and the two carry different versions (issue #751 recorded a 29.1.3 client against a 29.7.2 engine).
# A stopped or unreachable daemon makes the first reading exit non-zero, which is what the banner answers, so this reports the weaker number rather than nothing.
docker_version() {
    command -v docker > /dev/null || return 0
    local engine
    engine=$(docker version --format '{{.Server.Version}}' 2> /dev/null) || engine=""
    if [[ $engine =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
        printf '%s' "$engine"
        return 0
    fi
    docker --version 2> /dev/null | sed -n 's/^Docker version \([0-9][0-9.]*\).*/\1/p'
}

# Stripped of the epoch and the Debian package revision apt_candidate_version otherwise carries (e.g. "5:29.7.2-1~debian.13~trixie"), so this compares like for like against docker_version's plain reading rather than against dpkg's own packaging metadata.
# Only the native path reaches here, where the engine docker_version reports and the docker-ce package this reads are the same install, so the two remain comparable now that the engine is what it asks for.
docker_target() {
    local raw
    raw=$(apt_candidate_version docker-ce)
    [[ -z $raw ]] && return 0
    raw="${raw#*:}"
    printf '%s' "${raw%%-*}"
}

# Old and conflicting packages named here, per Docker's own uninstall list.
# Debian and Ubuntu never ship a package named docker-ce, so unlike gh and node there is no distro package the upstream one could be confused with, and tool_configured needs no entry for it.
docker_install() {
    # The only sanctioned source inside a WSL distribution is Docker Desktop's own WSL integration, confirmed with the maintainer as a hard rule with no override.
    # A native install here would run a second engine beside Desktop's, so this is always a skip rather than an install, on the same pattern dotnet_feed uses for an architecture Microsoft's feed does not carry.
    if [[ $IS_WSL == true ]]; then
        warn "This is a WSL distribution, and docker here comes only from Docker Desktop's own WSL integration, never from installing docker-ce directly. Enable it in Docker Desktop under Settings, Resources, WSL integration, or check it from Windows with setup-wsl.ps1 -Status. Skipping the native install."
        # A skip is success only where the integration already answers, since --install/--upgrade otherwise exits 0 having neither installed docker nor found it working.
        command -v docker > /dev/null && return 0
        warn "docker is not on PATH here either, so Docker Desktop's WSL integration is not enabled for this distribution yet."
        return 1
    fi
    [[ -n $CODENAME ]] ||
        die "/etc/os-release names no VERSION_CODENAME, so the Docker apt repository's suite cannot be worked out"

    local -a conflicts=(docker.io docker-doc docker-compose docker-compose-v2 docker-buildx podman-docker containerd runc)
    local -a present=()
    local pkg
    for pkg in "${conflicts[@]}"; do
        package_installed "$pkg" && present+=("$pkg")
    done
    if [[ ${#present[@]} -gt 0 ]]; then
        log "  Removing ${#present[@]} conflicting package(s): ${present[*]}"
        run_root apt-get remove -y "${present[@]}"
    fi

    ensure_prerequisites
    remove_stale "$SOURCES_DIR/docker.list"

    if install_keyring "https://download.docker.com/linux/$DISTRO_ID/gpg" \
        "$KEYRING_DIR/docker.gpg" \
        "https://download.docker.com/linux/$DISTRO_ID/dists/$CODENAME/InRelease" true; then
        APT_DIRTY=true
    fi

    if write_sources "docker" "https://download.docker.com/linux/$DISTRO_ID" "$CODENAME" "stable" \
        "$KEYRING_DIR/docker.gpg"; then
        APT_DIRTY=true
    fi

    apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    # Non-root use (usermod -aG docker $USER) is left to the operator, the same way this file leaves orphaned dependencies to a later apt autoremove: it is a user/group choice rather than a question of whether the tool is present and current.
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
# Shared by the report, which only names the shadow, and --install/--upgrade, which act on it (apply_tool decides when it is safe to remove).
# "type -P" skips aliases and shell functions, which "command -v" answers for with no file behind them.
tool_shadow_path() {
    local name="$1" resolved
    resolved=$(type -P "$name" 2> /dev/null || true)
    # A relative PATH entry (".", "./bin") makes this relative to the caller's current directory, not a real shadow.
    # What this returns gets removed by tool_unshadow, so only an absolute path is ever trusted as one.
    [[ $resolved == /* && $resolved != "$BIN_DIR/$name" ]] && printf '%s' "$resolved"
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
            # --upgrade removes it. --install removes it only when nothing managed exists yet, and warns instead when it leaves one in place.
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
        docker)
            if [[ $IS_WSL == true ]]; then
                note "docker" "this is a WSL distribution, docker here comes only from Docker Desktop's own WSL integration, never from installing docker-ce directly, so --install/--upgrade skip it"
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
            # A guarded call, not a bare one, so a real removal failure (a read-only filesystem) does not take set -e's whole run down with it.
            # Leaves this one tool shadowed and moves on instead.
            run_root rm -f "$resolved" || {
                warn "$tool: failed to remove $resolved, it will keep shadowing $BIN_DIR/$name"
                break
            }
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

# --- sudo timestamp ---

# The user whose credential cache this widens, which is the one who runs sudo rather than the one sudo runs as.
# Under sudo, $EUID is root's, and widening root's cache would leave the caller prompted in every terminal exactly as before.
sudo_target_user() {
    if [[ $EUID -ne 0 ]]; then
        id -un
        return 0
    fi
    [[ -n ${SUDO_USER:-} ]] ||
        die "Running as root with no invoking user to widen the cache for, run this as the user who runs sudo"
    printf '%s' "$SUDO_USER"
}

# The drop-in, scoped to one user, so every other account on this host keeps sudo's per-terminal default.
sudo_timestamp_content() {
    local user="$1"
    cat << EOF
# Written by host-setup/linux/install-tools.sh --sudo-timestamp, and removing this file undoes it.
# One credential cache for $user across every terminal, so a "sudo -v" in one is what a program started in another runs under.
Defaults:$user timestamp_type=global
Defaults:$user timestamp_timeout=$SUDO_TIMESTAMP_TIMEOUT
EOF
}

# Every installed sudo implementation, as "sudo-path visudo-path".
# The original sudo hides behind update-alternatives wherever sudo-rs holds the link, and it is the alternative rather than the link that names it.
sudo_implementations() {
    command -v update-alternatives > /dev/null || return 0
    local query
    query=$(update-alternatives --query sudo 2> /dev/null) || return 0
    awk '$1 == "Alternative:" { alt = $2 } alt != "" && $1 == "visudo" { print alt, $2 }' <<< "$query"
}

# A visudo named by a bare name resolved to a path, since a Debian host keeps /usr/sbin off an unprivileged PATH and the name alone would read there as an implementation that is not installed.
sudo_visudo_path() {
    local name="$1" candidate
    if [[ $name == /* ]]; then
        printf '%s' "$name"
        return 0
    fi
    if candidate=$(command -v "$name" 2> /dev/null); then
        printf '%s' "$candidate"
        return 0
    fi
    for candidate in /usr/local/sbin /usr/sbin /sbin /usr/bin; do
        if [[ -x "$candidate/$name" ]]; then
            printf '%s' "$candidate/$name"
            return 0
        fi
    done
    return 1
}

# Whether an implementation parses the drop-in, asked of its own visudo rather than of a version number.
# Ubuntu ships sudo-rs as its default sudo from 25.10, and it carries no timestamp_type at all, so a host can hold a sudo that rejects the one setting this writes.
sudo_parses() {
    local file="$1" checker
    checker=$(sudo_visudo_path "$2") || return 1
    "$checker" -cqf "$file" > /dev/null 2>&1
}

# What sudo itself reports as in effect, which is the only answer that accounts for the order it reads the drop-ins in.
# A read that needs a password prints nothing rather than waiting, since a run reaching here has already changed the host.
sudo_timestamp_report() {
    local user="$1" defaults="" effective=""

    if [[ $EUID -eq 0 ]]; then
        defaults=$(sudo -n -l -U "$user" 2> /dev/null) || defaults=""
    else
        defaults=$(sudo -n -l 2> /dev/null) || defaults=""
    fi
    # An unreadable list and a list naming no timestamp option are different answers, and only the raw read separates them.
    if [[ -z $defaults ]]; then
        info "Could not read the settings back, so check them with \"sudo -l\" as $user"
        return 0
    fi
    effective=$(grep -oE 'timestamp_(type|timeout)=[^, ]+' <<< "$defaults" | tr '\n' ' ') || effective=""
    effective="${effective% }"

    # A dry run reports the state it found rather than one this run reached, since nothing was written to reach it.
    if [[ $DRY_RUN == true ]]; then
        info "Set for $user as it stands: ${effective:-neither option, so one cache per terminal}"
        return 0
    fi

    if [[ $effective == *"timestamp_type=global"* ]]; then
        info "In effect for $user: $effective"
    elif [[ -z $effective ]]; then
        warn "sudo names no timestamp option for $user, so $SUDOERS_FILE is not being read"
    else
        warn "sudo reports \"$effective\" for $user, so something it reads after $SUDOERS_FILE overrides it"
    fi
}

# A username escaped for safe embedding in an ERE.
# A username can legally hold a regex metacharacter (a dot is common in a domain-joined account), and left unescaped it can match a different user's line as if it were this one's.
sudo_timestamp_user_re() {
    local user="$1" out="" i c
    for ((i = 0; i < ${#user}; i++)); do
        c="${user:i:1}"
        case "$c" in
            '.' | '[' | $'\\' | '^' | '$' | '(' | ')' | '*' | '+' | '?' | '{' | '}' | '|') out+="\\$c" ;;
            *) out+="$c" ;;
        esac
    done
    printf '%s' "$out"
}

# Whether a drop-in holds nothing but this user's timestamp Defaults, aside from blank lines and comments.
# That purity is what makes deleting the whole file safe, since a mixed file would lose whatever else it sets, and picking lines back out of one is guessing, not reading.
sudo_timestamp_file_is_pure() {
    local user="$1" file="$2" other user_re
    user_re=$(sudo_timestamp_user_re "$user")
    other=$("${SUDO[@]}" grep -vE "^[[:space:]]*(#.*)?\$|^[[:space:]]*Defaults:${user_re}[[:space:]]+timestamp_(type|timeout)=" \
        "$file" 2> /dev/null) || other=""
    [[ -z $other ]]
}

# Share one sudo credential cache across a user's terminals, rather than sudo's default of one per terminal.
configure_sudo_timestamp() {
    local user staged
    user=$(sudo_target_user)
    id -u "$user" > /dev/null 2>&1 || die "This host has no account named \"$user\""

    log "Sudo timestamp: one credential cache for $user across every terminal, valid $SUDO_TIMESTAMP_TIMEOUT minutes"

    staged="$TMP_DIR/sudo-timestamp"
    sudo_timestamp_content "$user" > "$staged"

    # The implementation already in place is preferred, so a host carrying the original sudo changes nothing but the drop-in.
    local switch_to="" switch_checker="" alternative checker
    if ! sudo_parses "$staged" visudo; then
        while read -r alternative checker; do
            [[ -n $checker ]] || continue
            if sudo_parses "$staged" "$checker"; then
                switch_to="$alternative"
                switch_checker="$checker"
                break
            fi
        done < <(sudo_implementations)
        [[ -n $switch_to ]] ||
            die "No sudo on this host parses timestamp_type, which is the setting that shares one cache across terminals. sudo-rs carries no such setting, so install the original sudo with \"apt-get install sudo\" and run this again."
    fi

    # A parse error in any file sudo reads makes every sudo on the host fail, so the set is proved to parse before this adds to it.
    "${SUDO[@]}" visudo -cq > /dev/null 2>&1 ||
        die "This host's sudoers does not parse as it stands, so fix that before adding to it (\"visudo -c\" names the file)"

    # An implementation this run would switch to has to parse the set as well, since switching to one that cannot is what locks every user out.
    if [[ -n $switch_checker ]]; then
        "${SUDO[@]}" "$switch_checker" -cq > /dev/null 2>&1 ||
            die "$switch_to does not parse this host's sudoers, so switching to it would lock every user out. This host is unchanged."
    fi

    local own_current=false
    "${SUDO[@]}" cmp -s "$staged" "$SUDOERS_FILE" 2> /dev/null && own_current=true

    # Another file setting either option is named rather than merged into, since which one wins is the order sudo reads them in and not something this can decide.
    local elsewhere
    # A name holding a dot or ending in a tilde is one sudo skips, this run's own staged file included, so a setting in it is an override sudo never reads.
    elsewhere=$("${SUDO[@]}" grep -rnsE '^[[:space:]]*Defaults.*timestamp_(type|timeout)' \
        --exclude='*.*' --exclude='*~' --exclude="${SUDOERS_FILE##*/}" \
        /etc/sudoers /etc/sudoers.d 2> /dev/null) || elsewhere=""

    # Only this user's own entry is ever a delete candidate; a different user's entry, or one with no user named at all, changes something beyond what this run was asked to change, so it is reported and left alone.
    local -a delete_files=() unsafe_files=()
    if [[ -n $elsewhere ]]; then
        local candidate user_re
        user_re=$(sudo_timestamp_user_re "$user")
        while read -r candidate; do
            [[ -n $candidate ]] || continue
            # The main sudoers file is never auto-edited, and a drop-in that sets something else besides is never guessed at line by line, since either mistake risks removing a rule unrelated to this.
            if [[ $candidate == "${SUDOERS_FILE%/*}"/* ]] && sudo_timestamp_file_is_pure "$user" "$candidate"; then
                delete_files+=("$candidate")
            else
                unsafe_files+=("$candidate")
            fi
        done < <(grep -E "Defaults:${user_re}[[:space:]]+timestamp_(type|timeout)=" <<< "$elsewhere" | awk -F: '{print $1}' | sort -u)
    fi

    if [[ $own_current == true && ${#delete_files[@]} -eq 0 ]]; then
        log "$SUDOERS_FILE already carries exactly this, leaving it alone"
        sudo_timestamp_report "$user"
        return 0
    fi

    if [[ -n $elsewhere ]]; then
        warn "A timestamp option is already set elsewhere, and the file sudo reads last wins:"
        local line
        while read -r line; do
            info "$line"
        done <<< "$elsewhere"
    fi

    if [[ ${#unsafe_files[@]} -gt 0 ]]; then
        die "${unsafe_files[*]} sets $user's timestamp option but also carries something unrelated (or is the main sudoers file itself), so this will not guess which lines are safe to remove. Resolve it by hand with visudo, then run this again."
    fi

    if [[ ${#delete_files[@]} -gt 0 ]]; then
        info "Continuing deletes: ${delete_files[*]} (nothing in it but $user's timestamp Defaults), leaving $SUDOERS_FILE as the one place setting this"
    fi

    if [[ -n $switch_to ]]; then
        log "The sudo this host runs parses no timestamp_type, and $switch_to does"
        info "Switching the alternative changes which sudo implementation every user on this host runs"
        info "\"update-alternatives --auto sudo\" puts that back"
    fi
    confirm "Change this host?" || die "Declined, leaving the host unchanged"

    if [[ -n $switch_to ]]; then
        run_root update-alternatives --set sudo "$switch_to"
        # Each implementation keeps its own credential cache, so the switch invalidates the one this run was using.
        info "The cached credential does not carry across the switch, so the next step may ask for a password"
    fi

    if [[ $own_current == false ]]; then
        # A drop-in whose name holds a dot is one sudo skips, so the content lands under such a name, is proved where it will be read, and only then is renamed over.
        # Renaming is atomic, which a copy into place is not, and a half-written file in that directory locks every user out of sudo.
        local pending="${SUDOERS_FILE%/*}/.${SUDOERS_FILE##*/}.pending"
        run_root install -m 0440 -o root -g root "$staged" "$pending"
        if [[ $DRY_RUN == false ]]; then
            "${SUDO[@]}" visudo -cqf "$pending" > /dev/null 2>&1 || {
                run_root rm -f "$pending"
                die "The staged drop-in does not parse where sudo would read it, so this host is unchanged"
            }
        fi
        run_root mv "$pending" "$SUDOERS_FILE"
    fi

    # A superseded file is removed only once $SUDOERS_FILE is proved in place, so a failure above never leaves this user's cache unset.
    local old
    for old in "${delete_files[@]}"; do
        run_root rm -f "$old"
        [[ $DRY_RUN == true ]] || log "Removed $old, superseded by $SUDOERS_FILE"
    done

    if [[ $DRY_RUN == true ]]; then
        sudo_timestamp_report "$user"
        return 0
    fi

    "${SUDO[@]}" visudo -cq > /dev/null 2>&1 ||
        die "sudoers stopped parsing once this run's changes landed. Remove $SUDOERS_FILE from a root shell to restore sudo, and recreate ${delete_files[*]:-any file this run deleted} if it turns out to have been needed."

    [[ $own_current == false ]] && log "Wrote $SUDOERS_FILE"
    sudo_timestamp_report "$user"
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
            --sudo-timestamp) MODE="sudo-timestamp" ;;
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

    # Checked against the action that won rather than inside the loop, since the last action given is the one that runs.
    if [[ $MODE == "sudo-timestamp" && ${#requested[@]} -gt 0 ]]; then
        die "--sudo-timestamp changes the host rather than a tool, so it takes no tool, and \"${requested[*]}\" names one"
    fi

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
        sudo-timestamp) configure_sudo_timestamp ;;
    esac
}

main "$@"
