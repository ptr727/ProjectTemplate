#!/usr/bin/env bash

# Stands a host up from nothing, by fetching this repository and handing control to the host tooling inside it.
# It is the one file fetched on its own, because a host with no git and no checkout is what it exists to fix.
# It reads no payload, no table, and no sibling module: it obtains a tree and runs one entry point inside that tree.
#
# A tarball rather than a clone, because a clone needs git on a host that may not have it, and because a tarball of a resolved commit cannot be stale.
# The commit it resolved is printed before anything runs, so a run says which revision of the fleet's tooling it used.

set -Eeuo pipefail

readonly REPO="ptr727/ProjectTemplate"
readonly DEFAULT_REF="main"

MODE=""
REF="$DEFAULT_REF"
DIR="${XDG_CACHE_HOME:-$HOME/.cache}/host-setup"
KEEP=false
DRY_RUN=false
ASSUME_YES=false
RESOLVED=""
TREE=""

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
Usage: bootstrap.sh [action] [options]

Stands a host up: upgrades its packages, installs the host tools, and configures git and GitHub.
Fetches this repository and runs the tooling from that tree, so the tools and the rules that
describe them come from one revision rather than from whatever a host happens to hold.

Actions, name one, default --report on a terminal is the menu:
  -r, --report      Report what each tool would do, change nothing
      --host        Share the sudo cache, upgrade packages, install the tools, configure git and
                    GitHub, install the skills
      --dev         As --host, and add the tools a development machine needs
      --upgrade     Upgrade the packages of the current release only
      --tools       Install the host tools only
      --github      Configure git, the SSH key, and commit signing only
      --skills      Install the fleet skills for the current user only
      --sudo        Share one sudo credential cache across this user's terminals only
      --release     Upgrade to the next distribution release, on its own
  -h, --help        Show this help

Options:
  -y, --yes         Do not prompt, and pass the same to each tool
  -n, --dry-run     Print what each step would run, change nothing
      --ref REF     Branch, tag, pull request ref, or commit to run from, default main
      --dir PATH    Where the tree is extracted, default ${XDG_CACHE_HOME:-~/.cache}/host-setup
      --keep        Leave the extracted tree in place, which is removed by default

With no action on a terminal, the menu asks. With no action and no terminal, the report runs, since
a pipe is not a place to answer a question.

Examples:
  bootstrap.sh                          Ask what to do
  bootstrap.sh --report                 Report only
  bootstrap.sh --host --yes             Stand a host up unattended
  bootstrap.sh --ref develop --report   Report using the tooling on develop
  bootstrap.sh --tools --dry-run        Show what installing the tools would run
EOF
}

# --- Fetch ---

fetch() {
    command -v curl >/dev/null || return 1
    curl -fsSL --retry 2 --connect-timeout 15 "$@"
}

# Resolve the ref to the commit it names, so the run reports a revision rather than a moving name.
# The plain-text accept header returns the commit alone, which keeps this free of a JSON parser on a host that has none.
resolve_ref() {
    local sha
    sha=$(fetch -H 'Accept: application/vnd.github.sha' "https://api.github.com/repos/$REPO/commits/$REF" 2>/dev/null) || sha=""

    if [[ -n $sha ]]; then
        RESOLVED="$sha"
        return 0
    fi

    # An unauthenticated request is rate limited per address, so a busy network can lose the lookup while the download itself is fine.
    # The run continues and says it cannot name its own revision, which is worth a warning rather than a refusal.
    warn "Could not resolve $REF to a commit, so this run cannot be attributed to one"
    RESOLVED=""
    return 0
}

# The paths this script creates under DIR, named in one place so the trap and the download agree.
# DIR itself is never removed, since --dir may name a directory the caller owns and put other things in.
tree_path() { printf '%s\n' "$DIR/tree"; }
archive_path() { printf '%s\n' "$DIR/tree.tar.gz"; }
marker_path() { printf '%s\n' "$DIR/tree/.bootstrap-owned"; }

