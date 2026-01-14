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

New project:

```shell
git init
dotnet new tool-manifest
dotnet tool install csharpier
dotnet tool install husky
dotnet tool install dotnet-outdated-tool
```

New pull:

```shell
dotnet tool restore
dotnet husky install
dotnet husky add pre-commit -c "dotnet husky run"
winget install nektos.act
```

Update tools:

```shell
dotnet tool update --all
dotnet outdated --upgrade:prompt
winget upgrade nektos.act
```

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
    - `Require status checks to pass`
    - `Block force pushes`
    - `Require branches to be up to date before merging`
    - Add checks: `Test project build PRs`
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
