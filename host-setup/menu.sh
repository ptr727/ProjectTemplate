#!/usr/bin/env bash

# A human-facing front end over the scripts this fleet otherwise authors for an agent following instructions: the host tooling in host-setup/linux/, and the repo-level tools in scripts/ and spec/ that ptr727/ProjectTemplate hosts and every other repo reaches rather than carries.
# Menu options rather than a command a human has to already know, and a forcing function on the tools it fronts: a task with no discoverable menu entry is a gap in the tools themselves.
#
# Fetchable on its own, like bootstrap.sh: run from a hub checkout directly, or curl this one file into a downstream repo and it clones the hub itself.
# Where bootstrap.sh stands a host up and stops, this loops so a human answers more than one question in a sitting, and it knows the difference between "the hub" and "a repo this host happens to be sitting in" so it can offer each their own tasks.

set -Eeuo pipefail

readonly HUB_REPO="ptr727/ProjectTemplate"
readonly HUB_URL="https://github.com/$HUB_REPO"
readonly DEFAULT_REF="main"

REF="$DEFAULT_REF"
DIR="${XDG_CACHE_HOME:-$HOME/.cache}/host-setup"
KEEP=false
DRY_RUN=false
ASSUME_YES=false

HUB_ROOT=""
HUB_FETCHED=false
DOWNSTREAM_ROOT=""
DOWNSTREAM_NAME=""

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
Usage: menu.sh [options]

An interactive menu over this fleet's host and repo tooling: update the host tools, upgrade the
OS, install the fleet skills, audit a cataloged repo, and pull the hub's verbatim-owned files into
a downstream repo's own worktree. Run from a hub checkout or from any other repo; the menu shows
each the tasks that apply to it.

Options:
  -y, --yes         Do not prompt, and pass the same to each tool this menu runs
  -n, --dry-run     Print what each step would run, change nothing
      --ref REF     Hub branch, tag, pull request ref, or commit to run from, default main
      --dir PATH    Where a fetched hub checkout is cloned, default ${XDG_CACHE_HOME:-~/.cache}/host-setup
      --keep        Leave a fetched hub checkout in place, which is removed by default
  -h, --help        Show this help

With no terminal to ask on, this prints the same reminder bootstrap.sh does and exits, since a
pipe is not a place to answer a menu.
EOF
}

# --- Hub resolution ---

# The owner/name from a remote.origin.url in any of the shapes git or the GitHub UI hand out, or empty where the checkout carries no origin at all (a fresh init, or a worktree add mid-flight).
origin_slug() {
    local root="$1" url slug
    url=$(git -C "$root" config --get remote.origin.url 2>/dev/null) || return 0
    slug="${url#git@github.com:}"
    slug="${slug#ssh://git@github.com/}"
    slug="${slug#https://github.com/}"
    slug="${slug%.git}"
    printf '%s\n' "$slug"
}

# A marker sitting beside the clone rather than inside it, the same convention bootstrap.sh's tree_is_ours uses, so a --dir pointed at a directory this run does not own is never the one removed on exit.
# Inside the clone it would be an untracked file, and carry.py's own hub-is-clean check would then refuse the tree this run just fetched for it.
marker_path() { printf '%s\n' "$DIR/hub.owned"; }

fetch_hub() {
    step "Fetching $HUB_REPO at $REF"
    mkdir -p "$DIR"
    [[ -e "$DIR/hub" ]] && rm -rf "$DIR/hub"
    # A full clone rather than --depth 1: spec/audit.py walks the hub's own history to judge whether a carried copy is trailing the file it was copied from, and a shallow clone would read every file as changed at the truncation boundary and misreport every repo as stale.
    git clone --quiet --branch "$REF" --single-branch "$HUB_URL" "$DIR/hub" ||
        die "Could not clone $HUB_REPO at $REF. Check the ref exists and that this host reaches github.com."
    touch "$(marker_path)"
    HUB_ROOT="$DIR/hub"
    HUB_FETCHED=true
    info "Cloned to $HUB_ROOT"
}

# A checkout already sitting on the hub is used as is, so a maintainer working in their own ProjectTemplate tree never pays for a second clone of the repo they are standing in.
detect_hub_root() {
    local top
    top=$(git rev-parse --show-toplevel 2>/dev/null) || return 0
    [[ $(origin_slug "$top") == "$HUB_REPO" ]] || return 0
    HUB_ROOT="$top"
}

ensure_hub_root() {
    [[ -n $HUB_ROOT ]] || fetch_hub
}

# A downstream repo is whatever git repo the menu is run from, when that repo is not the hub itself.
# It stays unset from inside the hub or off a checkout entirely, and the downstream section of the menu is what reads that.
detect_downstream_root() {
    local top
    top=$(git rev-parse --show-toplevel 2>/dev/null) || return 0
    [[ -n $HUB_ROOT && $top == "$HUB_ROOT" ]] && return 0
    DOWNSTREAM_ROOT="$top"
    DOWNSTREAM_NAME=$(basename "$(origin_slug "$top")")
}

cleanup() {
    [[ $KEEP == true || $HUB_FETCHED == false ]] && return 0
    [[ -e "$(marker_path)" ]] || return 0
    rm -rf "$DIR/hub" "$(marker_path)"
}

# --- Running a tool ---

