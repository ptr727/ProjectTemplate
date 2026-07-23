# Audit: PhotoCleaner

- **Audited branch:** main (`3b33b98956190b91b8ed1d3e49e8242545ea523c`)
- **Types:** csharp, console, docker (from registry)
- **Verdict:** not operational
- **Date:** 2026-07-23
- **Run stamp:** `audit run 2026-07-23T13:51:20Z | hub 614a291`

## Develop Drift

`develop` vs `main`: ahead 1, behind 3 (`gh api repos/ptr727/PhotoCleaner/compare/main...develop`). **Diverged, and the behind direction is the anomaly** - under the forward-only model `develop` never trails `main`, so the three `main`-only commits need a forward sync to `develop` - re-apply or cherry-pick them onto `develop`, never a `main -> develop` back-merge.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| csharp | fail | fail | drift | `.editorconfig:60` carries the repo-wide `dotnet_analyzer_diagnostic.severity = suggestion` CODESTYLE.md forbids (the hub canonical dropped it in ProjectTemplate#400). `PhotoCleaner/PhotoCleaner.csproj:3-4` sets `AnalysisLevel=latest-all` + `EnableNETAnalyzers=true` but **no `TreatWarningsAsErrors` anywhere**, so analyzers are demoted to suggestions and warnings never fail the build. The ProjectTemplate#353 probe measured what this hides: 362 `xUnit1051` + 2 `xUnit1030`, plus pre-existing `NU1903` (SQLitePCLRaw 2.1.10 high-severity advisory) and 4 `CS8625` that warn without failing anything |
| console | fail | pass | drift | a real System.CommandLine console app (`PhotoCleaner/PhotoCleaner.csproj:9 OutputType=Exe`, `PhotoCleaner/PhotoCleaner.csproj:34 System.CommandLine 2.0.5`, net10.0) but no `build-executable-task.yml` and no release pipeline, so the smoke-subset and `release-asset-<branch>-<target>` checks have nothing to bind to |
| docker | fail | fail | drift | `Docker/Dockerfile` present but no docker build/push workflow and no `Docker/README.md` - the Dockerfile is dead weight until a pipeline exists |
| branch-model | fail | fail | defect | **no rulesets at all** - `develop` and `main` both unprotected (audit DEFECT x2). No `repo-config/` payloads to import. Branches diverged (see Develop Drift) |
| repo-setup | pass | fail | drift | secrets fully provisioned in both stores (`CODECOV_TOKEN`, `CODEGEN_APP_CLIENT_ID`, `CODEGEN_APP_PRIVATE_KEY` in actions + dependabot) but nothing consumes the App creds - `.github/dependabot.yml` is absent (no dual-target sync keystone) and there is no merge-bot workflow |
| linter-parity | pass | pass | pass | `validate-task.yml:66-88` runs csharpier check, `dotnet format style --verify-no-changes`, markdownlint (SHA-pinned action), cspell (SHA-pinned), actionlint (SHA-pinned), and editorconfig-checker via docker `:latest`. The unit-test job collects coverage and uploads to codecov v7 (`validate-task.yml:36-40`). Matches the fleet lint architecture |
| recurring-violations | pass | pass | pass | `.gitattributes:3` fleet-standard `* -text` with LF pins for `*.sh` and `.husky/pre-commit`. README em-dash grep clean |
| readme-structure | fail | pass | drift | app-focused and thorough (`## Overview:5`, `## Usage:46`, flow sections, `## Docker:415`, `## Development Tooling:474`, `## License:653`) but not the fleet section set - no Build and Distribution block (nothing to badge pre-release), no Table of Contents, no Questions or Issues, Development-Environment-Setup slot filled by `Development Tooling` |
| workflow (WORKFLOW.md 5A/5B) | fail | pass | drift | no publisher, so the 5A publish guarantees are N/A. The PR gate itself is sound - `validate` runs unconditionally and the aggregator carries the canonical ruleset-bound name `Check pull request workflow status job` (`test-pull-request.yml:34-40`, interface contract passes, no paths-filter/smoke by design per the header `test-pull-request.yml:5`) - but with no ruleset the required check binds to nothing, so the gate is advisory until branch-model lands |

nuget, pypi, python: N/A (no packaging, no Python).

## Defects (most severe first)

1. **Both branch rulesets missing** - `develop` and `main` are unprotected: no signed-commit requirement, no PR gate, no required status check, force-push and deletion possible. Import `repo-config/develop.json` + `main.json` via `configure.sh apply` once `repo-config/` is carried.

## Drift Findings

- `AGENTS.md` is an old skeleton: 6 of the carried intent sections missing (Branching Model, Release Model, Pull Request Title and Commit Message Conventions, Documentation Style Conventions, PR Review Etiquette, Workflow YAML Conventions) and all 3 verbatim universal sections absent (Repository Boundaries and Write Safety, Git and Commit Rules, Verification Discipline) - re-vendor from the hub.
- `.markdownlint-cli2.jsonc` hand-modified (matches no hub revision) - re-vendor the canonical.
- Absent baseline files (audit LETTERs): `WORKFLOW.md`, `version.json`, `repo-config/` (all six), `AUDIT.md`, `spec/secrets.json`, `.github/dependabot.yml`, `Docker/README.md`.
- `.editorconfig:60` analyzer relaxation + no `TreatWarningsAsErrors` (see csharp dimension) - the ProjectTemplate#353 downstream item, sequenced as its own PR (362 sites).
- Committed `CLAUDE.md` and `PhotoCleaner.code-workspace` at the repo root - repo-local extras. `AGENTS.md` is the fleet's agent-agnostic doc, so a committed `CLAUDE.md` duplicates that role and can drift from it.

## Proposed Registry / Spec Updates

- Refresh the stale `driftNotes`: the repo is no longer pre-CI (PR gate + linters live). The current gaps are rulesets, `repo-config/`, `version.json`, governance docs, `dependabot.yml`, and the release pipeline. Applied in the same change as this report.
- `releaseTrigger: none` remains accurate until a publisher exists.
