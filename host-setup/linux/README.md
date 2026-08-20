# Linux Host Setup

The tooling that makes a Debian or Ubuntu based host satisfy the contract in [`docs/host-setup.md`][host-setup], Proxmox and WSL included. That document is the contract, meaning which tools a host must provide and why each floor exists. This directory is how a Linux host comes to satisfy it.

## What Is Here

- [`install-tools.sh`][install-tools] installs and upgrades the host tools. It reports what each is installed at, what upstream carries, where it comes from, and its status. Its `--sudo-timestamp` action is the one thing it does that is about the host rather than a tool.
- [`upgrade-host.sh`][upgrade-host] upgrades the packages of the current release. Moving to the next release is a separate action behind its own flag.
- [`setup-github.sh`][setup-github] configures the SSH key, git, and commit signing. It checks both key registrations against what GitHub publishes.
- [`install-skills.sh`][install-skills] drives the hub's skills installer at [`scripts/skills_install.py`][skills-install] from this tree.

Each runs on its own, and each takes `--help`.

```shell
host-setup/linux/install-tools.sh          # report
host-setup/linux/install-tools.sh --install
host-setup/linux/upgrade-host.sh --status
host-setup/linux/setup-github.sh --status
host-setup/linux/install-skills.sh --report
```

Each script is LF with a shebang, and its executable bit is tracked in git. A fresh checkout therefore runs each without a `bash` prefix. That is the Linux form of "this will run", and [`scripts/tests/test_bootstrap.py`][test-bootstrap] asserts both.

## Requirements

**A Debian or Ubuntu based host, identified from `/etc/os-release`.** A distribution that is neither but declares `ID_LIKE` debian is treated as Debian, with a warning that it is untested. Proxmox reports itself as its Debian base, so it needs no case of its own. `install-tools.sh` and `upgrade-host.sh` refuse anything else. `setup-github.sh` runs anywhere, and stops only when a missing prerequisite needs apt to install it.

**Root or sudo.** Nothing here elevates wholesale. Each script runs as the caller and puts `sudo` in front of only the commands that change the host. Every read and all file staging stay unprivileged. A run that is not root and finds no `sudo` refuses up front rather than failing partway.

**A terminal, or `--yes`.** A run with no terminal on standard input and no `--yes` refuses to change the host. A scheduled run therefore cannot hang on a prompt nobody answers. On `upgrade-host.sh`, `--yes` also keeps the installed configuration file on a packaging conflict. An unattended run has nobody to answer dpkg's prompt, and a replaced config is the harder half to notice afterwards.

`curl` is not a requirement, it is a managed prerequisite. A minimal image carries none, so every upstream read is guarded. An install run puts `ca-certificates`, `curl`, `gnupg`, and `gpgv` in place before the first tool.

## Why There Are Three Kinds of Source

The Windows registry has one source because `winget` tracks upstream. Here the distro package trails upstream on `gh`, on `node`, and on `uv`, so a tool comes from whichever source keeps up:

- **The distro**, for `git`, `python`, and Ripgrep, where apt's own package is current enough.
- **An upstream apt repository**, for `gh`, `node`, `docker`, and `powershell`, and for `dotnet` as a fallback, where upstream publishes one.
- **A released binary into `/usr/local/bin`**, for `jq`, `uv`, and `git-restore-mtime`, where upstream publishes no repository.

No version is written into the script. Each upstream is asked what it carries now, so the script does not go stale between releases. Every step is idempotent, so a keyring or sources file is written only when its content differs. A re-run repairs drift rather than assuming a clean host.

**A keyring is proved, not trusted.** Before a fetched signing key is installed, `gpgv` checks that it signs the repository's own `InRelease` metadata. An upstream that rotates or adds a key breaks a pinned fingerprint list but not this check. A host where the check cannot run stops rather than trusting the download. A released binary is checked against the sha256 list its upstream publishes beside it, for the same reason.

**Keyrings land in `/etc/apt/keyrings`, and sources land as deb822 files in `/etc/apt/sources.list.d`.** A predecessor in the old location or the old one-line format is removed first, so apt never reads the same repository twice.

## PATH and Shadowing

`/usr/local/bin` precedes `/usr/bin`, which is why the distro's `jq` can stay installed and stay shadowed. The upstream binary wins without removing a package something else may depend on.

