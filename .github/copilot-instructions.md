# GitHub Copilot Instructions for ProjectTemplate

## Project Overview

**ProjectTemplate** is a C# .NET template project that demonstrates best practices for C# .NET development. The project includes:

- **NuGetLibrary**: Core .NET NuGet library with AOT compatibility (`NuGetLibrary.csproj`, published as `ptr727.ProjectTemplate.Library`)
- **Console**: Command-line application using System.CommandLine (`Console.csproj`)
- **Tests**: Unit tests using xUnit and AwesomeAssertions (`Tests.csproj`)
- **Benchmarks**: Performance benchmarks using BenchmarkDotNet (`Benchmarks.csproj`)
- **Docker**: Docker build configurations for Linux containers

## Build Requirements

### Zero Warnings Policy

**CRITICAL**: All builds must complete without warnings. The project enforces this through:

1. **VS Code Task**: The `.Net Format` task must run successfully with `--verify-no-changes` flag
   - Command: `dotnet format style --verify-no-changes --severity=info --verbosity=detailed`
   - This task must pass before any code is committed
   - Task dependencies: `CSharpier Format` → `.Net Build` → `.Net Format`

2. **Analysis Level**: Projects use `<AnalysisLevel>latest-all</AnalysisLevel>`
   - All .NET analyzers enabled: `<EnableNETAnalyzers>true</EnableNETAnalyzers>`
   - Analyzer severity: `suggestion` (but must be addressed)

3. **Husky.Net Pre-commit Hooks**: Automated checks run before commits

### Build Tasks

Available VS Code tasks (use via `run_task` tool):
- `.Net Build`: Build with diagnostic verbosity
- `.Net Format`: Verify formatting and style (must pass)
- `CSharpier Format`: Auto-format code with CSharpier
- `.Net Tool Update`: Update dotnet tools
- `.Net Outdated Upgrade`: Upgrade outdated NuGet dependencies (interactive prompt)
- `Husky.Net Run`: Run pre-commit hooks manually

## Coding Standards and Conventions

### C# Language Features

1. **File-Scoped Namespaces**: Always use file-scoped namespaces
   ```csharp
   namespace ptr727.ProjectTemplate.NuGetLibrary;
   ```

2. **Nullable Reference Types**: Enabled (`<Nullable>enable</Nullable>`)
   - Always use nullable annotations appropriately
   - Use `required` modifier for mandatory properties

