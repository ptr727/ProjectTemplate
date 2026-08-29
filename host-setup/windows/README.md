# Windows Host Setup

The tooling that makes a native Windows host satisfy the contract in [`docs/host-setup.md`][host-setup], through `winget` and PowerShell 7. That document is the contract, meaning which tools a host must provide and why each floor exists. This directory is how a Windows host comes to satisfy it.

## What Is Here

- [`install-tools.ps1`][install-tools] installs and upgrades the host tools, and reports what each one is installed at, where it came from, and which scope it sits in. Installing, upgrading or reinstalling `docker` also brings the WSL platform up to Docker Desktop's own floor where it is behind.
- [`upgrade-host.ps1`][upgrade-host] upgrades the packages `winget` manages and updates the WSL platform.
- [`setup-github.ps1`][setup-github] configures the SSH key, git, and commit signing.
- [`setup-wsl.ps1`][setup-wsl] installs a WSL distribution and reports how Docker Desktop is integrated with the ones this host runs.

Each runs on its own, and each takes `-Help`.

```powershell
host-setup\windows\install-tools.ps1            # report
host-setup\windows\install-tools.ps1 -Install
host-setup\windows\upgrade-host.ps1 -Status
host-setup\windows\setup-github.ps1 -Status
host-setup\windows\setup-wsl.ps1 -Status
```

## Requirements

**PowerShell 7 or later**, which is `pwsh` rather than the `powershell.exe` that ships with Windows. Each script refuses an older one and prints `winget install --id Microsoft.PowerShell --exact --source winget` as the remedy. `pwsh` is deliberately not a managed tool: a host that cannot run these scripts cannot be repaired by them.

**winget**, which arrives with App Installer from the Microsoft Store.

