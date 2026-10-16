# Audit: VSCode-Server-DotNetCore

- **Audited branch:** main (`e5718224c45eb598356d50a47be9df9566d79e7b`)
- **Types:** docker (from registry)
- **Verdict:** operational
- **Date:** 2026-10-16

Basis: `python3 spec/audit.py VSCode-Server-DotNetCore` run 2026-10-16T06:40:34Z at hub `ddaeefb4`, reporting 0 defect, letter, or error findings, plus a read of the `main` tree. This report replaces the 2026-07-04 report, which described the repository before its resync to hub 2.0.685 (ptr727/VSCode-Server-DotNetCore#127 through #131) and the second resync to hub 2.0.972 (ptr727/VSCode-Server-DotNetCore#153).

## Develop Drift

`develop` vs `main`: ahead 2, behind 78 (`gh api repos/ptr727/VSCode-Server-DotNetCore/compare/main...develop` -> `status: diverged`). Classified by content, the two trees are identical (a recursive diff of the `main` and `develop` tarballs reports no difference). The two `develop` commits are the Dependabot bumps ptr727/VSCode-Server-DotNetCore#157 and #159, whose same-titled twins ptr727/VSCode-Server-DotNetCore#158 and #160 reached `main`. Benign, inherent to merge-commit promotions. Not a drift finding.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| csharp | - | - | N/A | no `*.csproj`, `*.slnx`, or `*.cs` in the tree; the image installs the .NET SDKs and ships no in-repo source |
| nuget | - | - | N/A | `enable_nuget: false` (`publish-release.yml`, publish job `with:`) |
| pypi | - | - | N/A | no Python project; `enable_pypi: false` |
| python | - | - | N/A | no `pyproject.toml` or `setup.py` |
| dotnet-publish | - | - | N/A | `enable_dotnet_publish: false` in both callers |
| hugo | - | - | N/A | no site |
| docker | pass | pass | operational | the image is built by the hub's `build-release-task.yml` with `enable_docker: true` and `docker_image: ptr727/vscode-server-dotnetcore` (`publish-release.yml:41-59`); the Hub overview is published by the hub's `publish-docker-readme-task.yml` (`publish-release.yml:66`), main-only per the job comment, and no local copy of either task remains (`.github/workflows/` holds three files). Static `DOCKER_HUB_*` secrets, expected for docker |
| branch-model | pass | pass | operational | rulesets `develop` and `main` both `enforcement: active` (`gh api repos/ptr727/VSCode-Server-DotNetCore/rulesets`); the required context `Check pull request workflow status job` matches the aggregator `name:` (`test-pull-request.yml`); the stale `repo-config/ruleset-*.json` copies are gone |
| carried-scope | pass | pass | operational | the `spec/audit.py` run reports no `scope` finding |
| repo-setup | pass | pass | operational | the actions and dependabot secret stores both hold `DOCKER_HUB_USERNAME`, `DOCKER_HUB_ACCESS_TOKEN`, `CODEGEN_APP_CLIENT_ID`, and `CODEGEN_APP_PRIVATE_KEY`; no forbidden `NUGET_API_KEY` or `CODEGEN_APP_ID` |
| runtime-secrets | pass | pass | operational | every secret the callers map (`publish-release.yml:46-47,70-71`, `test-pull-request.yml` smoke job) exists in the actions store |
| verbatim-tree | drift | pass | drift | the carried instruction files and skills tree match a past hub revision, not the current canonical (`spec/audit.py`: `verbatim` and `tree` findings for `GOVERNANCE.md`, `.markdownlint-cli2.jsonc`, and 16 `.github/skills/` files), because the hub advanced after the 2.0.972 resync. Re-vendor from hub `main` |
| linter-parity | pass | pass | operational | one `.markdownlint-cli2.jsonc` and `cspell.json` drive editor and CI, and the hub's `validate-task.yml` runs in both `test-pull-request.yml` and `publish-release.yml:33` |
| recurring-violations | pass | pass | operational | `.editorconfig:21` sets the global `end_of_line = lf` default; no committed rulesets or mixed-ending workflow remain; `Docker/Dockerfile` is the path the hub Docker core expects |
| readme-structure | pass | pass | operational | `README.md` carries `## Build and Distribution` (`:7`), `## Table of Contents` (`:35`), `## Installation`, `## Usage`, `## Questions or Issues`, `## 3rd Party Tools`, and `## License` last (`:90`) |
| workflow (WORKFLOW.md 5A/5B) | pass | pass | operational | the three workflows are thin callers pinned to hub 2.0.1004 (`098016c8`): `test-pull-request.yml` calls `validate-task.yml` and `build-release-task.yml` with `smoke: true`, `github: false`, `dockerhub: false`, and an always-run aggregator; `publish-release.yml` calls `publish-plan-task.yml`, `validate-task.yml`, `build-release-task.yml`, and `publish-docker-readme-task.yml`, with the ref-keyed concurrency group (`:10-11`) and a weekly schedule (`:8`); `merge-bot-pull-request.yml` calls the hub merge-bot task. The five retired task workflows, the bespoke release task among them, are gone |

## Defects (most severe first)

None. The `spec/audit.py` run reports 0 defect, letter, or error findings.

## Drift Findings

- Carried content trails the hub (evidence: the `spec/audit.py` run named under Basis, whose `verbatim`, `tree`, and `intent` lines name each file and the later hub commit; the carried `GOVERNANCE.md` sections sit at `GOVERNANCE.md:27` Repository Boundaries and Write Safety, `:174` Verification Discipline, `:248` Workflow YAML Conventions, and `:279` Editor and Tasks): `GOVERNANCE.md` (four sections), `CODESTYLE.md`, `WORKFLOW.md`, `.github/copilot-instructions.md`, `.editorconfig`, `.editorconfig-checker.json`, `.gitattributes`, `.markdownlint-cli2.jsonc`, `cspell.json`, `version.json`, and 16 files under `.github/skills/` match a past hub revision or predate a later hub change. The resync landed at hub 2.0.972 and the hub has moved since. The next resync closes it, and it is that repository's work.
- The earlier report's structural findings (single-branch publisher, no paths-filter, mixed line endings, off-baseline `repo-config/` names, reshaped README, bespoke release task) are resolved by the resync and no longer apply.

## Proposed Registry / Spec Updates

- The `driftNotes` entry "Separate publish-docker-readme-task for the Hub overview." is stale, since the repository calls the hub's task and keeps no local copy. Deleted in the same change as this report.
