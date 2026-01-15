# ProjectTemplate

C# .NET project template.

## Build Status

Code and Pipeline is on [GitHub](https://github.com/ptr727/ProjectTemplate)\
![GitHub Last Commit](https://img.shields.io/github/last-commit/ptr727/ProjectTemplate?logo=github)\
![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/ptr727/ProjectTemplate/publish-release.yml?logo=github)

## NuGet Package

Packages published on [NuGet](https://www.nuget.org/packages/ptr727.ProjectTemplate/)\
![NuGet](https://img.shields.io/nuget/v/ptr727.ProjectTemplate?logo=nuget)

## Version History

- v1.0:
  - Initial release.

## Developer Environment Setup

### Tools Setup

- Install VSCode and / or Visual Studio

### Git Setup

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

### Project Workspace Setup

- New project:
  - Create new project directory.
  - Copy and rename template projects.

  ```shell
  git init
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
  chmod +x ./.husky/pre-commit # Make sure file is executable in Linux
  ```

- Update tools in existing project:

  ```shell
  dotnet tool update --all
  dotnet outdated --upgrade:prompt
  ```

- Linux / macOS:
  - Verify that shell files are `+x` executable and `LF` line ending mode.
  - Verify that there are no duplicate files with different case names.

### GitHub Local Actions Setup

- Install [ACT](https://nektosact.com/installation/index.html):

  ```shell
  winget install nektos.act
  winget upgrade nektos.act
  ```

- Install [VSCode extension](https://sanjulaganepola.github.io/github-local-actions-docs/).
- Update [settings](https://nektosact.com/usage/index.html#action-artifacts) to start the artifact server.

  ```json
  "githubLocalActions.actCommand": "act --artifact-server-path ./.artifacts",
  ```

- Update local secrets:
  - Save the existing [Docker Hub Personal Access Token](https://app.docker.com/accounts/ptr727/settings/personal-access-tokens) as `DOCKER_HUB_ACCESS_TOKEN` and `DOCKER_HUB_USERNAME`.
  - Create a [GitHub Personal Access Token](https://github.com/settings/personal-access-tokens) as `GITHUB_TOKEN`.

## GitHub Setup

### GitHub Secrets Setup

- Create a [NuGet API Key](https://www.nuget.org/account/apikeys).
  - Save the Key as `NUGET_API_KEY` in:
    - GitHub project security Settings / Secrets / Actions.
    - GitHub project security Settings / Secrets / Dependabot.
    - GitHub Local Actions Settings / Secrets.
- Create a [Docker Hub Personal Access Token](https://app.docker.com/accounts/ptr727/settings/personal-access-tokens).
  - Save the PAT as `DOCKER_HUB_ACCESS_TOKEN` and `DOCKER_HUB_USERNAME` in:
    - GitHub project security Settings / Secrets / Actions.
    - GitHub project security Settings / Secrets / Dependabot.

### GitHub Project Settings

- General:
  - Default branch: `main`
  - Pull requests:
    - `Allow squash merging`
      - [TODO:](https://github.com/orgs/community/discussions/184410): Disable merge and rebase merging, ruleset merge rules do not work.
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

## Branching Workflow

- Create persistent `main` and `develop` branches.
- Make sure that `main` and `develop` are always building.
- Create feature branches from the `develop` branch.
- Always "Squash and merge" from feature branches to `develop` to reduce the history size.
- Always "Merge commit" from `develop` to `main` to retain merge history.

## GitHub Actions Workflow

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

Licensed under the [MIT License](./LICENSE)\
![GitHub](https://img.shields.io/github/license/ptr727/ProjectTemplate)