# Every host tool runs from inside the hub tree, and this is the only place a path inside it is named, matching bootstrap.sh's run_tool.
host_tool() {
    local tool="$1"
    shift
    local path="$HUB_ROOT/host-setup/linux/$tool"
    [[ -x $path ]] || die "$HUB_ROOT carries no $tool at host-setup/linux, so this ref is not one to run tasks from"

    local -a flags=()
    [[ $ASSUME_YES == true ]] && flags+=(--yes)
    [[ $DRY_RUN == true ]] && flags+=(--dry-run)
    "$path" "$@" "${flags[@]}"
}

# The Python tools under scripts/ and spec/ resolve their own root from __file__ rather than the working directory, so they are called by absolute path from wherever this script runs and need no cd.
# Checked here rather than upfront in main, the same reasoning host-setup/linux/install-skills.sh already carries: a host with no interpreter yet can still use every host action, and only the actions that need one name it as their own prerequisite.
hub_python() {
    command -v python3 >/dev/null || die "python3 is required for this task; host-setup/linux/install-tools.sh provides it"
    local script="$1"
    shift
    python3 "$HUB_ROOT/$script" "$@"
}

# --- Actions ---

audit_repo() {
    local default="${DOWNSTREAM_NAME:-$HUB_REPO}"
    default="${default##*/}"
    local name
    read -r -p "Repo to audit [$default]: " name
    name="${name:-$default}"
    ensure_hub_root
    hub_python spec/audit.py "$name"
}

check_skills_dist() {
    ensure_hub_root
    if hub_python scripts/build_dist.py --check; then
        info "Every generated Skills distribution matches .agents/skills/"
    else
        info "A generated Skills distribution is stale; this menu does not regenerate it from a fetched checkout, since the result has to be committed in the hub itself"
    fi
}

carry_action() {
    local mode="$1"
    [[ -n $DOWNSTREAM_ROOT ]] ||
        die "No downstream repo checkout found; run this menu from inside the target repo's own worktree"
    local default="$DOWNSTREAM_NAME"
    local name
    read -r -p "Repo name as cataloged in registry/repos.json [$default]: " name
    name="${name:-$default}"
    ensure_hub_root
    hub_python scripts/carry.py "$mode" "$name" --target "$DOWNSTREAM_ROOT"
}

# --- Menu ---

menu_heading() {
    log "Hub:        $HUB_REPO${HUB_ROOT:+ ($HUB_ROOT)}"
    if [[ -n $DOWNSTREAM_ROOT ]]; then
        log "Downstream: $DOWNSTREAM_NAME ($DOWNSTREAM_ROOT)"
    else
        log "Downstream: none (run from inside a repo's own checkout for the pull-from-hub tasks)"
    fi
}

print_menu() {
    log ""
    menu_heading
    log ""
    log "Host, on this machine:"
    log "   1  Report installed host tools"
    log "   2  Install missing host tools"
    log "   3  Upgrade the host tools that trail upstream"
    log "   4  Report the host OS upgrade status"
    log "   5  Upgrade the host OS packages"
    log "   6  Report git and GitHub setup"
    log "   7  Configure git and GitHub"
    log "   8  Report fleet Skills install status"
    log "   9  Install or update the fleet Skills"
    log ""
    log "Hub, ptr727/ProjectTemplate:"
    log "  10  Audit a cataloged repo"
    log "  11  Check the generated Skills distributions are current"
    log ""
    log "Downstream, the repo this menu is run from:"
    log "  12  Check what the hub would change here, change nothing"
    log "  13  Pull the hub's verbatim-owned files into this repo"
    log ""
    log "   q  Quit"
    log ""
}

# A failing task (a real install error, a network hiccup) is reported and returns to the menu rather than ending the session, so dispatch's own exit status cannot double as "quit": QUIT is a separate flag the q/Q case sets, read by the loop after every dispatch regardless of whether the task it ran succeeded.
QUIT=false

dispatch() {
    case "$1" in
    1) host_tool install-tools.sh --report ;;
    2) host_tool install-tools.sh --install ;;
    3) host_tool install-tools.sh --upgrade ;;
    4) host_tool upgrade-host.sh --status ;;
    5) host_tool upgrade-host.sh --packages ;;
    6) host_tool setup-github.sh --status ;;
    7) host_tool setup-github.sh --configure ;;
    8) host_tool install-skills.sh --report ;;
    9) host_tool install-skills.sh ;;
    10) audit_repo ;;
    11) check_skills_dist ;;
    12) carry_action check ;;
    13) carry_action apply ;;
    q | Q) QUIT=true ;;
    *)
        warn "Not one of the choices"
        return 2
        ;;
    esac
}

interactive_menu() {
    local choice rc
    while true; do
        print_menu
        read -r -p "Choose: " choice
        QUIT=false
        rc=0
        dispatch "$choice" || rc=$?
        [[ $QUIT == true ]] && break
        # An unrecognized choice is rc 2, already warned by dispatch, so this loops straight back rather than reading a pointless confirmation.
        ((rc == 2)) && continue
        if ((rc == 0)); then
            step "Done"
        else
            warn "That task ended with an error"
        fi
        read -r -p "Press Enter to return to the menu... " _
    done
}

# --- Entry ---

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
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
}

main() {
    parse_args "$@"

    if [[ ! -t 0 ]]; then
        warn "No terminal to ask on, so there is no menu to show"
        info "Download the file and run it from a terminal:"
        info "  curl -fsSLo menu.sh https://raw.githubusercontent.com/$HUB_REPO/$DEFAULT_REF/host-setup/menu.sh"
        info "  bash menu.sh"
        exit 0
    fi

    command -v git >/dev/null || die "git is required"

    trap cleanup EXIT
    detect_hub_root
    detect_downstream_root
    interactive_menu
}

main "$@"
