# Host Setup

Prerequisites for working with this repo locally, applied once per machine before opening the devcontainer or building outside one.

Supported hosts:

- **Linux** - both the devcontainer flow and the host-install flow.
- **macOS** - both the devcontainer flow and the host-install flow.
- **Windows** - the devcontainer flow requires **WSL2**, and native Windows (PowerShell + winget) is supported only for the host-install flow described in `README.md`. The bind-mounts in the `catalog/snippets/devcontainer/` definitions rely on POSIX paths and only work from Linux/macOS/WSL2.

> **Shell assumptions in this doc**: every command snippet below assumes a **POSIX shell** (bash/zsh) and POSIX path conventions (`~/.ssh/...`, `mkdir -p`, `$(...)` command substitution), except where a block is marked `powershell`. Such a block is the **Windows-native** form of the step it sits in, meant to run in PowerShell rather than translated. On Windows, run the POSIX snippets from **WSL2** or **Git Bash**, since they will not work as-is in PowerShell or `cmd.exe`. The git config and `gh` commands are portable, and only the file and path manipulation differs by shell.

## What a Host Must Provide

This section is the **contract**: which tools a host needs and which repo procedure stops working without each one. It deliberately names no installer, because `winget`, `brew` and `apt` differ per platform while the requirement does not. Per-platform install commands are tracked separately, in [`host-setup/`][host-setup-dir], so this table stays true on every host.

| Tool | Needed by | Present when | Floor |
| --- | --- | --- | --- |
| `git` | everything, and the identity and signing contract in [`STANDUP.md`][standup] step 0 | `git --version` | none |
| `gh` | the PR and review loop, `gh api` queries, `repo-config/configure.sh` | `gh --version` | **2.47.0**, measured |
| Python 3 | `scripts/` and `spec/` (standard library only, no packages to install) | `python3 --version`, or `py -3 --version` on native Windows | **3.13**, target |
| `jq` | the ruleset normalizer in `repo-config/configure.sh`, the ruleset diff in [`AUDIT.md`][audit] section 6, and payload regeneration | `jq --version` | **1.7**, target |
| Ripgrep | repository searches and file discovery by coding agents | `rg --version` | **13.0.0**, target |
| `docker` | the four linters, which run as pinned images rather than local installs | `docker --version` | **29.6.2**, target |
| `uv` / `uvx` | coverage runs, and the Python toolchain (`ruff`, `pyright` or `mypy`) in a Python repo | `uv --version` | **0.12.2**, target |

The **Floor** column exists because presence and sufficiency are different questions and the answer to the first was being read as the answer to the second. A tool below its floor still answers `--version`, so every other column reports it as fine while `scripts/host_gate.py` fails it. The kind is named beside the number, since a **measured** floor sits above a version known to break a documented procedure and gives a failing host a defect to point at, where a **target** floor names the version the repo's toolchain is configured for and does not. The next section carries the reasoning behind each one.

Two consequences worth reading off the table rather than discovering later. **Python 3 needs no packages**, because every script here is standard library only, so a bare interpreter is enough. And **the linters need only `docker`**, not `node`, `dotnet` or a local `markdownlint`, since each runs as a pinned image, which is what keeps a local run and CI the same check.

**The interpreter is not called `python3` everywhere.** On native Windows the installer registers `python`, `py` and `python3.13` but **not** `python3`, where that name instead resolves to a Microsoft Store alias stub that reports the interpreter as missing, so a correctly set-up host fails a `python3` check. Stock Debian is the mirror image, carrying `python3` and no bare `python`. Use `py -3` on native Windows and `python3` elsewhere, and prefer `python3` in any script that must run on both, since WSL2 shadows the Windows stub.

A missing tool is a host gap, not a repo problem. Install it and re-run, rather than working around it in a repo.

### Where a Tool Comes From, and How Old It May Be

Presence is the weaker half of this contract. Both host defects this fleet has actually hit are **version** facts on a tool that is installed, answers `--version`, and looks healthy, so the table above cannot see either one. [`spec/host-tools.json`][host-tools] carries the floors as data and records the defect each one encodes, and [`scripts/host_gate.py`][host-gate] reads it. A floor is one of two kinds and names its own kind in the `why` it carries. A **measured** floor sits above a version known to break a documented procedure, which is what both `gh` and `git-restore-mtime` carry. A **target** floor names the version the repo's own toolchain is configured for, which is what `python3` carries at 3.13, where `pyproject.toml` sets ruff and mypy to that version, so a lower interpreter is unverified rather than known broken and the entry says exactly that. Everything else carries none, deliberately: a floor nobody can justify becomes a host failure nobody can act on.

