# Devcontainer Setup

The repo ships a single unified [Dev Container](https://containers.dev/) that hosts both the .NET 10 SDK and the Python `uv` toolchain. Open the repo in VS Code with the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) installed and pick **Reopen in Container**.

Prerequisite: complete [host setup](./host-setup.md) first — without git config, an SSH key, and the allowed-signers file on the host, the devcontainer will not be able to sign commits.

## What's Inside

| Component | Source | Purpose |
|---|---|---|
| .NET 10 SDK | base image `mcr.microsoft.com/devcontainers/dotnet:1-10.0` | Build, test, pack the .NET projects |
| `uv` | `https://astral.sh/uv/<UV_VERSION>/install.sh` (version-pinned) downloaded by `.devcontainer/post-create.sh` | Python env, dependency, build, and publish manager for the PyPi sibling |
| `gh` CLI | `ghcr.io/devcontainers/features/github-cli:1` | Issue/PR/release management from inside the container |
| Common utilities | `ghcr.io/devcontainers/features/common-utils:2` | bash, curl, wget, sudo, `vscode` user |
| VS Code extensions | `customizations.vscode.extensions` in `devcontainer.json` | Mirrors `ProjectTemplate.code-workspace` recommendations so the container has the same tooling |

The extension list in `.devcontainer/devcontainer.json` and the `recommendations` array in `ProjectTemplate.code-workspace` are kept identical — when you add an extension to one, add it to the other.

## Bind Mounts

The host SSH key, allowed-signers file, and `gh` config directory are mounted into the container so commits sign correctly and `gh` is pre-authenticated **when the host stores its `gh` token in a file** (`~/.config/gh/hosts.yml`). Hosts that store the token in macOS Keychain or Linux libsecret will need an in-container `gh auth login` instead — see [`gh` credential store](#gh-credential-store) below for the full picture.

| Host path | Container path | Mode | Purpose |
|---|---|---|---|
| `~/.ssh/id_ed25519.pub` | `/home/vscode/.ssh/id_ed25519.pub` | read-only | Public half of the SSH key. The private key never enters the container — SSH agent forwarding handles signing. |
| `~/.config/git/allowed_signers` | `/home/vscode/.config/git/allowed_signers` | read-only | Maps your email to your public key so `git verify-commit` and `git log --show-signature` work inside the container. |
| `~/.config/gh` | `/home/vscode/.config/gh` | read-write | `gh` CLI auth state shared with the host. See [`gh` credential store](#gh-credential-store) below. |

VS Code Dev Containers automatically copies your host `~/.gitconfig` into the container at startup, so `user.name`, `user.email`, `user.signingkey`, `gpg.format`, and `commit.gpgsign` propagate without an explicit mount.

The SSH agent is forwarded automatically by the Dev Containers extension via `SSH_AUTH_SOCK`, so signing works as long as the agent on the host has your key loaded.

## Lifecycle Commands

`devcontainer.json` runs two scripts at well-defined points:

- **`onCreateCommand`** — `sudo install -d -m 700 -o vscode -g vscode /home/vscode/.ssh`. On macOS hosts the bind-mount surfaces `/home/vscode/.ssh` as root-owned, which would block writes from inside the container (e.g. `gh` updating `known_hosts`). This chown fixes it. Idempotent on Linux and WSL2.
- **`postCreateCommand`** — `.devcontainer/post-create.sh`, which installs `uv`, runs `dotnet tool restore`, installs Husky.Net hooks, and pre-syncs `PyPiLibrary` if it exists. Re-runs are idempotent.

To force them to run again after editing the script: VS Code → Command Palette → **Dev Containers: Rebuild Container**.

## `gh` Credential Store

`gh auth login` writes its token to either a file or an OS credential store. Which one depends on your host:

| Host | Default token storage |
|---|---|
| Linux | libsecret (gnome-keyring) when available, otherwise file |
| WSL2 | file (no native credential store) |
| macOS | macOS Keychain |

The bind-mount of `~/.config/gh` covers the **file** case. If your host stores the token in Keychain or libsecret, the bind-mount carries the rest of `gh` config but **not the token** — the container will report "no authentication" until you either:

1. Re-run `gh auth login` inside the container (writes a file token to the mounted directory), or
2. Skip in-container `gh` and run those commands on the host instead.

The file-token path is slightly less secure than Keychain/libsecret because it's plaintext on disk inside `~/.config/gh/hosts.yml`. For most contributors that's an acceptable trade-off; if it isn't, use option 2.

## Verify the Devcontainer

After **Reopen in Container** finishes, run:

```shell
dotnet --version                                   # 10.x
uv --version                                       # uv 0.x
gh auth status                                     # logged in as you
git -c gpg.format=ssh commit -S --allow-empty -m "verify-signing"
git log --show-signature -1                        # "Good 'git' signature for ..."
dotnet build                                       # 0 warnings, 0 errors
dotnet test                                        # tests pass
```

If `git -c gpg.format=ssh commit -S` errors with `signing failed: no allowed signers`, the bind-mount of `allowed_signers` is missing or the file on the host is empty — re-run the snippet in [host setup](./host-setup.md).

## Troubleshooting

**Permission denied writing to `~/.ssh/known_hosts` in the container** — The `onCreateCommand` should have chowned `~/.ssh` to `vscode`. Rebuild the container; if it persists, open a shell and run the same `sudo install -d -m 700 -o vscode -g vscode ~/.ssh` manually.

**`git commit` fails with "no SSH agent socket"** — VS Code Dev Containers forwards `SSH_AUTH_SOCK` automatically, but only if the host has `ssh-agent` running with at least one key. Run `ssh-add -l` on the host first; if it says "could not open a connection to your authentication agent", start the agent (see [host setup](./host-setup.md)).

**uv not on `PATH` after rebuild** — The post-create installer adds `~/.local/bin` to `PATH` via the user shell init scripts, which take effect on next shell. Either re-open the integrated terminal or `source ~/.bashrc`.

**Container builds but extensions don't auto-install** — Make sure VS Code is using the Dev Containers extension (not "Remote - SSH" or "Remote - Tunnels"). The extension auto-install is keyed on `customizations.vscode.extensions` and only Dev Containers honors that.
