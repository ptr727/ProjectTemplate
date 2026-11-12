#!/bin/bash

# Writes the host-wide agent rules to Codex's and opencode's global instruction files, by driving global_rules.py in the tree this script sits in.
# The rules are the ones the Claude Code safety kit renders into ~/.claude/CLAUDE.md.
# Python 3 is its one dependency, which is why the bootstrap runs it after the tools step.
# An agent that is not installed for this user is skipped by the writer itself, on any Python 3.
# The writer asks for 3.11 only once an agent is present, and says so then.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
readonly ROOT

MODE="install"
DRY_RUN=false

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF2'
Usage: install-global-rules.sh [options]

Writes the fleet's host-wide agent rules to the global instruction file of each of Codex and
opencode that is installed for this user, by running host-setup/agent-safety/global_rules.py from
this tree. It writes rules and registers no hook.

Run it as yourself, never under sudo: it writes only under your own home.

Actions, name one, default install:
  -r, --report      Read-only: is each file current?
  -h, --help        Show this help

Options:
  -n, --dry-run     Say what would be written, change nothing
  -y, --yes         Accepted for symmetry with the sibling scripts, since the writer never prompts
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

# Under sudo the files would land in root's home, as install-skills.sh states for the skills.
refuse_sudo() {
    [[ $EUID -eq 0 && -n ${SUDO_USER:-} && $SUDO_USER != "root" ]] || return 0
    die "Running under sudo writes the rules for root rather than for $SUDO_USER. This script needs no elevation. Re-run it as $SUDO_USER without sudo."
}

# The first candidate that is a Python 3.11 or newer, which the writer needs where an agent is present.
# Where none is, the first Python 3 runs.
# A host with no agent still works, and a host with one is told the 3.11 need.
find_python() {
    local candidate floor
    for floor in 11 0; do
        for candidate in python3 python; do
            if command -v "$candidate" >/dev/null &&
                "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info[0] == 3 and sys.version_info[1] >= $floor else 1)" 2>/dev/null; then
                printf '%s' "$candidate"
                return 0
            fi
        done
    done
    return 1
}

main() {
    parse_args "$@"
    refuse_sudo

    local writer="$ROOT/host-setup/agent-safety/global_rules.py"
    [[ -f $writer ]] || die "This tree carries no host-setup/agent-safety/global_rules.py, so there is nothing to drive"

    local py
    py=$(find_python) || die "Python 3 not found. install-tools.sh provides it, so run the tools step first and this one after."

    local -a args=()
    [[ $MODE == "report" ]] && args+=(--report)
    # A report is read-only already, and the writer refuses the two flags together.
    [[ $DRY_RUN == true && $MODE != "report" ]] && args+=(--dry-run)

    exec "$py" "$writer" "${args[@]}"
}

main "$@"
