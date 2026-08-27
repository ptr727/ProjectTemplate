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
# Reports a task-time error without ending the process, unlike die: a dispatched action's failure returns to the menu, and only a startup failure (bad arguments, no git) is fatal.
fail() { printf 'ERROR: %s\n' "$*" >&2; }

usage() {
    cat <<'EOF'
Usage: menu.sh [options]

An interactive menu over this fleet's host and repo tooling: update the host tools, upgrade the
OS, install the fleet skills, audit a cataloged repo, and pull the hub's verbatim-owned files into
a downstream repo's own worktree. Run from a hub checkout or from any other repo. The menu shows
each the tasks that apply to it.

Options:
  -y, --yes         Pass --yes to each tool this menu runs, so a tool does not prompt. The menu's
                    own choice, confirmation, and repo-name prompts still ask.
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

# Refuses to remove an existing $DIR/hub this run did not create, rather than trusting the name, mirroring bootstrap.sh's own remove_tree.
remove_unowned_hub_check() {
    [[ -e "$DIR/hub" || -L "$DIR/hub" ]] || return 0
    [[ -e "$(marker_path)" ]] && return 0
    fail "$DIR/hub exists and this run did not create it, so it will not be removed. Pass --dir to choose another cache location."
    return 1
}

fetch_hub() {
    # --dry-run promises to change nothing, and fetching is the one real change this whole script makes to the host.
    [[ $DRY_RUN == true ]] && {
        fail "This task needs a fetched hub checkout, and fetching one is itself a change --dry-run does not make. Run without --dry-run, or from inside a hub checkout already on $DEFAULT_REF."
        return 1
    }
    step "Fetching $HUB_REPO at $REF"
    mkdir -p "$DIR"
    remove_unowned_hub_check || return 1
    rm -rf "$DIR/hub"
    # A full clone of the default branch first, whatever $REF names: spec/audit.py walks the hub's own history to judge whether a carried copy is trailing the file it was copied from, and a shallow clone would read every file as changed at the truncation boundary and misreport every repo as stale.
    git clone --quiet --branch "$DEFAULT_REF" --single-branch "$HUB_URL" "$DIR/hub" ||
        {
            fail "Could not clone $HUB_REPO. Check that this host reaches github.com."
            return 1
        }
    # Marked as ours the moment the clone lands rather than only once every later step also succeeds, so a failure below still leaves a tree remove_unowned_hub_check will clean up on the next run instead of blocking every retry as somebody else's.
    touch "$(marker_path)"
    # A branch name is already checked out by the clone above.
    # A tag, a pull request ref, or a commit needs an explicit fetch and checkout, since "git clone --branch" only takes a branch or a tag, not an arbitrary commit.
    if [[ $REF != "$DEFAULT_REF" ]]; then
        git -C "$DIR/hub" fetch --quiet origin "$REF" ||
            {
                fail "Could not fetch $REF from $HUB_REPO. Check the ref exists."
                return 1
            }
        git -C "$DIR/hub" checkout --quiet FETCH_HEAD ||
            {
                fail "Could not check out $REF"
                return 1
            }
    fi
    HUB_ROOT="$DIR/hub"
    HUB_FETCHED=true
    info "Cloned to $HUB_ROOT"
}

# Whether the current checkout is the hub, by origin identity alone, independent of --ref or of whether that checkout is fresh enough to reuse.
# Read by detect_downstream_root so the hub is never misclassified as a downstream repo, whatever ref was asked for.
IS_HUB_CHECKOUT=false

# A checkout already sitting on the hub is a candidate to reuse as is, so a maintainer working in their own ProjectTemplate tree never pays for a second clone of the repo they are standing in.
# Only for the default ref: naming any other --ref always fetches fresh, even from inside the hub itself, since AUDIT.md and the sync procedure both rely on this loader reaching a ref other than whatever happens to be checked out locally.
# HUB_ROOT set here is tentative, verified against a freshly fetched origin/main by ensure_hub_root before any tool actually reads it, since a candidate this stale is exactly the wrong answer for an audit or a Skills-distribution check.
detect_hub_root() {
    local top
    top=$(git rev-parse --show-toplevel 2>/dev/null) || return 0
    [[ $(origin_slug "$top") == "$HUB_REPO" ]] || return 0
    IS_HUB_CHECKOUT=true
    [[ $REF == "$DEFAULT_REF" ]] && HUB_ROOT="$top"
    return 0
}

# Confirms a tentative local HUB_ROOT still matches a clean, freshly fetched origin/main before any tool reads it, checked here rather than at startup so opening the menu costs no network call until a hub-dependent task actually runs.
# A local checkout that has moved on (a feature branch, a commit behind, an uncommitted edit) falls back to a real fetch rather than being trusted, the same freshness and cleanliness carry.py's own verify_hub already requires of its own hub argument.
ensure_hub_root() {
    # The freshness check below itself fetches, which updates FETCH_HEAD and the remote-tracking ref even though it touches no working file, so it is as much a change as fetch_hub's own clone and is refused for the same reason.
    [[ $DRY_RUN == true ]] && {
        fail "This task needs to confirm the hub checkout is fresh, and confirming it means fetching, which --dry-run does not do. Run without --dry-run."
        return 1
    }
    if [[ -z $HUB_ROOT ]]; then
        fetch_hub
        return
    fi
    if git -C "$HUB_ROOT" fetch --quiet origin "$DEFAULT_REF" &&
        [[ -z $(git -C "$HUB_ROOT" status --porcelain) ]] &&
        [[ $(git -C "$HUB_ROOT" rev-parse HEAD) == "$(git -C "$HUB_ROOT" rev-parse "origin/$DEFAULT_REF")" ]]; then
        return 0
    fi
    HUB_ROOT=""
    fetch_hub
}

# A downstream repo is whatever git repo the menu is run from, when that repo is not the hub itself.
# It stays unset from inside the hub or off a checkout entirely, and the downstream section of the menu is what reads that.
# Gated on IS_HUB_CHECKOUT rather than HUB_ROOT: the hub is never a downstream repo, even when --ref left HUB_ROOT unset.
detect_downstream_root() {
    [[ $IS_HUB_CHECKOUT == true ]] && return 0
    local top
    top=$(git rev-parse --show-toplevel 2>/dev/null) || return 0
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
# Resolves the hub root itself rather than assuming a caller already did: a host task must work standalone, off a downstream checkout, or off no checkout at all, none of which set HUB_ROOT on their own.
host_tool() {
    local tool="$1"
    shift
    ensure_hub_root || return 1
    local path="$HUB_ROOT/host-setup/linux/$tool"
    [[ -x $path ]] || {
        fail "$HUB_ROOT carries no $tool at host-setup/linux, so this ref is not one to run tasks from"
        return 1
    }

    local -a flags=()
    [[ $ASSUME_YES == true ]] && flags+=(--yes)
    [[ $DRY_RUN == true ]] && flags+=(--dry-run)
    "$path" "$@" "${flags[@]}"
}

# The Python tools under scripts/ and spec/ resolve their own root from __file__ rather than the working directory, so they are called by absolute path from wherever this script runs and need no cd.
# Checked here rather than upfront in main, the same reasoning host-setup/linux/install-skills.sh already carries: a host with no interpreter yet can still use every host action, and only the actions that need one name it as their own prerequisite.
# The prerequisite failure returns 127, bash's own "command not found" convention, so a caller reading a specific exit code from the tool itself (build_dist.py's 0-clean/1-stale contract) can tell "python3 never ran" apart from "python3 ran and returned 1".
hub_python() {
    command -v python3 >/dev/null || {
        fail "python3 is required for this task. host-setup/linux/install-tools.sh provides it."
        return 127
    }
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
    ensure_hub_root || return 1
    hub_python spec/audit.py "$name"
}

check_skills_dist() {
    ensure_hub_root || return 1
    local rc=0
    hub_python scripts/build_dist.py --check || rc=$?
    # Only 0 (clean) and 1 (stale) are outcomes scripts/build_dist.py --check documents for itself, so only those two read as a check result.
    # Anything else, 127 included, is hub_python or the tool itself failing to run rather than a finding, and is reported as the task error it is.
    case "$rc" in
    0) info "Every generated Skills distribution matches .agents/skills/" ;;
    1) info "A generated Skills distribution is stale. This menu does not regenerate it from a fetched checkout, since the result has to be committed in the hub itself." ;;
    *)
        fail "scripts/build_dist.py --check did not run to completion (exit $rc)"
        return 1
        ;;
    esac
}

