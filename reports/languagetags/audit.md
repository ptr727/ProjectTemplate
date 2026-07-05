# Audit: LanguageTags

- **Audited branch:** main (`7322d72a5cf975a6c8cc4378165e9df2da095c9a`)
- **Types:** csharp, nuget, codegen (from registry)
- **Verdict:** operational
- **Date:** 2026-07-04

## Develop Drift

`develop` vs `main`: ahead 0, behind 53 (`gh api repos/ptr727/LanguageTags/compare/main...develop` -> `status: behind, ahead_by: 0, behind_by: 53`). **Stale - a drift finding.** `develop` carries none of `main`'s 53 commits. Under the forward-only model (no `main -> develop` back-merge) `develop` must receive `main`-only changes directly (Dependabot bumps merged to `main`, the audited pipeline state); it has not, so `develop` does not reflect the released pipeline.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| csharp | pass | pass | pass | `.editorconfig:58` carries the shared `[*.cs]`+ReSharper block (matches the template block); analyzers enforced in `Directory.Build.props:6-9` (`AnalysisLevel=latest-all`, `AnalysisMode=All`, `EnableNETAnalyzers=true`, `TreatWarningsAsErrors=true`); per-project suppressions narrow and documented (`LanguageTags/.editorconfig:7,10` `CA1308`/`CS1591`). See Drift re global EOL default |
| nuget | pass | pass | pass | OIDC login `NuGet/login` `build-release-task.yml:134-136` with an ephemeral key (`steps.nuget-login.outputs.NUGET_API_KEY` at `:145`, not a stored `secrets.NUGET_API_KEY`), no stored API key; `dotnet nuget push --skip-duplicate` `:143-146`, gated `if: inputs.publish && !inputs.smoke` `:140`; `.snupkg` symbols packed/pushed (`LanguageTags.csproj:13,27`) |
| pypi | - | - | N/A | no `pyproject.toml` / `gh-action-pypi-publish` |
| python | - | - | N/A | no Python project |
| console | - | - | N/A | shipped target is the packable library `LanguageTags.csproj:16` (`IsPackable=true`); `LanguageTagsCreate` is the codegen creator (a `System.CommandLine` tool) that ships nothing - no `build-executable-task.yml`, no executable `release-asset` |
| docker | - | - | N/A | no `Dockerfile` / `build-docker-task.yml` |
| codegen | pass | pass | pass | output input-deterministic - generated files carry a static `[GeneratedCode(..., "1.0")]` stamp (`LanguageTags/Iso6392DataGen.cs:6`, `UnM49DataGen.cs:6`), no per-run timestamp/GUID; matrix over `main`+`develop`, each leg checking out its own ref and opening its own PR (`codegen-main` -> `main`, `codegen-develop` -> `develop`) (`run-codegen-pull-request-task.yml:28-33,76-77`), driven daily (`run-periodic-codegen-pull-request.yml:8-9`); App secrets present |
| branch-model | drift | pass | drift | both branches protected (`develop`/`main` rulesets `enforcement: active`); live rulesets match committed `repo-config/ruleset-{develop,main}.json` **except** `bypass_actors` (live carries `RepositoryRole 5 always`, committed omits) and `required_reviewers: []` (live present, committed omits). The required-check `context` `Check pull request workflow status job` matches both the committed rulesets and the repo's own aggregator `name:` (`test-pull-request.yml:45`) - the new fleet canonical, not a drift. Off-baseline filenames - see Drift |
| repo-setup | pass | pass | pass | actions + dependabot stores both hold `CODEGEN_APP_CLIENT_ID`, `CODEGEN_APP_PRIVATE_KEY` (codegen App + merge-bot) and `NUGET_USERNAME` (the OIDC `NuGet/login` profile name `build-release-task.yml:136`); no forbidden `NUGET_API_KEY` (nuget-oidc) or `CODEGEN_APP_ID` (codegen-app) |
| linter-parity | pass | pass | pass | one `.markdownlint-cli2.jsonc` + `cspell.json` drive editor + CLI + CI (`validate-task.yml:56-69`); csharpier/editorconfig shared via `.config/dotnet-tools.json`; CI runs each linter (csharpier check `:47-51`, `dotnet format style` `:53-54`, markdownlint `:56-59`, cspell `:63-69`, actionlint `:71-72`) |
| recurring-violations | drift | pass | drift | docs ASCII-clean (em-dash/smart-quote grep of README/HISTORY/AGENTS/CODESTYLE/WORKFLOW/copilot-instructions -> none); `cspell.json:3` sets `"language": "en-US"`; real endings compliant (`.gitattributes:6,12-19` `* -text` + LF pins for `*.sh`/Dockerfiles/`.husky/pre-commit`). **Letter miss:** `.editorconfig` lacks the global `[*] end_of_line = crlf` default - see Drift |
| readme-structure | drift | pass | drift | Build and Distribution (Build Status/Releases/Release Notes), Getting Started, Table of Contents, Use Cases, Usage, Installation, Questions or Issues, 3rd Party Tools, License all present, but `## Installation` `README.md:373` sits **after** `## Usage` `:112` (spec order is Installation before Usage), `## Contributing` `:437` fills the Development-Environment-Setup slot under a different name, extra `## Build Artifacts`/`## Tag Theory` interleaved; minor: missing colon `:9`, shields header `<!-- Shields links -->` vs canonical `<!-- Shields -->` `:575` |
| workflow (WORKFLOW.md 5A/5B) | drift | pass | drift | all applicable D-guarantees hold by **outcome**; divergences are structural (see 5A/5B): continuous per-branch push-publish instead of two-phase scheduled matrix, no `changes` paths-filter, keys off `github.ref_name` without `IGNORE_GITHUB_REF`, plain `release-asset` name + exact-name download, dispatch guard skips instead of failing fast |

