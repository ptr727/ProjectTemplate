#!/usr/bin/env bash

# Stands a host up from nothing, by fetching this repository and handing control to the host tooling inside it.
# It is the one file fetched on its own, because a host with no git and no checkout is what it exists to fix.
# It reads no payload, no table, and no sibling module: it obtains a tree and runs one entry point inside that tree.
#
# A tarball rather than a clone, because a clone needs git on a host that may not have it, and because a tarball of a resolved commit cannot be stale.
# The commit it resolved is printed before anything runs, so a run says which revision of the fleet's tooling it used.
#
# A run that installs the skills keeps its tree rather than removing it, because the Claude Code marketplace it registers loads that directory in place.
# That tree lives under the data directory rather than the cache, is replaced whole by the next such run, and is never a git checkout, so the bootstrap still needs no git and modifies no checkout of anyone's.

set -Eeuo pipefail

readonly REPO="ptr727/ProjectTemplate"
readonly DEFAULT_REF="main"

MODE=""
REF="$DEFAULT_REF"
DIR=""
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
      --dir PATH    Where the tree is extracted, default ${XDG_CACHE_HOME:-~/.cache}/host-setup, or
                    ${XDG_DATA_HOME:-~/.local/share}/host-setup for --host, --dev, --skills without --dry-run
      --keep        Leave the extracted tree in place, which is removed by default

--host, --dev, and --skills keep their tree, at skills-tree under that directory, because the
Claude Code plugin they register loads it in place. The next such run replaces it, and a --dry-run
leaves it.

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
    # This fallback is deliberate and gates no mutation: download_tree falls back to fetching $REF by name when RESOLVED is empty, exactly as it would if resolve_ref did not exist, and it has its own die on a real download failure.
    warn "Could not resolve $REF to a commit, so this run cannot be attributed to one"
    RESOLVED=""
    return 0
}

# The actions that install the skills register a Claude Code marketplace that loads their tree in place, so that tree is kept.
# A dry run changes nothing, so it takes a transient tree like any other action rather than replacing the one the plugin loads.
keeps_tree() {
    [[ $DRY_RUN != true ]] || return 1
    [[ $MODE == "host" || $MODE == "dev" || $MODE == "skills" ]]
}

# A kept tree is data rather than cache, since removing it silently breaks the plugin registered against it.
default_dir() {
    if keeps_tree; then
        printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/host-setup"
    else
        printf '%s\n' "${XDG_CACHE_HOME:-$HOME/.cache}/host-setup"
    fi
}

# The paths this script creates under DIR, named in one place so the trap and the download agree.
# DIR itself is never removed, since --dir may name a directory the caller owns and put other things in.
# A kept tree and a transient one take different names, so a report run sharing a --dir with a host run never removes the tree Claude Code loads.
tree_name() {
    if keeps_tree; then printf 'skills-tree'; else printf 'tree'; fi
}
tree_path() { printf '%s\n' "$DIR/$(tree_name)"; }
staging_path() { printf '%s\n' "$DIR/$(tree_name).new"; }
retired_path() { printf '%s\n' "$DIR/$(tree_name).old"; }
archive_path() { printf '%s\n' "$DIR/$(tree_name).tar.gz"; }

# A tree carries a marker this loader wrote, and a tree without one is somebody else's.
# DIR is a caller-supplied path, so a tree under it is not necessarily ours: pointing --dir at a directory that already holds one would otherwise have this remove it, both before extracting and again on exit.
is_ours() { [[ ! -L $1 && -e $1/.bootstrap-owned ]]; }

exists() { [[ -e $1 || -L $1 ]]; }

# Removes the ownership marker last, so a removal that stops part way leaves a directory the next run still recognizes as its own.
remove_tree() {
    # Returned on explicitly rather than left to set -e, which a caller testing the result suspends.
    find "$1" -mindepth 1 -maxdepth 1 ! -name .bootstrap-owned -exec rm -rf {} + || return
    rm -rf "$1"
}

# Refuses to remove a tree this run did not create, rather than trusting the name.
remove_owned() {
    local path="$1"
    exists "$path" || return 0
    is_ours "$path" || die "$path exists and this loader did not create it, so it will not be removed. Choose another --dir."
    remove_tree "$path"
}

