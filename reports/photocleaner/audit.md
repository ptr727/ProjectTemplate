# Audit: PhotoCleaner

- **Audited branch:** main (`c457ff3`)
- **Types:** csharp, console, docker (from registry)
- **Verdict:** not operational on `main`, converged on `develop`, promotion in flight
- **Date:** 2026-08-15
- **Run stamps:** `audit run 2026-08-15T14:28:27Z | hub 0e84805` (main), `audit run 2026-08-15T14:58:12Z | hub d54862a | branch override develop` (develop at `e8b7a81`)

Supersedes the 2026-08-01 snapshot. Everything that snapshot listed as a defect or drift has landed on `develop`: the `AGENTS.md`/`GOVERNANCE.md` router split, the `validate-release` gate, the README structure, and the two 2026-08-13 and 2026-08-15 resyncs against the hub ([#49](https://github.com/ptr727/PhotoCleaner/pull/49), [#50](https://github.com/ptr727/PhotoCleaner/pull/50)). None of it has reached `main` yet, so `main` still measures as the pre-resync state and this report says so rather than reading `develop` as ground truth.

## Develop Drift

`develop` vs `main`: two content commits ahead (#49, #50), nothing behind. This is convergence awaiting promotion, not divergence. The audit at `main` reports 43 findings, 6 of them letters (README section and reference-name letters, `host-tools.json` absent). The audit at `develop` reports 2, both the `investigate`-dispositioned hub-only files (`publish-release.yml`, `validate-task.yml`) that every repo carries and that are settled in `spec/divergences.json` rather than per repo.

The promotion [#51](https://github.com/ptr727/PhotoCleaner/pull/51) is open and **blocked by a hub defect, not a repo one**: the prose gate diffs a promotion against `main`, and `prose_lint.py dead-path` flags three mentions of `repo-config/configure.sh` (a file the repo retired per the `retire` disposition), two of them in verbatim hub text the repo cannot reword. Filed as [#721](https://github.com/ptr727/ProjectTemplate/issues/721). Copilot read 18 of 19 files on the promotion, omitting `GOVERNANCE.md`, and generated no comments.

## Dimensions

Judged at `main` unless the row says otherwise. Type-dimension checks are hand-judged per AUDIT.md section 4 and are unchanged from the 2026-08-01 snapshot, since #49 and #50 touched governance and configuration only.

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| csharp | pass | pass | pass | `Directory.Build.props` carries the analyzer set and `TreatWarningsAsErrors`, `Directory.Packages.props` centralizes versions, `.editorconfig` carries the shared `[*.cs]` block |
| console | pass | pass | pass | System.CommandLine console (`PhotoCleaner/PhotoCleaner.csproj`, `OutputType=Exe`, net10.0), `build-executable-task.yml` aggregates per-runtime output to `release-asset-<branch>-*`, gated `!smoke` |
| docker | pass | pass | pass | `Docker/Dockerfile` multi-arch, `Docker/README.md` present, `build-docker-task.yml` uses a registry layer cache (`buildcache-<branch>`), the image re-pushes on publish |
| branch-model | pass | pass | pass | `repo-config/configure.sh check ptr727/PhotoCleaner release` at hub `0e84805`: both rulesets and every general setting match, nothing to apply |
| repo-setup | pass | pass | pass | Every name in `spec/secrets.json` present in both the Actions and Dependabot stores (`CODECOV_TOKEN` included), no forbidden name |
| linter-parity | pass | pass | pass | `validate-task.yml` runs csharpier check, `dotnet format style --verify-no-changes`, markdownlint, cspell, actionlint, editorconfig-checker, and the hub prose gate. One config per linter |
| recurring-violations | pass | pass | pass | `prose_lint.py --diff origin/develop` clean on #50, `repo_gate.py` clean on `develop` (the `eol-coverage` forward-declaration gap closed in #50) |
| readme-structure | fail | pass | drift | At `main`: no `3rd Party Tools` section, four reference-name letters, an `Internal` link group. All fixed on `develop` in #49 |
| workflow (WORKFLOW.md 5A/5B) | pass | pass | pass | `build-release-task.yml` carries `validate-release` and the re-vendored `github-release` job (interface contract satisfied by name), the two hub-only workflows are `investigate` in the ledger |
| agent-instruction-set | fail | pass | drift | At `main`: `Fleet Bootstrap` and `Hub-Hosted Tooling` absent, 15 verbatim sections stale, `AGENTS.md`/`GOVERNANCE.md`/`CODESTYLE.md`/`WORKFLOW.md`/`copilot-instructions.md` intent copies trail the hub. All re-vendored or reconciled on `develop` in #49 and #50, where the audit reports no instruction-set finding |

nuget, pypi, python: N/A (no packaging, no Python).

## Defects (most severe first)

None on `develop`. On `main`, the readme-structure and agent-instruction-set rows above are letter misses whose intent holds (the content exists and is correct on `develop`), so they are drift awaiting promotion rather than defects.

## Drift Findings

- Every finding the `main` audit reports is closed on `develop` by #49 or #50, and the promotion is #51.
- `publish-release.yml` and `validate-task.yml` are `hub-only` at `investigate` in `spec/divergences.json`, a fleet-wide question rather than this repo's.

## Convergence in Flight

- [#50](https://github.com/ptr727/PhotoCleaner/pull/50) merged to `develop` at `e8b7a81`: re-vendors the 10 stale verbatim sections, reconciles the intent carries against the hub's history since each last synced (with the repo's own adaptations kept, per the carried-instruction-file-guard probe), claims `CODECOV_TOKEN` in both stores, and clears the `eol-coverage` gap.
- [#51](https://github.com/ptr727/PhotoCleaner/pull/51) `develop -> main` promotion, open, blocked on the hub prose gate ([#721](https://github.com/ptr727/ProjectTemplate/issues/721)).

## Proposed Registry / Spec Updates

- The `driftNotes` are unchanged. The first describes the publish shape (multi-arch Docker plus a github-release 7z, two-phase release), which still holds. The second records the private-for-now decision, which still holds (`isPrivate: true` at the time of this run) and still means the GitHub-sourced shields render broken.

## Escalations

Two hub findings from this pass, filed rather than patched per repo:

1. [#721](https://github.com/ptr727/ProjectTemplate/issues/721): `prose_lint.py dead-path` cannot recognize a hub-hosted path in a repo that retired the file, so verbatim text naming `repo-config/configure.sh` fails a downstream promotion gate.
2. [#722](https://github.com/ptr727/ProjectTemplate/issues/722): the hub's `.github/copilot-instructions.md` links `GOVERNANCE.md#every-finding-ends-in-an-action`, an anchor that left `GOVERNANCE.md` when PR Review Etiquette was packaged as a Skill. PhotoCleaner re-pointed its copy in #50 after Copilot raised it.
