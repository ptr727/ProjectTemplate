# ProjectTemplate

C# .NET project template.

<!-- Start of generic project layout -->

## Build and Distribution

- **Source Code**: [GitHub][github-link] - Source code, issues, discussions, and CI/CD pipelines.
- **Versioned Releases**: [GitHub Releases][releases-link] - Version tagged source code and build artifacts.
- **Docker Images**: [Docker Hub][docker-link] - Container images with all tools pre-installed.
- **NuGet Packages**: [NuGet Packages][nuget-link] - .NET libraries published to NuGet.org.
- **PyPI Packages**: [PyPI Packages][pypi-link] - Python library published to PyPI.org.

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

The recommended setup is one of the per-language [Dev Containers](./docs/devcontainer.md) under `.devcontainer/`:

- **`.devcontainer/dotnet/`** — .NET 10 SDK + GitHub CLI. Pair with `DotNet.code-workspace`.
- **`.devcontainer/python/`** — Python 3.14 + `uv` + GitHub CLI. Pair with `Python.code-workspace`.

Each container bind-mounts your SSH public key, allowed-signers file, and `gh` config from the host so commits sign correctly. `gh` is pre-authenticated when the host token is file-backed; macOS Keychain and Linux libsecret-backed tokens require an in-container `gh auth login` — see the [credential-store nuance](./docs/devcontainer.md#gh-credential-store) section.

> **Windows note**: Python work is intentionally not supported on the Windows host. The Python extension caches the Linux-layout `PyPiLibrary/.venv/bin/python` against a venv whose actual Windows path is `PyPiLibrary\.venv\Scripts\python.exe`, breaking Ruff. Use the python devcontainer.

**Recommended (devcontainer)**:

1. Complete [host setup](./docs/host-setup.md) once per machine (git identity, SSH key, allowed_signers, `gh auth login`, [SSH commit signing](./docs/ssh-signing.md)).
2. Clone the repo, open the matching workspace (`DotNet.code-workspace` or `Python.code-workspace`) in VS Code with the [Dev Containers extension][devcontainers-link], and run **Reopen in Container** — pick the language flavor.
3. The `postCreateCommand` runs `dotnet tool restore` (.NET container) or installs `uv` and runs `uv sync` (Python container). No git hooks are installed by default — see "Optional: enable git hooks locally" below.

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
    ```

  - Open `DotNet.code-workspace` (or `Python.code-workspace`) in Visual Studio Code.
  - Open `[Project].slnx` in Visual Studio.

**Optional: enable git hooks locally**:

Hooks are not shipped with the template — CI is the lint backstop. Opt in per language if you want pre-commit checks locally.

- **For .NET work** — install [Husky.Net][huskynet-link]:

    ```shell
    dotnet new tool-manifest  # if no tool manifest exists yet
    dotnet tool install Husky
    dotnet husky install
    dotnet husky add pre-commit -c "dotnet csharpier check . && dotnet format style --verify-no-changes --severity=info"
    ```

- **For Python work** — install [pre-commit][precommit-link]:

    ```shell
    uv tool install pre-commit
    pre-commit install
    ```

    Sample `.pre-commit-config.yaml` (the hooks shell into `PyPiLibrary/` because the uv project — and therefore ruff/pyright and their configs — lives there, not at the repo root):

    ```yaml
    repos:
      - repo: local
        hooks:
          - id: ruff-check
            name: ruff check
            entry: uv run --directory PyPiLibrary ruff check
            language: system
            files: ^PyPiLibrary/.*\.py$
            pass_filenames: false
          - id: ruff-format
            name: ruff format
            entry: uv run --directory PyPiLibrary ruff format --check
            language: system
            files: ^PyPiLibrary/.*\.py$
            pass_filenames: false
          - id: pyright
            name: pyright
            entry: uv run --directory PyPiLibrary pyright
            language: system
            files: ^PyPiLibrary/.*\.py$
            pass_filenames: false
    ```

CI runs these same checks on every PR, so hooks are purely a local convenience.

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
- [ ] Rename `DotNet.code-workspace` to `[NewProject].code-workspace` and `Python.code-workspace` to `[NewProject]-Python.code-workspace`, or delete the workspace for the language you don't need. Rename `ProjectTemplate.slnx` to `[NewProject].slnx`.
- [ ] Open the workspace file for the language you kept (`[NewProject].code-workspace` and/or `[NewProject]-Python.code-workspace`) in Visual Studio Code.
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
  dotnet tool install dotnet-outdated-tool
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
- Create a [GitHub App](https://github.com/settings/apps) for the codegen and merge-bot workflows.
  - App name: `ptr727-codegen`. Bot user: `ptr727-codegen[bot]`.
  - Permissions required (repository scope):
    - Contents: Read & write — push commits to the `codegen` branch and merge bot PRs.
    - Pull requests: Read & write — open, update, and merge pull requests.
    - Metadata: Read-only (auto-required).
  - Note the **Client ID** from the app's settings page (labeled "Client ID" directly under the App name on the General tab — it looks like `Iv23li...`; **not** the numeric App ID shown above it). `actions/create-github-app-token` deprecated the numeric `app-id` input in v3.0.0 in favor of `client-id`. Also generate a private key (downloads a `.pem` file).
  - [Install the app](https://github.com/settings/apps) on your account and grant it access to the repository. The app must be both created **and** installed — creating it alone is not sufficient (`actions/create-github-app-token` fails with `Not Found` if the app isn't installed on the repository).
  - Save the Client ID as `CODEGEN_APP_CLIENT_ID` and the private key contents as `CODEGEN_APP_PRIVATE_KEY` in **both** of:
    - GitHub project security Settings / Secrets / Actions — for the codegen workflow and the codegen merge job.
    - GitHub project security Settings / Secrets / Dependabot — **required** because Dependabot-triggered `pull_request` workflow runs use a separate, restricted secret context that doesn't see Actions secrets. Without the App secrets in the Dependabot store, the `merge-dependabot` job in `merge-bot-pull-request.yml` can't mint an App token and the PR will never auto-merge.
  - If the codegen workflows require additional secrets (e.g. third-party API keys), register them in the Actions store; if a Dependabot-triggered workflow ever needs them, register them in the Dependabot store too.
  - The App token is used by **both** the codegen workflow (`run-codegen-pull-request-task.yml`) **and** every job in `merge-bot-pull-request.yml`. App-authored pushes/PRs trigger downstream `pull_request` and `push` workflow events directly — unlike `GITHUB_TOKEN`-authored events, which are blocked by GitHub's recursion guard. This matters for two reasons: bot-opened PRs trigger the `test-pull-request.yml` smoke build (so they can't auto-merge unvalidated), and — when `PUBLISH_ON_MERGE` is enabled — the merge commit triggers `publish-release.yml`. It also means the codegen workflow no longer needs the legacy close/reopen dance to trigger auto-merge.
  - The codegen auto-merge condition in `merge-bot-pull-request.yml` (`merge-codegen` job) requires:
    - **Event is `opened` or `reopened`** — auto-merge is enabled once per PR at open time; subsequent `synchronize` events do not re-enable. This is what lets the `disable-auto-merge-on-maintainer-push` safeguard (below) stick.
    - `github.event.pull_request.user.login == 'ptr727-codegen[bot]'` — PR was opened by the App.
    - `github.event.pull_request.head.repo.full_name == github.repository` — PR is from this repo (not a fork).
    - **Strict head/base pairing** — `(head.ref == 'codegen-main' && base.ref == 'main') || (head.ref == 'codegen-develop' && base.ref == 'develop')`. Codegen runs as a matrix opening one PR per branch; this pairing prevents a misconfigured workflow from sneaking a `codegen-develop` branch into `main` or vice versa.
  - The `disable-auto-merge-on-maintainer-push` job in `merge-bot-pull-request.yml` runs on `synchronize` events against bot-authored PRs (Dependabot or codegen) when the event actor is NOT the same bot — i.e. a maintainer pushed commits. It calls `gh pr merge --disable-auto` so the maintainer's commits don't auto-merge along with the bot's content. Re-enable auto-merge manually (`gh pr merge --auto <PR>` or the GitHub UI) when ready.

  Codegen targets `main` AND `develop` in parallel (matrix in `run-codegen-pull-request-task.yml`), so generated content lands on both branches independently without any back-merging. See [AGENTS.md "Branching Model"](./AGENTS.md#branching-model) for why this dual-target pattern beats develop-only-with-flow-through.

**Codegen workflow schedule**:

- `run-periodic-codegen-pull-request.yml` runs **daily** at 04:00 UTC (staggered two hours after the weekly publish), plus on-demand via `workflow_dispatch`. It uses the App token (`CODEGEN_APP_CLIENT_ID` + `CODEGEN_APP_PRIVATE_KEY`) to commit, open the PR as `ptr727-codegen[bot]`, and let the merge-bot auto-merge once CI passes. No PAT, no close/reopen dance. Daily is cheap in the default two-phase model — codegen merges only smoke-test; the weekly publish batches the actual release.

**GitHub project settings**:

- General:
  - Default branch: `main`
  - Pull requests — **both** merge methods enabled at the repo level so each branch ruleset can pick the right one (develop = `Squash`, main = `Merge`):
    - `Allow merge commits` ✓ (required for develop → main releases)
    - `Allow squash merging` ✓ (required for feature → develop merges)
    - `Allow rebase merging` — disabled (no flow uses it; the develop ruleset forbids it anyway)
    - `Always suggest updating pull request branches`
    - `Allow auto-merge`
- Rules / Rulesets — **separate rulesets per branch**. Develop and main intentionally diverge on two rules — allowed merge methods and `Require linear history`. `Require branches to be up to date before merging` is **off on both** for related-but-distinct reasons (below); everything else is shared.
  - "Develop":
    - Target branches: `develop`.
    - Allowed merge methods: `Squash`
    - `Require linear history` (develop is kept linear; main carries merge commits by design, so this setting belongs to develop only)
    - `Require status checks to pass` → `Require branches to be up to date before merging` **intentionally OFF**. Leaving it on stalls bot auto-merge when two bot PRs against develop land within the same window — the first merge flips the second to `mergeStateStatus: BEHIND`, and GitHub's auto-merge will not fire while strict is on. The merge-bot in [`.github/workflows/merge-bot-pull-request.yml`](./.github/workflows/merge-bot-pull-request.yml) only enables auto-merge on `opened`/`reopened` and never auto-updates bot branches; Dependabot's rebase isn't real-time. With strict off, squash mechanics still rebase the diff onto develop's tip on merge, `Require linear history` still enforces linearity, textual conflicts still block `mergeable: CONFLICTING`, and the required `Check pull request workflow status` still gates merges. See [AGENTS.md "Branching Model"](./AGENTS.md#branching-model) for the full reasoning.
    - Plus shared settings (below).
  - "Main":
    - Target branches: `main`.
    - Allowed merge methods: `Merge`
    - `Require status checks to pass` → `Require branches to be up to date before merging` **intentionally OFF**. This rule is incompatible with the forward-only develop model. GitHub's "up to date" check is graph-based: it asks whether main's tip commit is reachable from develop. After any develop → main release, main's new tip is a brand-new merge commit that develop's history doesn't contain. Forward-only develop never adds it (no back-merge of main into develop, no rebase of develop onto main), so the check fails permanently on every subsequent release. Leaving the rule on would force every release through an admin bypass. See [AGENTS.md "Branching Model"](./AGENTS.md#branching-model) for the full reasoning.
    - Plus shared settings (below).
  - Shared settings (apply to both rulesets):
    - `Restrict deletions`
    - `Require signed commits`
    - `Require a pull request before merging`
      - `Dismiss stale pull request approvals when new commits are pushed`
      - `Require conversation resolution before merging`
    - `Require status checks to pass`
      - Status checks that are required: `Check pull request workflow status`
    - `Block force pushes`
    - `Automatically request Copilot code review`
      - `Review new pushes`
      - `Review draft pull requests`
- Actions / General:
  - `Allow GitHub Actions to create and approve pull requests`

### Template - Branching Workflow

See [AGENTS.md "Branching Model"](./AGENTS.md#branching-model) for the authoritative definition. Summary:

- Persistent `main` and `develop` branches, each with its own ruleset (above). Both must always be building error free.
- Feature branches off `develop`. Only commit on feature branches, never directly to `develop` or `main`.
- Feature → `develop`: **squash-merge** (develop ruleset enforces this; develop is kept linear).
- `develop` → `main`: **merge-commit** (preserves develop's commit list as a real second-parent reference on main; main ruleset enforces this).
- **`develop` is forward-only.** No `main → develop` back-merges. The develop squash-only ruleset physically blocks merge commits.
- **Bots open parallel PRs against both branches.** [`.github/dependabot.yml`](./.github/dependabot.yml) duplicates each ecosystem entry per branch, and [`.github/workflows/run-codegen-pull-request-task.yml`](./.github/workflows/run-codegen-pull-request-task.yml) runs as a matrix (branch names `codegen-main` and `codegen-develop`). Each branch absorbs its own bot PRs independently — neither falls behind, no back-merges needed.
- **Review-then-merge loop.** Every PR is reviewed by GitHub Copilot. The agent pushes, re-requests a review on the new head (now reliable via the `requestReviews` GraphQL mutation), addresses and resolves each finding, repeats until green, and then **waits for the maintainer's explicit permission to merge** — it does not self-merge. See [AGENTS.md "PR Review Etiquette"](./AGENTS.md#pr-review-etiquette) and the [Copilot Review Runbook](./.github/copilot-instructions.md#github-copilot-review-runbook) for the mechanics.

### Template - Release Distribution Model: Two-Phase by Default

This template ships with a **two-phase model** that decouples merging from publishing:

- **Pull requests smoke-test only.** [`.github/workflows/test-pull-request.yml`](./.github/workflows/test-pull-request.yml) always runs unit tests, then path-gates a **reduced** build of only the targets a PR touches (`dorny/paths-filter`): Docker as `linux/amd64` only (no QEMU/arm64), the executable as a representative runtime subset, and nothing is pushed. A docs-only PR runs unit tests alone; a Dependabot github-actions bump is unit-tests-only. This is fast feedback, not a release.
- **Merges to `main`/`develop` do not publish.** A push only smoke-tested the PR; merging it republishes nothing.
- **The weekly schedule + manual dispatch are the sole publishers.** [`.github/workflows/publish-release.yml`](./.github/workflows/publish-release.yml) runs every **Monday 02:00 UTC** and on-demand via `workflow_dispatch`, and on either trigger does the **full** build/publish of **both** `main` (Release / `latest` / non-prerelease) and `develop` (Debug / `develop` / prerelease) — GitHub release, NuGet/PyPI uploads, multi-arch Docker tags, platform executables, and a refreshed Docker base image. Trigger a release on demand from the Actions UI when you want one between weekly runs.

This batches cheap bot churn (Dependabot/codegen merge daily, validated by smoke builds) into one periodic publish instead of one release per merge, and keeps PR feedback fast by deferring the slow `arm64`/full-matrix builds to the publisher.

**Opt in to publish-on-merge.** Set the repository variable `PUBLISH_ON_MERGE` to `true` (Settings → Secrets and variables → Actions → Variables) to restore the legacy **continuous-release** model: every push/merge to `main` publishes `main` and every push to `develop` publishes `develop`, immediately. The weekly + manual publishers still run. Leave the variable unset (or `false`) for the two-phase default. It's a repository variable, not a workflow edit, so pulling template updates never conflicts with your choice.

Which to pick: two-phase suits projects whose consumers are **pushed** updates (HACS for Home Assistant, package managers that auto-update, Linux distros that vendor from `main`) where every release is a forced update and frequent bot-driven releases are noise. `PUBLISH_ON_MERGE=true` suits projects whose consumers **pull** at their own cadence (Docker pulls, NuGet/PyPI installs, manual downloads) and want every merged change available immediately. For an example of a push-distribution project, see [homeassistant-purpleair](https://github.com/ptr727/homeassistant-purpleair) (ships through HACS).

<!--- Shields links (alphabetized per AGENTS.md) --->

[actions-link]: https://github.com/ptr727/ProjectTemplate/actions
[commits-link]: https://github.com/ptr727/ProjectTemplate/commits/main
[discussions-link]: https://github.com/ptr727/ProjectTemplate/discussions
[docker-link]: https://hub.docker.com/r/ptr727/projecttemplate
[dockerbuildstatus-shield]: https://img.shields.io/github/actions/workflow/status/ptr727/ProjectTemplate/publish-release.yml?logo=github&label=Docker%20Build
[dockerdevelopversion-shield]: https://img.shields.io/docker/v/ptr727/projecttemplate/develop?label=Docker%20Develop&logo=docker&color=orange
[dockerlatestversion-shield]: https://img.shields.io/docker/v/ptr727/projecttemplate/latest?label=Docker%20Latest&logo=docker
[github-link]: https://github.com/ptr727/ProjectTemplate
[issues-link]: https://github.com/ptr727/ProjectTemplate/issues
[lastbuild-shield]: https://byob.yarr.is/ptr727/ProjectTemplate/lastbuild
[lastcommit-shield]: https://img.shields.io/github/last-commit/ptr727/ProjectTemplate?logo=github&label=Last%20Commit
[license-link]: ./LICENSE
[license-shield]: https://img.shields.io/github/license/ptr727/ProjectTemplate?label=License
[nuget-link]: https://www.nuget.org/packages/ptr727.ProjectTemplate.Library/
[nugetreleaseversion-shield]: https://img.shields.io/nuget/v/ptr727.ProjectTemplate.Library?logo=nuget&label=NuGet%20Release
[prereleaseversion-shield]: https://img.shields.io/github/v/release/ptr727/ProjectTemplate?include_prereleases&filter=*-g*&label=GitHub%20Pre-Release&logo=github
[pypi-link]: https://pypi.org/project/ptr727-projecttemplate-library/
[pypireleaseversion-shield]: https://img.shields.io/pypi/v/ptr727-projecttemplate-library?logo=pypi&label=PyPI%20Release
[releasebuildstatus-shield]: https://img.shields.io/github/actions/workflow/status/ptr727/ProjectTemplate/publish-release.yml?logo=github&label=Releases%20Build
[releases-link]: https://github.com/ptr727/ProjectTemplate/releases
[releaseversion-shield]: https://img.shields.io/github/v/release/ptr727/ProjectTemplate?logo=github&label=GitHub%20Release

<!-- 3rd Party tool links (alphabetized per AGENTS.md) -->

[apininjas-link]: https://api-ninjas.com/api/quotes
[awesomeassertions-link]: https://awesomeassertions.org/
[byob-link]: https://github.com/marketplace/actions/bring-your-own-badge
[createpr-link]: https://github.com/marketplace/actions/create-pull-request
[csharpier-link]: https://csharpier.com/
[devcontainers-link]: https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers
[ghactions-link]: https://github.com/actions
[ghautocommit-link]: https://github.com/marketplace/actions/git-auto-commit
[ghdependabot-link]: https://github.com/dependabot
[ghrelease-link]: https://github.com/marketplace/actions/gh-release
[huskynet-link]: https://alirezanet.github.io/Husky.Net/
[nerbankgitversion-link]: https://github.com/marketplace/actions/nerdbank-gitversioning
[precommit-link]: https://pre-commit.com/
[serilog-link]: https://serilog.net/
[xunit-link]: https://xunit.net/
