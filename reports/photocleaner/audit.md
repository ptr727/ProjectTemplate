# Audit: PhotoCleaner

- **Audited branch:** main (`15b9b5b`)
- **Types:** csharp, console, docker (from registry)
- **Verdict:** not operational
- **Date:** 2026-08-01
- **Run stamp:** `audit run 2026-08-01T15:30:19Z | hub 6501479`

Supersedes the 2026-07-23 snapshot, which predated the release pipeline. Everything that snapshot listed as a defect has landed: both rulesets are live, `repo-config/` is carried, and the publisher cut release `1.0.9` on 2026-07-23 with the multi-arch image and the executable 7z attached. What remains is a different set, created mostly by the hub advancing rather than by the repo regressing.

## Develop Drift

`develop` vs `main`: the audit reads both and reports the same findings on each, so `develop` carries no conformance content `main` lacks and vice versa. The commit-count gap is the promotion-merge ancestry artifact recorded before and is **benign**. No action.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| csharp | pass | pass | pass | `Directory.Build.props` carries the analyzer set and `TreatWarningsAsErrors`, `Directory.Packages.props` centralizes versions, and the repo-wide analyzer relaxation the previous snapshot flagged at `.editorconfig:60` is gone. The shared `[*.cs]` block is present |
| console | pass | pass | pass | System.CommandLine console (`PhotoCleaner/PhotoCleaner.csproj`, `OutputType=Exe`, net10.0). `build-executable-task.yml` aggregates per-runtime output to `release-asset-<branch>-*`, and the smoke matrix is a strict subset |
| docker | pass | pass | pass | `Docker/Dockerfile` multi-arch, `Docker/README.md` present, `build-docker-task.yml` uses a registry layer cache (`buildcache-<branch>`), and the image re-pushes on publish |
| branch-model | pass | pass | pass | Both rulesets live and matching `repo-config/develop.json` and `repo-config/main.json` by normalized diff |
| repo-setup | pass | pass | pass | Every name in `spec/secrets.json` present in the store its mechanism claims, and no forbidden name |
| linter-parity | pass | pass | pass | `validate-task.yml:66-88` runs csharpier check, `dotnet format style --verify-no-changes`, markdownlint, cspell, actionlint, and editorconfig-checker. One config per linter |
| recurring-violations | pass | pass | pass | ASCII clean across the carried docs, and `.gitattributes` is fleet-standard with the LF pins |
| readme-structure | fail | fail | defect | Intro line 150 characters against the 100-character cap (`README.md:3`), and the Docker Hub short description does not mirror it. No Build and Distribution block, no Table of Contents, no Questions or Issues, and the Development-Environment-Setup slot is filled by `Development Tooling` (`README.md:482`) |
| workflow (WORKFLOW.md 5A/5B) | fail | pass | drift | The publisher, build tasks, and PR gate satisfy the D-guarantees, **except** that `build-release-task.yml` carries no `validate-release` job, so D2.2 (branch matches version classification) is unimplemented and scenarios S1, S4, and S10 bind to a job name that does not exist. The `github-release` job body matches no hub revision |
| agent-instruction set | fail | fail | defect | `GOVERNANCE.md` absent, and `AGENTS.md` is the pre-split single file, so neither verbatim region can be compared and all ten of its sections read as undeclared. `.github/copilot-instructions.md` lacks `Reviewing Carried Fleet Content` |

nuget, pypi, python: N/A (no packaging, no Python).

## Defects (most severe first)

1. **The agent instruction set predates the router split.** `GOVERNANCE.md` is absent (the only LETTER-class file finding) and `AGENTS.md` still carries the ten topical rule sections inline, so no verbatim region can be compared and every section reads as undeclared. A downstream agent reading this repo gets rule text that no longer tracks the canonical.
2. **README intro over the cap, and the description mirrors diverged.** The intro is 150 characters against the 100-character Docker Hub cap, and the Docker Hub short description (`Pre-process media files for import into photo management systems.`) no longer matches it, so the repo carries two different canonical sentences.

## Drift Findings

