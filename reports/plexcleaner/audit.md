# Audit: PlexCleaner

- **Audited branch:** main (`e84f1426dfa1746777ff470d019e1562632cd9f0`)
- **Types:** csharp, console, docker (from registry)
- **Verdict:** operational
- **Date:** 2026-07-04

## Develop Drift

`develop` vs `main`: ahead 13, behind 23 (`gh api repos/ptr727/PlexCleaner/compare/main...develop` -> `status: diverged, ahead_by: 13, behind_by: 23`). **Diverged - a drift finding.** `develop` carries 13 commits `main` lacks (normal unreleased work under the forward-only model) but is also 23 behind. With no `main -> develop` back-merge, `main`-only fixes (e.g. Dependabot bumps merged straight to `main`) have not reached `develop`, so `develop` does not reflect the released pipeline. Not a break; worth reconciling.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| csharp | pass | pass | pass | `.editorconfig:59` carries the shared `[*.cs]` rule block (matches the template block, plus 17 documented repo-wide CA relaxations `:66-84`); analyzers enforced in `Directory.Build.props:6-9` (`AnalysisLevel=latest-all`, `AnalysisMode=All`, `EnableNETAnalyzers=true`, `TreatWarningsAsErrors=true`). See Drift re CODESTYLE.md:242 |
| nuget | - | - | N/A | no `build-nugetlibrary-task.yml` / `dotnet nuget push`; `Directory.Build.props:11 IsPackable=false` |
| pypi | - | - | N/A | no `pyproject.toml` / `gh-action-pypi-publish` |
| python | - | - | N/A | no Python project |
| console | pass | pass | pass | System.CommandLine app, `PlexCleaner/PlexCleaner.csproj:16 OutputType=Exe`; smoke matrix `["linux-x64","win-x64"]` is a strict subset of the 7-runtime full matrix `build-executable-task.yml:44`; per-runtime `publish-<branch>-<runtime>` aggregated by `pattern:` + `merge-multiple:` into one `release-asset-<branch>-executable` `:91-105`, both the per-runtime upload `:74` and the aggregation job `:85` gated `!smoke` |
| docker | pass | pass | pass | registry layer cache `buildcache-<branch>` (never `type=gha`) `build-docker-task.yml:89-92` (`cache-from` both branches, `cache-to` only-branch + only-on-push); trimmed `Docker/README.md` published via `peter-evans/dockerhub-description`, main-only `:102-110`; image always re-pushes on `inputs.push` (`dockerhub && !smoke`), independent of the release-create skip `build-release-task.yml:82`. Publish uses the static `DOCKER_HUB_*` secret (expected for docker, not OIDC) |
| branch-model | drift | pass | drift | both branches protected (rulesets `develop`/`main` `enforcement: active`); the required-status-check `context` `Check pull request workflow status job` is the fleet canonical (adopted template-wide) and matches PlexCleaner's own aggregator `test-pull-request.yml:48`, so the naming is **not** a drift; residual drift is the off-baseline `repo-config/ruleset-{develop,main}.json` filenames (see Drift) |
| repo-setup | pass | pass | pass | actions + dependabot stores both hold `DOCKER_HUB_USERNAME`, `DOCKER_HUB_ACCESS_TOKEN` (docker-hub), `CODEGEN_APP_CLIENT_ID`, `CODEGEN_APP_PRIVATE_KEY` (used by the merge-bot `merge-bot-pull-request.yml:41-42`); no forbidden `NUGET_API_KEY` / `CODEGEN_APP_ID`. `CODECOV_TOKEN` present but unused (see Drift) |
| linter-parity | pass | pass | pass | one `.markdownlint-cli2.jsonc` + `cspell.json` drive editor + CLI + CI (`validate-task.yml:63-78`); csharpier/editorconfig shared via `.config/dotnet-tools.json`; CI runs each linter (csharpier check, `dotnet format style`, markdownlint, cspell, actionlint) `validate-task.yml:54-78` |
| recurring-violations | pass | pass | pass | comments concise; docs ASCII-clean (README/HISTORY/AGENTS/CODESTYLE/WORKFLOW/Docker README grep for em-dash/smart quotes -> none); `cspell.json:3 "language": "en-US"`; real endings compliant (`.gitattributes:6,12-18` `* -text` + LF pins for `*.sh`/`.husky/pre-commit`/`Dockerfile`). See Drift re `.editorconfig` global default |
| readme-structure | drift | pass | drift | Build and Distribution (with Build Status/Releases/Release Notes), Getting Started, Table of Contents, Use Cases, Installation, Configuration, Usage, 3rd Party Tools, License all present and largely ordered, but `## Questions or Issues` sits at `README.md:99` (after the ToC) instead of spec position 9 (after Usage), and `## Development Tooling` `:917` occupies the Development-Environment-Setup slot under a different name; extra sections interleaved |
| workflow (WORKFLOW.md 5A/5B) | drift | pass | drift | all applicable D-guarantees hold by **outcome**; divergences are structural (see 5A/5B): no `changes` paths-filter, single-branch-per-run publisher (no both-branch matrix), dispatch guard skips instead of failing fast, one-directional release gate embedded as a step, NBGV threaded from a single run without `IGNORE_GITHUB_REF` |