The hazard runs the other way too. A copy of `jq`, `uv`, or `git-restore-mtime` sitting earlier on `PATH` keeps answering after this script installs a newer one. That reads as an upgrade that did not take, so the report names such a shadow. `--upgrade` removes it after a prompt. `--install` removes it only when no managed copy exists yet, since removing a newer shadow would downgrade what `PATH` resolves to. A file a distro package owns is never removed, because deleting it would desync dpkg's database from the filesystem. The remedy named there is the `PATH` order itself. The removal loops, since `PATH` can stack more than one shadow ahead of `/usr/local/bin`. A relative `PATH` entry is never trusted as a shadow at all.

## What the Report Says

A report changes nothing and reads the apt cache as it stands. An available version is therefore as current as the last `apt update`. Versions compare like with like: apt versions for an apt managed tool, upstream versions for a standalone binary. `docker` is read from the CLI rather than from the `docker-ce` package. On a WSL distribution, Docker Desktop's integration is a working `docker` with no apt package behind it. Its target is stripped of the epoch and packaging revision for the same like-with-like reason.

`unmanaged` means the tool is installed from the distro while its upstream repository is unconfigured. The one thing the report must not say is that such a tool is current against the distro's own version. The Windows report uses the same word for a different mechanism, a tool on `PATH` that `winget` knows no package for.

An install or upgrade collects a tool whose install fails and carries on, so one failure does not strand the rest of the run. A refusal is different and ends the run. An unverifiable keyring, a checksum mismatch, or a declined prompt stops everything, because continuing past one would install something nobody vouched for.

## Docker, node, dotnet, and powershell

**Inside a WSL distribution, docker comes only from Docker Desktop's own WSL integration, never from installing `docker-ce`.** A native install would run a second engine beside Desktop's. `--install` and `--upgrade` therefore always skip it there and point at Docker Desktop's settings instead. The skip counts as success only where `docker` already answers, so a run cannot exit clean having found nothing working. On a native host, the conflicting packages Docker's own uninstall list names are removed first. Non-root use (`usermod -aG docker`) is left to the operator, as a group choice rather than a question of presence.

**Installing `node` displaces distro packages.** The upstream package carries `npm` itself and conflicts with the distro's `npm` and `nodejs-doc`. The script asks apt what it would remove and puts that list in front of the operator first. Asking apt beats naming the conflicts here, because the conflict set belongs to the upstream package and changes without notice. The major line installed is whatever upstream currently marks LTS, read from its release index at run time.

**For `dotnet`, the distro feed is the default and Microsoft's feed is the fallback.** The fallback is added only where the distro carries no SDK at all, because mixing the two feeds is what breaks a host. Microsoft's feed carries amd64 only, so any other architecture without a distro SDK is a named skip. The default set is the newest SDK line the feed carries. `--optional` adds every other line, for a host that builds against more than one.

**`powershell` is optional and comes from Microsoft's feed, the same one `dotnet` falls back to.** The distro never carries a package, so there is no distro version to prefer and no mixing concern to weigh. It joins the default selection only under `--optional`, and naming it on the command line selects it either way. Like `dotnet`, the feed carries amd64 only, so any other architecture is a named skip. It is not part of the fleet contract, which is why it stays optional: a repository that needs `pwsh` opts in rather than every host carrying it.

## The Sudo Credential Cache

`install-tools.sh --sudo-timestamp` shares one sudo credential cache across the invoking user's terminals. Sudo's default is one cache per terminal, so a `sudo -v` answered in one terminal does nothing for a program started in another. The action installs no tool and reports on none. It sits here because a host stand-up already runs this script.

**The drop-in is scoped to one user.** It lands at `/etc/sudoers.d/90-host-setup-sudo-timestamp` and names the invoking user, so every other account keeps the per-terminal default. Under `sudo` the invoking user is `$SUDO_USER` rather than root, since widening root's cache leaves the caller prompted exactly as before. A run as root with no invoking user to name is refused.

**Writing to `/etc/sudoers.d` is the one change here that can lock a host out.** A parse error in any file sudo reads makes every `sudo` on that host fail, and the remedy then needs a root shell. So the whole set is proved to parse before anything is added to it. The new content then lands under a name sudo skips, since sudo ignores a file name holding a dot. It is proved again where sudo will read it, and only then is renamed over. A rename is atomic and a copy into place is not.

