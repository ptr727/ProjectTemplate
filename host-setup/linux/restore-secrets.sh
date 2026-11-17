#!/bin/bash

# Pulls exactly the buckets it is given, and derives none from the hostname or a file prefix.
# It reaches outside host-setup/, so a copy fetched without the tree has nothing to drive.
# It ends by exec-ing the sync, so a signal reaches that process directly and this script holds nothing to release.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
readonly ROOT

BUCKETS=()
DRY_RUN=false
ASSUME_YES=false

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF2'
Usage: restore-secrets.sh --bucket NAME [--bucket NAME ...] [options]

Pulls the named secret buckets into this user's home directory, by running
scripts/secrets_sync.py pull from this tree. A bucket name takes lowercase letters, digits, and
hyphens, and starts with a letter or digit. Only the buckets named are pulled. The sync prompts
before it replaces a file, and it needs a terminal on stdin to write.

Run it as yourself, never as root or under sudo: the sync refuses root, and the files belong in
your own home. It needs op, which install-tools.sh --install op provides, and Python 3.11 or newer.

Options:
  -b, --bucket NAME Pull this bucket, repeatable
  -n, --dry-run     Print the plan and the op writes, send none
  -y, --yes         Answer the sync's confirmation prompt yes
  -h, --help        Show this help
EOF2
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
        -b | --bucket)
            [[ $# -ge 2 && -n $2 ]] || die "--bucket takes a bucket name"
            [[ $2 =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "--bucket takes lowercase letters, digits, and hyphens that start with a letter or digit, not \"$2\""
            BUCKETS+=("$2")
            shift
            ;;
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
    [[ ${#BUCKETS[@]} -gt 0 ]] || die "Name at least one bucket with --bucket"
}

# The sync refuses root, so this says why before the interpreter and op are looked for.
refuse_root() {
    [[ $EUID -ne 0 ]] || die "Running as root restores into root's home, and the sync refuses root. Run this as the user the files belong to, without sudo."
}

# The first candidate that is Python 3.11 or newer, which the sync needs.
find_python() {
    local candidate
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null &&
            "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

main() {
    parse_args "$@"
    refuse_root

    local sync="$ROOT/scripts/secrets_sync.py"
    [[ -f $sync ]] || die "This tree carries no scripts/secrets_sync.py, so there is nothing to drive"

    local -a args=(pull)
    local bucket
    for bucket in "${BUCKETS[@]}"; do
        args+=(--bucket "$bucket")
    done
    [[ $DRY_RUN == true ]] && args+=(--dry-run)
    [[ $ASSUME_YES == true ]] && args+=(--yes)

    # A dry run on a host that has not installed op yet prints the command rather than failing on the missing tool.
    if [[ $DRY_RUN == true ]] && ! command -v op >/dev/null; then
        printf 'Would run the sync: %s %s\n' "$sync" "${args[*]}"
        return 0
    fi

    command -v op >/dev/null || die "op is not installed. install-tools.sh --install op provides it, so run that first."
    local py
    py=$(find_python) || die "Python 3.11 or newer not found, and the sync needs one."

    exec "$py" "$sync" "${args[@]}"
}

main "$@"
