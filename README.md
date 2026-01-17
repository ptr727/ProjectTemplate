# ProjectTemplate

C# .NET project template.

## Build and Distribution

- **Source Code**: [GitHub][github-link] - Source code, issues, discussions, and CI/CD pipelines.
- **Binary Releases**: [GitHub Releases][releases-link] - Pre-compiled binaries for Windows, Linux, and macOS.
- **Docker Images**: [Docker Hub][docker-link] - Container images with all tools pre-installed.
- **NuGet Packages** [NuGet Packages][nuget-link] - .NET libraries.

### Build Status

[![Release Status][release-build-status-shield]][actions-link]\
[![Docker Status][docker-build-status-shield]][actions-link]\
[![Last Commit][last-commit-shield]][commits-link]\
[![Last Build][last-build-shield]][actions-link]

### Releases

[![GitHub Release][release-version-shield]][releases-link]\
[![GitHub Pre-Release][prerelease-version-shield]][releases-link]\
[![Docker Latest][docker-latest-version-shield]][docker-link]\
[![Docker Develop][docker-develop-version-shield]][docker-link]\
[![NuGet Release][nuget-release-version-shield]][nuget-link]\
[![NuGet Pre-Release][nuget-prerelease-version-shield]][nuget-link]

### Release Notes

**Version: 1.0**:

**Summary:**

- Something.
- And something else.

> **⚠️ Breaking Changes:**
>
> - Something.
> - And something else.

See [Release History](./HISTORY.md) for complete release notes and older versions.

## Getting Started

Get started with ProjectTemplate in three easy steps:

> **⚠️ Important**: Some important warning.
>
> **ℹ️ Note**: Some interesting note.

```shell
ls -la
```