**A re-run that would write the same bytes changes nothing.** A drop-in carrying something else is replaced, and a timestamp option set in another file is named rather than merged into. Which of the two wins is the order sudo reads them in, not something this can decide.

**Ubuntu 25.10 and later ship `sudo-rs` as the default `sudo`, and it carries no `timestamp_type` setting at all.** Nothing there shares a cache across terminals. The action therefore asks each installed implementation's own `visudo` whether it parses the drop-in, rather than reading a version number. Where the active one does not, the run offers to point the `sudo` alternative at one that does. That changes which sudo every user on the host runs, so it is stated before the prompt, and `update-alternatives --auto sudo` puts it back. A host where no installed sudo parses the setting is refused, naming `apt-get install sudo` as the remedy.

The cache stays valid for 60 minutes, from `SUDO_TIMESTAMP_TIMEOUT` in the script. Removing the drop-in undoes the sharing. Where the run also switched the sudo alternative, `update-alternatives --auto sudo` is what undoes that half.

## Release Upgrades

`upgrade-host.sh` splits the routine from the rare. `--packages` upgrades within the current release, and `--release` is its own action because the release upgrade is where hosts differ.

**A release upgrade is refused where this script cannot carry it safely.** Proxmox major upgrades are a documented procedure with their own preconditions, currently the [Proxmox upgrade guide][proxmox-upgrade], and the refusal points there. A distribution that is neither Debian nor Ubuntu is refused too, since the sources rewrite below has no meaning there. Refusing is the point of running this rather than apt by hand.

**One release at a time.** Both distributions support exactly that, so a host two releases behind is upgraded by running this twice.

**Debian is carried by rewriting the codename in its apt sources, and only in sources that point at Debian's own mirrors.** A third party repository may have no suite for the new release yet. It is therefore named and left alone, and what to do about it is the operator's call. The sources are backed up to `/var/backups/upgrade-host` first. A backup that cannot be taken stops the upgrade, since it is the only way back. A host on a mirror outside `debian.org`, or one tracking `stable` rather than a codename, is refused. The refusal says to move such a host by editing its sources itself.

**Ubuntu is carried by `do-release-upgrade`**, which handles its own sources. Whether an LTS or every release is offered is the host's own policy in `/etc/update-manager/release-upgrades`, deliberately not decided here.

**Preconditions run before the point of no return.** Held packages, half-configured packages from a dpkg audit, and low free space on `/var` are each surfaced first. A release upgrade failing partway is the worst place to find any of them.

## Restarts, Kernels, and WSL

A WSL distribution runs the kernel Windows gives it. A restart there is `wsl --shutdown` from Windows followed by a relaunch, and the script says exactly that instead of suggesting `reboot`. Debian does not always write `/var/run/reboot-required`, so the newest kernel in `/boot` is also compared against `uname -r`. A host with no `/boot`, which a container and a WSL distribution both are, has no kernel of its own to compare.

## GitHub Setup

Two steps cannot be automated, because they happen in a browser. The public key is registered once as an authentication key and again as a signing key. `setup-github.sh --configure` stops at each, prints the key, and says where to paste it. It then checks that the registration took, by reading the key lists GitHub publishes for the account, which needs no token. A check that could not reach GitHub is reported apart from a key that is not registered. Sending someone to register a key that is already there is the wrong remedy.

**`--status` is read-only end to end.** Its SSH probes run in batch mode, so a passphrase prompt cannot hang an unattended run. No probe ever enrolls github.com's host key behind the reader's back. Enrolling is `--configure`'s job, and the first enrollment is the one moment a substituted host key would be accepted for good. The offered key is therefore checked against the fingerprints GitHub publishes at `api.github.com/meta` before it is recorded. A check that cannot run is a refusal.

**The identity comes from the flags, then from what the host already carries, then from the maintainer's default, in that order.** Reading the host first keeps a machine configured for somebody else from being quietly rewritten by a repeatable run.

**The managed key is probed on its own**, with the host's ssh config and agent excluded. A default identity file or an agent key can authenticate as a different account. A host that reaches GitHub with some other key is working but not managed. The run says so rather than ending in "Done" with the managed key registered nowhere.

