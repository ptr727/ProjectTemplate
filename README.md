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

- [Register](https://github.com/settings/keys) SSH Key on GitHub.

  ```shell
  ssh-keygen -t ed25519 # If not already created
  cat ~/.ssh/id_ed25519.pub
  ssh-keyscan github.com >> ~/.ssh/known_hosts
  ssh -v -T git@github.com
  ```

- Add [SSH keys to GitHub](https://github.com/settings/keys), add both an authentication and a signing key.
- Configure git for [SSH signing](https://docs.github.com/en/authentication/managing-commit-signature-verification/telling-git-about-your-signing-key):

  ```shell
  git config --global credential.helper "cache --timeout=3600"
  git config --global user.name "Pieter Viljoen"
  git config --global user.email "ptr727@users.noreply.github.com"
  git config --global core.sharedRepository group
  git config --global --add safe.directory '*'

  git config --global gpg.format ssh
  cat ~/.ssh/id_ed25519.pub
  git config --global user.signingkey "~/.ssh/id_ed25519.pub"
  git config --global commit.gpgsign true
  git config --global tag.gpgsign true

  git show --show-signature # error: gpg.ssh.allowedSignersFile needs to be configured and exist for ssh signature verification
  mkdir -p ~/.config/git
  touch ~/.config/git/allowed_signers
  git config --global gpg.ssh.allowedSignersFile "~/.config/git/allowed_signers"
  echo "$(git config --get user.email) namespaces=\"git\" $(cat ~/.ssh/id_ed25519.pub)" >> ~/.config/git/allowed_signers
  cat ~/.config/git/allowed_signers
  git log --show-signature # Good "git" signature for ptr727@users.noreply.github.com with ED25519 key SHA256:[secret]

  git config --list --show-origin
  ```

- New project:

  ```shell
  git init
  dotnet new tool-manifest
  dotnet tool install csharpier
  dotnet tool install husky
  dotnet tool install dotnet-outdated-tool
  dotnet husky install
  dotnet husky add pre-commit -c "dotnet husky run"
  ```

- New deployment:

  ```shell
  dotnet tool restore
  dotnet husky install
  chmod +x ./.husky/pre-commit # Make sure file is executable in Linux
  winget install nektos.act # Windows optional
  ```

- Update tools:

  ```shell
  dotnet tool update --all
  dotnet outdated --upgrade:prompt
  winget upgrade nektos.act # Windows optional
  ```

- Linux file modes:
  - `chmod +x [filename.sh]`
  - All shell files are `LF` mode.

## Secrets Setup

- Create a [Nuget API Key](https://www.nuget.org/account/apikeys).
  - Save the Key as `NUGET_API_KEY` in:
    - GitHub project security Settings / Secrets / Actions.
    - GitHub project security Settings / Secrets / Dependabot.
    - GitHub Local Actions Settings / Secrets.
- Create a [Docker Hub Personal Access Token](https://app.docker.com/accounts/ptr727/settings/personal-access-tokens).
  - Save the PAT as `DOCKER_HUB_ACCESS_TOKEN` and `DOCKER_HUB_USERNAME` in:
    - GitHub project security Settings / Secrets / Actions.
    - GitHub project security Settings / Secrets / Dependabot.
    - GitHub Local Actions Settings / Secrets.
- Create a [GitHub Personal Access Token](https://github.com/settings/personal-access-tokens).
  - Save the PAT as `GITHUB_TOKEN` in:
    - GitHub Local Actions Settings / Secrets.

## GitHub Local Actions Setup

- Update [ACT settings](https://nektosact.com/usage/index.html#action-artifacts) to start the artifact server `act --artifact-server-path ./.artifacts`.

## GitHub Project Settings

- General:
  - Set the default branch to `main`.
  - Enable `Always suggest updating pull request branches`.
  - Enable `Allow auto-merge`.
  - Enable `Automatically delete head branches`.
- Rules / Branch Ruleset:
  - `main`:
    - `Restrict deletions`
    - `Require linear history`
    - `Require signed commits`
    - `Require a pull request before merging`
    - `Dismiss stale pull request approvals when new commits are pushed`
    - `Require status checks to pass`
      - `Require branches to be up to date before merging`
      - Add checks: `Check pull request workflow status`
      - `Block force pushes`
    - `Automatically request Copilot code review`
  - `develop`:
    - `Restrict deletions`
    - `Require linear history`
    - `Require signed commits`
    - `Automatically request Copilot code review`
- Actions / General:
  - `Allow GitHub Actions to create and approve pull requests`

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
