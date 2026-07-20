#!/usr/bin/env bash
# Thin wrapper: run the cross-platform installer with the available python (Linux / WSL / macOS / Proxmox).
# All logic lives in install.py so every OS runs one tested code path. Idempotent; safe to re-run.
#   ./install.sh            installs to ~/.claude
#   CLAUDE_HOME=/x ./install.sh   overrides the target (testing)
set -Eeuo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="$(command -v python3 || command -v python || true)"
[ -n "$py" ] || { echo "python3 is required and was not found on PATH." >&2; exit 1; }
exec "$py" "$here/install.py" "$@"