**Signing is proved end to end**, by signing and verifying a commit in a throwaway repository. Reading the settings back cannot catch a wrong `allowed_signers` entry: that reads as correct and fails only when a signature is checked.

**The key path settings are written in tilde form** (`~/.ssh/id_ed25519.pub`), because git expands the tilde. The hosts configured by hand already hold that form, so writing it leaves an already configured host untouched.

**`--shared-checkout` exists because `safe.directory` and `core.sharedRepository` are relaxations, not defaults.** They are applied only for a path the caller names, and a host one account uses needs neither. `*` is accepted but called out as turning the ownership check off everywhere.

## install-skills.sh Is the Exception

The sibling scripts are independently fetchable, and this one deliberately is not. It drives `scripts/skills_install.py` at the tree root, and the skills content lives in the tree, so a copy fetched alone has nothing to install. Python 3.7 or later is its one dependency, which is why the bootstrap runs it last. Run on a host without one, it stops and names the tools step as its prerequisite.

## Why There Is No Linter Category

Neither this tooling nor its Windows sibling installs `markdownlint`, `cspell`, `actionlint`, `editorconfig-checker`, `shellcheck`, `PSScriptAnalyzer` or `ruff`. That is a decision rather than a gap. Each runs as a pinned container image or through `uvx`, which is what keeps a local run and CI the same check. Installing native copies would put a second, unpinned version of each on the host. The only host requirements any of it creates are `docker` and `uv`, and both are already managed here. The [Windows README][windows-readme] states the same decision from its side.

## bootstrap.sh

[`bootstrap.sh`][bootstrap] sits beside [`bootstrap.ps1`][bootstrap-ps1] at the top of [`host-setup/`][host-setup-readme] rather than here. Standing up a host with no git and no checkout is one concern across two platforms, not a fifth member of this directory. It needs only `curl` and `tar`, fetches this repository, and runs the scripts here from that tree.

## Differences From the Windows Tooling

The comparison is tabulated once, in the [Windows README][windows-readme], so the two columns cannot drift apart. The short version: this side needs three kinds of source where `winget` needs one, and carries the release upgrade Windows Update owns over there. It elevates per command through `sudo` where `winget` raises UAC per installer. It also manages `git-restore-mtime`, which the spec declares not applicable on Windows.

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
host-setup/linux/install-tools.sh --install --repo /path/to/repository --dry-run
host-setup/linux/install-tools.sh --sudo-timestamp --dry-run
host-setup/linux/upgrade-host.sh --release --dry-run
host-setup/linux/setup-github.sh --configure --dry-run
```

Two of those are guards rather than previews. `--release --dry-run` on a Proxmox host prints the refusal, not the commands. A docker `--upgrade --dry-run` inside a WSL distribution prints the skip. A `[dry run]` line from either means the guard sits in the wrong place.

`--sudo-timestamp --dry-run` is the one dry run that can ask for a password. Only root reads `/etc/sudoers`, and what is already set there is what the run reports on.

`--repo` adds the repository's `install.linux` entries to the report or action. Only `apt` package names are accepted. The script does not execute the declaration's `remedy` text. Reading the JSON needs `jq`, which the ordinary fleet install provides.

The scripts are checked by `shellcheck`, which runs in CI over every `.sh` file `git ls-files` returns. A local run uses the same `koalaman/shellcheck:stable` container. [`scripts/tests/test_bootstrap.py`][test-bootstrap] asserts that every tool the spec requires on Linux is one `install-tools.sh` can provide, or a recorded exception. It also asserts each script here is tracked executable, so a fresh checkout can run it.

<!-- Repo -->

[bootstrap]: ../bootstrap.sh
[bootstrap-ps1]: ../bootstrap.ps1
[host-setup]: ../../docs/host-setup.md
[host-setup-readme]: ../README.md
[install-skills]: ./install-skills.sh
[install-tools]: ./install-tools.sh
[setup-github]: ./setup-github.sh
[skills-install]: ../../scripts/skills_install.py
[test-bootstrap]: ../../scripts/tests/test_bootstrap.py
[upgrade-host]: ./upgrade-host.sh
[windows-readme]: ../windows/README.md

<!-- External -->

[proxmox-upgrade]: https://pve.proxmox.com/wiki/Upgrade_from_8_to_9
