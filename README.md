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

## Dev Setup

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