**`gh` must not come from the distribution's package on Linux.** This is the one place this document names a source, because here the source *is* the requirement rather than a convenience. The GitHub CLI maintainers state that the community-distributed `2.45.x` / `2.46.x` is **broken by deprecated GitHub APIs**, so install from the official apt repository at [cli.github.com][cli-install-link] and upgrade from there. Both `gh` limitations recorded in [`OPERATIONS.md`][operations] were observed on a host carrying a distribution `gh 2.46.0`, and both are the deprecation class that note describes. On **Windows** `winget` tracks upstream releases, and on macOS Homebrew does, so neither raises this hazard and neither needs a note of its own.

**`git-restore-mtime` must not come from it either, where a repo uses it.** Debian and Ubuntu package **2022.12**, which shells out to `git whatchanged`. Current `git` refuses that without a hidden opt-in flag a caller cannot pass through, so the tool restores nothing, prints its ordinary statistics and **exits 0**. A deploy keyed on mtimes then ships a full copy and reports success. Take the upstream release from [git-tools][git-tools-link], or in CI the [action][git-restore-mtime-action-link] that vendors it. Note the direction of that interaction: a **newer** `git` is the trigger rather than the remedy, so a host old enough to still allow `whatchanged` hides the defect rather than avoiding it. No procedure in this repo needs the tool, so the gate declares it **optional** and skips it when absent.

**`docker` must not come from the distribution's own package either, with one exception.** Debian and Ubuntu package `docker.io`, an older build that trails and conflicts with `docker-ce`, so [`host-setup/linux/install-tools.sh`][host-setup-dir] removes it and installs from Docker's own apt repository at [download.docker.com][docker-install-link] instead, the same shape it already uses for `gh` and `node`. The exception is a **WSL distribution**, where the only sanctioned source is Docker Desktop's own WSL integration (Settings, Resources, WSL integration, on the Windows side, reported read-only by [`setup-wsl.ps1`][host-setup-windows]) and a native install is refused outright, with no override: running `docker-ce` directly inside a WSL distribution risks a second engine beside Desktop's own. On native **Windows**, `winget` already tracks upstream Desktop releases, so neither hazard arises there.

**`docker`'s floor is read from the engine, not from the CLI banner.** The `docker` on `PATH` inside a WSL distribution can be a separately packaged client that talks to Docker Desktop's engine over the integration socket, and the two then carry different versions: a host was recorded with a distribution client at `29.1.3` against an engine at `29.7.2`, so `docker --version` failed a floor the engine cleared comfortably. The gate therefore asks the daemon first, with `docker version --format '{{.Server.Version}}'`, and falls back to the `docker --version` banner only where that exits non-zero, which is what a stopped or unreachable daemon does. The **Present when** column above still names the banner, because presence is what it answers and a host with the daemon stopped still has `docker` installed.

**The rest of the table takes the distribution's package, and one more does not.** `git` and the Python interpreter come from the distribution, because each keeps up well enough that a second source buys nothing and costs a repository to trust. `jq` is the same on a current Debian or Ubuntu, which carries a version at or above the floor, and the upstream release binary is the answer only where it does not. `uv` is published by its authors as a release archive and packaged by neither distribution, so upstream is the only source there is. Where a repository needs `node`, the distribution's package trails upstream by whole release lines, so it comes from the NodeSource repository on the line upstream currently carries as long term support. Where a repository needs `dotnet`, the distribution's feed is preferred where it carries an SDK and Microsoft's feed is the fallback, because mixing the two is what breaks a host rather than either one alone, and Microsoft's carries `amd64` only. Where a repository needs `pwsh`, that same feed is the only source, since no distribution packages it, and the tooling installs it as the `powershell` tool under `--optional`, with `pwsh` the command that tool provides.

Ripgrep also comes from the distribution, where every supported current release meets its floor. It provides `rg` search and `rg --files` discovery for coding agents.

Neither `node`, `dotnet`, nor `pwsh` is in the table above, deliberately: they serve the repositories that need them rather than the fleet contract, and a repository needing one declares it in a `host-tools.json` of its own, which [`scripts/host_gate.py`][host-gate] merges over this one. The merge tightens only, so a repository may raise a floor or add one and may not lower or remove one.

