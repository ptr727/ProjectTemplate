# Linux Host Setup

The tooling that makes a Debian or Ubuntu based host satisfy the contract in [`docs/host-setup.md`][host-setup], Proxmox and WSL included. That document is the contract, meaning which tools a host must provide and why each floor exists. This directory is how a Linux host comes to satisfy it.

## What Is Here

- [`install-tools.sh`][install-tools] installs and upgrades the host tools, and reports what each is installed at, what upstream carries, where it comes from, and its status.
- [`upgrade-host.sh`][upgrade-host] upgrades the packages of the current release, and moves the host to the next release as a separate action.
- [`setup-github.sh`][setup-github] configures the SSH key, git, and commit signing, and checks both key registrations against what GitHub publishes.
- [`install-skills.sh`][install-skills] drives the hub's skills installer at [`scripts/skills_install.py`][skills-install] from this tree.

Each runs on its own, and each takes `--help`.

```shell
host-setup/linux/install-tools.sh          # report
host-setup/linux/install-tools.sh --install
host-setup/linux/upgrade-host.sh --status
host-setup/linux/setup-github.sh --status
host-setup/linux/install-skills.sh --report
```

Each script is LF with a shebang, and its executable bit is tracked in git, so a fresh checkout runs it without a `bash` prefix. That is the Linux form of "this will run", and [`scripts/test_bootstrap.py`][test-bootstrap] asserts both.

## Requirements

**A Debian or Ubuntu based host, identified from `/etc/os-release`.** A distribution that is neither but declares `ID_LIKE` debian is treated as Debian, with a warning that it is untested. Proxmox reports itself as its Debian base, so it needs no case of its own. Anything else is refused, since every install path here is apt or assumes apt's layout.

**Root or sudo.** Nothing here elevates wholesale. Each script runs as the caller and puts `sudo` in front of only the commands that change the host, so file staging and every read stay unprivileged. A run that is not root and finds no `sudo` refuses up front rather than failing partway.

**A terminal, or `--yes`.** A run with no terminal on standard input and no `--yes` refuses to change the host, so a scheduled run cannot hang on a prompt nobody answers. On `upgrade-host.sh`, `--yes` also keeps the installed configuration file on a packaging conflict, since an unattended run has nobody to answer dpkg's prompt and a replaced config is the harder half to notice afterwards.

`curl` is not a requirement, it is a managed prerequisite: a minimal image carries none, so every upstream read is guarded, and an install run puts `ca-certificates`, `curl`, `gnupg`, and `gpgv` in place before the first tool.

## Why There Are Three Kinds of Source

The Windows registry has one source because `winget` tracks upstream. Here the distro package trails upstream on `gh`, on `node`, and on `uv`, so a tool comes from whichever source keeps up:

- **The distro**, for `git` and `python`, where apt's own package is current enough.
- **An upstream apt repository**, for `gh`, `node`, and `docker`, and for `dotnet` as a fallback, where upstream publishes one.
- **A released binary into `/usr/local/bin`**, for `jq`, `uv`, and `git-restore-mtime`, where upstream publishes no repository.

No version is written into the script. Each upstream is asked what it carries now, so the script does not go stale between releases, and every step is idempotent: a keyring or sources file is written only when its content differs, and a re-run repairs drift rather than assuming a clean host.

**A keyring is proved, not trusted.** Before a fetched signing key is installed, `gpgv` checks that it actually signs the repository's own `InRelease` metadata. An upstream that rotates or adds a key breaks a pinned fingerprint list but not this check, and a host where the check cannot run stops rather than trusting the download. A released binary is checked against the sha256 list its upstream publishes beside it, for the same reason.

**Keyrings land in `/etc/apt/keyrings` and sources as deb822 files in `/etc/apt/sources.list.d`.** A predecessor in the old location or the old one-line format is removed first, so apt never reads the same repository twice.

## PATH and Shadowing

`/usr/local/bin` precedes `/usr/bin`, which is why the distro's `jq` can stay installed and stay shadowed: the upstream binary wins without removing a package something else may depend on.

The hazard runs the other way too. A copy of `jq`, `uv`, or `git-restore-mtime` sitting earlier on `PATH` keeps answering after this script installs a newer one, which reads as an upgrade that did not take. The report names such a shadow. `--upgrade` removes it after a prompt, and `--install` removes it only when no managed copy exists yet, since removing a newer shadow beside an older managed copy would downgrade what `PATH` resolves to. A file a distro package owns is never removed, because deleting it would desync dpkg's database from the filesystem, so the remedy named there is the `PATH` order itself. The removal loops, since `PATH` can stack more than one shadow ahead of `/usr/local/bin`, and a relative `PATH` entry is never trusted as a shadow at all.

## What the Report Says

