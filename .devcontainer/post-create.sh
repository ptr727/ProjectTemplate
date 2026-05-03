#!/usr/bin/env bash
set -euo pipefail

# Install uv (Astral) for the Python sibling project. Idempotent — re-running
# overwrites in place. Adds $HOME/.local/bin to PATH via uv's installer hook.
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Restore the .NET local-tool manifest (CSharpier, Husky.Net, dotnet-outdated).
dotnet tool restore

# Install Husky.Net git hooks so commits run pre-commit checks.
dotnet husky install || true

# Pre-warm uv environment for PyPiLibrary if it exists. Guarded so this script
# is safe before PyPiLibrary lands in the repo.
if [[ -f PyPiLibrary/pyproject.toml ]]; then
    (cd PyPiLibrary && "$HOME/.local/bin/uv" sync)
fi