**A host being stood up needs no Python.** The tooling under [`host-setup/`][host-setup-dir] is shell and PowerShell, deliberately, because requiring an interpreter to upgrade a package or install a tool would make the first step of standing a host up depend on the thing that step exists to provide. The Python floor above is a development requirement, meaning [`scripts/`][scripts-dir] and [`spec/`][spec-dir], and a host that only runs services never has to meet it. `bootstrap.sh` needs `curl` and `tar`, both of which a base install carries or can install without a network tool of its own. `bootstrap.ps1` needs only `tar.exe`, which has shipped with Windows since 1803, and installs its one further dependency, PowerShell 7, itself through `winget`. The one exception is the skills step at the end of a stand-up, which drives the Python installer in [`scripts/`][scripts-dir], and it runs last for exactly that reason: `install-tools` has provided the interpreter by then, and run alone on a host without one it stops and names the tools step as its prerequisite.

**Standing a host up.** [`host-setup/`][host-setup-dir] carries the tooling that makes a host satisfy this contract, and its README is the usage. A host with nothing runs [`host-setup/bootstrap.sh`][bootstrap], which fetches this repository and runs that tooling from the fetched tree. A native Windows host with nothing runs [`host-setup/bootstrap.ps1`][bootstrap-ps1] the same way, which finds or installs PowerShell 7 before it fetches anything, since every script under [`host-setup/windows/`][host-setup-windows] requires it. Neither is called by [`scripts/host_gate.py`][host-gate] and neither calls it: the gate measures a host against the floors above, and the tooling is a remedy a person chooses when the gate reports a gap.

A repository that needs more than the fleet does adds its own `host-tools.json` at its root, which the gate layers over the hub's. It may add a tool nobody else uses, raise a floor, or turn an optional tool required. It may **not** lower a floor or turn a required tool optional, since those edits retire a fleet check from inside the repository it protects, and the gate reports a rejected relaxation rather than dropping it.

A repository-only tool can declare constrained package metadata under `install.linux` or `install.windows`. Linux accepts only an `apt` package name. Windows accepts only a `winget` package ID. The installer reads those values as package identifiers and never evaluates `remedy` text from the repository.

Run the platform installer with the repository path to include those packages:

```shell
host-setup/linux/install-tools.sh --install --repo /path/to/repository
```

```powershell
host-setup\windows\install-tools.ps1 -Install -Repo C:\path\to\repository
```

The report, list, dry-run, install, and upgrade actions accept the same repository option. A platform with no matching metadata reports no constrained installer for that tool. The Linux reader needs `jq`, which the fleet tools provide. Install the fleet tools first when a minimal host does not carry it.

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
echo "$(git config --global user.email) namespaces=\"git\" $(cat ~/.ssh/id_ed25519.pub)" >> ~/.config/git/allowed_signers
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

## Agent Write-Safety

Host-level write safety is required where an agent runs with the maintainer's `gh` credentials. Each provider's implementation stays in its own subsection.

The requirements every agent's kit is built and audited against, agent-agnostic, are in
[`host-setup/agent-safety/README.md`][agent-safety], the spec. Each agent's own implementation
detail lives one level down, following the same contract-vs-implementation split this file uses
for [`host-setup/`][host-setup-dir]'s own per-platform subdirectories: this file states the
requirement, the per-agent `README.md` owns the how-to.

### Claude Code Write Safety

The Claude Code safety kit is the first agent-specific control to deploy on a new system, and the
only one implemented today. Install, verify, scope limits, and the cross-owner write grant
mechanism are all in [`host-setup/agent-safety/claude/README.md`][agent-safety-claude]. This is a
**host** control, not a repo one: the carried `GOVERNANCE.md` rules reach fleet repos only, while
the hook and the `CLAUDE.md` block cover every session on the machine, including ad-hoc work in no
project at all, which is where the incident behind the kit happened.

### Codex Write Safety

