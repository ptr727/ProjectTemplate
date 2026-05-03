# ProjectTemplate

C# .NET project template.

<!-- Start of generic project layout -->

## Build and Distribution

- **Source Code**: [GitHub][github-link] - Source code, issues, discussions, and CI/CD pipelines.
- **Versioned Releases**: [GitHub Releases][releases-link] - Version tagged source code and build artifacts.
- **Docker Images**: [Docker Hub][docker-link] - Container images with all tools pre-installed.
- **NuGet Packages** [NuGet Packages][nuget-link] - .NET libraries published to NuGet.org.
- **PyPI Packages** [PyPI Packages][pypi-link] - Python library published to PyPI.org.

### Build Status

[![Release Status][releasebuildstatus-shield]][actions-link]\
[![Docker Status][dockerbuildstatus-shield]][actions-link]\
[![Last Commit][lastcommit-shield]][commits-link]\
[![Last Build][lastbuild-shield]][actions-link]

### Releases

[![GitHub Release][releaseversion-shield]][releases-link]\
[![GitHub Pre-Release][prereleaseversion-shield]][releases-link]\
[![Docker Latest][dockerlatestversion-shield]][docker-link]\
[![Docker Develop][dockerdevelopversion-shield]][docker-link]\
[![NuGet Release][nugetreleaseversion-shield]][nuget-link]\
[![NuGet Pre-Release][nugetprereleaseversion-shield]][nuget-link]\
[![PyPI Release][pypireleaseversion-shield]][pypi-link]

### Release Notes

**Version: 1.0**:

**Summary**:

- Something.
- And something else.

> **⚠️ Breaking Changes**:
>
> - Something.
> - And something else.

See [Release History](./HISTORY.md) for complete release notes and older versions.

## Getting Started

Get started with ProjectTemplate in three easy steps:

> **⚠️ Important**: Some important warning.
>
> **ℹ️ Note**: Some interesting note.

1. **Install ProjectTemplate**:
   - Do something.
2. **Configure ProjectTemplate**:
   - Then something else.
3. **Run ProjectTemplate**:

    ```shell
    Console --loglevel=Debug
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
  - [Command Quick Reference](#command-quick-reference)
  - [Global Options](#global-options)
  - [Test Command](#test-command)
- [Questions or Issues](#questions-or-issues)
- [Development Environment Setup](#development-environment-setup)
- [3rd Party Tools](#3rd-party-tools)
- [License](#license)
- [Template Project Setup](#template-project-setup)
  - [Template - TODO List](#template---todo-list)
  - [Template - Developer Environment Setup](#template---developer-environment-setup)
  - [Template - GitHub Setup](#template---github-setup)
  - [Template - Branching Workflow](#template---branching-workflow)

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
- **Method 2**: Custom configuration options.
  - ✅ Some good reason.
  - ⚠️ Some not so good reason.
  - ❌ Strong reason to avoid.
  - Best for: Specialized devices.

## Configuration

> **⚠️ Important**: The spinner setting must be configured before first use.

**Required configuration**:

- Set `foo` to something.
- Set `bar` to something else.

**Optional configuration**:

- Set `advanced` to `special`.
- Some other custom option.

## Usage

`Console [global options] <command> [command options]`

### Command Quick Reference

| Command | Description | Notes |
| ------- | ------- | ----------- |
| default | Default action when no command is specified | First time setup |
| `test` | Do something else useful | Some note |
| `--help` | Show help output | Use `<command> --help` for command specific help |
| `--version` | Show version output | |

Use the `--help` option to get a list of all commands and global options.\
To get help for a specific command run `Console <command> --help`.

### Global Options

**Global options apply to all commands**:

| Option | Description | Default |
| ------- | ------- | ----------- |
| `--logfile` | Debug log file | Optional |
| `--loglevel` | Debug log level | Default is `Information` |
| `--logfile-clear` | Clear log file at startup | Default is `false` |

**General help**:

```text
>.\Console\bin\Debug\net10.0\Console --help
Description:
  C# .NET console project

Usage:
  Console [command] [options]

Options:
  -l, --loglevel <Debug|Error|Fatal|Information|Verbose|Warning>  Set the log level (default: Information). [default: Information]
  -f, --logfile <logfile>                                         Write logs to the specified file (optional).
  -c, --logfile-clear                                             Clear the log file before writing (default: false).
  -?, -h, --help                                                  Show help and usage information
  --version                                                       Show version information

