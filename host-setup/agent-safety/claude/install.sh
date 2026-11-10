#!/usr/bin/env bash
# Thin wrapper: run the cross-platform installer with a Python 3 (Linux / WSL / macOS).
# All logic lives in install.py so every OS runs one tested code path.
# It is idempotent and safe to re-run.
#   ./install.sh            installs to ~/.claude
#   CLAUDE_HOME=/x ./install.sh   overrides the target (testing)
set -Eeuo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pick the first candidate that is actually Python 3.
# The installer and the hook use Python 3 syntax.
# A bare `python` that is Python 2 is therefore rejected rather than handed the script, which would fail on import.
py=""
for c in python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    if out="$("$c" -c 'import sys; print("ok" if sys.version_info >= (3, 11) else "old")')" && [ "$out" = "ok" ]; then
        py="$c"
        break
    fi
    echo "Passed over $c: it did not report Python 3.11 or newer." >&2
done
[ -n "$py" ] || {
    echo "Python 3.11+ is required and was not found on PATH (tried python3, python)." >&2
    exit 1
}

exec "$py" "$here/install.py" "$@"