No equivalent host write hook ships yet for Codex. Keep Codex's sandbox and execution policies
enabled meanwhile. [`host-setup/agent-safety/codex/README.md`][agent-safety-codex] states the gap
and what implementing against the spec would look like. [Issue #781][issue-781] tracks it.

### opencode Write Safety

No equivalent host write hook ships yet for opencode. Keep opencode's own permission model enabled
meanwhile. [`host-setup/agent-safety/opencode/README.md`][agent-safety-opencode] states the gap and
what implementing against the spec would look like. [Issue #781][issue-781] tracks it.

## Agent Worktree Access

Fleet tasks create registered worktrees beside the primary checkout, under the host's standard worktree directory. That path is host-specific, and permission configuration is agent-specific. Configure each agent on every host rather than carrying one repository setting that assumes a username or home directory.

The shared requirement is access to the same worktree parent. Each agent's configuration stays in its own subsection below.

### Codex Worktree Access

Codex reads its user configuration from `~/.codex/config.toml`, including sessions started by the VS Code extension. Keep the workspace sandbox and add the absolute worktree parent as an extra writable root:

```toml
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
writable_roots = ["/absolute/path/to/repos/worktrees"]
```

Use the real host path. A Linux, macOS, or WSL host using the fleet layout normally resolves it to `/home/<user>/repos/worktrees` or `/Users/<user>/repos/worktrees`. Native Windows uses a form such as `C:/Users/<user>/repos/worktrees`. Use forward slashes in the TOML value. Reload the VS Code window or restart the Codex session after changing the file, since a running session keeps the permissions it started with.

Validate the file before relying on it:

```shell
codex --strict-config --version
```

This setting grants filesystem writes only under the worktree parent. It does not grant Docker socket access or authorize a third-party container to read a checkout. Codex treats those as separate approval boundaries, per [the official configuration reference][codex-config]. Do not replace this setting with `danger-full-access`, and do not add an unconstrained `docker run` execution rule.

### Claude Code Worktree Access

Claude Code does not read Codex's `config.toml`. Keep its permission mode in Claude Code's user-level configuration.

**The one-time approval prompt on `EnterWorktree` into the fleet's worktree parent is expected, and not eliminable through permission rules.** Claude Code's own `EnterWorktree` tool defaults to `.claude/worktrees/<name>/` under the repository root. Entering a path outside that directory (the fleet's own `~/repos/worktrees/<Repo>-<task-slug>` convention always is) asks for approval first, because the move relocates the session's working directory, write access, and project configuration. Neither an `EnterWorktree(...)` permission rule nor "don't ask again" suppresses this specific prompt -- only `defaultMode: "bypassPermissions"` does, which is not recommended as a standing setting. Stop trying to permission this prompt away. It fires once per worktree entered, by design.

Separately, ordinary `Bash(...)` rules do stop the *follow-up* command prompts once inside the worktree, as long as the pattern matches what actually runs there. A command executed after Claude has moved into the worktree (via `cd`, or via `EnterWorktree` itself) runs with no `-C`/prefix naming the worktree, so a rule scoped to `Bash(git -C <worktree-parent>:*)` does not match it -- match the bare command instead:

```json
{
  "permissions": {
    "allow": [
      "Bash(git worktree add:*)",
      "Bash(git worktree list:*)",
      "Bash(git add:*)",
      "Bash(git commit:*)",
      "Bash(git push:*)"
    ],
    "additionalDirectories": [
      "/absolute/path/to/repos/worktrees"
    ]
  }
}
```

`additionalDirectories` grants filesystem read/write scope under that path. It does not itself suppress a Bash or `EnterWorktree` confirmation, so it is necessary alongside the rules above, not a substitute for them.

**Call `EnterWorktree` after `git worktree add`, not just `cd`.** The `repo-worktree` skill already documents this (`git worktree add`, then attach with `EnterWorktree` `path:`), and it is worth the one extra approval: Claude Code tracks a session as "isolated in a worktree" only once `EnterWorktree` (or the `--worktree` launch flag) has actually run, and while a session is tracked that way it gets a further, built-in enforcement layer with no configuration at all -- blocking a file edit that targets the main checkout, a Bash/PowerShell/Monitor command whose working directory resolves to (or can't be verified to stay outside) the main checkout, a git redirect into the main checkout (`git -C`, `--git-dir`, `GIT_DIR`/`GIT_WORK_TREE`, or a `cd` into the main checkout before running git), and any command shape it can't verify stays inside the worktree at all. A `cd` alone, with no `EnterWorktree` call, gets none of this: the session's own bookkeeping never marked it as isolated, so these checks never engage, and only `host-setup/agent-safety/claude/gh-write-guard.py`'s own rule 6 stands between the session and a mutating command run back in the main checkout by mistake.

### opencode Worktree Access

opencode does not read Codex's `config.toml`. Keep its permission mode in opencode's user-level configuration. Its sessions must be allowed to create and edit the same host-specific worktree parent.

## Fleet Skills Install

The fleet's agent skills are hand-authored in the hub at `.agents/skills/` and installed per user by [`scripts/skills_install.py`][skills-install]: an overlay copy into `~/.agents/skills/` for Codex and opencode, and a user-scope Claude Code plugin install where the `claude` CLI is present. Every run stamps the hub commit it installed from into `~/.agents/skills-install-stamp.json`, and `--report` reads that stamp against the checkout and exits non-zero where the machine is behind it.

Install from a hub checkout, once per machine:

```shell
python3 scripts/skills_install.py            # or the scripts/skills_install.sh / .ps1 wrapper
python3 scripts/skills_install.py --report   # read-only: is this machine current?
```

A bootstrapped host does not run this by hand: the `--host` mode of [`host-setup/bootstrap.sh`][bootstrap] and [`bootstrap.ps1`][bootstrap-ps1] ends with the same installer, driven from the fetched tree by `install-skills.sh` or `install-skills.ps1`, and the `--skills` action runs that step on its own.

The `claude` CLI is deliberately absent from the tool catalog in [`spec/host-tools.json`][host-tools]. A Codex-only machine is a complete machine, so the installer degrades where the CLI is missing, still landing the overlay half, saying so, and recording the partial install in the stamp, where cataloging the CLI would instead fail every host that never wanted it.

**The refresh cadence**: re-run the installer when `--report` exits non-zero, and after any hub merge that touches `.agents/skills/`. Session entry runs no automatic check, by design: the trigger is suspicion, and a rule that keeps needing to be restated in a session is the loudest form of it, which is the symptom the `fleet-conformance-check` skill routes to this report. The maintainer runs the refresh by hand, and an automated one stays out of scope until the fleet has evidence the manual cadence fails.

## Verify Host Setup

```shell
python3 scripts/host_gate.py           # presence and version floors, from spec/host-tools.json
python3 scripts/skills_install.py --report   # the skills install stamp is current
git config --global --list | grep -E "user\.|signing|gpg\."
# One physical line, not backslash-joined, so the whole probe copy-pastes cleanly into a shell.
d=$(mktemp -d "${TMPDIR:-/tmp}/sign-check.XXXXXX") && ( trap 'rm -rf "$d"' 0; email=$(git config --global --get user.email) && git init -q "$d" && git -C "$d" commit --allow-empty -q -m check && out=$(git -C "$d" log -1 --format='sig=%G? author=%an <%ae> committer=%cn <%ce>') && echo "$out" && ae=$(git -C "$d" log -1 --format='%ae') && ce=$(git -C "$d" log -1 --format='%ce') && case "$out" in sig=G\ *|sig=U\ *) true ;; *) false ;; esac && case "$email" in *@users.noreply.github.com) true ;; *) false ;; esac && [ "$ae" = "$email" ] && [ "$ce" = "$email" ] )
gh auth status
```

`sig` must read `G` (good signature) or `U` (good signature, unrecognized signer). For GPG, `U` is a valid signature from a key whose trust level is merely undefined, common right after generating a new key. For SSH, it's a valid signature from a key not found in the local `allowed_signers` file, which doesn't affect whether GitHub itself verifies the commit, only local `git verify-commit` output. Both the `author` and `committer` email must be an actual noreply address, and both must match `user.email` from the config line above, all enforced by the snippet itself. `ssh-add -L` (or a `gpg --list-secret-keys` equivalent) is not a substitute: it only proves an agent holds a key, and a host that signs straight from a key file with no agent running passes this scratch commit while failing that probe, per [GOVERNANCE.md "Git and Commit Rules"][governance-git-and-commit-rules]. If signing fails locally, the devcontainer will fail too, so fix here first.

The gate replaced a line that ran `--version` on each tool and read only whether it answered. That form reported a host carrying the broken `gh` as fully set up, which is the failure it exists to stop. It exits non-zero on a missing required tool or one below its floor, and a below-floor finding prints the defect behind the floor rather than the number alone, names where to install from, and prints the command that installs or upgrades the tool on the current platform, so that failure carries its own fix. A missing tool prints the one-line fact, and [`host-setup/`][host-setup-dir] is its remedy.

**This block is POSIX, and on native Windows two lines need translating.** Run the POSIX form from WSL2 or Git Bash per the shell note, or use the PowerShell form below. Git Bash inherits the Windows `PATH`, so `python3` reaches the same Store alias stub it does in PowerShell and reports a working interpreter as missing.

```powershell
py -3 scripts/host_gate.py                   # presence and version floors, from spec/host-tools.json
py -3 scripts/skills_install.py --report     # the skills install stamp is current
git config --global --list | Select-String "user\.|signing|gpg\."
$d = Join-Path $env:TEMP ([guid]::NewGuid())
try {
  $email = git config --global --get user.email
  git init -q "$d" `
    && git -C "$d" commit --allow-empty -q -m check
  $out = git -C "$d" log -1 --format='sig=%G? author=%an <%ae> committer=%cn <%ce>'
  $out
  $ae = git -C "$d" log -1 --format='%ae'
  $ce = git -C "$d" log -1 --format='%ce'
  if ($out -notmatch '^sig=[GU] ' -or $email -notmatch '@users\.noreply\.github\.com$' `
      -or $ae -ne $email -or $ce -ne $email) {
    throw "signing/identity check failed: $out"
  }
} finally {
  if (Test-Path "$d") { Remove-Item -Recurse -Force "$d" }
}
gh auth status
```

Verified on Windows 11 Pro 10.0.26200 with PowerShell 7.6.4, where `py -3 scripts/host_gate.py` exits 0 over seven declared tools. It was supplied under [#483][issue-483], which had deferred it until somebody had executed it on a Windows host. Only two lines differ from the POSIX block: the interpreter, and the filter, because `grep` has no Windows peer and `Select-String` is the one that ships.

**`py -3` rather than `python`, and the reason is not only the Store stub.** Both names reach the same interpreter on a correctly set-up host, so the stub rules out `python3` and chooses nothing between the other two. What chooses is that an activated virtual environment puts its own interpreter first, so `python` resolves to that environment's. That is right for running project code and wrong here, because this gate measures **the host's** interpreter against a floor, and run as `python` from an activated environment it grades the environment instead. `py` is the launcher and reaches a registered system interpreter whatever is active. The prescription is therefore narrow: `py -3` for this gate, and `python` for everything else.

**What the host can do once this passes**, which is the point of the contract above:

| Now possible | Because |
| --- | --- |
| Stand up a new repo through [`STANDUP.md`][standup] | step 0 verifies identity and signing, and its window closes at the first commit |
| Run the four linters locally, matching CI | `docker` runs each as the same pinned image CI uses |
| Run the repo's own gates and tests | Python 3 covers `scripts/` and `spec/` with no packages to install |
| Drive the PR and Copilot review loop | `gh` and an authenticated session |
| Let an agent work with the `gh` credentials live | the write-safety kit is installed |
| Have the fleet skills surface in every agent session | the skills install stamp is current per `skills_install.py --report` |

A host that fails any row is not ready for the procedure that row names, and the fix belongs on the host rather than in a repo.

## Next Steps

- [Devcontainer setup][devcontainer]: open the repo in the per-language .NET or Python devcontainer.
- [SSH commit signing][ssh-signing]: per-OS setup details, verification, and troubleshooting.

<!-- Repo -->

[agent-safety]: ../host-setup/agent-safety/README.md
[agent-safety-claude]: ../host-setup/agent-safety/claude/README.md
[agent-safety-codex]: ../host-setup/agent-safety/codex/README.md
[agent-safety-opencode]: ../host-setup/agent-safety/opencode/README.md
[audit]: ../AUDIT.md
[bootstrap]: ../host-setup/bootstrap.sh
[bootstrap-ps1]: ../host-setup/bootstrap.ps1
[devcontainer]: ./devcontainer.md
[governance-git-and-commit-rules]: ../GOVERNANCE.md#git-and-commit-rules
[host-gate]: ../scripts/host_gate.py
[host-setup-dir]: ../host-setup/
[host-setup-windows]: ../host-setup/windows/
[host-tools]: ../spec/host-tools.json
[issue-483]: https://github.com/ptr727/ProjectTemplate/issues/483
[issue-781]: https://github.com/ptr727/ProjectTemplate/issues/781
[operations]: ../OPERATIONS.md
[scripts-dir]: ../scripts/
[skills-install]: ../scripts/skills_install.py
[spec-dir]: ../spec/
[ssh-signing]: ./ssh-signing.md
[standup]: ../STANDUP.md

<!-- External -->

[codex-config]: https://developers.openai.com/codex/config-reference/
[cli-install-link]: https://github.com/cli/cli/blob/trunk/docs/install_linux.md
[cli-link]: https://cli.github.com/
[docker-install-link]: https://docs.docker.com/engine/install/
[git-restore-mtime-action-link]: https://github.com/chetan/git-restore-mtime-action
[git-tools-link]: https://github.com/MestreLion/git-tools
[keys-link]: https://github.com/settings/keys