download_tree() {
    local archive staging want="${RESOLVED:-$REF}"
    archive=$(archive_path)

    step "Fetching $REPO at $REF"
    [[ -n $RESOLVED ]] && info "Commit: $RESOLVED"

    mkdir -p "$DIR"
    fetch -o "$archive" "https://codeload.github.com/$REPO/tar.gz/$want" ||
        die "Could not download $REPO at $REF. Check the ref exists and that this host reaches codeload.github.com."

    # The archive holds one top-level directory named for the repository and the revision.
    # It is extracted beside the tree rather than over it, so a failed download or extract leaves a kept tree, and the plugin loading it, as they were.
    staging=$(staging_path)
    remove_owned "$staging"
    mkdir -p "$staging"
    touch "$staging/.bootstrap-owned"
    tar -xzf "$archive" -C "$staging" --strip-components=1 ||
        die "Could not extract the downloaded archive"
    rm -f "$archive"
    # The commit a later report reads for this tree, since a tarball has no .git to answer for it.
    [[ -n $RESOLVED ]] && printf '%s\n' "$RESOLVED" >"$staging/.bootstrap-commit"

    TREE="$staging"
    # A kept tree is swapped in by the skills step itself, so a stand-up failing before it leaves the plugin loading what it loaded before.
    keeps_tree || swap_in
    return 0
}

# Moves the old tree aside before the new one takes its name, and removes it only after, so no failure part way leaves the name empty or half-deleted.
swap_in() {
    local staging tree retired moved=false
    staging=$(staging_path)
    tree=$(tree_path)
    retired=$(retired_path)

    if exists "$tree"; then
        is_ours "$tree" || die "$tree exists and this loader did not create it, so it will not be replaced. Choose another --dir."
        remove_owned "$retired"
        mv "$tree" "$retired"
        moved=true
    fi
    if ! mv "$staging" "$tree"; then
        if [[ $moved == true ]] && ! mv "$retired" "$tree"; then
            die "Could not move the extracted tree into place at $tree, and could not put the previous tree back from $retired"
        fi
        die "Could not move the extracted tree into place at $tree"
    fi
    if is_ours "$retired"; then
        remove_tree "$retired" || warn "Could not remove the previous tree at $retired, and a later run removes it"
    fi

    TREE="$tree"
    info "Extracted to $TREE"
}

cleanup() {
    # Removes each path this run can create by its fixed name rather than through TREE, since a failed extract leaves an archive and a part-written staging tree before TREE names anything.
    rm -f "$(archive_path)"
    # A path that is not ours was already refused where it mattered, at the download.
    # Refusing again from the exit trap would print the same error a second time, after the one that actually stopped the run.
    # A swap stopped between its two renames leaves the old tree aside and nothing at the name, so the old tree goes back rather than away.
    if is_ours "$(retired_path)"; then
        if exists "$(tree_path)"; then
            remove_tree "$(retired_path)" 2>/dev/null || :
        elif ! mv "$(retired_path)" "$(tree_path)"; then
            keeps_tree && warn "Could not put the previous tree back from $(retired_path), so move it to $(tree_path) by hand"
        fi
    fi
    if is_ours "$(staging_path)"; then
        remove_tree "$(staging_path)" || warn "Could not remove the extracted tree at $(staging_path), and a later run removes it"
    fi
    [[ $KEEP == true ]] && return 0
    keeps_tree && return 0
    is_ours "$(tree_path)" && remove_tree "$(tree_path)"
    return 0
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
        info "The fleet skills copy is not current, and the report's reason says why: --host or --skills lands a missing or stale one, and re-installing does not settle one that cannot be judged"
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
    install_skills
}

# The installer registers the directory it runs from, so the kept tree has to hold its name before it runs.
install_skills() {
    keeps_tree && swap_in
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

    # Resolved only now, since the default depends on the action, which the menu may have just chosen.
    [[ -n $DIR ]] || DIR=$(default_dir)

    trap cleanup EXIT
    resolve_ref
    download_tree

    case "$MODE" in
    report) report ;;
    upgrade) run_tool upgrade-host.sh --packages ;;
    tools) run_tool install-tools.sh --install ;;
    github) run_tool setup-github.sh --configure ;;
    skills) install_skills ;;
    sudo) run_tool install-tools.sh --sudo-timestamp ;;
    release) run_tool upgrade-host.sh --release ;;
    host) stand_up host ;;
    dev) stand_up dev ;;
    esac

    step "Done"
    if keeps_tree; then
        info "The fleet skills tree is kept at $TREE"
    elif [[ $KEEP == true ]]; then
        info "The fetched tree is at $TREE"
    fi
    return 0
}

main "$@"