Commands:
  test  Test command
```

### Test Command

**Test command options**:

| Option | Description | Default |
| ------- | ------- | ----------- |
| `--test` | Test options | Optional |

**Test command help**:

```text
>.\Console\bin\Debug\net10.0\Console test --help
Description:
  Test command

Usage:
  Console test [options]

Options:
  -t, --test <test>                                               Test command option (optional).
  -?, -h, --help                                                  Show help and usage information
  -l, --loglevel <Debug|Error|Fatal|Information|Verbose|Warning>  Set the log level (default: Information). [default: Information]
  -f, --logfile <logfile>                                         Write logs to the specified file (optional).
  -c, --logfile-clear                                             Clear the log file before writing (default: false).
```

## Questions or Issues

**For General Questions**:

- Use the [Discussions][discussions-link] forum for general questions.

**For Bug Reports**:

- Ask in the [Discussions][discussions-link] forum if you are not sure if it is a bug.
- Check the existing [Issues][issues-link] tracker for known problems.
- If the issue is unique and a bug, file it in [Issues][issues-link], and include all pertinent steps to reproduce the issue.

## Development Environment Setup

The recommended setup is the [Dev Container](./docs/devcontainer.md) — a single image with the .NET 10 SDK, the `uv` Python toolchain, and the GitHub CLI. It bind-mounts your SSH public key, allowed-signers file, and `gh` config from the host so commits sign correctly. `gh` is pre-authenticated when the host token is file-backed; macOS Keychain and Linux libsecret-backed tokens require an in-container `gh auth login` — see the [credential-store nuance](./docs/devcontainer.md#gh-credential-store) section.

**Recommended (devcontainer)**:

1. Complete [host setup](./docs/host-setup.md) once per machine (git identity, SSH key, allowed_signers, `gh auth login`, [SSH commit signing](./docs/ssh-signing.md)).
2. Clone the repo, open in VS Code with the [Dev Containers extension][devcontainers-link], and run **Reopen in Container**.
3. The `postCreateCommand` runs `dotnet tool restore`, installs Husky.Net hooks, and installs `uv`.

**Alternative (host install)**:

- **Install Developer Tools**:

  - Install [.NET SDK](https://dotnet.microsoft.com/en-us/download):

    ```shell
    # Windows
    winget install Microsoft.DotNet.SDK.10

    # Linux
    apt install dotnet-sdk-10.0
    ```

  - Install [Visual Studio Code](https://code.visualstudio.com/download):

    ```shell
    # Windows
    winget install Microsoft.VisualStudioCode
    ```

  - Install [Visual Studio](https://visualstudio.microsoft.com/downloads/):

    ```shell
    # Windows
    winget install Microsoft.VisualStudio.Community
    ```

- **Clone and Configure Project**:

  - Clone the repository and initialize tools:

    ```shell
    # Clone from CLI (or clone from VSCode)
    git clone -b main https://github.com/ptr727/[Project].git ./[Project]

    # Initialize dotnet tools
    cd ./[Project]
    dotnet tool restore
    dotnet husky install
    ```

  - Open `[Project].code-workspace` in Visual Studio Code.
  - Open `[Project].slnx` in Visual Studio.

## 3rd Party Tools

**3rd Party tools used in this project**:

- [API Ninjas][apininjas-link]
- [AwesomeAssertions][awesomeassertions-link]
- [Bring Your Own Badge][byob-link]
- [Create Pull Request][createpr-link]
- [CSharpier][csharpier-link]
- [GH Release][ghrelease-link]
- [Git Auto Commit][ghautocommit-link]
- [GitHub Actions][ghactions-link]
- [GitHub Dependabot][ghdependabot-link]
- [Husky.Net][huskynet-link]
- [Nerdbank.GitVersioning][nerbankgitversion-link]
- [Serilog][serilog-link]
- [xUnit.Net][xunit-link]

## License

Licensed under the [MIT License][license-link]\
![GitHub License][license-shield]

<!-- Start of template instructions -->

## Template Project Setup

### Template - TODO List

- [ ] Configure git for SSH signing and SSH forwarding in dev containers — see [docs/host-setup.md](./docs/host-setup.md), [docs/ssh-signing.md](./docs/ssh-signing.md), and [docs/devcontainer.md](./docs/devcontainer.md).
- [ ] Decide whether your project needs the .NET (`NuGetLibrary/`) side, the Python (`PyPiLibrary/`) side, or both. Delete the unused folder and remove its references from `ProjectTemplate.slnx`, `.github/dependabot.yml`, and the corresponding `.github/workflows/build-*-task.yml`.
- [ ] Start on Linux to avoid file permission issues when moving from Windows.
- [ ] Configure the [Developer Environment](#template---developer-environment-setup).
- [ ] Open the project directory (*not the workspace*) in Visual Studio Code, and rename (Ctrl-Shift-H) all instances of `ProjectTemplate` to `[NewProject]` in code.
- [ ] Rename `ProjectTemplate.code-workspace` to `[NewProject].code-workspace` and `ProjectTemplate.slnx` to `[NewProject].slnx`.
- [ ] Open `[NewProject].code-workspace` workspace in Visual Studio Code.
- [ ] Delete any projects and associated actions that will not be used, update dependencies in actions to remove deleted actions.
- [ ] Rename projects to match the naming, update `.slnx` and `.csproj` files, and update actions to match the naming.
- [ ] Update the `namespace` in `.cs` and `.csproj` files to match the naming.
- [ ] Update all ref-links in `README.md` to point to the naming.
- [ ] Publish to GitHub from VSCode to create a new empty GitHub repository.
- [ ] Commit and push the `first-branch`.
- [ ] Edit and iterate only in `first-branch` until ready to start with git history.
- [ ] Setup `main` as the first permanent branch when ready.
- [ ] Configure [GitHub](#template---github-setup) for the new repository.
- [ ] Follow the [Branching Workflow](#template---branching-workflow).
- [ ] Delete the `Project Template Setup` section from `README.md`.

### Template - Developer Environment Setup

#### Template - Git Setup

- **⚠️ Prerequisites**:
  - Configure git for SSH signing — see [SSH commit signing](./docs/ssh-signing.md).
  - Configure host prerequisites (SSH key, `allowed_signers`, `gh` auth) — see [host setup](./docs/host-setup.md).
  - Configure SSH forwarding for dev containers — see [devcontainer setup](./docs/devcontainer.md).
- Setup new project from template:

  ```shell
  # Clone the template project
  git clone -b main https://github.com/ptr727/ProjectTemplate.git ./[NewProject]

  # Reset git to start a new repo
  rm -r ./[NewProject]/.git
  cd ./[NewProject]
  git init -b first-branch

  # Init dotnet tools
  dotnet tool restore
  dotnet husky install

  # Update dotnet tools
  dotnet tool update --all
  dotnet outdated --upgrade:prompt
  ```

- Setup new project from scratch:

  > **⚠️ Linux**: Start configuration on Linux to avoid file permission issues.

  ```shell
  # Init git
  mkdir ./[NewProject]
  cd ./[NewProject]
  git init -b first-branch

  # Init dotnet tools
  dotnet new tool-manifest
  dotnet tool install csharpier
  dotnet tool install husky
  dotnet tool install dotnet-outdated-tool
  dotnet husky install
  dotnet husky add pre-commit -c "dotnet husky run"

  # Make sure pre-commit is executable on Linux
  chmod +x ./.husky/pre-commit
  ```

- Use `first-branch` for all the initial project setup and testing.
- When ready, *only when ready*, create `main` branch from `first-branch` with no history:

  ```shell
  # Create main branch with no history
  git checkout --orphan main
  git commit --allow-empty -m "temp"

  # Squash merge changes
  git merge --squash first-branch
  git commit -m "Initial import (squashed)"

  # Drop the temporary commit
  git reset --hard HEAD~1

  # Delete first-branch
  git branch -D first-branch
  ```

### Template - GitHub Setup

**GitHub secrets setup**:

- Create a [NuGet API Key](https://www.nuget.org/account/apikeys).
  - Save the Key as `NUGET_API_KEY` in:
    - GitHub project security Settings / Secrets / Actions.
    - GitHub project security Settings / Secrets / Dependabot.
    - GitHub Local Actions Settings / Secrets.
- Create a [Docker Hub Personal Access Token](https://app.docker.com/accounts/ptr727/settings/personal-access-tokens).
  - Save the PAT as `DOCKER_HUB_ACCESS_TOKEN` and `DOCKER_HUB_USERNAME` in:
    - GitHub project security Settings / Secrets / Actions.
    - GitHub project security Settings / Secrets / Dependabot.
- Create a [GitHub Personal Access Token](https://github.com/settings/personal-access-tokens).
  - Save the PAT as `WORKFLOW_PAT`.
  - Permissions:
    - Pull requests: Read & write — to close and reopen the PR, triggering `pull_request` workflow events under the PAT owner's identity
    - Workflows: Read & write — required for the PAT to trigger `pull_request` events in other workflows
    - Metadata: Read-only (auto-required)
  - The codegen workflow uses `GITHUB_TOKEN` to create a signed commit and open the PR as `github-actions[bot]`. It then uses `WORKFLOW_PAT` to close and reopen the PR so the `pull_request` event fires under the repository owner's identity (`github.repository_owner`), which triggers the auto-merge workflow. PRs created or updated by `GITHUB_TOKEN` alone do not trigger other workflows, hence the close/reopen step.
  - The auto-merge condition in `merge-bot-pull-request.yml` requires all of the following to be true:
    - `github.event.pull_request.user.login == 'github-actions[bot]'` — the PR was created by the Actions bot (via `GITHUB_TOKEN`)
    - `github.event.pull_request.head.ref == 'codegen'` — the source branch is `codegen`
    - `github.event.pull_request.base.ref == 'main'` — the PR targets `main`
    - `github.event.pull_request.head.repo.full_name == github.repository` — the PR is from the same repository (not a fork)
    - For `reopened` events: `github.actor == github.repository_owner` — the reopen was triggered by the repository owner account
    - For all other events: `github.actor == 'github-actions[bot]'` — triggered by normal workflow activity
  - Save the PAT as `WORKFLOW_PAT` in:
    - GitHub project security Settings / Secrets / Actions.
- Create a [GitHub App](https://github.com/settings/apps) as an alternative to the PAT workflow.
  - App name: `ptr727-codegen`
  - The app bot user will be `ptr727-codegen[bot]`.
  - Permissions required:
    - Repository permissions:
      - Contents: Read & write — to push commits to the `codegen` branch
      - Pull requests: Read & write — to open and update pull requests
      - Metadata: Read-only (auto-required)
  - Note the App ID from the app's settings page.
  - Generate a private key (downloads a `.pem` file).
  - [Install the app](https://github.com/settings/apps) on your account and grant it access to the repository. The app must be both created **and** installed — creating the app alone is not sufficient. The `actions/create-github-app-token` action will fail with a `Not Found` error if the app is not installed on the repository.
  - Save the App ID as `CODEGEN_APP_ID` and the private key contents as `CODEGEN_APP_PRIVATE_KEY` in:
    - GitHub project security Settings / Secrets / Actions.
  - If the codegen workflows require additional secrets (e.g. third-party API keys), register them in the same location and reference them in the reusable workflow task files.
  - Unlike the PAT workflow, the GitHub App token triggers `pull_request` workflow events directly when opening a PR. No close/reopen step is required.
  - The auto-merge condition in `merge-bot-pull-request.yml` for the app workflow requires all of the following to be true:
    - `github.actor == 'ptr727-codegen[bot]'` — the event was triggered by the app
    - `github.event.pull_request.user.login == 'ptr727-codegen[bot]'` — the PR was created by the app
    - `github.event.pull_request.head.ref == 'codegen'` — the source branch is `codegen`
    - `github.event.pull_request.base.ref == 'main'` — the PR targets `main`
    - `github.event.pull_request.head.repo.full_name == github.repository` — the PR is from the same repository (not a fork)

**Codegen workflow schedule**:

- The PAT-based codegen workflow (`run-periodic-codegen-pull-request.yml`) runs every **Monday** at 02:00 UTC.
  - Uses `WORKFLOW_PAT` to close and reopen the PR after creation so that the `reopened` `pull_request` event is triggered by the repository owner account, which is required by the auto-merge condition (`github.actor == github.repository_owner`). Therefore, `WORKFLOW_PAT` must belong to the repository owner account.
- The App-based codegen workflow (`run-periodic-codegen-app-pull-request.yml`) runs every **Thursday** at 02:00 UTC.
  - Uses `CODEGEN_APP_ID` and `CODEGEN_APP_PRIVATE_KEY` to generate a GitHub App installation token. The app token triggers `pull_request` events directly when the PR is opened — no close/reopen step needed.
- The two workflows alternate through the week as independent verification that both authentication paths continue to work.

**GitHub project settings**:

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
      - `Require conversation resolution before merging`
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

### Template - Branching Workflow

- Create persistent `main` and `develop` branches.
- Protect `main` and `develop` branches with branch protection rules.
- Make sure that `main` and `develop` are always building error free.
- Create feature branches from the `develop` branch.
- Only commit to feature branches, do not commit directly to `develop` or to `main`.
- Always "Squash and merge" from feature branches to the `develop` branch to minimize change history.
- Always "Squash and merge" from `develop` to `main` to maintain a linear history.
- Bot generated pull requests (codegen, dependabot) always checkout from and merge into `main` directly.
- If `develop` falls behind after a bot merge, re-run codegen or rebase `develop` on `main` before merging `develop` to `main`.

<!--- Shields links --->

[github-link]: https://github.com/ptr727/ProjectTemplate
[actions-link]: https://github.com/ptr727/ProjectTemplate/actions
[discussions-link]: https://github.com/ptr727/ProjectTemplate/discussions
[commits-link]: https://github.com/ptr727/ProjectTemplate/commits/main
[issues-link]: https://github.com/ptr727/ProjectTemplate/issues
[releases-link]: https://github.com/ptr727/ProjectTemplate/releases

[license-link]: ./LICENSE
[license-shield]: https://img.shields.io/github/license/ptr727/ProjectTemplate?label=License

[docker-link]: https://hub.docker.com/r/ptr727/projecttemplate
[dockerlatestversion-shield]: https://img.shields.io/docker/v/ptr727/projecttemplate/latest?label=Docker%20Latest&logo=docker
[dockerdevelopversion-shield]: https://img.shields.io/docker/v/ptr727/projecttemplate/develop?label=Docker%20Develop&logo=docker&color=orange
[dockerbuildstatus-shield]: https://img.shields.io/github/actions/workflow/status/ptr727/ProjectTemplate/publish-periodic-docker-release.yml?logo=github&label=Docker%20Build

[lastbuild-shield]: https://byob.yarr.is/ptr727/ProjectTemplate/lastbuild
[lastcommit-shield]: https://img.shields.io/github/last-commit/ptr727/ProjectTemplate?logo=github&label=Last%20Commit

[releaseversion-shield]: https://img.shields.io/github/v/release/ptr727/ProjectTemplate?logo=github&label=GitHub%20Release
[prereleaseversion-shield]: https://img.shields.io/github/v/release/ptr727/ProjectTemplate?include_prereleases&label=GitHub%20Pre-Release&logo=github
[releasebuildstatus-shield]: https://img.shields.io/github/actions/workflow/status/ptr727/ProjectTemplate/publish-release.yml?logo=github&label=Releases%20Build

[nuget-link]: https://www.nuget.org/packages/ptr727.ProjectTemplate.Library/
[nugetreleaseversion-shield]: https://img.shields.io/nuget/v/ptr727.ProjectTemplate.Library?logo=nuget&label=NuGet%20Release
[nugetprereleaseversion-shield]: https://img.shields.io/nuget/vpre/ptr727.ProjectTemplate.Library?logo=nuget&&label=NuGet%20Pre-Release&color=orange

[pypi-link]: https://pypi.org/project/ptr727-projecttemplate-library/
[pypireleaseversion-shield]: https://img.shields.io/pypi/v/ptr727-projecttemplate-library?logo=pypi&label=PyPI%20Release

<!-- 3rd Party tool links -->

[devcontainers-link]: https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers

[apininjas-link]: https://api-ninjas.com/api/quotes
[awesomeassertions-link]: https://awesomeassertions.org/
[byob-link]: https://github.com/marketplace/actions/bring-your-own-badge
[createpr-link]: https://github.com/marketplace/actions/create-pull-request
[csharpier-link]: https://csharpier.com/
[ghactions-link]: https://github.com/actions
[ghautocommit-link]: https://github.com/marketplace/actions/git-auto-commit
[ghdependabot-link]: https://github.com/dependabot
[ghrelease-link]: https://github.com/marketplace/actions/gh-release
[huskynet-link]: https://alirezanet.github.io/Husky.Net/
[nerbankgitversion-link]: https://github.com/marketplace/actions/nerdbank-gitversioning
[serilog-link]: https://serilog.net/
[xunit-link]: https://xunit.net/