See [Installation](#installation) for detailed setup instructions.

## Table of Contents

- [Build and Distribution](#build-and-distribution)
  - [Build Status](#build-status)
  - [Releases](#releases)
  - [Release Notes](#release-notes)
- [Getting Started](#getting-started)
- [Table of Contents](#table-of-contents)
- [Use Cases](#use-cases)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Commands Quick Reference](#commands-quick-reference)
  - [Global Options](#global-options)
  - [Some Command](#some-command)
- [Questions or Issues](#questions-or-issues)
- [Project Template Setup](#project-template-setup)
  - [Template Setup TODO List](#template-setup-todo-list)
  - [Developer Environment Setup](#developer-environment-setup)
  - [GitHub Setup](#github-setup)
- [3rd Party Tools](#3rd-party-tools)
- [License](#license)

## Use Cases

> **ℹ️ TL;DR**: This widget is special because it does something special.

- It does something special.
- And it does something else special.

## Installation

Choose an installation method based on your platform and requirements:

- **Method 1** (Recommended): Easiest and most up-to-date option.
  - ✅ Some good reason.
  - ⚠️ Some not so good reason.
  - ❌ Strong reason to avoid.
  - Best for: Linux, NAS devices, servers, cross-platform deployments.

## Configuration

> **⚠️ Important**: The default settings file must be edited to match your requirements before processing media files.

Describe configuration steps.

## Usage

### Commands Quick Reference

| Command | Description | Notes |
| ------- | ------- | ----------- |
| `somecommand` | Do something useful | First time setup |
| `othercommand` | Do something else useful | Some note |

See detailed command documentation below for all options and usage examples.

---

Use the `--help` commandline option to get a list of commands and options.\
To get help for a specific command run `PlexCleaner <command> --help`.

```text
> PlexCleaner --help
Description:
  Utility to optimize media files for Direct Play in Plex, Emby, Jellyfin, etc.
```

### Global Options

Global options apply to all commands:

- `--logfile`:
  - Path to the log file.

| Option | Description | Default |
| ------- | ------- | ----------- |
| `--logfile` | Do something useful | Required |
| `--debuglevel` | Set the debug log level | `Information` |

### Some Command

```text
> PlexCleaner process --help
Description:
  Process media files
```

Options:

- `--settingsfile`: (required)
  - Path to the JSON settings file.
  - Something else that is relevant.

## Questions or Issues

**For General Questions:**

- Use the [Discussions][discussions-link] forum for general questions, feature requests, and sharing working configurations.

**For Bug Reports:**

- Ask in the [Discussions][discussions-link] forum if you are not sure if it is a bug.
- Check the existing [Issues][issues-link] tracker for known problems.
- If the issue is unique and a bug, file it in [Issues][issues-link], and include all pertinent steps to reproduce the issue.

## Project Template Setup

### Template Setup TODO List

- [ ] Start on Linux to avoid file permission issues when moving from Windows.
- [ ] `git clone -b main https://github.com/ptr727/ProjectTemplate.git ./[NewProject]`.
- [ ] `rm -r ./[NewProject]/.git` to start a fresh repo.
- [ ] Configure the [developer environment](#developer-environment-setup).
- [ ] Open the project directory in Visual Studio Code, and rename all instances of `ProjectTemplate` to `[NewProject]` in code and filenames.
- [ ] Open the workspace in Visual Studio Code, continue editing.
- [ ] Delete any sub-projects that will not be used.
- [ ] Update all ref-links in `README.md` to point to `[NewProject]`.
- [ ] `Publish Branch` to GitHub, default new branch is `first-branch`.
- [ ] Configure [GitHub](#git-setup).
- [ ] Create `develop` branch from `first-branch` when ready.
- [ ] Create `main` branch from `develop` when ready.
- [ ] Create feature branches and PR to `develop` and PR from `develop` to main.
- [ ] Delete the `Project Template Setup` section from `README.md`.

### Developer Environment Setup

#### Tools Setup

- Install [.NET SDK](https://dotnet.microsoft.com/en-us/download):

  ```shell
  winget install Microsoft.DotNet.SDK.10
  winget upgrade Microsoft.DotNet.SDK.10
  ```

- Install [Visual Studio Code](https://code.visualstudio.com/download):

  ```shell
  winget install Microsoft.VisualStudioCode
  winget upgrade Microsoft.VisualStudioCode
  ```

- Install [Visual Studio](https://visualstudio.microsoft.com/downloads/):

  ```shell
  winget install Microsoft.VisualStudio.Community
  winget upgrade Microsoft.VisualStudio.Community
  ```

- Install [Nektos ACT](https://nektosact.com/):

  ```shell
  winget install nektos.act
  winget upgrade nektos.act
  ```

#### Git Setup

- Configure Git options:

  ```shell
  git config --global credential.helper "cache --timeout=3600"
  git config --global user.name "Pieter Viljoen"
  git config --global user.email "ptr727@users.noreply.github.com"
  git config --global core.sharedRepository group
  git config --global --add safe.directory '*'
  git config --list --show-origin
  ```

- [Register](https://github.com/settings/keys) SSH key for Authentication and Signing on GitHub.

  ```shell
  ssh-keygen -t ed25519 # If not already created
  cat ~/.ssh/id_ed25519.pub # Paste into GitHub
  ssh-keyscan github.com >> ~/.ssh/known_hosts
  ssh -v -T git@github.com
  ```

- Configure Git for [SSH signing](https://docs.github.com/en/authentication/managing-commit-signature-verification/telling-git-about-your-signing-key):

  ```shell
  git config --global gpg.format ssh
  git config --global user.signingkey "~/.ssh/id_ed25519.pub"
  git config --global commit.gpgsign true
  git config --global tag.gpgsign true
  mkdir -p ~/.config/git
  echo "$(git config --get user.email) namespaces=\"git\" $(cat ~/.ssh/id_ed25519.pub)" >> ~/.config/git/allowed_signers
  git config --global gpg.ssh.allowedSignersFile "~/.config/git/allowed_signers"
  git log --show-signature
  git config --list --show-origin
  ```

#### Project Workspace Setup

- Setup new project environment:

  ```shell
  git init -b first-branch
  dotnet new tool-manifest
  dotnet tool install csharpier
  dotnet tool install husky
  dotnet tool install dotnet-outdated-tool
  dotnet husky install
  dotnet husky add pre-commit -c "dotnet husky run"
  ```

- New pull of existing project:

  ```shell
  dotnet tool restore
  dotnet husky install
  chmod +x ./.husky/pre-commit
  ```

- Update tools in existing project:

  ```shell
  dotnet tool update --all
  dotnet outdated --upgrade:prompt
  ```

- Linux / macOS:
  - Verify that shell files are `+x` executable and `LF` line ending mode.
  - Verify that there are no duplicate files with different case names.

#### GitHub Local Actions Setup

- Install [Nektos ACT](https://nektosact.com/):

  ```shell
  winget install nektos.act
  winget upgrade nektos.act
  ```

- Install [GitHub Local Actions](https://marketplace.visualstudio.com/items?itemName=SanjulaGanepola.github-local-actions) Visual Studio Code extension.
- Update [settings](https://nektosact.com/usage/index.html#action-artifacts) to always start the artifact server.

  ```json
  "githubLocalActions.actCommand": "act --artifact-server-path ./.artifacts",
  ```

- Update local secrets:
  - Save the existing [Docker Hub Personal Access Token](https://app.docker.com/accounts/ptr727/settings/personal-access-tokens) as `DOCKER_HUB_ACCESS_TOKEN` and `DOCKER_HUB_USERNAME`.
  - Create a [GitHub Personal Access Token](https://github.com/settings/personal-access-tokens) as `GITHUB_TOKEN`.

### GitHub Setup

#### GitHub Secrets Setup

- Create a [NuGet API Key](https://www.nuget.org/account/apikeys).
  - Save the Key as `NUGET_API_KEY` in:
    - GitHub project security Settings / Secrets / Actions.
    - GitHub project security Settings / Secrets / Dependabot.
    - GitHub Local Actions Settings / Secrets.
- Create a [Docker Hub Personal Access Token](https://app.docker.com/accounts/ptr727/settings/personal-access-tokens).
  - Save the PAT as `DOCKER_HUB_ACCESS_TOKEN` and `DOCKER_HUB_USERNAME` in:
    - GitHub project security Settings / Secrets / Actions.
    - GitHub project security Settings / Secrets / Dependabot.

#### GitHub Project Settings

- General:
  - Default branch: `main`
  - Pull requests:
    - `Allow squash merging`
      - [TODO:](https://github.com/orgs/community/discussions/184410): Disable merge and rebase merging, ruleset merge rules do not currently work.
    - `Always suggest updating pull request branches`
    - `Allow auto-merge`
- Rules / Rulesets:
  - "Main and Develop":
    - Target branches: `main`, `develop`.
    - `Restrict deletions`
    - `Require linear history`
    - `Require signed commits`
    - `Require a pull request before merging`
      - `Dismiss stale pull request approvals when new commits are pushed`
      - Allowed merge methods: `Squash`
    - `Require status checks to pass`
      - `Require branches to be up to date before merging`
      - Status checks that are required: `Check pull request workflow status`
    - `Block force pushes`
    - `Automatically request Copilot code review`
      - `Review new pushes`
      - `Review draft pull requests`
- Actions / General:
  - `Allow GitHub Actions to create and approve pull requests`

#### Branching Workflow

- Create persistent `main` and `develop` branches.
- Protect `main` and `develop` branches with [branch protection rules](#github-project-settings).
- Make sure that `main` and `develop` are always building error free.
- Create feature branches from the `develop` branch.
- Always "Squash and merge" from feature branches to the `develop` branch to minimize change history.
- Always "Squash and merge" from `develop` to `main` to maintain a linear history.

#### GitHub Actions Workflow

- Use reusable tasks to eliminate duplication.
- Create one pull request test action, and register that task as a [branch rule](#github-project-settings) check.

## 3rd Party Tools

- [AwesomeAssertions](https://awesomeassertions.org/)
- [Bring Your Own Badge](https://github.com/marketplace/actions/bring-your-own-badge)
- [CSharpier](https://csharpier.com/)
- [Create Pull Request](https://github.com/marketplace/actions/create-pull-request)
- [GH Release](https://github.com/marketplace/actions/gh-release)
- [Git Auto Commit](https://github.com/marketplace/actions/git-auto-commit)
- [GitHub Actions](https://github.com/actions)
- [GitHub Dependabot](https://github.com/dependabot)
- [Husky.Net](https://alirezanet.github.io/Husky.Net/)
- [Nerdbank.GitVersioning](https://github.com/marketplace/actions/nerdbank-gitversioning)
- [Serilog](https://serilog.net/)
- [xUnit.Net](https://xunit.net/)

## License

Licensed under the [MIT License][license-link]\
![GitHub License][license-shield]

<!--- TODO: Shields.io requires public GitHub repos, update links to point to the target project --->

[github-link]: https://github.com/ptr727/ProjectTemplate
[actions-link]: https://github.com/ptr727/ProjectTemplate/actions
[discussions-link]: https://github.com/ptr727/ProjectTemplate/discussions
[commits-link]: https://github.com/ptr727/ProjectTemplate/commits/main
[issues-link]: https://github.com/ptr727/ProjectTemplate/issues
[releases-link]: https://github.com/ptr727/ProjectTemplate/releases

[license-link]: ./LICENSE
[license-shield]: https://img.shields.io/github/license/ptr727/LanguageTags?label=License

[docker-link]: https://hub.docker.com/r/ptr727/projecttemplate
[docker-latest-version-shield]: https://img.shields.io/docker/v/ptr727/projecttemplate/latest?label=Docker%20Latest&logo=docker
[docker-develop-version-shield]: https://img.shields.io/docker/v/ptr727/projecttemplate/develop?label=Docker%20Develop&logo=docker&color=orange
[docker-build-status-shield]: https://img.shields.io/github/actions/workflow/status/ptr727/PlexCleaner/BuildDockerPush.yml?logo=github&label=Docker%20Build

[last-build-shield]: https://byob.yarr.is/ptr727/PlexCleaner/lastbuild
[last-commit-shield]: https://img.shields.io/github/last-commit/ptr727/LanguageTags?logo=github&label=Last%20Commit

[release-version-shield]: https://img.shields.io/github/v/release/ptr727/LanguageTags?logo=github&label=GitHub%20Release
[prerelease-version-shield]: https://img.shields.io/github/v/release/ptr727/LanguageTags?include_prereleases&label=GitHub%20Pre-Release&logo=github
[release-build-status-shield]: https://img.shields.io/github/actions/workflow/status/ptr727/PlexCleaner/BuildGitHubRelease.yml?logo=github&label=Releases%20Build

[nuget-link]: https://www.nuget.org/packages/ptr727.ProjectTemplate/
[nuget-release-version-shield]: https://img.shields.io/nuget/v/ptr727.LanguageTags?logo=nuget&label=NuGet%20Release
[nuget-prerelease-version-shield]: https://img.shields.io/nuget/vpre/ptr727.LanguageTags?logo=nuget&&label=NuGet%20Pre-Release&color=orange