## WORKFLOW.md 5A Static Audit (applicable D-guarantees)

- **D1.1 / D1.4 (paths-filter):** DRIFT - no `changes` / `dorny/paths-filter` job; `validate` and `smoke-build` run on **every** push including docs-only and `.github/workflows/**`-only (`test-pull-request.yml:25-42`, deliberate per header `:3-10`). Over-builds; the "changed target slips unbuilt" failure mode cannot occur (both targets always built).
- **D1.2 (validation always runs):** PASS - `validate` (unit-test + lint) runs unconditionally `if: !github.event.deleted` (`test-pull-request.yml:25-28`); the aggregator `needs:` it `:50`.
- **D1.3 (smoke uploads/pushes nothing):** PASS - smoke sets `github: false`, `dockerhub: false` (`test-pull-request.yml:40-41`); executable upload gated `!smoke` (`build-executable-task.yml:74,85`), docker push `dockerhub && !smoke` (`build-release-task.yml:82`), github-release `github && !smoke` (`:87`).
- **D1.5 (one aggregator):** PASS - `check-workflow-status`, `needs: [validate, smoke-build]`, `always() && !github.event.deleted`, fails on any non-`success` (`test-pull-request.yml:47-63`). Name is ruleset-bound, the fleet-canonical `<name> job` (see branch-model).
- **D2.1 / D2.2 (validate-at-entry):** DRIFT - the release gate is the `Verify public release version step` **inside** github-release (`build-release-task.yml:99-111`), not an upfront entry job the builds `needs:`. It strips `+buildmetadata` and refuses a prerelease `-` on `main`, but checks **one direction only** (main-not-prerelease). Safe because `version.json:4` makes `main` the sole public ref, so NBGV structurally prerelease-tags every other branch.
- **D2.3 (dispatch guard):** DRIFT - the publish job `if: github.ref_name == 'main' || github.ref_name == 'develop'` (`publish-release.yml:33`) **silently skips** a dispatch from any other ref rather than failing fast with `::error::`. Skip is safe but non-canonical.
- **D3.1 (version from checked-out branch):** DRIFT (letter) / PASS (intent) - NBGV runs **once** in `get-version` on `inputs.ref` (`get-version-task.yml:46-56`) and threads outputs; no `IGNORE_GITHUB_REF=true`. Correct here because the publisher builds one branch per run with `github.ref` aligned to it (`publish-release.yml:4-7,39`), so no ref leak.
- **D3.2 (default = public, others = prerelease):** PASS - `version.json:4 publicReleaseRefSpec ^refs/heads/main$`; `prerelease: inputs.branch != 'main'` (`build-release-task.yml:149`); gate literal `inputs.branch == 'main'` (`:102`). All name `main`.
- **D3.3 (floor + git height):** PASS - `version.json:3 version "3.19"`; NBGV appends the height.
- **D3.4 (registry versions per classification):** PASS - Docker tags `main => latest`, else `develop`, plus `:SemVer2` (`build-docker-task.yml:84-85`); no nuget/pypi registry.
- **D4.1 (two-phase / both branches):** DRIFT - publisher triggers are `schedule` (weekly, `main` only) + `workflow_dispatch`, **no push trigger** (`publish-release.yml:3-7`), so merges never publish (correct). But it does **not** build both branches via a matrix - it builds only the trigger branch (`:26-44`); `develop` publishes only via a manual dispatch from `develop`. Outcomes are correct; the divergence is structural.
- **D4.2 (tag the built commit):** PASS - `target_commitish: needs.get-version.outputs.GitCommitId` (`build-release-task.yml:148`).
- **D4.3 (release contents):** PASS - tag + `generate_release_notes` + `LICENSE` + `README.md` + `./Publish/*` (the 7z) (`build-release-task.yml:147-154`); `prerelease = branch != main` `:149`; `fail_on_unmatched_files: true` `:150`.
- **D4.4 (no-op republish):** PASS - release-create gated `exists == false || workflow_dispatch` (`build-release-task.yml:143`); Docker still re-pushes (push independent of the existence check).
- **D5 (cleanup):** DRIFT (minor) - every upload sets `retention-days: 1` and there is no blanket `.artifacts[].id` delete (D5.5 pass), but the cross-job `release-asset-<branch>-executable` consumed by github-release (`build-release-task.yml:114-119`) has **no** consume-delete step; it relies on the retention backstop alone (D5.1 prefers a delete at the consumer). Safe.
- **D6.1 (pattern handoff):** PASS - github-release downloads `pattern: release-asset-${{ inputs.branch }}-*` + `merge-multiple: true` (`build-release-task.yml:117-118`); the leaf uploads `release-asset-<branch>-executable` (`build-executable-task.yml:104`). Canonical, no `artifact-ids:`.
- **D6.2 / D6.3 (branch drives config, suffixed artifacts):** PASS - leaves key config/tags off `inputs.branch` (`build-executable-task.yml:63`, `build-docker-task.yml:84,95`); artifacts branch-suffixed.
- **D6.4 (target set consistent):** PASS - both targets have a `build-<target>` job and a `github-release` `needs:` entry (`build-release-task.yml:89`); no paths-filter to keep in lockstep (D1 drift).
- **D7.1 (publisher concurrency):** PASS - group `${{ github.workflow }}` (ref-independent), `cancel-in-progress: false` (`publish-release.yml:20-22`).
- **D7.2 (permissions):** PASS - publisher grants `contents: write` at the entry (`publish-release.yml:36-37`); no OIDC needed (docker uses the static secret); merge-bot jobs declare least-privilege `permissions:`.
- **D7.4 (optional-dependency chaining):** PASS - build jobs use `!cancelled() && get-version == 'success' && (validate == 'success' || 'skipped')` (`build-release-task.yml:53,69`).
- **D8.1 (merge-bot):** PASS - auto-merge on `opened`/`reopened`, method by base ref (`develop => --squash`, `main => --merge`) `merge-bot-pull-request.yml:57-65`, disable on maintainer `synchronize` `:70-97`, concurrency keyed on the PR number `:18`.
- **D9.1 (SHA-pinning):** PASS - all actions SHA-pinned with version comments; `dotnet/nbgv@master` is the sanctioned lagging-tag exception (`get-version-task.yml:54-56`).
- **D9.4 (docker cache):** PASS - registry buildcache, per-branch, `cache-to` writes only the built branch and only on push, `cache-from` reads both (`build-docker-task.yml:89-92`).
- **PyPI / NuGet / wrapper 5A addenda:** N/A.

