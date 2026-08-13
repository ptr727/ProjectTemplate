#!/usr/bin/env bash
# Thin wrapper: run the cross-platform installer with a Python 3 (Linux / WSL / macOS).
# All logic lives in skills_install.py so every OS runs one tested code path.
# It is idempotent and safe to re-run.
#   Run: scripts/skills_install.sh              installs the fleet skills for this user
#   Or: AGENTS_HOME=/x scripts/skills_install.sh   overrides the target (testing)
set -Eeuo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pick the first candidate that is actually Python 3.
# The installer uses Python 3 syntax, so a bare `python` that is Python 2 is rejected rather than handed the script, which would fail on import.
py=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[0] == 3 else 1)' 2>/dev/null; then
    py="$c"; break
  fi
done
[ -n "$py" ] || { echo "Python 3 is required and was not found on PATH (tried python3, python)." >&2; exit 1; }

exec "$py" "$here/skills_install.py" "$@"
