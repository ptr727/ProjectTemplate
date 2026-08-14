#!/bin/bash

# Installs the fleet skills for the current user, by driving the hub-hosted installer in the tree this script sits in.
# This is the one script here that reaches outside host-setup/, deliberately: the skills content lives at the tree root, so a copy fetched without the tree has nothing to install, and the independent-fetchability rule the sibling scripts follow cannot apply to it.
# Python is its one dependency, which is why the bootstrap runs it last: install-tools.sh provides the interpreter before this needs one.
#
# Every step is idempotent, because the installer it drives is.
# SKILLS_SOURCE_COMMIT, where the caller sets it, names the commit of a tree git cannot answer for, which is what a tarball fetched by the bootstrap is.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
readonly ROOT

MODE="install"
DRY_RUN=false

log() { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: install-skills.sh [options]

Installs the fleet skills for the current user, by running scripts/skills_install.py from this
tree. The installer copies the skills to ~/.agents/skills/ for Codex and opencode, registers the
Claude Code plugin where the claude CLI is present and says so where it is not, and stamps what it
installed so a later report can answer whether this machine is current.

Actions, name one, default install:
  -r, --report      Read-only: is this machine's skills install current against this tree?
  -h, --help        Show this help

Options:
  -n, --dry-run     Print the command instead of running it
  -y, --yes         Accepted for symmetry with the sibling scripts, since the installer never prompts
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -r | --report) MODE="report" ;;
            -n | --dry-run) DRY_RUN=true ;;
            -y | --yes) ;;
            -h | --help)
                usage
                exit 0
                ;;
            *) die "Unknown option \"$1\", --help lists the options" ;;
        esac
        shift
    done
}

# The first candidate that is a Python 3.7+, since the installer needs `from __future__ import annotations`.
find_python() {
    local candidate
    for candidate in python3 python; do
        if command -v "$candidate" > /dev/null &&
            "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)' 2> /dev/null; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

main() {
    parse_args "$@"

    local installer="$ROOT/scripts/skills_install.py"
    [[ -f $installer ]] || die "This tree carries no scripts/skills_install.py, so there is nothing to drive"

    local py
    py=$(find_python) || die "Python 3.7+ not found. install-tools.sh provides it, so run the tools step first and this one after."

    local -a args=()
    [[ $MODE == "report" ]] && args+=(--report)

    if [[ $DRY_RUN == true ]]; then
        info "[dry run] $py $installer ${args[*]}"
        return 0
    fi

    exec "$py" "$installer" "${args[@]}"
}

main "$@"
