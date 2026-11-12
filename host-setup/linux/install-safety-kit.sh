#!/bin/bash

# Installs the Claude Code safety kit for the current user, by driving the installer in the tree this script sits in.
# Like install-skills.sh it reaches outside host-setup/linux/, deliberately, since the kit lives beside the other agent-safety content.
# A host without Claude Code is skipped with a line saying so, because installing would create ~/.claude for an agent nobody installed.
# The kit installer takes no dry-run flag, so a dry run here prints the command and runs nothing.
#
# AGENT_SAFETY_SOURCE_COMMIT, where the caller sets it, names the commit of a tree git cannot answer for.
# A tarball fetched by the bootstrap is such a tree.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
readonly ROOT

MODE="install"
DRY_RUN=false

info() { printf '  %s\n' "$*"; }
die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF2'
Usage: install-safety-kit.sh [options]

Installs the Claude Code safety kit for the current user: the write guard, the model tier guard,
the stray-process sweep, the containment prefix, and the host-wide rules in ~/.claude/CLAUDE.md. It
runs host-setup/agent-safety/claude/install.sh from this tree and does nothing where Claude Code
is not installed for this user.

Run it as yourself, never under sudo: it writes only under your own home.

Actions, name one, default install:
  -r, --report      Read-only: does the installed kit match this tree?
  -h, --help        Show this help

Options:
  -n, --dry-run     Print the command instead of running it
  -y, --yes         Accepted for symmetry with the sibling scripts, since the installer never prompts
EOF2
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

# Under sudo the kit would land in root's home and read root's PATH, as install-skills.sh states for the skills.
refuse_sudo() {
    [[ $EUID -eq 0 && -n ${SUDO_USER:-} && $SUDO_USER != "root" ]] || return 0
    die "Running under sudo installs the kit for root rather than for $SUDO_USER. This script needs no elevation. Re-run it as $SUDO_USER without sudo."
}

claude_present() {
    command -v claude >/dev/null || [[ -d ${CLAUDE_HOME:-$HOME/.claude} ]]
}

main() {
    parse_args "$@"
    refuse_sudo

    local installer="$ROOT/host-setup/agent-safety/claude/install.sh"
    [[ -f $installer ]] || die "This tree carries no host-setup/agent-safety/claude/install.sh, so there is nothing to drive"

    if ! claude_present; then
        info "Claude Code is not installed for this user, so the safety kit is skipped"
        return 0
    fi

    local -a args=()
    [[ $MODE == "report" ]] && args+=(--report)

    # A report is read-only already, so it runs for real under --dry-run.
    if [[ $DRY_RUN == true && $MODE != "report" ]]; then
        info "[dry run] $installer ${args[*]}"
        return 0
    fi

    exec bash "$installer" "${args[@]}"
}

main "$@"