## WORKFLOW.md 5A Static Audit (applicable D-guarantees)

- **D1.1 / D1.4 (paths-filter):** DRIFT - no `changes` / `dorny/paths-filter` job; `validate` and `smoke-build` run on **every** push including docs-only and `.github/workflows/**`-only (`test-pull-request.yml:25-40`, deliberate per header `:3-11`). Over-builds; the "changed target slips unbuilt" failure cannot occur with a single always-built target.
- **D1.2 (validation always runs):** PASS - `validate` runs unconditionally `if: !github.event.deleted` (`test-pull-request.yml:25-28`); the aggregator `needs:` it `:47`.
- **D1.3 (smoke uploads/pushes nothing):** PASS - the smoke caller passes `publish: false` (`test-pull-request.yml:38-40`); every push/upload step is gated `if: inputs.publish && !inputs.smoke` (`build-release-task.yml:132,140,150,163,172`).
- **D1.5 (one aggregator):** PASS - `check-workflow-status`, `needs: [validate, smoke-build]`, `always() && !github.event.deleted`, fails on any non-`success` (`test-pull-request.yml:44-60`). Name is the ruleset-bound context `:45`.
- **D2.1 / D2.2 (validate-at-entry):** PASS - `validate-release` asserts branch<->classification **both** directions, strips `+buildmetadata`, skips on smoke (`build-release-task.yml:65-94`); `build` and `github-release` both `needs:` it `:98,173`.
- **D2.3 (dispatch guard):** DRIFT - the publish job `if: github.event_name == 'push' || github.ref_name == 'main' || github.ref_name == 'develop'` (`publish-release.yml:48`) silently **skips** a dispatch from any other ref rather than failing fast with `::error::`. Skip is safe but non-canonical.
- **D3.1 (version from checked-out branch):** DRIFT (letter) / PASS (intent) - `get-version` uses the default checkout and NBGV classifies from `GITHUB_REF`; **no** `IGNORE_GITHUB_REF=true` (`build-release-task.yml:50-61`). Correct here because the publisher builds only the triggering branch per run (`github.ref` aligned); a future both-branch matrix would misclassify.
- **D3.2 (default = public, others = prerelease):** PASS - `version.json:4` `publicReleaseRefSpec ^refs/heads/main$`; configuration `github.ref_name == 'main' && 'Release' || 'Debug'` `:120`; `prerelease: github.ref_name != 'main'` `:213`. All name `main`.
- **D3.3 (floor + git height):** PASS - `version.json:3` `version "1.5"`; NBGV appends the git height.
- **D3.4 (registry version per classification):** PASS - build injects `-property:PackageVersion=SemVer2` `build-release-task.yml:123,127`; NuGet.org derives prerelease from the `-g<sha>` suffix.
- **D4.1 (two-phase / both branches):** DRIFT - the publisher `push`-triggers on every push to `main`/`develop` touching shipped paths (`publish-release.yml:3-11`), with **no** weekly schedule and **no** both-branch matrix - WORKFLOW.md's opt-in continuous-release mode used as the default (no opt-in repository variable). Outcomes correct (main -> stable, develop -> prerelease); the divergence is structural.
- **D4.2 / D4.3 / D4.4 (release):** PASS - `target_commitish` = `GitCommitId` `:212`, `prerelease = github.ref_name != 'main'` `:213`, create gated `exists == false || workflow_dispatch` `:207`, `fail_on_unmatched_files: true` `:214`, contents tag + `LICENSE` + `README.md` + `LanguageTags.7z` `:215-218`; re-push no-op via `--skip-duplicate` `:146`.
- **D5 (cleanup):** PASS - asset delete at the consumer under the **same** gate as create `:224`, `continue-on-error: true` `:225`, filtered by `select(.name == "release-asset")` (not a blanket `.artifacts[].id`) `:230-238`; the one upload sets `retention-days: 1` `:168`.
- **D6.1 / D6.3 (seam):** DRIFT - single asset named `release-asset` (not `release-asset-<branch>-<target>`) `:166`, downloaded by exact `name:` (not `pattern:` / `merge-multiple:`) `:187`. D6.2: branch-derived config keys off `github.ref_name` (no `inputs.branch`) - acceptable for a single-branch-per-run publisher, non-canonical.
- **D7.1 / D7.2 (concurrency / permissions):** PASS - publisher group `${{ github.workflow }}` ref-independent + `cancel-in-progress: false` (`publish-release.yml:29-31`); the reusable task declares no `permissions:`, the publish caller grants `contents` / `id-token` / `actions: write` at the one entry point `:51-54`, and the smoke caller grants only `contents: read` (`test-pull-request.yml:36-37`).
- **D8.1 (merge-bot):** PASS - auto-merge on `opened`/`reopened`, method by base ref (`develop => --squash`, `main => --merge`) `merge-bot-pull-request.yml:51-56`; codegen head/base pairing pinned `:66-73`; disable on maintainer `synchronize` `:105-112`; concurrency keyed on the PR number `:20-22`.
- **D8.2 (codegen + Dependabot):** PASS - codegen runs as a `main`/`develop` matrix, deterministic from upstream registries, each leg its own PR (`run-codegen-pull-request-task.yml:28-33,70-81`), driven daily; Dependabot dual-targets `main`+`develop` for `nuget` and `github-actions` (`.github/dependabot.yml`), security PRs to the default branch.
- **D9.1 (SHA-pinning):** PASS - all actions SHA-pinned with version comments; `dotnet/nbgv@master` is the sanctioned lagging-tag exception (`build-release-task.yml:57-61`).
- **Console / PyPI / Docker / wrapper 5A addenda:** N/A.