- `.markdownlint-cli2.jsonc` matches a past hub revision, so re-vendor it (the base gained `MD033 allowed_elements` for `details` and `summary`).
- `repo-config/configure.sh` matches a past hub revision, so re-vendor it (the base gained the `per_page` cap guard in `ruleset_id` and explicit failure guards on four reads).
- `build-release-task.yml` is missing the `validate-release` job, and its `github-release` body matches no hub revision.
- `version.json` carries a `nugetPackageVersion` block for a repo that publishes no package (STANDUP.md section 2, "carry only the fields the repo uses"). Not a mechanical finding, since `version.json` is checked at `intent`.

## Convergence in Flight

Six pull requests opened 2026-08-01 against `develop`, one per drift class per AUDIT.md section 10, each driven to a Copilot review on its head SHA and left for the maintainer to merge:

- [#26](https://github.com/ptr727/PhotoCleaner/pull/26) the workspace extension set (`gruntfuggly.todo-tree` to `fanaticpythoner.better-todo-tree`)
- [#27](https://github.com/ptr727/PhotoCleaner/pull/27) re-vendor the two stale verbatim carries
- [#28](https://github.com/ptr727/PhotoCleaner/pull/28) the `AGENTS.md` and `GOVERNANCE.md` split, plus `Reviewing Carried Fleet Content`
- [#29](https://github.com/ptr727/PhotoCleaner/pull/29) the `validate-release` entry gate and the re-vendored `github-release`
- [#30](https://github.com/ptr727/PhotoCleaner/pull/30) the README and HISTORY structure
- [#31](https://github.com/ptr727/PhotoCleaner/pull/31) drop the unused `nugetPackageVersion` block

## Proposed Registry / Spec Updates

- Refresh the `driftNotes`: the second note asserted this report predated the docker and release wiring, which it no longer does. Applied in the same change.

## Escalations

Five spec questions this audit surfaced, raised rather than resolved, per AUDIT.md section 9.

1. **The `HISTORY.md` mirror rule has no carried home.** `spec/readme-structure.md` owns it and the audit enforces it, but that file is hub-only, so a downstream repo cannot point at the rule it is measured against. PhotoCleaner kept the rule as repo-local prose in `CODESTYLE.md` for want of a destination. Either promote it into a carried section, or accept that mechanical-only enforcement is the intent.
2. **The hub's own `.github/copilot-instructions.md` still describes the pre-split file.** `Reviewing Carried Fleet Content` says "Most of `AGENTS.md` is universal fleet law: every section that states a rule, as opposed to the two that describe this repository's own directory tree and devcontainer". After the split those sections live in `GOVERNANCE.md`, and `AGENTS.md` carries exactly two verbatim sections and no repo-specific ones. Every repo carrying this section downstream inherits the stale description.
3. **`CODESTYLE.md` contradicts `.markdownlint-cli2.jsonc` on MD033.** The Markdown-linting item says "HTML elements are flagged", but the canonical config now sets `MD033: { allowed_elements: ["details", "summary"] }`. The prose was not swept when the config changed.
4. **`spec/readme-structure.md` assumes a public repository.** PhotoCleaner is private, so shields.io cannot read its GitHub release, build, or commit data and every GitHub-sourced badge renders broken. The Build Status and Releases sub-sections state no behavior for a private repo, and the pre-existing License shield was already broken for this reason.
5. **`WORKFLOW.md` D2.2 "skipped on smoke" is ambiguous, and a reviewer misread it.** The phrase names the validation, and scenario S1 confirms it (`validate-release **skipped (smoke), succeeds**`), but a Copilot review read it as the GitHub job status and proposed a job-level `if: !inputs.smoke` that would have coupled `github-release` to smoke through its `needs`. Worth disambiguating in the D-guarantee text.

A sixth question is about the repo rather than the spec, so it goes to the maintainer: the registry lists PhotoCleaner's `publish` channels as `docker` and `github-release` with `consumerModel: pull`, but the repository is **private**, so its GitHub Releases are not pullable by anyone but the maintainer while its Docker image is public. Either the visibility or the declared channel is wrong.