# A tree carries a marker this loader wrote, and a tree without one is somebody else's.
# DIR is a caller-supplied path, so 'tree' under it is not necessarily ours: pointing --dir at a directory that already holds one would otherwise have this remove it, both before extracting and again on exit.
tree_is_ours() { [[ -e $(marker_path) ]]; }

# Refuses to remove a tree this run did not create, rather than trusting the name.
remove_tree() {
    local tree
    tree=$(tree_path)
    [[ -e $tree ]] || return 0
    tree_is_ours || die "$tree exists and this loader did not create it, so it will not be removed. Choose another --dir."
    rm -rf "$tree"
}

download_tree() {
    local archive tree want="${RESOLVED:-$REF}"
    archive=$(archive_path)

    step "Fetching $REPO at $REF"
    [[ -n $RESOLVED ]] && info "Commit: $RESOLVED"

    mkdir -p "$DIR"
    fetch -o "$archive" "https://codeload.github.com/$REPO/tar.gz/$want" ||
        die "Could not download $REPO at $REF. Check the ref exists and that this host reaches codeload.github.com."

    # The archive holds one top-level directory named for the repository and the revision.
    # Extracting into a directory of our own keeps a second run from reading the first one's tree.
    tree=$(tree_path)
    remove_tree
    mkdir -p "$tree"
    touch "$(marker_path)"
    tar -xzf "$archive" -C "$tree" --strip-components=1 ||
        die "Could not extract the downloaded archive"
    rm -f "$archive"

    TREE="$tree"
    info "Extracted to $TREE"
}

cleanup() {
    [[ $KEEP == true ]] && return 0
    # A tree that is not ours was already refused where it mattered, at the download.
    # Refusing again from the exit trap would print the same error a second time, after the one that actually stopped the run.
    if [[ -e $(tree_path) ]] && ! tree_is_ours; then
        rm -f "$(archive_path)"
        return 0
    fi
    # Removes what this run created rather than what it finished, because TREE is set only once extraction has succeeded.
    # A failed extract leaves both the archive and a part-written tree, so keying the cleanup on TREE left a tarball in the cache on every failed attempt.
    remove_tree
    rm -f "$(archive_path)"
}

# --- Handoff ---

# Every tool runs from inside the fetched tree, and this is the only place a path inside it is named.
run_tool() {
    local tool="$1"
    shift

    local path="$TREE/host-setup/linux/$tool"
    [[ -x $path ]] || die "The fetched tree carries no $tool at host-setup/linux, so this ref is not one to bootstrap from"

    local -a flags=()
    [[ $ASSUME_YES == true ]] && flags+=(--yes)
    [[ $DRY_RUN == true ]] && flags+=(--dry-run)

    "$path" "$@" "${flags[@]}"
}

report() {
    run_tool upgrade-host.sh --status
    run_tool install-tools.sh --report
    run_tool setup-github.sh --status
    # Tolerated rather than fatal, since a missing install is a finding for a report to name and not a reason to stop naming the rest.
    if ! SKILLS_SOURCE_COMMIT="$RESOLVED" run_tool install-skills.sh --report; then
        info "The fleet skills install is missing or stale, and --host or --skills lands it"
    fi
}

# The order is fixed rather than chosen.
# The sudo step comes first, so the one credential it caches covers every step after it.
# Packages come before the tools, so a keyring or a repository is added against a current apt state.
# GitHub comes after the tools, because it is the only step that waits on a person in a browser.
# The skills step comes last, and after the tools in particular, because install-tools.sh provides the interpreter it needs.
stand_up() {
    local profile="$1"

    run_tool install-tools.sh --sudo-timestamp
    run_tool upgrade-host.sh --packages
    if [[ $profile == "dev" ]]; then
        run_tool install-tools.sh --install --optional
    else
        run_tool install-tools.sh --install
    fi
    run_tool setup-github.sh --configure
    SKILLS_SOURCE_COMMIT="$RESOLVED" run_tool install-skills.sh
}

