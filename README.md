# ProjectTemplate

C# .NET project template.

## Build and Distribution

- **Source Code**: [GitHub][github-link] - Source code, issues, discussions, and CI/CD pipelines.
- **Binary Releases**: [GitHub Releases][releases-link] - Pre-compiled binaries for Windows, Linux, and macOS.
- **Versioned Releases**: [GitHub Releases][releases-link] - Version tagged source code and build artifacts.
- **Docker Images**: [Docker Hub][docker-link] - Container images with all tools pre-installed.
- **NuGet Packages** [NuGet Packages][nuget-link] - .NET libraries published to NuGet.org.

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
- [Development Environment Setup](#development-environment-setup)
- [Template Project Setup](#template-project-setup)
  - [Template - TODO List](#template---todo-list)
  - [Template - Developer Environment Setup](#template---developer-environment-setup)
  - [Template - GitHub Setup](#template---github-setup)
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

## Development Environment Setup

- **Install Developer Tools:**

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

- **Clone and Configure the Project:**

  - Clone the repository and initialize tools:

    ```shell
    git clone -b main https://github.com/ptr727/[ProjectTemplate].git ./[NewProject]
    dotnet tool restore
    dotnet husky install
    ```

  - Open `[ProjectTemplate].code-workspace` in Visual Studio Code.
  - Open `[ProjectTemplate].slnx` in Visual Studio.

## Template Project Setup

### Template - TODO List

- [ ] Start on Linux to avoid file permission issues when moving from Windows.
- [ ] Configure the [Developer Environment](#template---developer-environment-setup).
- [ ] Configure the [Global Git Setup](#template---global-git-setup) and the [Project Git Setup](#template---project-git-setup).
- [ ] Open the project directory in Visual Studio Code, and rename (Ctrl-Shift-H) all instances of `ProjectTemplate` to `[NewProject]` in code.
- [ ] Rename `ProjectTemplate.code-workspace` to `[NewProject].code-workspace` and `ProjectTemplate.slnx` to `[NewProject].slnx`.
- [ ] Open `[NewProject].code-workspace` workspace in Visual Studio Code.
- [ ] Delete any projects and associated actions that will not be used, update dependencies in actions to remove deleted actions.
- [ ] Rename projects to match the naming, update `.slnx` and `.csproj` files, and update actions to match the naming.
- [ ] Update the `namespace` in `.cs` and `.csproj` files to match the naming.
- [ ] Update all ref-links in `README.md` to point to the naming.
- [ ] Publish to GitHub to create a new empty GitHub repository.
- [ ] Commit and push the `first-branch`.
- [ ] Edit and iterate only in `first-branch` until ready to start with git history.
- [ ] Setup `main` as the [First Permanent Branch](#template---git-permanent-branch) when ready.
- [ ] Configure [GitHub](#template---github-setup) for the new repository.
- [ ] Follow the [Branching Workflow](#template---branching-workflow), create `develop` from `main`, PR from `feature-branch` to `develop` to `main`.
- [ ] Delete the `Project Template Setup` section from `README.md`.

### Template - Developer Environment Setup

#### Template - Tools Setup

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

#### Template - Global Git Setup

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

#### Template - Project Git Setup

- Template project setup:

  ```shell
  git clone -b main https://github.com/ptr727/ProjectTemplate.git ./[NewProject]
  rm -r ./[NewProject]/.git
  cd ./[NewProject]
  git init -b first-branch
  dotnet tool restore
  dotnet husky install
  ```

#### Template - Git Permanent Branch

- Create `main` branch from `first-branch` with no history:

  ```shell
  # When you're ready to create main with ONLY ONE squashed commit:
  git checkout --orphan main                 # creates main with no history
  git commit --allow-empty -m "temp"         # required so we can merge into it

  git merge --squash first-branch            # bring in final state as staged changes
  git commit -m "Initial import (squashed)"  # main now has exactly 1 real commit

  git reset --hard HEAD~1                    # drop the temporary commit (leaves your squashed commit as the first)

  # delete the feature branch
  git branch -D feature-big-branch
  ```

#### Template - Project Workspace Setup

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

#### Template - GitHub Local Actions Setup

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

### Template - GitHub Setup

#### Template - GitHub Secrets Setup

- Create a [NuGet API Key](https://www.nuget.org/account/apikeys).
  - Save the Key as `NUGET_API_KEY` in:
    - GitHub project security Settings / Secrets / Actions.
    - GitHub project security Settings / Secrets / Dependabot.
    - GitHub Local Actions Settings / Secrets.
- Create a [Docker Hub Personal Access Token](https://app.docker.com/accounts/ptr727/settings/personal-access-tokens).
  - Save the PAT as `DOCKER_HUB_ACCESS_TOKEN` and `DOCKER_HUB_USERNAME` in:
    - GitHub project security Settings / Secrets / Actions.
    - GitHub project security Settings / Secrets / Dependabot.

#### Template - GitHub Project Settings

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

#### Template - Branching Workflow

- Create persistent `main` and `develop` branches.
- Protect `main` and `develop` branches with [branch protection rules](#template---github-project-settings).
- Make sure that `main` and `develop` are always building error free.
- Create feature branches from the `develop` branch.
- Always "Squash and merge" from feature branches to the `develop` branch to minimize change history.
- Always "Squash and merge" from `develop` to `main` to maintain a linear history.

#### Template - GitHub Actions Workflow

- Use reusable tasks to eliminate duplication.
- Create one pull request test action, and register that task as a [branch rule](#template---github-project-settings) check.

## 3rd Party Tools

**3rd Party tools used in this project:**

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
