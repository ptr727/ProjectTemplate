# Devcontainer Setup

The repo ships **two per-language [Dev Containers](https://containers.dev/)** so each container carries only one toolchain, one extension surface, and one `postCreateCommand` - matching the language you'll actually edit.

| Workspace | Devcontainer | Image | Toolchain |
| --------- | ------------ | ----- | --------- |
| `DotNet.code-workspace` | [`catalog/snippets/devcontainer/dotnet/devcontainer.json`](../catalog/snippets/devcontainer/dotnet/devcontainer.json) | `mcr.microsoft.com/devcontainers/dotnet:1-10.0` | .NET 10 SDK |
| `Python.code-workspace` | [`catalog/snippets/devcontainer/python/devcontainer.json`](../catalog/snippets/devcontainer/python/devcontainer.json) | `mcr.microsoft.com/devcontainers/python:1-3.14-bookworm` | Python 3.14 + version-pinned `uv` |

Open the workspace file matching the language you want, install the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers), and pick **Reopen in Container**.

Prerequisite: complete [host setup](./host-setup.md) first - without git config, an SSH key, and the allowed-signers file on the host, neither devcontainer will be able to sign commits.

## What's Inside (Both Containers)

| Component | Source | Purpose |
| --------- | ------ | ------- |
| `gh` CLI | `ghcr.io/devcontainers/features/github-cli:1` | Issue/PR/release management from inside the container |
| Common utilities | `ghcr.io/devcontainers/features/common-utils:2` | bash, curl, wget, sudo, `vscode` user |
| VS Code extensions | `customizations.vscode.extensions` in each `devcontainer.json` | Mirrors the matching workspace's `recommendations` so the container has the same tooling |

The .NET container additionally ships the `csharpier`/`dotnet-outdated` local tools (restored by `catalog/snippets/devcontainer/dotnet/post-create.sh`). The Python container additionally ships `uv` (installed by `catalog/snippets/devcontainer/python/post-create.sh` from a version-pinned URL) and pre-syncs the `PyPiLibrary` venv.

Each devcontainer's extension list and the matching workspace's `recommendations` are kept identical - when you add an extension to one, add it to the other.

## Bind Mounts (Both Containers)

