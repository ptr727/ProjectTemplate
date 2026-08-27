# Host Setup

What a machine needs before it can be worked in, and the tooling that puts it there. [`docs/host-setup.md`][host-setup] is the contract, meaning which tools a host must provide and why each floor exists. This directory is how a host comes to satisfy it.

## What Is Here

- [`bootstrap.sh`][bootstrap] stands a Debian or Ubuntu host up from nothing. It is the one file fetched on its own, because a host with no git and no checkout is what it exists to fix. It fetches this repository and runs the tooling from that tree.
- [`bootstrap.ps1`][bootstrap-ps1] does the same for native Windows. It runs under Windows PowerShell 5.1, the version every fresh Windows host guarantees, and hands off to PowerShell 7 once it has found or installed it, since every script it drives requires that version.
- [`menu.sh`][menu] is a human-facing front end over this repository's tooling: the host actions above, plus the repo-level tools ([`spec/audit.py`][audit-runner], [`scripts/carry.py`][carry], [`scripts/build_dist.py`][build-dist]) most of this repository authors for an agent following instructions rather than for a person choosing from a menu. It has no Windows counterpart yet.
- [`linux/`][linux] holds the tooling itself, for Debian and Ubuntu based hosts, Proxmox and WSL included. `install-tools.sh` installs and upgrades the host tools, `upgrade-host.sh` upgrades the packages of the current release or moves to the next one, `setup-github.sh` configures the SSH key, git, and commit signing, and `install-skills.sh` drives the hub's skills installer from the same tree.
- [`windows/`][windows] holds the tooling for native Windows, through `winget` and PowerShell 7. `install-tools.ps1` installs and upgrades the host tools, `upgrade-host.ps1` upgrades the winget packages and updates the WSL platform, `setup-github.ps1` configures the SSH key, git, and commit signing, `setup-wsl.ps1` installs a WSL distribution and reports the Docker Desktop integration, and `install-skills.ps1` drives the hub's skills installer from the same tree.
- [`agent-safety/`][agent-safety] holds the write-safety guards, deployed per machine and per account.

## Standing a Host Up

Three lines, on a Debian or Ubuntu host that has nothing:

```shell
sudo apt-get update && sudo apt-get install -y curl ca-certificates tar
curl -fsSLo bootstrap.sh https://raw.githubusercontent.com/ptr727/ProjectTemplate/main/host-setup/bootstrap.sh
bash bootstrap.sh
```

Downloaded and run rather than piped into a shell, for three reasons. Standard input stays a terminal, so the menu can ask. The file is readable before it is executed as root, which matters more here than anywhere else in this repository. And a re-run on a console reached over IPMI costs one line rather than a re-paste.

Piping it is still detected: with no action and no terminal it reports rather than guessing, and prints the three lines above as the remedy.

```shell
./bootstrap.sh --report                 # what each tool would do, changing nothing
./bootstrap.sh --host --yes             # sudo cache, packages, tools, git and GitHub, unattended
./bootstrap.sh --ref develop --report   # run the tooling as it is on develop
./bootstrap.sh --help                   # every action and option
```

Three lines as well, on a native Windows host that has nothing, pasted into a stock `powershell.exe` console:

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -UseBasicParsing -Uri https://raw.githubusercontent.com/ptr727/ProjectTemplate/main/host-setup/bootstrap.ps1 -OutFile bootstrap.ps1
powershell -ExecutionPolicy Bypass -File bootstrap.ps1
```

The first line is the Windows counterpart of installing `curl`: a fresh console's default TLS floor can predate 1.2, which every host this fetches from requires. The third runs under the `powershell.exe` a fresh host guarantees rather than `pwsh`, since `bootstrap.ps1` finds or installs PowerShell 7 itself and hands the rest of the run to it. `-ExecutionPolicy Bypass` clears both the default `Restricted` policy and the mark of the web `Invoke-WebRequest` leaves on the file, in one flag.

```powershell
.\bootstrap.ps1 -Report                # what each tool would do, changing nothing
.\bootstrap.ps1 -Host -Yes              # packages, tools, git and GitHub, unattended
.\bootstrap.ps1 -Ref develop -Report    # run the tooling as it is on develop
.\bootstrap.ps1 -Help                   # every action and option
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

## The Human Menu

`menu.sh` is fetchable on its own too, the same three-line shape as `bootstrap.sh`, and it also runs directly from a checkout that already has one:

```shell
curl -fsSLo menu.sh https://raw.githubusercontent.com/ptr727/ProjectTemplate/main/host-setup/menu.sh
bash menu.sh
host-setup/menu.sh   # from a checkout of this repository, or of any other repo in the fleet
```

It answers a different question than `bootstrap.sh` does. `bootstrap.sh` stands a host up and stops. `menu.sh` loops, because a person sitting down with it usually wants more than one thing done in a sitting, and it tells the hub apart from whichever repo it happens to be run from: a checkout of `ptr727/ProjectTemplate` gets the hub tasks (audit a cataloged repo, check the generated Skills distributions), a checkout of any other repo gets the downstream tasks too (check or pull the hub's verbatim-owned files into that repo's own worktree, per [`scripts/carry.py`][carry]), and every run gets the host tasks this directory's `linux/` tooling already provides. Only the downstream tasks need a repo to run from. The hub tasks fetch the hub themselves when there is no local checkout to reuse, so they still show and still work when the menu is run entirely standalone, off no checkout at all.