3. **Modern C# Features**: Prefer modern language constructs
   - Primary constructors when appropriate
   - Top-level statements for console apps
   - Pattern matching over traditional checks
   - Collection expressions when types loosely match
   - Extension methods using `extension()` syntax (C# 13)
   - Implicit object creation when type is apparent
   - Range and index operators

4. **Expression-Bodied Members**: Use for all applicable members
   - Methods, properties, accessors, operators, lambdas, local functions

5. **var Keyword**: Do NOT use `var` - always use explicit types
   ```csharp
   // Correct
   int count = 42;
   string name = "test";

   // Incorrect
   var count = 42;
   var name = "test";
   ```

### Naming Conventions

1. **Private Fields**: Use underscore prefix with camelCase
   ```csharp
   private readonly HttpClient _httpClient;
   private int _counter;
   ```

2. **Static Fields**: Use `s_` prefix with camelCase
   ```csharp
   private static int s_instanceCount;
   ```

3. **Constants**: Use PascalCase
   ```csharp
   private const int MaxRetries = 3;
   ```

4. **Namespace**: Follow format `ptr727.ProjectTemplate.<ProjectName>`
   - NuGetLibrary: `ptr727.ProjectTemplate.NuGetLibrary`
   - Console: `ptr727.ProjectTemplate.Console`
   - Tests: `ptr727.ProjectTemplate.Tests`

### Code Structure

1. **Global Usings**: Use `GlobalUsings.cs` for common namespaces
   ```csharp
   global using System;
   global using System.Net.Http;
   global using System.Threading.Tasks;
   global using Serilog;
   ```

2. **Usings Placement**: Outside namespace, sorted with System directives first
   ```csharp
   using System.CommandLine;
   using System.Runtime.CompilerServices;
   using ptr727.ProjectTemplate.NuGetLibrary;

   namespace ptr727.ProjectTemplate.Console;
   ```

3. **Braces**: New line before all braces (Allman style)
   ```csharp
   public void Method()
   {
       if (condition)
       {
           // code
       }
   }
   ```

4. **Indentation**:
   - C# files: 4 spaces
   - XML/csproj files: 2 spaces
   - YAML files: 2 spaces
   - JSON files: 4 spaces

5. **Line Endings**:
   - C#, XML, YAML, JSON, Windows scripts: CRLF
   - Linux scripts (.sh): LF

### Comments and Documentation

1. **XML Documentation**: Generate documentation files
   - `<GenerateDocumentationFile>true</GenerateDocumentationFile>`
   - Missing XML comments for public APIs are suppressed (NoWarn 1591)

2. **Code Analysis Suppressions**: Use attributes with justifications
   ```csharp
   [System.Diagnostics.CodeAnalysis.SuppressMessage(
       "Design",
       "CA1034:Nested types should not be visible",
       Justification = "https://github.com/dotnet/sdk/issues/51681"
   )]
   ```

3. **Spelling**: All code must pass the Code Spell Checker extension
   - Configure exceptions in workspace settings if needed
   - British and American spelling both accepted

4. **Markdown Quality**: Markdown files must pass Markdownlint
   - Proper heading hierarchy, spacing, and formatting


### Error Handling and Logging

1. **Serilog Logging**: Use structured logging with Serilog
   ```csharp
   logger.Error(exception, "{Function}", function);
   ```

2. **CallerMemberName**: Use for automatic function name tracking
   ```csharp
   public bool LogAndPropagate(
       Exception exception,
       [CallerMemberName] string function = "unknown"
   )
   ```

3. **Extension Methods**: Use for logger extensions
   ```csharp
   extension(ILogger logger)
   {
       public bool LogAndPropagate(Exception exception, ...) { }
   }
   ```

### Testing Conventions

1. **Test Framework**: xUnit with AwesomeAssertions
   ```csharp
   [Fact]
   public void MethodName_Scenario_ExpectedBehavior()
   {
       // Arrange
       int expected = 42;

       // Act
       int actual = GetValue();

       // Assert
       actual.Should().Be(expected);
   }
   ```

2. **Test Organization**: Arrange-Act-Assert pattern
3. **Test Naming**: Use descriptive names with underscores separating parts
4. **Theory Tests**: Use `[Theory]` with `[InlineData]` for parameterized tests
5. **Avoid Regions**: Don't use regions in test files
6. **Logical Grouping**: Organize tests in separate files by feature or class


### Project Configuration

1. **Target Framework**: .NET 10.0 (`<TargetFramework>net10.0</TargetFramework>`)

2. **AOT Compatibility**: NuGetLibrary is AOT compatible
   - `<IsAotCompatible>true</IsAotCompatible>`
   - `<VerifyReferenceAotCompatibility>true</VerifyReferenceAotCompatibility>`

3. **Assembly Information**:
   - Use semantic versioning
   - Include SourceLink: `<PublishRepositoryUrl>true</PublishRepositoryUrl>`
   - Embed untracked sources: `<EmbedUntrackedSources>true</EmbedUntrackedSources>`

4. **Internal Visibility**: Use `InternalsVisibleTo` for test and console access
   ```xml
   <ItemGroup>
     <InternalsVisibleTo Include="Console" />
     <InternalsVisibleTo Include="Tests" />
   </ItemGroup>
   ```

5. **Directory.Build.props**: Common MSBuild properties shared across all projects
   (`TargetFramework`, `Nullable`, `ImplicitUsings`, `AnalysisLevel`, `AnalysisMode`,
   `EnableNETAnalyzers`, `ArtifactsPath`, `IsPackable`, `ManagePackageVersionsCentrally`)
   live here at the solution root. Only add a property to a `.csproj` when it is
   specific to that project or requires an explicit override of the shared default.

6. **Directory.Packages.props**: All NuGet package versions are centralised here via
   `PackageVersion` items. Individual `.csproj` files use `PackageReference Include="..."`
   with no `Version` attribute. Asset metadata (`PrivateAssets`, `IncludeAssets`) stays
   in the `.csproj` `PackageReference` element. Use `VersionOverride` only when a project
   genuinely requires a different version from the central default.

### Code Formatting Tools

1. **CSharpier**: Primary code formatter
   - Run before committing: `dotnet csharpier format --log-level=debug .`

2. **dotnet format**: Style verification
   - Verify no changes: `dotnet format style --verify-no-changes --severity=info --verbosity=detailed`

3. **Husky.Net**: Git hooks for automated checks
   - Installed via restore target in `.csproj`
   - Pre-commit hooks run formatting checks

## Dependencies and Packages

### Core Dependencies

- **CliWrap**: Command-line process execution
- **System.CommandLine**: Command-line argument parsing
- **Serilog**: Structured logging with sinks (Console, File, Async)
- **Microsoft.Extensions.Http.Resilience**: HTTP client with resilience
- **Microsoft.SourceLink.GitHub**: Source link for debugging

### Testing Dependencies

- **xUnit**: Test framework
- **AwesomeAssertions**: Fluent assertion library
- **BenchmarkDotNet**: Performance benchmarking

### Development Tools

- **CSharpier**: Code formatter
- **Husky.Net**: Git hooks
- **dotnet-outdated-tool**: Dependency update checks
- **Nerdbank.GitVersioning**: Version management

## Docker

- Base images: Ubuntu Rolling
- Multi-platform support: linux/amd64, linux/arm64
- Build script: `Build.sh`
- Debug tools: `InstallDebugTools.sh`

## Project Structure

- `.config/` - .NET tools configuration
- `.github/` - GitHub Actions workflows and Copilot instructions
- `.husky/` - Husky.Net git hooks
- `.vscode/` - Visual Studio Code settings and launch configurations
- `Benchmarks/` - BenchmarkDotNet performance measurement project
- `CodeGen/` - Code generation utilities (internal tooling)
- `Console/` - Console/CLI application using System.CommandLine
- `Docker/` - Docker build scripts and Dockerfile
- `NuGetLibrary/` - Core reusable .NET NuGet library (published as `ptr727.ProjectTemplate.Library`)
- `Tests/` - Unit tests using xUnit and AwesomeAssertions

## Best Practices

1. **Immutability**: Prefer `readonly` and `required` for fields and properties
2. **Async/Await**: Use async patterns consistently
3. **Cancellation Tokens**: Support cancellation in async methods
4. **Parallel Processing**: Use `ParallelOptions` for controlled parallelism
5. **HTTP Clients**: Use `HttpClientFactory` for HTTP client creation
6. **Dispose Pattern**: Implement IDisposable/IAsyncDisposable when managing resources
7. **Static Analysis**: Address all analyzer warnings - zero warnings policy
8. **Code Reviews**: All changes go through pull requests
9. **Git Versioning**: Use Nerdbank.GitVersioning for version management
10. **No Regions**: Avoid code regions - use logical file separation instead


## Editor Configuration

The project includes comprehensive `.editorconfig` settings that enforce:
- Character encoding (UTF-8)
- Indentation rules
- Line ending conventions
- C# style preferences
- Naming conventions
- Code analysis settings

**Always respect the .editorconfig settings** - these are verified by the build process.

## Git and Commit Rules

**These rules are absolute — no exceptions:**

- **Never make git commits.** All commits must be cryptographically signed (SSH/GPG). AI coding agents cannot produce signed commits. Stage changes with `git add` and leave `git commit` to the developer, who must run it in their own environment where signing keys are available.
- **Never force push.** Do not run `git push --force` or `git push --force-with-lease`. Force pushing rewrites shared branch history and is blocked by branch protection rules.
- **Never run destructive git commands** (`git reset --hard`, `git checkout .`, `git restore .`, `git clean -f`) without explicit developer instruction.
- **Staging is the limit.** Prepare changes and stage files; the developer handles all commits and pushes.

## Workflow

1. **Before coding**: Run `dotnet tool restore` to ensure tools are installed
2. **During development**: Use CSharpier for formatting as you go
3. **Before committing**:
   - Run `.Net Format` task to verify compliance
   - Husky hooks will run automatically
4. **Dependency updates**: Run `.Net Outdated Upgrade` task (`dotnet outdated --upgrade:prompt`) regularly
5. **Testing**: Run tests via VS Code test explorer or `dotnet test`

## Reference Links

- [Microsoft C# Coding Conventions](https://learn.microsoft.com/en-us/dotnet/csharp/fundamentals/coding-style/coding-conventions)
- [.NET Runtime Coding Style](https://github.com/dotnet/runtime/blob/main/docs/coding-guidelines/coding-style.md)
- [dotnet format Documentation](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-format)
- [EditorConfig Documentation](https://editorconfig.org)
- [CSharpier Documentation](https://csharpier.com)
- [Husky.Net Documentation](https://alirezanet.github.io/Husky.Net)
- [xUnit Documentation](https://xunit.net)
- [AwesomeAssertions Documentation](https://awesomeassertions.org/)
- [BenchmarkDotNet Documentation](https://benchmarkdotnet.org)
- [System.CommandLine Documentation](https://learn.microsoft.com/en-us/dotnet/standard/commandline/)
- [Serilog Documentation](https://serilog.net)

