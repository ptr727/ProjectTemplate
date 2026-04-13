# Instructions for AI Coding Agents

**ProjectTemplate** is a C# .NET template project demonstrating best practices. Developers use this as a baseline to create their own projects.

For comprehensive coding standards and detailed conventions, refer to [`.github/copilot-instructions.md`](./.github/copilot-instructions.md) and [`CODESTYLE.md`](./CODESTYLE.md).

## Git and Commit Rules

**These rules are absolute — no exceptions:**

- **Never make git commits.** Claude Code cannot produce cryptographically signed commits. All commits must be signed (SSH/GPG) and must be made by the developer. Stage changes with `git add` and leave the commit to the developer.
- **Never force push.** Do not run `git push --force` or `git push --force-with-lease` under any circumstances. Force pushing rewrites shared history and can cause data loss.
- **Never run destructive git commands** (`git reset --hard`, `git checkout .`, `git restore .`, `git clean -f`) without explicit developer instruction.
- **Staging is the limit.** Prepare and stage file changes; the developer runs `git commit` in their own environment where signing keys are available.

## Key Requirements for All Projects Derived from This Template

### Build & Quality Standards

- **Zero Warnings Policy**: All builds must complete without errors or warnings
  - Use `CSharpier Format`, `.Net Format`, and `Husky.Net Run` tasks

- **Code Analysis**: Enable all .NET analyzers
  - `<EnableNETAnalyzers>true</EnableNETAnalyzers>`
  - `<AnalysisLevel>latest-all</AnalysisLevel>`

### Development Environment

- Target latest .NET SDK (currently .NET 10 with C# 14)
- Support Visual Studio Code (`.code-workspace`) and Visual Studio Community (`.slnx`)
- Support Linux, Windows, and macOS with correct line endings and permissions
- Use `.editorconfig` for style enforcement

### Project Structure

- **Library**: Core reusable library
- **Console**: CLI application using System.CommandLine
- **Tests**: xUnit with AwesomeAssertions (Arrange-Act-Assert pattern)
- **Benchmarks**: BenchmarkDotNet performance measurements
- **Docker**: Multi-platform Linux containers

### Testing

- Use xUnit v3 and AwesomeAssertions
- Organize tests logically in separate files
- Follow Arrange-Act-Assert pattern
- Test naming: `MethodName_Scenario_ExpectedBehavior()`

## Authoritative References

For detailed specifications, see:

- [`.github/copilot-instructions.md`](./.github/copilot-instructions.md) - Complete coding conventions and style guide
- [`CODESTYLE.md`](./CODESTYLE.md) - Code style and formatting rules
- [`.editorconfig`](./.editorconfig) - Automated style enforcement
- Project task definitions - `CSharpier Format`, `.Net Build`, `.Net Format`, `.Net Outdated Upgrade`, `Husky.Net Run`

## Quick Start for Derived Projects

1. **Clone this template** as baseline for your project
2. **Review** [`.github/copilot-instructions.md`](./.github/copilot-instructions.md) thoroughly
3. **Update** project-specific values:
   - `PackageId`, `RootNamespace` in `.csproj` files
   - Namespace conventions with your organization name
   - `README.md`, `HISTORY.md`, `version.json`, `LICENSE`
4. **Run tools** before first commit:
   - `dotnet tool restore`
   - `.Net Format` task
   - `CSharpier Format` task
5. **Enable Husky.Net** hooks: `dotnet husky install`
