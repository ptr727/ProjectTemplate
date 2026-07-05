# Audit: Utilities

- **Audited branch:** main (`8de105fd101c6ce4b60447bc1d99b5e6ab08683e`)
- **Types:** csharp, nuget (from registry)
- **Verdict:** operational
- **Date:** 2026-07-04

## Develop Drift

`develop` vs `main`: ahead 0, behind 28 (`gh api repos/ptr727/Utilities/compare/main...develop` -> `status: behind, ahead_by: 0, behind_by: 28`). **Stale - a drift finding.** `develop` has none of `main`'s 28 commits, including the CI/CD rework that is the audited `main` state. Under the forward-only model (no `main -> develop` back-merge) `develop` must receive those changes directly; it has not, so `develop` does not reflect the released pipeline.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| csharp | pass | pass | pass | `.editorconfig:64` carries the shared `[*.cs]`+ReSharper block (verbatim vs template); analyzers enforced in `Directory.Build.props:6-9` (`AnalysisLevel=latest-all`, `AnalysisMode=All`, `EnableNETAnalyzers=true`, `TreatWarningsAsErrors=true`); suppressions narrow and documented per project (`Utilities/.editorconfig:9-27`, `UtilitiesTests/.editorconfig:12-32`), not a blanket relax |
| nuget | pass | pass | pass | OIDC login (`NuGet/login`) `build-release-task.yml:131-136`, ephemeral key at `:145`, no stored `NUGET_API_KEY`; `dotnet nuget push --skip-duplicate` `:143-146`, gated `if: inputs.publish && !inputs.smoke` `:140` (push-gated, not existence-gated); `.snupkg` symbols pushed `Utilities/Utilities.csproj:29-30` |
| pypi | - | - | N/A | no `pyproject.toml` / `gh-action-pypi-publish` |
| python | - | - | N/A | no Python project |
| console | - | - | N/A | shipped target is the packable library `Utilities/Utilities.csproj:10-11`; no `build-executable-task` / System.CommandLine app (`Sandbox` is a non-shipped scratch project) |
| docker | - | - | N/A | no `Dockerfile` / docker build task |
| branch-model | drift | pass | drift | both branches protected; live rulesets match template `repo-config/{develop,main}.json` **except** the required-status-check `context` = `Check pull request workflow status job` vs canonical `Check pull request workflow status` - matches Utilities' own aggregator `test-pull-request.yml:45`, so enforcement is intact |
| repo-setup | pass | pass | pass | actions + dependabot stores hold `CODEGEN_APP_CLIENT_ID`, `CODEGEN_APP_PRIVATE_KEY`, `NUGET_USERNAME`; no forbidden `NUGET_API_KEY` (nuget-oidc) or `CODEGEN_APP_ID` (codegen-app); nuget-oidc needs no stored secret |
| linter-parity | pass | pass | pass | one `.markdownlint-cli2.jsonc` + `cspell.json` drive editor + CLI + CI (`validate-task.yml:56-69`); editorconfig/csharpier shared (`.vscode/tasks.json`, `.config/dotnet-tools.json`); CI runs each linter |
| recurring-violations | pass | pass | pass | comments concise; docs ASCII-clean (only non-ASCII is a deliberate warning emoji `Utilities/FileEx.cs:1090` and Unicode test fixtures `UtilitiesTests/ExtensionsTests.cs:231`); `cspell.json:3` sets `"language": "en-US"`; real endings compliant (CRLF docs/workflows, LF `.husky/pre-commit`). See Drift re `.editorconfig` global default |
| readme-structure | drift | drift | drift | sections mostly present and ordered but omits `## Table of Contents` and `## Questions or Issues`, and uses `## Contributing` for the Development-Environment-Setup slot; minor: missing colon `README.md:9`, shields header `<!-- Shields links -->` vs canonical `<!-- Shields -->` `README.md:65` |
| workflow (WORKFLOW.md 5A/5B) | drift | pass | drift | all applicable D-guarantees hold by **outcome**; divergences are structural (see below): continuous per-branch push-publish instead of two-phase scheduled matrix, no `changes` paths-filter, plain `release-asset` name + exact-name download, keys off `github.ref_name` instead of `inputs.branch` |