carry_action() {
    local mode="$1"
    [[ -n $DOWNSTREAM_ROOT ]] ||
        {
            fail "No downstream repo checkout found. Run this menu from inside the target repo's own worktree."
            return 1
        }
    # Its hub argument must be exactly on origin/main by carry.py's own requirement, and a non-default --ref checks out something else entirely, so this would always fail deep inside carry.py with no clue why.
    # Refused here instead, with the actual reason.
    [[ $REF == "$DEFAULT_REF" ]] ||
        {
            fail "Pulling hub files needs the hub's $DEFAULT_REF branch, and this session was started with --ref $REF. Run without --ref, or start a separate session on $DEFAULT_REF for this task."
            return 1
        }
    local default="$DOWNSTREAM_NAME"
    local name
    read -r -p "Repo name as cataloged in registry/repos.json [$default]: " name
    name="${name:-$default}"
    ensure_hub_root || return 1
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
    # Also gated on REF: carry.py always rejects a hub checkout that is not exactly on the default ref, so these tasks cannot work in a non-default --ref session regardless of a downstream repo being detected.
    if [[ -n $DOWNSTREAM_ROOT && $REF == "$DEFAULT_REF" ]]; then
        log ""
        log "Downstream, the repo this menu is run from:"
        log "  12  Check what the hub would change here, change nothing"
        log "  13  Pull the hub's verbatim-owned files into this repo"
    fi
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
    # --dry-run changes nothing, and carry.py itself has no dry-run mode, so a dry-run apply reads as its own check instead of silently mutating the downstream worktree.
    13)
        if [[ $DRY_RUN == true ]]; then
            carry_action check
        else
            carry_action apply
        fi
        ;;
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
            # Canonicalized before the root check, since a literal "/tmp/.." is not the string "/" but resolves to it the moment anything below opens a path under it.
            local canonical
            canonical=$(readlink -m -- "$2") || die "--dir could not be resolved: \"$2\""
            [[ $canonical != "/" ]] || die "--dir may not be the root directory"
            DIR="$canonical"
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