A report changes nothing and reads the apt cache as it stands, so an available version is as current as the last `apt update`. Versions compare like with like: apt versions for an apt managed tool, upstream versions for a standalone binary. `docker` is read from the CLI rather than from the `docker-ce` package, because on a WSL distribution Docker Desktop's integration is a working `docker` with no apt package behind it, and its target is stripped of the epoch and packaging revision for the same like-with-like reason.

`unmanaged` means the tool is installed from the distro while its upstream repository is unconfigured. Reporting it as current against the distro's own version is the one thing the report must not say, since the question is currency against upstream. This is the same word the Windows report uses for a different mechanism, where it means a tool on `PATH` that `winget` knows no package for.

An install or upgrade collects a tool whose install fails and carries on, so one failure does not strand the rest of the run. A refusal is different and ends the run: an unverifiable keyring, a checksum mismatch, or a declined prompt stops everything, because continuing past one would install something nobody vouched for.

## Docker, node, and dotnet

**Inside a WSL distribution, docker comes only from Docker Desktop's own WSL integration, never from installing `docker-ce`.** A native install would run a second engine beside Desktop's, so `--install` and `--upgrade` always skip it there and point at Docker Desktop's Settings instead. The skip counts as success only where `docker` already answers, so a run cannot exit clean having neither installed docker nor found it working. On a native host, the conflicting packages Docker's own uninstall list names are removed first, and non-root use (`usermod -aG docker`) is left to the operator as a group choice rather than a question of presence.

**Installing `node` displaces distro packages.** The upstream package carries `npm` itself and conflicts with the distro's `npm` and `nodejs-doc`, so the script asks apt what it would remove and puts that list in front of the operator before continuing. Asking apt beats naming the conflicts here, because the conflict set belongs to the upstream package and changes without notice. The major line installed is whatever upstream currently marks LTS, read from its release index at run time.

**For `dotnet`, the distro feed is the default and Microsoft's feed is the fallback**, added only where the distro carries no SDK at all. Mixing the two feeds is what breaks a host, and Microsoft's feed carries amd64 only, so any other architecture without a distro SDK is a named skip. The default set is the newest SDK line the feed carries, and `--optional` adds every other line, for a host that builds against more than one.

## Release Upgrades

`upgrade-host.sh` splits the routine from the rare: `--packages` upgrades within the current release, and `--release` is its own action because the release upgrade is where hosts differ.

**A release upgrade is refused where this script cannot carry it safely.** Proxmox major upgrades are a documented procedure with their own preconditions, currently the [Proxmox upgrade guide][proxmox-upgrade], and the refusal points there. A distribution that is neither Debian nor Ubuntu is refused too, since the sources rewrite below has no meaning there. Refusing is the point of running this rather than apt by hand.

**One release at a time.** Both distributions support exactly that, so a host two releases behind is upgraded by running this twice.

**Debian is carried by rewriting the codename in its apt sources, and only in sources that point at Debian's own mirrors.** A third party repository may have no suite for the new release yet, so it is named and left alone, and what to do about it is the operator's call. The sources are backed up to `/var/backups/upgrade-host` first, and a backup that cannot be taken stops the upgrade, since it is the only way back. A host on a mirror outside `debian.org`, or one tracking `stable` rather than a codename, is refused with instructions to edit its own sources.

**Ubuntu is carried by `do-release-upgrade`**, which handles its own sources. Whether an LTS or every release is offered is the host's own policy in `/etc/update-manager/release-upgrades`, deliberately not decided here.

**Preconditions run before the point of no return.** Held packages, a dpkg audit reporting half-configured packages, and low free space on `/var` are each surfaced first, because a release upgrade failing partway is the worst place to find any of them.

## Restarts, Kernels, and WSL

A WSL distribution runs the kernel Windows gives it, so a restart there is `wsl --shutdown` from Windows followed by a relaunch, and the script says exactly that instead of suggesting `reboot`. Debian does not always write `/var/run/reboot-required`, so the newest kernel in `/boot` is also compared against `uname -r`, and a host with no `/boot`, which a container and a WSL distribution both are, has no kernel of its own to compare.

## GitHub Setup

Two steps cannot be automated, because they happen in a browser: registering the public key as an authentication key, and registering the same key again as a signing key. `setup-github.sh --configure` stops at each, prints the key and where to paste it, then checks afterwards that the registration took, by reading the key lists GitHub publishes for the account, which needs no token. A check that could not reach GitHub is reported apart from a key that is not registered, since sending someone to register a key that is already there is the wrong remedy.

**`--status` is read-only end to end.** Its SSH probes run in batch mode so a passphrase prompt cannot hang an unattended run, and no probe ever enrolls github.com's host key behind the reader's back. Enrolling is `--configure`'s job, and the first enrollment is the one moment a substituted host key would be accepted for good, so the offered key is checked against the fingerprints GitHub publishes at `api.github.com/meta` before it is recorded, and a check that cannot run is a refusal.

