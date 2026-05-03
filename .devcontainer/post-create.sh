#!/usr/bin/env bash
set -euo pipefail

# Install uv (Astral) for the Python sibling project. Idempotent — re-running
# overwrites in place. The installer drops the binary in $HOME/.local/bin and
# updates user shell init to add it to PATH for new shells; we add it to the
# current PATH explicitly so the rest of this script can invoke `uv` without a
# hard-coded path.
#
# uv is pinned to a specific version (via the version-prefixed install URL,
# https://astral.sh/uv/<version>/install.sh) so a compromised or broken
# upstream `latest` script cannot silently change what runs on contributors'
# machines and CI runners. Bump UV_VERSION when you've reviewed release notes.
#
# We re-install when uv is missing OR when the installed version doesn't
# match the pin. The latter handles the case where a contributor (or a
# previous run with a different pin) left a different uv version on PATH —
# the pin is what's reproducible and what the lockfile is generated against.
UV_VERSION="0.11.8"
installed_uv_version=""
if command -v uv >/dev/null 2>&1; then
    installed_uv_version="$(uv --version | awk '{print $2}')"
fi
if [[ "$installed_uv_version" != "$UV_VERSION" ]]; then
    # Download the pinned installer to a temp file first instead of piping
    # `curl … | sh`. This produces a logged sha256 of exactly the bytes we
    # ran, so a compromised installer leaves a forensic trail; it also lets
    # a future change pin a known-good checksum (set EXPECTED_SHA below).
    installer=$(mktemp -t uv-install.XXXXXX.sh)
    trap 'rm -f "$installer"' EXIT
    curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" -o "$installer"
    actual_sha=$(sha256sum "$installer" | awk '{print $1}')
    echo "uv installer (v${UV_VERSION}) sha256: ${actual_sha}" >&2
    # EXPECTED_SHA="<paste-from-trusted-source>"  # set to enforce
    if [[ -n "${EXPECTED_SHA:-}" && "${actual_sha}" != "${EXPECTED_SHA}" ]]; then
        echo "uv installer sha256 mismatch — refusing to run" >&2
        exit 1
    fi
    sh "$installer"
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
