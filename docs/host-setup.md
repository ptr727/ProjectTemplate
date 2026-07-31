# Host Setup

Prerequisites for working with this repo locally, applied once per machine before opening the devcontainer or building outside one.

Supported hosts:

- **Linux** - both the devcontainer flow and the host-install flow.
- **macOS** - both the devcontainer flow and the host-install flow.
- **Windows** - the devcontainer flow requires **WSL2**, and native Windows (PowerShell + winget) is supported only for the host-install flow described in `README.md`. The bind-mounts in `.devcontainer/dotnet/devcontainer.json` and `.devcontainer/python/devcontainer.json` rely on POSIX paths and only work from Linux/macOS/WSL2.

> **Shell assumptions in this doc**: every command snippet below assumes a **POSIX shell** (bash/zsh) and POSIX path conventions (`~/.ssh/...`, `mkdir -p`, `$(...)` command substitution). On Windows, run them from **WSL2** or **Git Bash**, since they will not work as-is in PowerShell or `cmd.exe`. The git config and `gh` commands are portable; only the file/path manipulation differs by shell.

## Git Identity

Configure your name and email, used for commit authorship. **The email is the committing account's GitHub `noreply` address, never a private, personal, or invented one**, per [GOVERNANCE.md "Git and Commit Rules"][governance-git-and-commit-rules], which owns the rule and states the fleet's value. A private address trips GitHub's email-privacy push protection (GH007), and an invented one pollutes history.

```shell
git config --global user.name "Your Name"
git config --global user.email "<id>+<username>@users.noreply.github.com"
```

Set this **globally**, once per machine. Repositories inherit it, so a repo-local `user.email` is redundant where the global is right and a wrong identity where it is not. An agent standing up a repo verifies this configuration rather than setting it ([`STANDUP.md`][standup] step 0).

## SSH Key

Generate an Ed25519 SSH key for both authentication and commit signing. One key serves both roles.

```shell
ssh-keygen -t ed25519 -C "you@example.com" -f ~/.ssh/id_ed25519
```

Add the public key (`~/.ssh/id_ed25519.pub`) to GitHub twice:

1. **Authentication key** - [GitHub -> Settings -> SSH and GPG keys -> New SSH key][keys-link], key type **Authentication Key**.
2. **Signing key** - same page, but **Signing Key** type. GitHub treats these independently even though it's the same public key.

Test the auth key:

```shell
ssh -T git@github.com
```

## SSH Config

Tell SSH which key to use for `github.com`. Pick the snippet for your platform.

### Linux / WSL2

```sshconfig
# ~/.ssh/config
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```

Make sure ssh-agent is running and the key is loaded. On systemd-based distros:

```shell
systemctl --user enable --now ssh-agent.socket
ssh-add ~/.ssh/id_ed25519
```

For non-systemd shells, add to `~/.bashrc` or `~/.zshrc`. The check probes the agent for at least one loaded key, because `[ -z "$SSH_AUTH_SOCK" ]` alone would miss the case where `SSH_AUTH_SOCK` is set but points at a stale socket or a keyless agent:

```shell
if [ -z "$SSH_AUTH_SOCK" ] || ! ssh-add -l >/dev/null 2>&1; then
    eval "$(ssh-agent -s)" >/dev/null
    ssh-add ~/.ssh/id_ed25519 2>/dev/null
fi
```

### macOS

```sshconfig
# ~/.ssh/config
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    UseKeychain yes
    AddKeysToAgent yes
```

Load the key into the macOS Keychain so it's available without re-entering the passphrase:

```shell
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

## Allowed Signers File

Required for SSH signature verification by `git verify-commit` and similar tools. Without it git can sign commits but not verify them locally.

```shell
mkdir -p ~/.config/git
echo "$(git config user.email) namespaces=\"git\" $(cat ~/.ssh/id_ed25519.pub)" \
    >> ~/.config/git/allowed_signers
git config --global gpg.ssh.allowedSignersFile ~/.config/git/allowed_signers
```

## Configure Git for SSH Signing

```shell
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
git config --global tag.gpgsign true
```

See [SSH commit signing][ssh-signing] for verification steps and per-OS troubleshooting.

## GitHub CLI

Install [`gh`][cli-link] and authenticate.

```shell
gh auth login --hostname github.com --git-protocol ssh
```

Choose the SSH key generated above when prompted.

## Verify Host Setup

```shell
git config --global --list | grep -E "user\.|signing|gpg\."
ssh-add -L                                   # should list your public key
git -c gpg.format=ssh commit -S --allow-empty -m "verify-signing"
git log --show-signature -1
gh auth status
```

If signing fails locally, the devcontainer will fail too, so fix here first.

## Next Steps

- [Devcontainer setup][devcontainer]: open the repo in the per-language .NET or Python devcontainer.
- [SSH commit signing][ssh-signing]: per-OS setup details, verification, and troubleshooting.

<!-- Repo -->

[devcontainer]: ./devcontainer.md
[governance-git-and-commit-rules]: ../GOVERNANCE.md#git-and-commit-rules
[ssh-signing]: ./ssh-signing.md
[standup]: ../STANDUP.md

<!-- External -->

[cli-link]: https://cli.github.com/
[keys-link]: https://github.com/settings/keys