## WORKFLOW.md 5B Trace (applicable scenarios)

- **S1** (PR touches a target): validate + smoke-build (amd64-only Docker, 2-runtime executable) run; no push, **no uploads**; release skipped; aggregator success; version prerelease. PASS.
- **S2 / S3** (docs-only / workflow-only PR): smoke-build **runs** (no paths-filter) instead of skipping; aggregator success - safe but over-builds. DRIFT.
- **S4** (PR base = main): smoke versions prerelease; github-release skipped on smoke; promotion not blocked. PASS.
- **S5** (push, opt-in unset): the publisher has **no push trigger**, so no push publishes. PASS (stronger than opt-in).
- **S6** (push to develop, opt-in set): N/A - there is no push-publish path or opt-in variable; develop publishes only via `workflow_dispatch` from `develop`.
- **S7** (scheduled/dispatched publish): builds **only** the trigger branch. Schedule -> `main` -> `X.Y.Z` stable, `latest` image, Docker Hub overview refreshed; dispatch from `develop` -> prerelease, `develop` image. No dangling artifacts (retention backstop). DRIFT (single-branch, not a both-branch matrix); outcomes correct.
- **S8** (dispatch from a non-publishable ref): publish job **skipped** (template fails fast). DRIFT.
- **S9** (re-run, version unchanged): release-create skipped (tag exists, non-dispatch); Docker still re-pushes; no duplicate release. PASS.
- **S10** (branch/version disagree): the main-only `Verify public release version step` fails loud if `main` carries a prerelease suffix (`build-release-task.yml:108-110`). PASS (the develop-plain case cannot arise - NBGV).
- **S11** (wrapper bump): N/A.

## Defects (most severe first)

None. No applicable check fails both letter and intent.

## Drift Findings

