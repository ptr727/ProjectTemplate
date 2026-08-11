# Host Setup

What a machine needs before it can be worked in, and the tooling that puts it there. [`docs/host-setup.md`][host-setup] is the contract, meaning which tools a host must provide and why each floor exists. This directory is how a host comes to satisfy it.

## What Is Here

- [`bootstrap.sh`][bootstrap] stands a host up from nothing. It is the one file fetched on its own, because a host with no git and no checkout is what it exists to fix. It fetches this repository and runs the tooling from that tree.
- [`linux/`][linux] holds the tooling itself, for Debian and Ubuntu based hosts, Proxmox and WSL included. `install-tools.sh` installs and upgrades the host tools, `upgrade-host.sh` upgrades the packages of the current release or moves to the next one, and `setup-github.sh` configures the SSH key, git, and commit signing.
- [`windows/`][windows] holds the tooling for native Windows, through `winget` and PowerShell 7. `install-tools.ps1` installs and upgrades the host tools, `upgrade-host.ps1` upgrades the winget packages and updates the WSL platform, `setup-github.ps1` configures the SSH key, git, and commit signing, and `setup-wsl.ps1` installs a WSL distribution and reports the Docker Desktop integration.
- [`agent-safety/`][agent-safety] holds the write-safety guards, deployed per machine and per account.

## Standing a Host Up

Three lines, on a host that has nothing:

```shell
sudo apt-get update && sudo apt-get install -y curl ca-certificates tar
curl -fsSLo bootstrap.sh https://raw.githubusercontent.com/ptr727/ProjectTemplate/main/host-setup/bootstrap.sh
bash bootstrap.sh
```

Downloaded and run rather than piped into a shell, for three reasons. Standard input stays a terminal, so the menu can ask. The file is readable before it is executed as root, which matters more here than anywhere else in this repository. And a re-run on a console reached over IPMI costs one line rather than a re-paste.

Piping it is still detected: with no action and no terminal it reports rather than guessing, and prints the three lines above as the remedy.

```shell
./bootstrap.sh --report                 # what each tool would do, changing nothing
./bootstrap.sh --host --yes             # packages, tools, git and GitHub, unattended
./bootstrap.sh --ref develop --report   # run the tooling as it is on develop
./bootstrap.sh --help                   # every action and option
```

Each tool also runs on its own, on a host that already has a checkout:

```shell
host-setup/linux/install-tools.sh          # report
host-setup/linux/install-tools.sh --install
host-setup/linux/upgrade-host.sh --status
host-setup/linux/setup-github.sh --status
```

On native Windows, in PowerShell 7:

```powershell
host-setup\windows\install-tools.ps1          # report
host-setup\windows\install-tools.ps1 -Install
host-setup\windows\upgrade-host.ps1 -Status
host-setup\windows\setup-github.ps1 -Status
host-setup\windows\setup-wsl.ps1 -Status
```

There is no `bootstrap.ps1`, so a Windows host obtains this repository first. The problem `bootstrap.sh` solves is a host with no git and no checkout, and the Windows form of that problem has no one-liner anybody here has run, so none is offered.

## Which Revision a Run Used

`bootstrap.sh` resolves the ref it was given to the commit it names, prints that commit, and downloads that exact revision. A run therefore says which revision of the tooling it used, and a second run of the same ref cannot silently be a different tree. Where the resolve fails, which an unauthenticated rate limit can cause, the run says it cannot attribute itself and continues, since the download itself is unaffected.

`--ref` takes a branch, a tag, a pull request ref, or a commit. It is not only for testing an unmerged change: [`AUDIT.md`][audit] and the sync procedure both tell an agent to fetch this repository immediately before reading it, and a loader that could only reach `main` could not support a repository testing a change before it promotes.

## Three Rules This Directory Follows

**Group by whichever axis has one member.** `agent-safety/` is one concern across three platforms, so it is a concern directory holding `install.sh`, `install.ps1` and `install.py`. `linux/` is three concerns on one platform, so it is a platform directory. Windows host tooling therefore sits at `windows/` rather than beside the Linux scripts, because the `winget` equivalent of `install-tools.sh` is a different program rather than a translation of one. It carries one registry record per tool where the Linux script carries four functions, since every Windows source is `winget` and the per-tool variation those functions exist for does not arise. `windows/` also carries a fourth script with no Linux peer, because WSL is a Windows-side concern.

**Nothing here needs Python, and `bootstrap.sh` needs only `curl`.** [`docs/host-setup.md`][host-setup] carries that as part of the contract, with the reasoning. It is why `bootstrap.sh` runs no gate as a closing step: [`scripts/host_gate.py`][host-gate] measures a host against the floors and is not called from here, and nothing here is called from it. A host set up by hand years ago is an ordinary host, so the gate reports what it is missing and running this tooling is a remedy a person chooses. The two are joined at code time instead, by [`scripts/test_bootstrap.py`][test-bootstrap] asserting that every tool the spec requires is one this tooling can provide.

**The scripts under `linux/` and `windows/` share no file, and the duplication is deliberate.** Each is independently fetchable and runnable on its own, which is the property that lets a host with no checkout use one without the others. A shared helper file would take that away: the moment one script sources a sibling, fetching it alone yields a script that dies on a missing file. What is duplicated is about thirty lines each of logging, the dry-run wrapper, the confirmation prompt, and a temporary directory, and those copies are identical rather than merely similar. Do not factor them out. On the Windows side the fetchability argument is one no loader exercises yet, and the duplication is kept anyway so a loader added later inherits the property rather than having to introduce it.

<!-- Repo -->

[agent-safety]: ./agent-safety/
[audit]: ../AUDIT.md
[bootstrap]: ./bootstrap.sh
[host-gate]: ../scripts/host_gate.py
[host-setup]: ../docs/host-setup.md
[linux]: ./linux/
[test-bootstrap]: ../scripts/test_bootstrap.py
[windows]: ./windows/
