# Instructions for AI Coding Agents

**ProjectTemplate** is a C# .NET template project demonstrating best practices. Developers use this as a baseline to create their own projects.

For comprehensive coding standards and detailed conventions, refer to [`.github/copilot-instructions.md`](./.github/copilot-instructions.md) and [`CODESTYLE.md`](./CODESTYLE.md).

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
- Project task definitions - `CSharpier Format`, `.Net Build`, `.Net Format`, `Husky.Net Run`

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