## WORKFLOW.md 5B Trace (applicable scenarios)

- **S1** (PR touches the library): `validate` + `smoke-build` run, no push/upload (`publish: false`), `validate-release` skipped (smoke), aggregator success, version prerelease. PASS.
- **S2 / S3** (docs-only / workflow-only PR): `smoke-build` **runs** (no paths-filter) instead of skipping; aggregator success - safe but over-builds. DRIFT.
- **S4** (PR base = main): smoke prerelease, `validate-release` skipped (smoke), promotion not blocked. PASS.
- **S5** (push not touching shipped paths): publish-release not triggered (`on.push.paths` inclusion list). PASS.
- **S6** (push to develop touching shipped paths): publishes a develop **prerelease** by default, not opt-in. DRIFT (continuous release).
- **S7** (both-branch matrix publish): N/A - no schedule/matrix; a dispatch publishes only its triggering branch.
- **S8** (dispatch from a non-publishable ref): publish job **skipped** (template fails fast). DRIFT.
- **S9** (re-run, version unchanged): release-create + asset-delete skipped, NuGet `--skip-duplicate` server no-op. PASS.
- **S10** (branch/version disagree): `validate-release` fails loud. PASS.
- **S11** (wrapper bump): N/A.
- **Codegen (daily):** the task regenerates `LanguageData`/`*DataGen.cs` deterministically, opens `codegen-main`/`codegen-develop` PRs only on change, the merge-bot auto-merges each, and the shipped data change triggers the publisher. PASS.

## Defects (most severe first)

None. No applicable check fails both letter and intent.

## Drift Findings