The host SSH key, allowed-signers file, and `gh` config directory are mounted into the container so commits sign correctly and `gh` is pre-authenticated **when the host stores its `gh` token in a file** (`~/.config/gh/hosts.yml`). Hosts that store the token in macOS Keychain or Linux libsecret will need an in-container `gh auth login` instead - see [`gh` credential store](#gh-credential-store) below for the full picture.

| Host path | Container path | Mode | Purpose |
| --------- | -------------- | ---- | ------- |
| `~/.ssh/id_ed25519.pub` | `/home/vscode/.ssh/id_ed25519.pub` | read-only | Public half of the SSH key. The private key never enters the container - SSH agent forwarding handles signing. |
| `~/.config/git/allowed_signers` | `/home/vscode/.config/git/allowed_signers` | read-only | Maps your email to your public key so `git verify-commit` and `git log --show-signature` work inside the container. |
| `~/.config/gh` | `/home/vscode/.config/gh` | read-write | `gh` CLI auth state shared with the host. See [`gh` credential store](#gh-credential-store) below. |

VS Code Dev Containers automatically copies your host `~/.gitconfig` into the container at startup, so `user.name`, `user.email`, `user.signingkey`, `gpg.format`, and `commit.gpgsign` propagate without an explicit mount.

The SSH agent is forwarded automatically by the Dev Containers extension via `SSH_AUTH_SOCK`, so signing works as long as the agent on the host has your key loaded.

## Lifecycle Commands

Both `devcontainer.json` files run two scripts at well-defined points:

- **`onCreateCommand`** - `sudo install -d -m 700 -o vscode -g vscode /home/vscode/.ssh`. On macOS hosts the bind-mount surfaces `/home/vscode/.ssh` as root-owned, which would block writes from inside the container (e.g. `gh` updating `known_hosts`). This chown fixes it. Idempotent on Linux and WSL2.
- **`postCreateCommand`** - language-specific:
  - .NET: `catalog/snippets/devcontainer/dotnet/post-create.sh` - runs `dotnet tool restore` (csharpier, dotnet-outdated).
  - Python: `catalog/snippets/devcontainer/python/post-create.sh` - installs the pinned `uv` and pre-syncs `PyPiLibrary` if it exists.

Re-runs of either are idempotent. No git hooks are installed by default - see the README's **Optional: enable git hooks locally** section if you want pre-commit checks.

To force them to run again after editing a script: VS Code -> Command Palette -> **Dev Containers: Rebuild Container**.

## `gh` Credential Store

`gh auth login` writes its token to either a file or an OS credential store. Which one depends on your host:

| Host | Default token storage |
| ---- | --------------------- |
| Linux | libsecret (gnome-keyring) when available, otherwise file |
| WSL2 | file (no native credential store) |
| macOS | macOS Keychain |

The bind-mount of `~/.config/gh` covers the **file** case. If your host stores the token in Keychain or libsecret, the bind-mount carries the rest of `gh` config but **not the token** - the container will report "no authentication" until you either:

1. Re-run `gh auth login` inside the container (writes a file token to the mounted directory), or
2. Skip in-container `gh` and run those commands on the host instead.

The file-token path is slightly less secure than Keychain/libsecret because it's plaintext on disk inside `~/.config/gh/hosts.yml`. For most contributors that's an acceptable trade-off; if it isn't, use option 2.

## Verify the Devcontainer

After **Reopen in Container** finishes, run the language-appropriate checks.

**Both containers** - verify SSH signing and `gh`:

```shell
gh auth status                                     # logged in as you
git -c gpg.format=ssh commit -S --allow-empty -m "verify-signing"
git log --show-signature -1                        # "Good 'git' signature for ..."
```

**.NET container** (`DotNet.code-workspace` -> Reopen in Container -> "dotnet"):

```shell
dotnet --version                                   # 10.x
which uv                                           # nothing - uv intentionally absent
dotnet build                                       # 0 warnings, 0 errors
dotnet test                                        # tests pass
```

**Python container** (`Python.code-workspace` -> Reopen in Container -> "python"):

```shell
uv --version                                       # uv 0.x
which dotnet                                       # nothing - dotnet intentionally absent
cd PyPiLibrary && uv sync && uv run pytest         # tests pass
```

If `git -c gpg.format=ssh commit -S` errors with `signing failed: no allowed signers`, the bind-mount of `allowed_signers` is missing or the file on the host is empty - re-run the snippet in [host setup](./host-setup.md).

## Troubleshooting

**Permission denied writing to `~/.ssh/known_hosts` in the container** - The `onCreateCommand` should have chowned `~/.ssh` to `vscode`. Rebuild the container; if it persists, open a shell and run the same `sudo install -d -m 700 -o vscode -g vscode ~/.ssh` manually.

**`git commit` fails with "no SSH agent socket"** - VS Code Dev Containers forwards `SSH_AUTH_SOCK` automatically, but only if the host has `ssh-agent` running with at least one key. Run `ssh-add -l` on the host first; if it says "could not open a connection to your authentication agent", start the agent (see [host setup](./host-setup.md)).

**uv not on `PATH` after rebuild** (Python container) - The post-create installer adds `~/.local/bin` to `PATH` via the user shell init scripts, which take effect on next shell. Either re-open the integrated terminal or `source ~/.bashrc`.

**Container builds but extensions don't auto-install** - Make sure VS Code is using the Dev Containers extension (not "Remote - SSH" or "Remote - Tunnels"). The extension auto-install is keyed on `customizations.vscode.extensions` and only Dev Containers honors that.

**Wrong-language work in the wrong container** - The `.NET` container has no `uv` and no Python extensions; the Python container has no `dotnet` SDK and no C# extensions. This is intentional - open the matching workspace and rebuild rather than installing the missing toolchain ad hoc.