**Script execution.** A `git clone` carries no mark of the web, so these run under the default `RemoteSigned` policy. A browser-downloaded zip does carry one, and is blocked until `Unblock-File` clears the mark. The `.\` prefix is required when running a script from the current directory, exactly as it is for [`agent-safety/claude/install.ps1`][agent-safety].

`pwsh -File .\install-tools.ps1` answers the `.\` rule and **not** the policy, which still applies to it: on a marked file under `RemoteSigned` it fails with a `SecurityError` naming the file as unsigned. The form that runs whatever the policy says is `pwsh -ExecutionPolicy Bypass -File .\install-tools.ps1`, which is what [`docs/host-setup.md`][host-setup] already gives for the write-safety installer. Prefer clearing the mark with `Unblock-File` over bypassing, since the bypass covers every script that run touches.

## Why winget Is the Only Source

Every tool the contract names has a winget package, so nothing here carries a fallback. That is the whole difference from the Linux script, which needs three kinds of source because the distribution's package trails upstream on `gh`, on `node` and on `uv`. Where `winget` tracks upstream, the machinery that exists to work around a stale feed has nothing to do.

Ripgrep uses the upstream project's documented `BurntSushi.ripgrep.MSVC` package. It provides the `rg` command used for repository searches and `rg --files` discovery.

A tool that turns out to have no winget package is a finding to raise rather than a second source to add quietly, because the moment one tool comes from somewhere else this directory stops being one program and becomes two.

## Elevation and Scope

**Run these unelevated.** No `--scope` is passed unless `-Scope` names one, so `winget` acts on the copy it finds and an installer that needs administrator raises its own prompt. That is the path with the fewest failures, for three reasons that point the same way: forcing user scope installs a second copy beside a working machine wide one rather than upgrading it, some installers fail outright when launched from an already elevated process, and a user scope install made from an elevated process lands in the administrator's profile rather than the caller's.

Nothing here elevates itself. A run that is already elevated says so and carries on, since that is a caution rather than a refusal.

**Scope is measured, not assumed.** `winget list --scope user` and `--scope machine` answer separately, so the report names where each tool actually sits and catches the case worth catching, which is a tool installed in **both** scopes with one copy shadowing the other on `PATH`.

**`-Reinstall` is the only action that removes anything**, and it always asks first. An `-Upgrade` whose `-Scope` disagrees with the installed copy refuses and names it, rather than upgrading in place or adding a second copy.

**A state that could not be read is never reported as an absence.** Where `winget` does not answer what is installed, the tool reports `unreadable` rather than `missing`, and an install or upgrade skips it and collects it as a failure. Installing against a state nobody measured is how a second copy lands beside a first one that was there all along.

**What provenance can and cannot be detected.** Scope is solid, and so is a tool that answers on `PATH` while `winget` knows no package for it, which reports as `unmanaged`. Whether a package was installed *by* winget is not solid and is not claimed: winget runs the vendor's own installer for an `exe` or an `msi`, so the resulting uninstall entry is identical whether winget invoked it or a person did. The one positive marker is the uninstall key winget writes for itself on a portable or archive package, which the report names where it is present and says nothing about where it is absent.

## Self-Updating Packages

Some applications update themselves and never rewrite the version recorded at install time. `winget` reports them as permanently behind, and its manifest marks them as requiring explicit targeting so an upgrade of everything leaves them alone.

These are listed apart, left alone, and printed with **no command beside them**. Offering one invites a full reinstall over a working, already current copy in pursuit of a number that will not move. `MSYS2` is the worked example: it upgrades through `pacman` from inside the msys shell, and the version `winget` shows is the installer's rather than the one it runs. `install-tools.ps1` reports such a tool as `self-updating` rather than `outdated` for the same reason.

## Why There Is No Linter Category

Neither this tooling nor its Linux sibling installs `markdownlint`, `cspell`, `actionlint`, `editorconfig-checker`, `shellcheck`, `shfmt`, `PSScriptAnalyzer` or `ruff`, and that is a decision rather than a gap.

Each of those runs as a pinned container image or through `uvx`, which is what keeps a local run and CI the same check: the image tag fixes the version. Installing native copies through `winget` would put a second, unpinned version of each on the host, and a local run would then differ from CI, which is the exact property the pinned images exist to guarantee. The only host requirements any of it creates are `docker` and `uv`, and both are already in the registry.

## bootstrap.ps1

[`bootstrap.ps1`][bootstrap-ps1] is a fifth script, and the odd one out: it sits beside [`bootstrap.sh`][bootstrap] at the top of [`host-setup/`][host-setup-readme] rather than here, since it is the same concern as that file rather than a fifth member of this registry. It exists to stand up a host that has no git and no checkout, the same problem `bootstrap.sh` solves on Linux.

An unverified loader is worse than none, and that is why this one does more than fetch a tarball. It runs under Windows PowerShell 5.1, the one shell a fresh Windows host guarantees, and hands off to `pwsh` only once it has found or installed it through `winget`, since every script in this directory refuses to run under anything older. It pins TLS 1.2 itself rather than assume a fresh console's default reaches GitHub. And it checks a fetched tree for the marker it wrote before removing anything under `-Dir`, the same rule `bootstrap.sh` follows on Linux. None of that verifies the tooling it goes on to run, which stays exactly as unverified against a genuinely fresh host as it always was. It verifies the one step earlier this loader adds, standing up the interpreter everything past it depends on.

```powershell
pwsh -NoProfile -File ..\bootstrap.ps1 -Help
..\bootstrap.ps1 -Report -DryRun
```

## menu.ps1

[`menu.ps1`][menu-ps1] sits beside [`menu.sh`][menu] at the top of [`host-setup/`][host-setup-readme] for the same reason `bootstrap.ps1` sits beside `bootstrap.sh`: it fronts this registry's scripts rather than joining it, and [`host-setup/README.md`][host-setup-readme] "The Human Menu" carries its full contract. It shares `bootstrap.ps1`'s PowerShell 5.1 to `pwsh` handoff, and it adds one entry with no Linux peer, `setup-wsl.ps1 -Status`, since WSL is a Windows-side concern.

```powershell
pwsh -NoProfile -File ..\menu.ps1 -Help
..\menu.ps1 -DryRun
```

## Docker Desktop and WSL

`setup-wsl.ps1` **reports** the Docker Desktop integration and never writes it. Docker holds those settings in memory and rewrites its settings file from that copy while it runs, so an edit made here is discarded at Docker's next save and an edit made while it is stopped is undone by the next start. Change it in Docker Desktop under Settings, Resources, WSL integration.

`upgrade-host.ps1 -Wsl` **refuses while Docker Desktop is running**, because Docker holds the WSL service open and the update then fails part way rather than declining. Quit Docker from its tray icon first, since pausing it is not enough. The refusal fires under `-DryRun` too, so a dry run reports the truth rather than printing a command that would not have worked.

Docker's own `docker-desktop` distribution is excluded from every distribution listing, since it is Docker's rather than one an operator installed.

`install-tools.ps1` checks, before installing or upgrading `docker`, that `wsl.exe` is present and reports a WSL version at or above `2.1.5`, Docker Desktop's own documented floor for the platform it depends on. Where `wsl.exe` is missing entirely it skips `docker` and names the remedy (`wsl --install --no-distribution`), the one case this still leaves to a person: standing up WSL from nothing is a different action from bringing an existing platform current, the same reasoning that keeps a distribution *install* in `setup-wsl.ps1` rather than folded in here. The same check surfaces as a note under `-Report`, read-only, before a caller ever runs `-Install`.

Where WSL is present but behind the floor, or `docker` itself is about to change version, `install-tools.ps1` drives the fix itself rather than only naming it, asking first unless `-Yes` was given. Where Docker Desktop is running, it stops it through its own CLI (`docker desktop stop`, not the tray icon) rather than leave it up through the change, runs `wsl --update` where the floor is not met, and once the `docker` package itself is also settled, shuts every WSL distribution down with `wsl --shutdown` and starts Docker Desktop again (`docker desktop start`). A run that finds Docker Desktop already stopped has nothing to restore, so it stops there too: neither the shutdown nor the restart runs.

This moved a boundary the tool used to hold: WSL used to be read-only here, and `upgrade-host.ps1 -Wsl` was the only thing that ever ran `wsl --update`, on the reasoning that an update restarts every distribution and that does not belong as a side effect of installing a different tool. Two things moved it. Docker Desktop's own per-distro WSL integration goes stale across an engine bump often enough to have a name on Docker's own tracker (`WSL integration with distro '<name>' unexpectedly stopped`), and the dialog's own "Restart the WSL integration" button does not clear it, since it retries the proxy inside the distro that is already running against the same stale state. A full stop, `wsl --shutdown`, start cycle does clear it. And once a `docker` version bump already needed that exact stop-then-restart window for its own integration to recover, updating WSL inside that same window costs nothing extra, and skips the `upgrade-host.ps1 -Wsl` refusal a Docker Desktop already running would otherwise walk straight into.

`wsl --update` installs through its own MSI and raises a UAC prompt when this `pwsh` is not itself elevated, the state the rest of this tooling deliberately stays in. Nothing can answer that prompt unattended, so where the process is neither elevated nor at an interactive console, `install-tools.ps1` refuses to start the update rather than hang, and restores Docker Desktop to how it found it first.

## Differences From the Linux Tooling

| Linux | Windows | Why |
| --- | --- | --- |
| `upgrade-host.sh --release` moves to the next distribution release | no peer | Windows Update owns a feature update, and an action pretending to drive one is the one thing this must not carry |
| `install-tools.sh` carries four functions per tool | `install-tools.ps1` carries one registry record per tool | Every source is `winget`, so the per-tool variation those functions exist for does not arise |
| `git-restore-mtime` is managed | not managed | The spec declares it not applicable on Windows, since it serves a Linux deploy path |
| `install-tools.sh` refuses docker entirely inside a WSL *distribution* | `install-tools.ps1` checks the WSL *platform* version before installing docker | A WSL distribution takes docker only from Docker Desktop's own WSL integration, and Windows needs WSL2 present for Docker Desktop's own backend |
| `sudo` re-runs a command as root | nothing elevates | `winget` raises UAC per installer, which is the path with the fewest failures |
| `install-tools.sh --sudo-timestamp` shares one sudo credential cache across a user's terminals | no peer | Nothing here elevates on Windows, so there is no cached credential to share |
| `unmanaged` means the upstream repository is unconfigured | `unmanaged` means the tool is on `PATH` and winget knows no package for it | The same question, by a different mechanism |
| `credential.helper cache --timeout=3600` | `credential.helper manager`, and only where unset | Git Credential Manager ships with Git for Windows |
| `ssh-agent` is a socket, started per shell | `ssh-agent` is a Windows service, reported and not started | Starting it needs administrator, and nothing here elevates |
| no WSL script | `setup-wsl.ps1` | WSL is a Windows-side concern with no Linux-side peer |

The scripts here share no file with each other, and the roughly thirty duplicated lines of logging, the dry-run wrapper and the confirmation prompt are identical rather than merely similar. That is the same rule the Linux scripts follow, for the same reason, and it is stated in [`host-setup/README.md`][host-setup-readme]. Do not factor them out.

## Verification

Read-only first, and nothing below changes the host.

```powershell
pwsh -NoProfile -File host-setup\windows\install-tools.ps1 -Help
host-setup\windows\install-tools.ps1 -List
host-setup\windows\install-tools.ps1 -Report
host-setup\windows\upgrade-host.ps1 -Status
host-setup\windows\setup-github.ps1 -Status
host-setup\windows\setup-wsl.ps1 -Status
host-setup\bootstrap.ps1 -Report -DryRun
```

Then the dry runs, which print what each action would do:

```powershell
host-setup\windows\install-tools.ps1 -Upgrade -DryRun
host-setup\windows\install-tools.ps1 -Install -Repo C:\path\to\repository -DryRun
host-setup\windows\upgrade-host.ps1 -Packages -DryRun
host-setup\windows\setup-github.ps1 -Configure -DryRun
host-setup\windows\setup-wsl.ps1 -Install Debian -DryRun
```

`-Repo` adds the repository's `install.windows` entries to the report or action. Only `winget` package IDs are accepted. The script does not execute the declaration's `remedy` text.

Two of those are guards rather than previews, and each prints a refusal rather than a command: `upgrade-host.ps1 -Wsl -DryRun` on a host running Docker Desktop, and an `-Upgrade` whose `-Scope` disagrees with the installed copy. A `[dry run]` line from either means the guard sits in the wrong place.

The scripts are checked by `PSScriptAnalyzer`, which runs in CI as the peer of the `shellcheck` step and locally through the invocation in [`GOVERNANCE.md`][governance]. [`scripts/tests/test_bootstrap.py`][test-bootstrap] asserts that every tool the spec requires is one this registry carries, and that no script here opens with a shebang.

<!-- Repo -->

[agent-safety]: ../agent-safety/claude/install.ps1
[bootstrap]: ../bootstrap.sh
[bootstrap-ps1]: ../bootstrap.ps1
[governance]: ../../GOVERNANCE.md
[host-setup]: ../../docs/host-setup.md
[host-setup-readme]: ../README.md
[install-tools]: ./install-tools.ps1
[menu]: ../menu.sh
[menu-ps1]: ../menu.ps1
[setup-github]: ./setup-github.ps1
[setup-wsl]: ./setup-wsl.ps1
[test-bootstrap]: ../../scripts/tests/test_bootstrap.py
[upgrade-host]: ./upgrade-host.ps1
