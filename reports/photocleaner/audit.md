# Audit: PhotoCleaner

- **Audited branch:** main (`f56178a`)
- **Types:** csharp, console, docker (from registry)
- **Verdict:** operational
- **Date:** 2026-08-15
- **Run stamp:** `audit run 2026-08-15T18:43:20Z | hub b09078e`

Supersedes the earlier 2026-08-15 snapshot, which measured `main` before the promotion. The resyncs against the hub (ptr727/PhotoCleaner#49, ptr727/PhotoCleaner#50, ptr727/PhotoCleaner#52) are promoted to `main` by ptr727/PhotoCleaner#51, and the deterministic audit reports **clean** at `main` and at `develop` with the hub at `b09078e`. The two hub-only workflow findings the earlier snapshot carried are gone as well, since the hub now declares the publisher and validator workflows at intent fidelity (#736).

## Develop Drift

`develop` vs `main`: identical content, since ptr727/PhotoCleaner#51 merged `develop` at `ab23034` and nothing has landed since. The audit at `develop` reports zero findings.

The promotion was blocked for one round by the prose gate's `dead-path` rule flagging verbatim mentions of the retired `repo-config/configure.sh`, filed as #721 and fixed by #731. The gate fetches its rules from hub `develop` on a `develop`-targeted run, so a re-run cleared it with no repo change. Copilot read 18 of 19 changed files on the promotion in both rounds and raised no threads. Its one suppressed finding, a Mermaid edge in the hub-carried `WORKFLOW.md` diagram, was answered on the pull request as declined, since the diagram is hub content and the D1 text beside it already states the rule.

## Dimensions

Judged at `main`. Type-dimension checks are hand-judged per AUDIT.md section 4 and are unchanged from the 2026-08-01 snapshot, since ptr727/PhotoCleaner#49, ptr727/PhotoCleaner#50, and ptr727/PhotoCleaner#52 touched governance and configuration only.

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| csharp | pass | pass | pass | `Directory.Build.props` carries the analyzer set and `TreatWarningsAsErrors`, `Directory.Packages.props` centralizes versions, `.editorconfig` carries the shared `[*.cs]` block |
| console | pass | pass | pass | System.CommandLine console (`PhotoCleaner/PhotoCleaner.csproj`, `OutputType=Exe`, net10.0), `build-executable-task.yml` aggregates per-runtime output to `release-asset-<branch>-*`, gated `!smoke` |
| docker | pass | pass | pass | `Docker/Dockerfile` multi-arch, `Docker/README.md` present, `build-docker-task.yml` uses a registry layer cache (`buildcache-<branch>`), the image re-pushes on publish |
| branch-model | pass | pass | pass | `repo-config/configure.sh check ptr727/PhotoCleaner release` at hub `0e84805`: both rulesets and every general setting match, nothing to apply |
| repo-setup | pass | pass | pass | Every name in `spec/secrets.json` present in both the Actions and Dependabot stores (`CODECOV_TOKEN` included), no forbidden name |
| linter-parity | pass | pass | pass | `validate-task.yml` runs csharpier check, `dotnet format style --verify-no-changes`, markdownlint, cspell, actionlint, editorconfig-checker, and the hub prose gate. One config per linter |
| recurring-violations | pass | pass | pass | `prose_lint.py --diff origin/develop` clean on ptr727/PhotoCleaner#50, `repo_gate.py` clean on `develop` (the `eol-coverage` forward-declaration gap closed in ptr727/PhotoCleaner#50) |
| readme-structure | pass | pass | pass | Required sections present in order, reference names and link groups per `spec/readme-structure.md`, all mechanical README checks clean at `main` |
| workflow (WORKFLOW.md 5A/5B) | pass | pass | pass | `build-release-task.yml` carries `validate-release` and the re-vendored `github-release` job (interface contract satisfied by name), and `publish-release.yml` and `validate-task.yml` are declared intent carries as of hub #736 |
| agent-instruction-set | pass | pass | pass | Every verbatim `AGENTS.md`/`GOVERNANCE.md` section byte-matches the hub canonical, and every intent carry (`CODESTYLE.md`, `WORKFLOW.md`, `AUDIT.md`, `.github/copilot-instructions.md`, the linter configs) is reconciled against the hub's history in ptr727/PhotoCleaner#50 and ptr727/PhotoCleaner#52 with the repo's own adaptations kept |

nuget, pypi, python: N/A (no packaging, no Python).

## Defects (most severe first)

None.

## Drift Findings

None. The deterministic audit is clean at `main` and `develop`, and no hand-judged dimension reports a letter miss.

## Convergence in Flight

None. ptr727/PhotoCleaner#50, ptr727/PhotoCleaner#52 (to `develop`) and ptr727/PhotoCleaner#51 (`develop -> main`) are merged.

## Proposed Registry / Spec Updates

- The `driftNotes` are unchanged. The first describes the publish shape (multi-arch Docker plus a github-release 7z, two-phase release), which still holds. The second records the private-for-now decision, which still holds (`isPrivate: true` at the time of this run) and still means the GitHub-sourced shields render broken. Neither asserts outstanding work.

## Escalations

Two hub findings surfaced by the resync this report measures, filed rather than patched per repo, both closed before this run, so neither is a finding of the current audit:

1. #721: `prose_lint.py dead-path` could not recognize a hub-hosted path in a repo that retired the file, so verbatim text naming `repo-config/configure.sh` failed a downstream promotion gate. Fixed by #731.
2. #722: the hub's `.github/copilot-instructions.md` linked `GOVERNANCE.md#every-finding-ends-in-an-action`, an anchor that left `GOVERNANCE.md` when PR Review Etiquette was packaged as a Skill. PhotoCleaner re-pointed its copy in ptr727/PhotoCleaner#50 after Copilot raised it, and ptr727/PhotoCleaner#52 carried the canonical wording that #730 landed.