1. **Release model - continuous per-branch push-publish, not two-phase.** `publish-release.yml:3-11,48` publishes on every push to `main`/`develop` touching a shipped path, no weekly schedule, no both-branch matrix - WORKFLOW.md D4.1's opt-in continuous mode as the default. Outcomes correct (main -> stable, develop -> prerelease); structural divergence. **(Recurs with Utilities.)**
2. **Version classification keys off `github.ref_name` without `IGNORE_GITHUB_REF`.** `build-release-task.yml:50-61` runs NBGV on the default checkout; config/prerelease read `github.ref_name` `:120,213`. Correct only because the publisher builds one branch per run with `github.ref` aligned; a future both-branch matrix would misclassify. **(Recurs with Utilities.)**
3. **Seam handoff not canonical.** Single artifact `release-asset` with exact-name download instead of `release-asset-<branch>-<target>` + `pattern:` / `merge-multiple:`; forks the verbatim `github-release` carry and will not extend to a second target without rework. `build-release-task.yml:166,187`. **(Recurs with Utilities.)**
4. **No `changes` paths-filter; every push smoke-builds.** `test-pull-request.yml:25-40` runs `validate` + `smoke-build` on every push including docs-only and workflow-only. **(Recurs with Utilities / PlexCleaner.)**
5. **`.editorconfig` lacks the canonical global line-ending default.** The `[*]` block (`.editorconfig:17-22`) sets `charset`/indent but **no** `end_of_line`; CRLF is re-declared per file-type (`:26,31,36,41`) and on `[*.cs]` `:59` - the older per-extension form the strengthened `recurring.eol` flags. The template's own `.editorconfig:22` carries `[*] end_of_line = crlf`. `.gitattributes:6,12-19` still enforces endings, so real files are compliant. **(Recurs with Utilities / PlexCleaner - now three repos.)**
6. **Dispatch guard skips instead of failing fast.** `publish-release.yml:48` silently no-ops a `workflow_dispatch` from a non-`main`/`develop` ref; WORKFLOW.md D2.3 wants a fail-fast `::error::`. **(Recurs with Utilities / PlexCleaner.)**
7. **Committed rulesets omit fields the live rulesets carry.** `repo-config/ruleset-{develop,main}.json` omit `bypass_actors` (live carries `RepositoryRole 5 always`) and `required_reviewers: []`. The required-check `context` matches, so enforcement is intact. **(Recurs with Utilities.)**
8. **repo-config filenames off-baseline.** Committed as `repo-config/ruleset-{develop,main}.json`; `spec/files.json` expects `repo-config/develop.json` / `main.json`. **(Recurs with Utilities / PlexCleaner.)**
9. **README section order / naming.** `## Installation` `README.md:373` sits after `## Usage` `:112`; `## Contributing` `:437` fills the Development-Environment-Setup slot under a different name; extra `## Build Artifacts`/`## Tag Theory` interleaved; missing colon `:9`; shields header `<!-- Shields links -->` vs canonical `<!-- Shields -->` `:575`.
10. **Stale hardcoded versions in the csproj.** `LanguageTags.csproj:4,10,14,24,28` hardcode `AssemblyVersion`/`FileVersion`/`Version` `1.0.0.0` and `InformationalVersion`/`PackageVersion` `1.0.0-pre`, vs `version.json:3` floor `1.5`. CI overrides via `-property:*=SemVer2` (`build-release-task.yml:123-127`), so published packages are correct, but a local `dotnet pack` produces a wrong `1.0.0` package. **(Recurs with Utilities.)**
11. **`develop` stale** (behind 53, ahead 0). See Develop Drift.

## Proposed Registry / Spec Updates

- Registry `LanguageTags` types `["csharp","nuget","codegen"]`, publish `nuget` via `oidc`, status `cataloged` are accurate; no change. Consider advancing `status` once the structural workflow drifts are addressed.
- **Spec gap - `NUGET_USERNAME` under nuget-oidc.** `spec/secrets.json` models `nuget-oidc` as `requires: []`, but the OIDC `NuGet/login` needs `NUGET_USERNAME` (the nuget.org profile name, `build-release-task.yml:136`). Model `NUGET_USERNAME` as a required secret (stored `actions`, and `dependabot` since a bump republishes) so repo-setup does not read it as orphaned.
- **Spec signal (recurring across repos):** the missing global `[*] end_of_line = crlf` default (now caught by `recurring.eol`), the plain `release-asset` (non-`<branch>-<target>`, non-`pattern:`) seam, the single-branch continuous-publish shape, the skip-not-fail dispatch guard, and the off-baseline `repo-config/ruleset-*.json` filenames now recur across Utilities, PlexCleaner, and LanguageTags. Candidates for machine checks or a baseline-filename alias in `spec/files.json`, rather than per-repo notes.