1. **Publisher builds one branch per run, not a both-branch matrix.** `publish-release.yml:26-44` publishes only the trigger branch; the weekly schedule rebuilds `main` only `:3-7`, and `develop` prereleases only on a manual dispatch. WORKFLOW.md D4.1's model schedules both legs via a matrix. Outcomes correct (main -> stable/`latest`, develop -> on-demand prerelease); `develop` gets no scheduled base-image refresh.
2. **No `changes` paths-filter; every push smoke-builds both targets.** `test-pull-request.yml:25-42` runs `validate` + `smoke-build` on every push including docs-only and workflow-only (deliberate, per header `:3-10`). Over-builds; the "changed target slips unbuilt" failure cannot occur with two always-built targets.
3. **Ruleset naming matches the fleet canonical (not a drift).** The check `context` and the aggregator job `name:` are both `Check pull request workflow status job` (`test-pull-request.yml:48`) - the `<name> job` convention now adopted template-wide. This report predated that adoption; the naming is canonical, not a deviation.
4. **Dispatch guard skips instead of failing fast.** `publish-release.yml:33` silently no-ops a dispatch from a non-`main`/`develop` ref; WORKFLOW.md D2.3 wants a fail-fast `::error::`.
5. **Release gate is one-directional and embedded as a step.** `build-release-task.yml:99-111` verifies only that `main` is not prerelease, inside github-release rather than a dedicated entry job the builds `needs:`. Safe (the reverse case is structurally impossible via `version.json:4`), but weaker than the template's two-direction validate-at-entry.
6. **NBGV threaded from a single run without `IGNORE_GITHUB_REF`.** `get-version-task.yml:46-56` runs NBGV once; no `IGNORE_GITHUB_REF=true`. Correct only because the publisher builds one branch per run with `github.ref` aligned; a future both-branch matrix would misclassify without it.
7. **`release-asset-<branch>-executable` relies on the retention backstop, no consume-delete.** The transfer artifact consumed by github-release (`build-release-task.yml:114-119`) has no delete step; D5.1 prefers a delete at consumption. `retention-days: 1` reaps it - minor.
8. **`.editorconfig` lacks the canonical global line-ending default.** The `[*]` block (`.editorconfig:17-23`) sets charset/indent but **no** `end_of_line`; CRLF is re-declared per file-type (`:26,31,36,53,60`) - the older per-extension form. The template's own `.editorconfig:20-22` carries `[*] end_of_line = crlf`. `.gitattributes:6,12-18` still enforces endings, so real files are compliant. **(Recurs with Utilities - see Proposed Updates.)**
9. **Repo-wide CA relaxation batch (CODESTYLE.md:242 edge).** `.editorconfig:66-84` turns off 17 `CA*` rules repo-wide; each is documented and most are genuinely N/A for a console app, but a few (`CA1307`/`CA1308`/`CA2007`) are the "push it through" class CODESTYLE.md:242 cautions against. Analyzers remain enforced overall - a note, not a break.
10. **README section order/naming.** `## Questions or Issues` sits at `README.md:99` (after the ToC) rather than spec position 9 (after Usage); `## Development Tooling` `:917` fills the Development-Environment-Setup slot under a different name.
11. **`CODECOV_TOKEN` stored but unused.** Present in both the actions and dependabot stores, but no workflow references codecov (only `coverlet.collector` is a test-time package `Directory.Packages.props:5`). Not forbidden; a stale secret to prune or wire up.
12. **`develop` diverged from `main`** (ahead 13, behind 23). See Develop Drift.

## Proposed Registry / Spec Updates

- Registry `PlexCleaner` types `["csharp","console","docker"]` and publish (`docker` via `static-secret`, `github-release` via `none`) are accurate; no change. Consider advancing `status` beyond `cataloged` once the structural workflow drifts (D4.1 matrix, D1 paths-filter) are addressed.
- **Spec signal (recurring across two repos):** both PlexCleaner and Utilities show (a) the required-status-check `context` suffixed with `job` (matching the local aggregator name but deviating from the fleet canonical) and (b) the missing global `[*] end_of_line = crlf` default. Two repos sharing both strengthens the case for machine checks - assert the ruleset `context` equals the aggregator job `name:` and flag the fleet-canonical mismatch; assert a global `[*] end_of_line` default exists - in the spec/lint layer, not per-repo notes.
- **Spec gap - merge-bot app secrets:** `spec/secrets.json` models `CODEGEN_APP_CLIENT_ID`/`CODEGEN_APP_PRIVATE_KEY` only under the `codegen-app` mechanism, but here they are required by the **merge-bot** (`merge-bot-pull-request.yml:41-42`) on a repo with no codegen. Consider modeling a `merge-bot` mechanism that requires the App secrets so repo-setup does not read them as orphaned.
