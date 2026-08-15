#!/usr/bin/env bash
# Thin wrapper: run the cross-platform installer with a Python 3 (Linux / WSL / macOS).
# All logic lives in skills_install.py so every OS runs one tested code path.
# It is idempotent and safe to re-run.
#   Run: scripts/skills_install.sh              installs the fleet skills for this user
#   Or: AGENTS_HOME=/x scripts/skills_install.sh   overrides the target (testing)
set -Eeuo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pick the first candidate that is actually Python 3.7+.
# The installer uses `from __future__ import annotations`, which needs 3.7+, so a 3.6 or a
# Python-2 `python` is rejected rather than handed the script, which would fail to parse.
py=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)' 2>/dev/null; then
    py="$c"; break
  fi
done
[ -n "$py" ] || { echo "Python 3.7+ is required and was not found on PATH (tried python3, python)." >&2; exit 1; }

exec "$py" "$here/skills_install.py" "$@"
