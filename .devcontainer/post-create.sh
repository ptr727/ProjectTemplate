#!/usr/bin/env bash
set -euo pipefail

# Install uv (Astral) for the Python sibling project. Idempotent — re-running
# overwrites in place. The installer drops the binary in $HOME/.local/bin and
# updates user shell init to add it to PATH for new shells; we add it to the
# current PATH explicitly so the rest of this script can invoke `uv` without a
# hard-coded path.
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Restore the .NET local-tool manifest (CSharpier, Husky.Net, dotnet-outdated).
dotnet tool restore

# Install Husky.Net git hooks so commits run pre-commit checks. Failures here
# (e.g. missing .git directory, broken tool restore) should surface — the
# devcontainer setup is not "successful" if hook installation fails silently.
dotnet husky install

# Pre-warm uv environment for PyPiLibrary if it exists. Guarded so this script
# is safe before PyPiLibrary lands in the repo.
if [[ -f PyPiLibrary/pyproject.toml ]]; then
    (cd PyPiLibrary && uv sync)
fi
