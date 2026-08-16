---
name: dotnet-codestyle
description: >-
  Governs C#/.NET code style for ptr727/ProjectTemplate fleet repos: the zero-warnings build
  policy and its three-task clean-compile chain, central Directory.Build.props/
  Directory.Packages.props configuration, C# language and naming conventions, XML documentation,
  analyzer suppression scope, the library-versus-application logging split, async and
  error-handling patterns, xUnit v3 + AwesomeAssertions testing conventions, and AOT-compatible
  project configuration. Use this whenever writing, reviewing, or editing a .cs file, a .csproj,
  Directory.Build.props, or Directory.Packages.props, whenever choosing where to suppress an
  analyzer diagnostic, whenever a NuGet library needs to log without depending on Serilog
  directly, or whenever writing or reviewing an xUnit test. Triggers even when the task looks
  like a small local fix ("just silence this warning", "add a quick log line", "bump a package
  version"), because the zero-warnings policy, the suppression-scope order, the central-package-
  management rule, and the library/application logging split are each easy to violate one file at
  a time without the pattern ever showing up as a single obvious diff. Applies only to a repo's
  .NET side, a repo with no .NET projects has no use for this Skill.
---

# .NET Codestyle

## Why this exists

This is the .NET-specific half of the fleet's code style guide, kept in one place instead of
re-derived per repo or per session. CODESTYLE.md's General section still owns the rules every
language shares (clean-compile verification as a concept, the suppression-scope order, tooling
casing in prose), this Skill is everything specific to a C#/.NET project on top of that: the
concrete `.NET Format` task chain, the analyzer configuration that makes the zero-warnings policy
real, and the language, naming, logging, and testing conventions.

## Build requirements

### Zero warnings policy

All builds must complete without warnings, enforced three ways:

- **The `.NET Format` clean-compile task.** It chains `CSharpier Format` -> `.NET Build` ->
  `dotnet format style --verify-no-changes`. A repo carries those three task definitions in its
  own `.vscode/tasks.json`, matching the canonical `vscode-tasks.json` snippet at
  `github.com/ptr727/ProjectTemplate/blob/main/catalog/snippets/configs/vscode-tasks.json`. Run
  the `.NET Format` task after any code change, before commit. To run it natively instead,
  reproduce that exact task chain (`CSharpier Format`, then `.NET Build`, then
  `dotnet format style --verify-no-changes --severity=info --verbosity=detailed`) without dropping
  or loosening any argument, reading it from that same canonical snippet. Bare `dotnet format`
  alone, skipping CSharpier or the build, is not sufficient.
- **Analyzer configuration.** `<EnableNETAnalyzers>true</EnableNETAnalyzers>` with
  `<AnalysisLevel>latest-all</AnalysisLevel>` and `<AnalysisMode>All</AnalysisMode>` (the full
  analyzer set), plus `<TreatWarningsAsErrors>true</TreatWarningsAsErrors>`, so any diagnostic
  surfaced as a warning fails the build and must be fixed or deliberately suppressed at the
  narrowest scope that fits (see Analyzer suppressions below), never left to accumulate.
- **CI lint backstop.** CI runs the clean-compile checks on every PR as the authoritative gate.
  Git hooks are optional, and a repo may wire a local runner (Husky.Net, with `dotnet husky run`
  as a style step) for pre-commit enforcement, but CI is the gate that matters.

**A new port is not a license to silence diagnostics.** Brownfield or just-ported status never
justifies relaxing analyzer severities or muting newly surfaced warnings. Fix them. (The only
brownfield allowance in the fleet is the one-time git-signing / line-ending migration described in
GOVERNANCE.md and README.md, which has nothing to do with code analysis.)

### Central build and package configuration

Shared MSBuild configuration is centralized at the repository root, never duplicated per project:

- **`Directory.Build.props`** carries the properties every project shares: the analyzer set and
  `TreatWarningsAsErrors` from the zero-warnings policy above, plus `LangVersion`,
  `TargetFramework` where uniform, and any repo-wide build metadata. A `.csproj` carries only what
  is genuinely project-specific (`OutputType`, `IsPackable`, project references).
- **`Directory.Packages.props`** owns central package management: it sets
  `ManagePackageVersionsCentrally` to `true` (in this file, not `Directory.Build.props`) and
  declares every dependency version once as a `PackageVersion` item, so a `.csproj`'s
  `PackageReference` items are versionless. One file to review on a bump, one Dependabot surface,
  no version skew between projects.

A repo whose projects still carry per-project analyzer settings or versioned `PackageReference`
items is drifted, move the shared property or version up to the root file rather than editing it
in place.

### Build tasks

Run these from VS Code's task runner (Terminal -> Run Task) or an agent's task-running tool. The
three clean-compile tasks are carried verbatim, and a repo adds its own convenience tasks (tool
updates, dependency upgrades, benchmarks) on top:

- `.NET Build`: build with diagnostic verbosity *(clean-compile)*
- `CSharpier Format`: auto-format code with CSharpier *(clean-compile)*
- `.NET Format`: run CSharpier and build, then verify formatting and style with
  `--verify-no-changes` *(clean-compile, the task to run after edits)*

## Tooling and editor

- **CSharpier** is the primary code formatter, invoked by the `CSharpier Format` task or
  `dotnet csharpier format --log-level=debug .`.
- **`dotnet format`** verifies style:
  `dotnet format style --verify-no-changes --severity=info --verbosity=detailed`.
- **`dotnet-outdated-tool`** checks for dependency updates, and Nerdbank.GitVersioning owns
  version management.
- CI is the authoritative lint backstop. Local pre-commit hooks are optional, wire Husky.Net (or
  another runner) if you want local enforcement.
- **Required VS Code extensions**: CSharpier, markdownlint, CSpell. Use the workspace settings
  without overrides.

## Coding standards and conventions

Code snippets below are illustrative examples only, replace namespaces and types to match your
project.

### C# language features

1. **File-scoped namespaces**:

   ```csharp
   namespace Example.Project.Library;
   ```

2. **Nullable reference types**: enabled (`<Nullable>enable</Nullable>`), use nullable annotations
   appropriately, use `required` for mandatory properties.
3. **Modern C# features**: prefer modern language constructs, primary constructors when
   appropriate, top-level statements for console apps, pattern matching over traditional checks,
   collection expressions when types loosely match, extension methods (the classic
   `this`-parameter form or an `extension(<receiver>) { ... }` block on C# 14+), implicit object
   creation when the type is apparent, range and index operators.
4. **Expression-bodied members**: use for applicable methods, properties, accessors, operators,
   lambdas, local functions.
5. **`var` keyword**: do NOT use `var`, always use explicit types:

   ```csharp
   // Correct
   int count = 42;
   string name = "test";

   // Incorrect
   var count = 42;
   var name = "test";
   ```

### Naming conventions

1. **Private fields**: underscore prefix with camelCase:

   ```csharp
   private readonly HttpClient _httpClient;
   private int _counter;
   ```

2. **Static fields**: `s_` prefix with camelCase:

   ```csharp
   private static int s_instanceCount;
   ```

3. **Constants**: PascalCase:

   ```csharp
   private const int MaxRetries = 3;
   ```

### Code structure

1. **Global usings**: use `GlobalUsings.cs` for common namespaces:

   ```csharp
   global using System;
   global using System.Net.Http;
   global using System.Threading.Tasks;
   global using Microsoft.Extensions.Logging;
   ```

2. **Usings placement**: outside the namespace, sorted with `System` directives first:

   ```csharp
   using System.CommandLine;
   using System.Runtime.CompilerServices;
   using Example.Project.Library;

   namespace Example.Project.Console;
   ```

3. **Braces**: Allman style:

   ```csharp
   public void Method()
   {
       if (condition)
       {
           // code
       }
   }
   ```

4. **Indentation**: C# files 4 spaces, XML/csproj files 2 spaces, YAML files 2 spaces, JSON files
   4 spaces.
5. **Line endings**: not specified here, governed per repo by `.editorconfig` / `.gitattributes`
   per GOVERNANCE.md's "Line Endings" section.
6. **`#region`**: do not use regions, prefer logical file/folder/namespace organization.
7. **Member ordering (StyleCop SA1201)**: const -> static readonly -> static fields -> instance
   readonly fields -> instance fields -> constructors -> public (events -> properties -> indexers
   -> methods -> operators) -> non-public in same order -> nested types.

### Comments and documentation

XML documentation is on: `<GenerateDocumentationFile>true</GenerateDocumentationFile>`, and
missing XML comments for public APIs are suppressed in `.editorconfig`. Every public surface must
still be documented: a single-line summary, additional details in remarks, documented input
parameters, return values, exceptions, and crefs.

```csharp
/// <summary>
/// Example of a single line summary.
/// </summary>
/// <remarks>
/// Additional important details about usage.
/// Multiple lines if needed.
/// </remarks>
/// <param name="category">
/// The quote category to request
/// </param>
/// <param name="cancellationToken">
/// A <see cref="System.Threading.CancellationToken"/> that can be used to cancel the request.
/// </param>
/// <returns>
/// A <see cref="string"/> containing the quote text.
/// </returns>
/// <exception cref="System.ArgumentException">
/// Thrown when <paramref name="category"/> is not a supported value.
/// </exception>
public async Task<string> GetQuoteOfTheDayAsync(string category, CancellationToken cancellationToken) {}
```

## Analyzer suppressions (.NET)

CODESTYLE.md's General section sets the suppression-scope order fleet-wide: narrowest scope first,
symbol-scoped before project-scoped before repo-wide, and only for a genuine false-positive or a
deliberate, documented exception, never a blanket relaxation to get a brownfield port to build.
The .NET mechanics, narrowest first:

- **Never use `#pragma warning disable`** to silence an analyzer.
- **Symbol-scoped**: a `[System.Diagnostics.CodeAnalysis.SuppressMessage(...)]` attribute with a
  `Justification`, on the specific member or type:

  ```csharp
  [System.Diagnostics.CodeAnalysis.SuppressMessage(
      "Design",
      "CA1034:Nested types should not be visible",
      Justification = "https://github.com/dotnet/sdk/issues/51681"
  )]
  ```

- **Project-scoped** (e.g. a test project): a `dotnet_diagnostic.<RULE>.severity` entry in that
  project's own `.editorconfig`, with a comment explaining why.
- **Repo-wide**: a `dotnet_diagnostic.<RULE>.severity` entry in the root `.editorconfig`, only
  when the rule is genuinely not applicable to any project. Relaxing a batch of `CA*` rules (or
  `dotnet_analyzer_diagnostic.severity`) to push a brownfield port through the build is exactly
  what this forbids.

## Error handling and logging

1. **Structured logging**: use structured message templates. Serilog is the application's concrete
   backend, and a library never references it directly (see item 2):

   ```csharp
   logger.LogError(exception, "{Function}", function);
   ```

2. **Libraries log through abstractions, never a concrete backend.** A NuGet library depends only
   on `Microsoft.Extensions.Logging.Abstractions` and exposes an `ILoggerFactory` seam: a settable
   global factory defaulting to `NullLoggerFactory.Instance` (fallback `NullLogger.Instance`) with
   `SetFactory`/`TrySetFactory`, and/or an `ILoggerFactory`/`ILogger` parameter in its API. It
   must not reference Serilog or any sink, which would force a logging framework on every consumer
   and drag in AOT-incompatible dependencies. The consuming application owns the concrete logger
   (Serilog is fine there), bridges it to `ILoggerFactory` (e.g. `SerilogLoggerFactory` from
   `Serilog.Extensions.Logging`), and injects it. Reference pattern: a `LogOptions` seam in the
   library, against which the consuming CLI builds the Serilog-backed factory and injects it via
   `LogOptions.SetFactory`.
3. **CallerMemberName**: use for automatic function name tracking:

   ```csharp
   public bool LogAndPropagate(
       Exception exception,
       [CallerMemberName] string function = "unknown"
   )
   ```

4. **Logger extensions**: use `Extensions.cs` for logger and other extension methods:

   ```csharp
   extension(ILogger logger)
   {
       public bool LogAndPropagate(Exception exception, ...) { }
   }
   ```

5. **Exceptions**: do not swallow exceptions, either log and rethrow or translate to a
   domain-specific exception.

## Code patterns

1. **Guard clauses**: prefer early returns for validation and error handling.
2. **Async all the way**: avoid blocking calls (`.Result`, `.Wait()`), use `async`/`await`.
3. **Cancellation tokens**: accept `CancellationToken` as the last parameter and pass it through.
4. **ConfigureAwait**: in library code, use `ConfigureAwait(false)` unless context is required. Do
   not call `ConfigureAwait(false)` in xUnit tests (see xUnit1030).
5. **Disposables**: use `await using` for async disposables, prefer `using` declarations.
6. **LINQ vs loops**: use LINQ for clarity, loops for hot paths or allocations.
7. **HTTP**: reuse `HttpClient` via factory, never per-request instantiation.
8. **Collections**: prefer `IReadOnlyList<T>`/`IReadOnlyCollection<T>` for public APIs.
9. **Immutability**: prefer immutable records, use init-only setters when records are not
   suitable, and prefer immutable or frozen collections for read-only data.
10. **Exceptions as control flow**: avoid using exceptions for expected flow.
11. **Sealing classes**: seal classes that are not designed for inheritance.
12. **Lazy initialization**: use `Lazy<T>` for static, thread-safe instantiation (e.g. a logger
    factory, an HTTP factory).

## Testing conventions

1. **Framework**: xUnit v3 or later (the `xunit.v3` package, never the legacy v2 `xunit` package)
   with AwesomeAssertions for every assertion. Native xUnit asserts (`Assert.Equal`,
   `Assert.True`, ...) are not allowed, use the fluent `.Should()` API. Dynamic test skipping
   (`Assert.Skip`, `Assert.SkipWhen`) is control flow, not an assertion, and stays native:

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

2. **Organization**: Arrange-Act-Assert pattern.
3. **Naming**: descriptive names with underscores.
4. **Theory tests**: use `[Theory]` with `[InlineData]`.

## Project configuration

1. **Target framework**: .NET 10.0 (`<TargetFramework>net10.0</TargetFramework>`).
2. **AOT compatibility**: `<IsAotCompatible>true</IsAotCompatible>`,
   `<VerifyReferenceAotCompatibility>true</VerifyReferenceAotCompatibility>`.
3. **Assembly information**: use semantic versioning, include SourceLink
   (`<PublishRepositoryUrl>true</PublishRepositoryUrl>`), embed untracked sources
   (`<EmbedUntrackedSources>true</EmbedUntrackedSources>`).
4. **Internal visibility**: use `InternalsVisibleTo` for test and benchmark access (adapt the
   project names to your repo's test/benchmark projects):

   ```xml
   <ItemGroup>
     <InternalsVisibleTo Include="YourBenchmarkProject" />
     <InternalsVisibleTo Include="YourTestProject" />
   </ItemGroup>
   ```

## Best practices

All changes go through pull requests.