**The identity comes from the flags, then from what the host already carries, then from the maintainer's default, in that order.** Reading the host first is what keeps a machine configured for somebody else from being quietly rewritten by a run meant to be safe to repeat.

**The managed key is probed on its own**, with the host's ssh config and agent excluded, because a default identity file or an agent key can authenticate as a different account. A host that reaches GitHub with some other key is working but not managed, and the run says so rather than ending in "Done" with the managed key registered nowhere.

**Signing is proved end to end**, by signing and verifying a commit in a throwaway repository, since reading the settings back cannot catch a wrong `allowed_signers` entry: that reads as correct and fails only when a signature is actually checked.

**The key path settings are written in tilde form** (`~/.ssh/id_ed25519.pub`), because git expands the tilde and the hosts configured by hand already hold that form, so writing it leaves an already configured host untouched.

**`--shared-checkout` exists because `safe.directory` and `core.sharedRepository` are relaxations, not defaults.** They are applied only for a path the caller names, a host one account uses needs neither, and `*` is accepted but called out as turning the ownership check off everywhere.

## install-skills.sh Is the Exception

The sibling scripts are independently fetchable, and this one deliberately is not: it drives `scripts/skills_install.py` at the tree root, and the skills content lives in the tree, so a copy fetched alone has nothing to install. Python 3.7 or later is its one dependency, which is why the bootstrap runs it last, and run on a host without one it stops and names the tools step as its prerequisite.

## Why There Is No Linter Category

Neither this tooling nor its Windows sibling installs `markdownlint`, `cspell`, `actionlint`, `editorconfig-checker`, `shellcheck`, `PSScriptAnalyzer` or `ruff`, and that is a decision rather than a gap. Each runs as a pinned container image or through `uvx`, which is what keeps a local run and CI the same check, and installing native copies would put a second, unpinned version of each on the host. The only host requirements any of it creates are `docker` and `uv`, and both are already managed here. The [Windows README][windows-readme] states the same decision from its side.

## bootstrap.sh

[`bootstrap.sh`][bootstrap] sits beside [`bootstrap.ps1`][bootstrap-ps1] at the top of [`host-setup/`][host-setup-readme] rather than here, since standing up a host with no git and no checkout is one concern across two platforms rather than a fifth member of this directory. It needs only `curl` and `tar`, fetches this repository, and runs the scripts here from that tree.

## Differences From the Windows Tooling

The comparison is tabulated once, in the [Windows README][windows-readme], so the two columns cannot drift apart. The short version: this side needs three kinds of source where `winget` needs one, carries the release upgrade Windows Update owns on that side, elevates per command through `sudo` where `winget` raises UAC per installer, and manages `git-restore-mtime`, which the spec declares not applicable on Windows.

## Verification

Read-only first, and nothing below changes the host.

```shell
host-setup/linux/install-tools.sh --help
host-setup/linux/install-tools.sh --list
host-setup/linux/install-tools.sh          # report
host-setup/linux/upgrade-host.sh --status
host-setup/linux/setup-github.sh --status
host-setup/linux/install-skills.sh --report
```

Then the dry runs, which print what each action would run:

```shell
host-setup/linux/install-tools.sh --upgrade --dry-run
host-setup/linux/upgrade-host.sh --release --dry-run
host-setup/linux/setup-github.sh --configure --dry-run
```

Two of those are guards rather than previews. `--release --dry-run` on a Proxmox host prints the refusal, not the commands, and a docker `--upgrade --dry-run` inside a WSL distribution prints the skip. A `[dry run]` line from either means the guard sits in the wrong place.

The scripts are checked by `shellcheck`, which runs in CI over every `.sh` file `git ls-files` returns and locally through the same `koalaman/shellcheck:stable` container. [`scripts/test_bootstrap.py`][test-bootstrap] asserts that every tool the spec requires on Linux is one `install-tools.sh` can provide or a recorded exception, and that each script here is tracked executable so a fresh checkout can run it.

<!-- Repo -->

[bootstrap]: ../bootstrap.sh
[bootstrap-ps1]: ../bootstrap.ps1
[host-setup]: ../../docs/host-setup.md
[host-setup-readme]: ../README.md
[install-skills]: ./install-skills.sh
[install-tools]: ./install-tools.sh
[setup-github]: ./setup-github.sh
[skills-install]: ../../scripts/skills_install.py
[test-bootstrap]: ../../scripts/test_bootstrap.py
[upgrade-host]: ./upgrade-host.sh
[windows-readme]: ../windows/README.md

<!-- External -->

[proxmox-upgrade]: https://pve.proxmox.com/wiki/Upgrade_from_8_to_9
