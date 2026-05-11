# SSH Commit Signing

This repo enforces signed commits on `main` and `develop` via branch protection. Use SSH signing — one Ed25519 key serves both authentication (push) and signing.

If you haven't generated a key and configured git yet, follow [host setup](./host-setup.md) first.

## Why SSH Signing

- **One key for everything**. Same `id_ed25519` you use for `git push` also signs commits. No GPG keyring, no expirations to chase.
- **GitHub native**. GitHub treats authentication and signing keys independently but accepts the same public key for both — register it twice on the SSH and GPG keys page.
- **Survives rotation cleanly**. When you rotate the key, update the `allowed_signers` file and old signatures still verify against the historical entry.

## Configuration

Per-user (host) git config — set once:

```shell
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
git config --global tag.gpgsign true
git config --global gpg.ssh.allowedSignersFile ~/.config/git/allowed_signers
```

The `allowed_signers` file is what `git verify-commit` consults — without it, signatures sign fine but verify as "unknown signer". Format:

```text
you@example.com namespaces="git" ssh-ed25519 AAAA... your_public_key_contents_here
```

Build it from your existing public key:

```shell
mkdir -p ~/.config/git
echo "$(git config user.email) namespaces=\"git\" $(cat ~/.ssh/id_ed25519.pub)" \
    >> ~/.config/git/allowed_signers
```

If you collaborate with others, append their entries to the same file — each line maps an email to a public key.

## Per-OS Setup Notes

### Linux / WSL2

The SSH agent must be running for git to find the private key without prompting for the passphrase every commit. On systemd-based distros:

```shell
systemctl --user enable --now ssh-agent.socket
ssh-add ~/.ssh/id_ed25519
```

The agent socket lives at `$XDG_RUNTIME_DIR/ssh-agent.socket`. Make sure your shell exports `SSH_AUTH_SOCK` to point at it — most distros do this in `/etc/X11/Xsession.d` or systemd user environment.

For shells without systemd integration, fall back to ad-hoc agent in `~/.bashrc` or `~/.zshrc`:

```shell
if [ -z "$SSH_AUTH_SOCK" ] || ! ssh-add -l >/dev/null 2>&1; then
    eval "$(ssh-agent -s)" >/dev/null
    ssh-add ~/.ssh/id_ed25519 2>/dev/null
fi
```

WSL2 specifically: WSL inherits no agent from Windows. Run `ssh-agent` inside WSL; do not try to forward an agent from the Windows side.

### macOS

macOS has its own `ssh-agent` integrated with Keychain. To load your key once and have it persist across reboots:

```shell
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

Add to `~/.ssh/config` so `ssh` and `git` use the Keychain-aware agent automatically:

```sshconfig
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    UseKeychain yes
    AddKeysToAgent yes
```

The Keychain prompt for the passphrase appears on first use after each reboot; subsequent sessions are silent.

### Windows (without WSL)

Native Windows is **not supported** for the devcontainer setup in this repo. Use WSL2 instead. The reason: VS Code Dev Containers needs a Linux-like file system for the bind-mounts to behave consistently, and Docker Desktop's WSL2 backend is the supported path.

If you must work on Windows directly without a devcontainer, OpenSSH for Windows can sign with `gpg.format=ssh` — but the bind-mounted devcontainer setup expects Linux/WSL2 paths.

## Verify Signing

The `-S` flag and `-c gpg.format=ssh` override are explicit so the verification works even before `commit.gpgsign` and `gpg.format` are set globally — useful when verifying a fresh setup mid-configuration.

```shell
git -c gpg.format=ssh commit -S --allow-empty -m "verify-signing"
git log --show-signature -1
```

Expected output includes `Good "git" signature for <your-email>`. If you see `error: gpg.ssh.allowedSignersFile needs to be configured` or `No signature`, walk back through the host setup — most often `allowed_signers` is missing the entry, or the `user.signingkey` and `gpg.ssh.allowedSignersFile` configs aren't set yet.

## Inside the Devcontainer

The container picks up:

- Your `~/.gitconfig` automatically (VS Code Dev Containers copies it on start).
- The `~/.ssh/id_ed25519.pub` and `~/.config/git/allowed_signers` files via bind-mount declared in `devcontainer.json`.
- The forwarded SSH agent socket from `SSH_AUTH_SOCK`, so signing happens with the host's loaded private key without the private key ever entering the container.

If the container's `~/.ssh` directory exists with the wrong owner (root, surfaced by macOS bind-mount semantics), `gh auth login` writes to `~/.ssh/known_hosts` may fail. The `onCreateCommand` in `devcontainer.json` chowns the directory to `vscode` to fix this — see [devcontainer setup](./devcontainer.md) for the rationale.

## Troubleshooting

**`gpg.ssh.allowedSignersFile needs to be configured`** — Set `git config --global gpg.ssh.allowedSignersFile ~/.config/git/allowed_signers` and ensure the file exists.

**`signing failed: no allowed signers`** — The `allowed_signers` file exists but doesn't contain a line matching `user.email` + a key. Re-run the `echo $(git config user.email) namespaces="git" $(cat ~/.ssh/id_ed25519.pub) >> …` snippet.

**Verifies on the host but not in the container** — The bind-mount source path differs. `${localEnv:HOME}` resolves on Linux/macOS hosts; on Windows hosts (WSL2 backend) the `${localEnv:USERPROFILE}` fallback in `devcontainer.json` handles it. Check the actual mount with `mount | grep ssh` inside the container.

**SSH agent says "could not open a connection"** — The host's agent isn't running. Linux: `systemctl --user start ssh-agent.socket`. macOS: open a new terminal so launchd starts the agent.