# Names the host in the menu heading.
# Sourcing happens inside the command substitution that calls this, so the variables it defines do not reach the caller.
host_description() {
    [[ -r /etc/os-release ]] || {
        printf 'this host'
        return 0
    }
    # shellcheck disable=SC1091  # A host file, not present at lint time.
    . /etc/os-release
    printf '%s %s' "${ID:-a host}" "${VERSION_ID:-}"
}

menu() {
    log "Standing up $(host_description)"
    log ""
    log "  1  Report only, change nothing"
    log "  2  Upgrade the packages of the current release"
    log "  3  Install the host tools"
    log "  4  Configure git and GitHub"
    log "  5  Install the fleet skills"
    log "  6  Share one sudo credential cache across this user's terminals"
    log "  7  All of the above, which is a host stood up"
    log "  8  All of the above plus the development tools"
    log "  q  Quit"
    log ""

    local choice
    read -r -p "Choose: " choice
    case "$choice" in
    1) MODE="report" ;;
    2) MODE="upgrade" ;;
    3) MODE="tools" ;;
    4) MODE="github" ;;
    5) MODE="skills" ;;
    6) MODE="sudo" ;;
    7) MODE="host" ;;
    8) MODE="dev" ;;
    q | Q) exit 0 ;;
    *) die "Not one of the choices" ;;
    esac
}

# --- Entry ---

parse_args() {
    local -a actions=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
        -r | --report) actions+=(report) ;;
        --host) actions+=(host) ;;
        --dev) actions+=(dev) ;;
        --upgrade) actions+=(upgrade) ;;
        --tools) actions+=(tools) ;;
        --github) actions+=(github) ;;
        --skills) actions+=(skills) ;;
        --sudo) actions+=(sudo) ;;
        --release) actions+=(release) ;;
        -y | --yes) ASSUME_YES=true ;;
        -n | --dry-run) DRY_RUN=true ;;
        --keep) KEEP=true ;;
        --ref)
            [[ $# -ge 2 ]] || die "--ref takes a branch, tag, pull request ref, or commit"
            REF="$2"
            shift
            ;;
        --dir)
            [[ $# -ge 2 ]] || die "--dir takes a path"
            # An absolute path, and never the root, since everything below is created and removed under it.
            [[ $2 == /* ]] || die "--dir takes an absolute path, and \"$2\" is relative"
            [[ $2 != "/" ]] || die "--dir may not be the root directory"
            DIR="${2%/}"
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

    command -v curl >/dev/null ||
        die "curl is required to fetch the tooling. Install it with this host's package manager, then run this again."
    command -v tar >/dev/null || die "tar is required to unpack the tooling"

    # A run with no action and no terminal reports rather than guessing, which is what a pipe into a shell is.
    # The remedy is printed rather than assumed, since somebody reaching this has just pasted a one-line install.
    if [[ -z $MODE ]]; then
        if [[ -t 0 ]]; then
            menu
        else
            MODE="report"
            warn "No action given and no terminal to ask on, so this is a report"
            info "Download the file and run it, rather than piping it, to reach the menu:"
            info "  curl -fsSLo bootstrap.sh https://raw.githubusercontent.com/$REPO/$DEFAULT_REF/host-setup/bootstrap.sh"
            info "  bash bootstrap.sh"
        fi
    fi

    trap cleanup EXIT
    resolve_ref
    download_tree

    case "$MODE" in
    report) report ;;
    upgrade) run_tool upgrade-host.sh --packages ;;
    tools) run_tool install-tools.sh --install ;;
    github) run_tool setup-github.sh --configure ;;
    skills) SKILLS_SOURCE_COMMIT="$RESOLVED" run_tool install-skills.sh ;;
    sudo) run_tool install-tools.sh --sudo-timestamp ;;
    release) run_tool upgrade-host.sh --release ;;
    host) stand_up host ;;
    dev) stand_up dev ;;
    esac

    step "Done"
    [[ $KEEP == true ]] && info "The fetched tree is at $TREE"
    return 0
}

main "$@"