## WORKFLOW.md 5A Static Audit (applicable D-guarantees)

- **D1.2 / D1.5 (validation + aggregator):** PASS - `validate` (unit-test + lint) runs unconditionally `test-pull-request.yml:25-28`; aggregator `needs: [validate, smoke-build]`, requires `success` `:44-60`; the ruleset-bound name is kept in lockstep `:45`.
- **D1.1 / D1.4 (paths-filter):** DRIFT - no `changes` / `dorny/paths-filter` job; `smoke-build` runs on every push including docs/workflow-only `test-pull-request.yml:32-40`. Deliberate over-build; the "target slips unbuilt" failure mode cannot occur with a single always-built target.
- **D1.3 (smoke uploads/pushes nothing):** PASS - every push/upload gated `if: inputs.publish && !inputs.smoke` `build-release-task.yml:132,140,150,163`.
- **D2.1 / D2.2 (validate-at-entry):** PASS - `validate-release` asserts branch<->classification both directions, strips `+buildmetadata`, skips on smoke `build-release-task.yml:65-94`; downstream `needs:` it.
- **D2.3 (dispatch guard):** DRIFT - the publish job `if:` silently skips a dispatch from a non-main/develop ref `publish-release.yml:47` (template fails fast with `::error::`; skip is safe but non-canonical).
- **D3.1 / D3.2 (classification):** PASS by model - NBGV runs once on the triggering ref `build-release-task.yml:33-61`; config keys off `github.ref_name` `:120,213`, correct here because there is no cross-branch matrix building the other leg from a main-ref run. `version.json:4-6` `publicReleaseRefSpec ^refs/heads/main$`.
- **D3.4 (registry version per classification):** PASS - build injects `-property:PackageVersion=SemVer2` `build-release-task.yml:127`; NuGet.org derives prerelease from the `-g<sha>` suffix.
- **D4.1 (two-phase):** DRIFT - no schedule, no both-branch matrix; publishes on every push to main/develop touching shipped paths `publish-release.yml:16-24`. This is WORKFLOW.md's opt-in continuous-release mode used as the default (no `PUBLISH_ON_MERGE` variable exists).
- **D4.2 / D4.3 / D4.4 (release):** PASS - `target_commitish=GitCommitId` `:212`, `prerelease = ref_name != 'main'` `:213`, create gated `exists == false || workflow_dispatch` `:207`, `--skip-duplicate` no-op re-push `:146`.
- **D5 (cleanup):** PASS - asset delete at the consumer under the same gate as create, `continue-on-error`, filtered by name (not a blanket `.artifacts[].id`) `build-release-task.yml:223-238`; `retention-days: 1` on the one upload `:168`.
- **D6.1 / D6.3 (seam):** DRIFT - single asset named `release-asset` (not `release-asset-<branch>-<target>`) `:166`, downloaded by exact `name:` (not `pattern:` / `merge-multiple:`) `:184-188`. Works for one target; forks the canonical verbatim handoff.
- **D7.1 / D7.2 (concurrency/permissions):** PASS - publisher group ref-independent + `cancel-in-progress: false` `publish-release.yml:28-30`; reusable jobs inherit, caller grants `id-token` / `contents` / `actions: write` at the one entry point `:50-53`.
- **D9.1 (SHA-pinning):** PASS - all actions SHA-pinned with version comments; `dotnet/nbgv@master` is the sanctioned lagging-tag exception `build-release-task.yml:61`.
- **Wrapper / PyPI / Docker / console addenda:** N/A.

## WORKFLOW.md 5B Trace (applicable scenarios)