Reaching `spec/audit.py` and `scripts/carry.py` from outside a hub checkout means fetching one, the same "hosted and reached, never carried" model [`scripts/README.md`][scripts-readme] states for those tools generally. `menu.sh` clones fresh rather than reusing `bootstrap.sh`'s tarball, since `scripts/carry.py` itself checks that its hub argument is a real git checkout on a freshly fetched `origin/main` with no local changes, and a full clone rather than a shallow one, since `spec/audit.py` walks the hub's own commit history to judge whether a carried copy is trailing the file it was copied from. Run from inside the hub itself, that same freshness is confirmed against the local checkout before a hub task uses it, falling back to a fresh clone when the local checkout has moved on, so an audit or a Skills-distribution check never silently reads a stale or feature-branch tree.

## Which Revision a Run Used

`bootstrap.sh` resolves the ref it was given to the commit it names, prints that commit, and downloads that exact revision. A run therefore says which revision of the tooling it used, and a second run of the same ref cannot silently be a different tree. Where the resolve fails, which an unauthenticated rate limit can cause, the run says it cannot attribute itself and continues, since the download itself is unaffected.

`--ref` takes a branch, a tag, a pull request ref, or a commit. It is not only for testing an unmerged change: [`AUDIT.md`][audit] and the sync procedure both tell an agent to fetch this repository immediately before reading it, and a loader that could only reach `main` could not support a repository testing a change before it promotes.

## Three Rules This Directory Follows

**Group by whichever axis has one member.** `agent-safety/` is one concern across three platforms, so it is a concern directory holding `install.sh`, `install.ps1` and `install.py`. `linux/` is three concerns on one platform, so it is a platform directory. Windows host tooling therefore sits at `windows/` rather than beside the Linux scripts, because the `winget` equivalent of `install-tools.sh` is a different program rather than a translation of one. It carries one registry record per tool where the Linux script carries four functions, since every Windows source is `winget` and the per-tool variation those functions exist for does not arise. `windows/` also carries a fourth script with no Linux peer, because WSL is a Windows-side concern. The loader is the same shape as `agent-safety/`, not as `linux/`/`windows/`: one concern, two platforms, so `bootstrap.ps1` sits beside `bootstrap.sh` at the top level rather than inside `windows/`.

**Nothing here needs Python to stand a host up, and neither loader needs an interpreter to fetch what it drives.** [`docs/host-setup.md`][host-setup] carries that as part of the contract, with the reasoning. `bootstrap.sh` needs only `curl` and `tar`. `bootstrap.ps1` needs only `tar.exe`, which has shipped with Windows since 1803, and installs its one further dependency, `pwsh`, itself through `winget`. The one exception is the `install-skills` pair, which drives the Python installer at `scripts/skills_install.py` and runs last in a stand-up for exactly that reason: `install-tools` has provided the interpreter by then, and run alone on a host without one it stops and names the tools step as its prerequisite. `menu.sh` needs `git` to fetch the hub, and it checks for `python3` the same lazy way: only the tasks that call a Python tool ask for it, and every host task still works without one. Neither loader runs a gate as a closing step: [`scripts/host_gate.py`][host-gate] measures a host against the floors and is not called from here, and nothing here is called from it. A host set up by hand years ago is an ordinary host, so the gate reports what it is missing and running this tooling is a remedy a person chooses. The two are joined at code time instead, by [`scripts/tests/test_bootstrap.py`][test-bootstrap] asserting that every tool the spec requires is one this tooling can provide.

**The scripts under `linux/` and `windows/` share no file, and the duplication is deliberate.** Each is independently fetchable and runnable on its own, which is the property that lets a host with no checkout use one without the others. A shared helper file would take that away: the moment one script sources a sibling, fetching it alone yields a script that dies on a missing file. What is duplicated is about thirty lines each of logging, the dry-run wrapper, the confirmation prompt, and a temporary directory, and those copies are identical rather than merely similar. Do not factor them out. `bootstrap.ps1` now exercises the same fetchability argument `bootstrap.sh` always has, rather than merely being written to allow for it. The `install-skills` pair is the one recorded exception to independent fetchability: it drives `scripts/skills_install.py` at the tree root, because the skills content lives in the tree, so a copy fetched alone has nothing to install and the property cannot apply to it.

<!-- Repo -->

[agent-safety]: ./agent-safety/
[audit]: ../AUDIT.md
[audit-runner]: ../spec/audit.py
[bootstrap]: ./bootstrap.sh
[bootstrap-ps1]: ./bootstrap.ps1
[build-dist]: ../scripts/build_dist.py
[carry]: ../scripts/carry.py
[host-gate]: ../scripts/host_gate.py
[host-setup]: ../docs/host-setup.md
[linux]: ./linux/
[menu]: ./menu.sh
[scripts-readme]: ../scripts/README.md
[test-bootstrap]: ../scripts/tests/test_bootstrap.py
[windows]: ./windows/
