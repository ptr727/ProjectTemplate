# Instructions for AI Coding Agents

**ProjectTemplate** is a C# .NET template project demonstrating best practices. Developers use this as a baseline to create their own projects.

For comprehensive coding standards and detailed conventions, refer to [`.github/copilot-instructions.md`](./.github/copilot-instructions.md) and [`CODESTYLE.md`](./CODESTYLE.md).

## Git and Commit Rules

**These rules are absolute — no exceptions:**

- **Never make git commits.** AI coding agents cannot produce cryptographically signed commits. All commits must be signed (SSH/GPG) and must be made by the developer. Stage changes with `git add` and leave the commit to the developer.
- **Never force push.** Do not run `git push --force` or `git push --force-with-lease` under any circumstances. Force pushing rewrites shared history and can cause data loss.
- **Never run destructive git commands** (`git reset --hard`, `git checkout .`, `git restore .`, `git clean -f`) without explicit developer instruction.
- **Staging is the limit.** Prepare and stage file changes; the developer runs `git commit` in their own environment where signing keys are available.

## Pull Request Title and Commit Message Conventions

### Format

- Imperative subject summarizing the change, ≤72 characters, no trailing period. ("Add 24-hour PM2.5 average sensor", not "Added X" or "Adds X".)
- Optional body, blank-line separated, explaining *why* the change is being made when that's non-obvious. The diff shows *what*.

### Rules

- Don't write `update stuff`, `wip`, or other vague titles. (Dependabot's default `Bump X from Y to Z` titles are fine — keep them.)
- Don't add `Co-Authored-By:` lines unless the developer explicitly asks.
- Don't put release-bump magnitude in the title — no "minor", "patch", "release v0.2.0", etc. Nerdbank.GitVersioning computes the next release version from `version.json` + git history. Dependency versions in dependency-bump titles are fine and expected.
- Use US English spelling and match the existing heading style of the file you're editing: title case with lowercase short bind words (a, an, the, and, but, or, of, in, on, at, to, by, for, from); hyphenated compounds capitalize both parts unless the second is a short preposition (*Built-in*, *EPA-Corrected*, *24-Hour*).

### Examples

```text
Add structured logging extensions to library
Pin softprops/action-gh-release to commit SHA
Drop net8.0 multi-targeting from console project
Bump xunit.v3 from 3.2.2 to 3.3.0
Clarify devcontainer setup steps in README
```

## Documentation Style Conventions

### Markdown

- Use reference-style links for any URL referenced more than once or appearing in lists; alphabetize the reference definitions block.
- Inline single-use relative links (e.g. `[CODESTYLE.md](./CODESTYLE.md)`) are fine.
- One logical paragraph per line; no hard-wrap line-length limit.
- Headings follow the title-case-with-short-bind-words rule from the PR-title section.

### Quantitative Claims

- Any quantitative claim in `README.md` (counts, sizes, version floors, supported platforms) must be verified against current code. If a doc number is derived from a code constant, mark the dependency in a source-code comment so the next editor knows to update both.

## Workflow YAML Conventions

These conventions describe the target state. New and modified workflows must respect them; existing workflows are migrated opportunistically when they're being touched for other reasons. Don't open a PR purely to apply these rules across the repo — the churn isn't worth it.

- **Action pinning**: pin third-party actions to a commit SHA with a trailing `# vX.Y.Z` comment so Renovate / Dependabot can still bump it but a tag swap can't change the executed code. First-party `actions/*` are encouraged but not required to follow the same convention.
- **Naming**: every step's `name:` ends in `step`; every job's `name:` ends in `job`. Reusable workflow filenames end in `-task.yml`.
- **Concurrency**: top-level workflows declare `concurrency: { group: '${{ github.workflow }}-${{ github.ref }}', cancel-in-progress: true }` so a fresh push supersedes an in-flight run on the same ref.
- **Shells**: multi-line `run:` blocks with bash start with `set -euo pipefail` — fail fast, fail on undefined vars, fail on a failed pipe segment.
- **Conditionals**: multi-line `if:` uses folded scalar `if: >-` so YAML preserves whitespace correctly. Literal block (`if: |`) is wrong because it embeds newlines inside the boolean expression.
- **Boolean inputs**: workflows triggered both via `workflow_call` and `workflow_dispatch` must declare each boolean input in *both* trigger blocks — one definition does not propagate to the other.
- **Reusable workflows**: job-level `permissions:` are validated *before* the `if:` evaluates, so even a skipped job needs valid permissions declared.
- **Tag pinning on releases**: when using `softprops/action-gh-release` (or any tag-creating action), pass `target_commitish: ${{ github.sha }}` explicitly. Without it, GitHub's REST API defaults the new tag to the repository's default branch instead of the commit that built the artifact.

## Branching Model

- `develop` is the integration branch. Feature branches → `develop` is **squash-only**; the develop branch is kept linear.
- `develop` → `main` is **merge-commit only** (no squash, no rebase). Merge commits preserve develop's commit list as a real second-parent reference on main; this is what allows the "release on every push" model to attribute releases to the develop commits that produced them. Branch protection enforces this: the develop ruleset allows only `squash`, the main ruleset allows only `merge`.
- All commits on both branches must be cryptographically signed (SSH or GPG). Squash and merge commits created via the GitHub UI are signed by GitHub's web-flow key.

## Key Requirements for All Projects Derived from This Template

### Build & Quality Standards

- **Zero Warnings Policy**: All builds must complete without errors or warnings
  - Use `CSharpier Format`, `.Net Format`, and `Husky.Net Run` tasks

- **Code Analysis**: Enable all .NET analyzers
  - `<EnableNETAnalyzers>true</EnableNETAnalyzers>`
  - `<AnalysisLevel>latest-all</AnalysisLevel>`

### Project Configuration

- Common MSBuild properties (`TargetFramework`, `Nullable`, `ImplicitUsings`, `AnalysisLevel`, etc.)
  live in `Directory.Build.props` at the solution root. Do not duplicate these in individual `.csproj`
  files — only add a property to a `.csproj` when it is project-specific or overrides the shared default.
- All NuGet package versions are centralised in `Directory.Packages.props`. `PackageReference` elements
  in `.csproj` files must not include a `Version` attribute. Asset metadata (`PrivateAssets`,
  `IncludeAssets`) stays in the `.csproj` `PackageReference` element.

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