- **S1** (PR touches library): validate + smoke run, no push/upload, validate-release skipped (smoke), aggregator success, version prerelease. PASS.
- **S2 / S3** (docs-only / workflow-only PR): smoke-build **runs** (no paths-filter) instead of skipping; aggregator success - safe but over-builds. DRIFT.
- **S4** (PR base = main): smoke prerelease, validate-release skipped (smoke), promotion not blocked. PASS.
- **S5** (push not touching shipped paths): publish-release not triggered. PASS.
- **S6** (push to develop touching `Utilities/**`): publishes a develop **prerelease** by default, not opt-in. DRIFT (continuous release).
- **S7** (both-branch matrix publish): N/A - no schedule/matrix; a dispatch publishes only its own branch.
- **S8** (dispatch from a non-publishable ref): publish job skipped (template fails fast). DRIFT.
- **S9** (re-run, version unchanged): release-create + asset-delete skipped, NuGet `--skip-duplicate` no-op. PASS.
- **S10** (branch/version disagree): validate-release fails loud. PASS.
- **S11** (wrapper bump): N/A.

## Defects (most severe first)

None. No applicable check fails both letter and intent.

## Drift Findings

1. **Release model - continuous per-branch push-publish, not two-phase.** `publish-release.yml:16-24` triggers publish on every push to main/develop that touches shipped paths, with no weekly schedule and no both-branch matrix - WORKFLOW.md's opt-in continuous-release mode used as the default (no `PUBLISH_ON_MERGE` variable). Outcomes are correct (main -> stable, develop -> prerelease); the divergence is structural. `publish-release.yml:16-30`.
2. **Seam handoff not canonical.** Single artifact `release-asset` with exact-name download instead of `release-asset-<branch>-<target>` + `pattern:` / `merge-multiple:`; forks the verbatim `github-release` carry and will not extend to a second target without rework. `build-release-task.yml:166,184-188`.
3. **`.editorconfig` lacks the canonical global line-ending default.** `[*]` sets no `end_of_line`; CRLF is re-declared per file-type - the older per-extension form AGENTS.md warns against; any uncovered file type gets no CRLF default. Canonical form is `[*] end_of_line = crlf` + LF pins. `.editorconfig:1-22` (no `[*]` default), `:23-63`. (`.gitattributes:6,11-15` still enforces via `* -text` + LF pins, so real files are compliant.)
4. **Stale hardcoded package versions in the csproj.** `Utilities/Utilities.csproj:17-19` hardcodes `<Version>1.1.1.1</Version>`, `<FileVersion>1.1.1.1</FileVersion>`, `<AssemblyVersion>1.1.1.0</AssemblyVersion>`, contradicting the `version.json:3` floor `3.6`. CI overrides these via `-property:Version=SemVer2` (`build-release-task.yml:123-127`), so published packages are correct, but a local `dotnet pack` produces a wrong `1.1.1.1` package (no Nerdbank.GitVersioning PackageReference; NBGV is workflow-only).
5. **Ruleset-bound name diverges from the fleet canonical.** The live rulesets' required-status-check `context` is `Check pull request workflow status job`; the fleet canonical (`repo-config/{develop,main}.json`) is `Check pull request workflow status`. Utilities renamed both the aggregator job (`test-pull-request.yml:45`) and both ruleset contexts in lockstep, so enforcement holds, but AGENTS.md advises against suffixing the ruleset-bound job name.
6. **repo-config filenames off-baseline.** Committed as `repo-config/ruleset-develop.json` / `ruleset-main.json` (baseline expects `develop.json` / `main.json` per `spec/files.json`); the committed files also omit `bypass_actors`, though the **live** rulesets do carry `RepositoryRole 5 always` (matching template).
7. **README omissions.** No `## Table of Contents` heading, no `## Questions or Issues`; `## Contributing` occupies the Development-Environment-Setup slot; `README.md:9` missing colon; shields group header `<!-- Shields links -->` vs canonical `<!-- Shields -->`.
8. **develop stale.** See Develop Drift (28 behind, 0 ahead).

## Proposed Registry / Spec Updates

- Registry `Utilities` types `["csharp","nuget"]` and publish `nuget` via `oidc` are accurate; no change. Consider advancing `status` beyond `cataloged` once the drift findings are addressed.
- **Spec signal (candidate machine checks):** the required-status-check context-name divergence (`...status job`) and the per-extension `.editorconfig` shape may recur across derived repos. If several share them, the fix belongs in a spec/lint check - assert the ruleset `context` equals the workflow aggregator `name:`, and assert a global `[*] end_of_line` default exists - rather than per-repo notes.
