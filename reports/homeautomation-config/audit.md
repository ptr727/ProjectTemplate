# Audit: HomeAutomation-Config

- **Audited branch:** main (`5a7b1b1`), the merge of the `develop` promotion ptr727/HomeAutomation-Config#435 (`develop` at `9321ceb`)
- **Types:** source-only (from registry), release workflow model
- **Verdict:** not operational, on two `linter-parity` defects, both hand-judged. Every mechanized check is clean
- **Date:** 2026-09-24
- **Run stamp:** `audit run 2026-09-24T20:41:24Z | hub 2c5802f`

Second full run, replacing the 2026-08-15 report in whole. That report graded the repository on the operational workflow model. The registry moved it to the release model on 2026-09-24 (hub #1766, promoted in hub #1768), the repository described itself that way in ptr727/HomeAutomation-Config#431 (promoted in ptr727/HomeAutomation-Config#432), and it was resynced against the hub the same day in ptr727/HomeAutomation-Config#434, promoted to `main` in ptr727/HomeAutomation-Config#435. No release was dispatched after the promotion, so the newest GitHub release predates the reclassification. This run grades `main` at that promotion merge, and it is the first run of this repository against the `release` rulesets, the release trigger shape, and the `parity.hooks` check, which landed on 2026-08-23 after the previous report.

The mechanical run reports five advisories and no defect, letter, or error finding:

```text
audit run 2026-09-24T20:41:24Z | hub 2c5802f
== HomeAutomation-Config (source-only; release) @ main@5a7b1b1 ==
  DRIFT  intent: .editorconfig ... hub canonical changed later at 2026-09-23 (f747d1e)
  DRIFT  intent: .editorconfig-checker.json ... (f747d1e)
  DRIFT  intent: .gitattributes ... (f747d1e)
  DRIFT  intent: version.json ... hub canonical changed later at 2026-09-18 (5de7af3)
  DRIFT  hub-only: PSScriptAnalyzerSettings.psd1 is undeclared in spec/files.json and this repo carries it
1 repo(s) audited; 0 defect/letter/error finding(s).
```

The two defects below are both outside what the runner evaluates, which is why a clean run and a not-operational verdict coexist here.

## Develop Drift

`develop` vs `main`: ahead 0, behind 16. `git diff origin/develop origin/main` is empty, so the trees are identical and the 16 commits are promotion-merge ancestry, the benign artifact a merge-commit promotion always leaves. No drift finding: `main` carries no content `develop` lacks.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| source-only (`sourceonly.release.tagonly`) | pass | pass | pass | `.github/workflows/publish-release.yml:35-53` reaches the hub `build-release-task.yml` (pinned at `d28ed04`, 2.0.657) with `github: true`, every `enable_*` input false, and `expect_release_assets: false`, so the release is the tag, the source archive, README, and LICENSE |
| source-only (`sourceonly.nbgv.retained`) | pass | pass | pass | `version.json:1-10` is retained and byte-identical to the hub canonical. The pinned release task computes the version once through `get-version-task.yml`, the fleet's single NBGV run |
| branch-model | pass | pass | pass | `repo-config/configure.sh check ptr727/HomeAutomation-Config release` from hub `main` reported "Configuration matches" on 2026-09-24: both rulesets match `repo-config/main.json` and the PR-gated `repo-config/develop.json`, whose required context `Check pull request workflow status job` (`repo-config/main.json:42`, `repo-config/develop.json:45`) is the aggregator's own name at `.github/workflows/test-pull-request.yml:28`. `branch.operational.lintci` and `branch.operational.prtriggers` are N/A on the release model |
| carried-scope | drift | pass | drift | One `hub-only` hit, `PSScriptAnalyzerSettings.psd1`. It is the repository's own content at a path the hub also uses: its exclusion comments name the `host-install/windows/` scripts (`PSScriptAnalyzerSettings.psd1:3-8`) and it differs from the hub's copy. No `spec/divergences.json` entry triages it yet, tracked as hub #1718. See Drift Findings |
| repo-setup | pass | pass | pass | Both the Actions and the Dependabot store hold exactly the two baseline App-token names `spec/secrets.json` requires, no forbidden or unclaimed name, matching the empty registry `requiredSecrets` for a `github-release` target with mechanism `none`. The merge-bot passes both by name (`.github/workflows/merge-bot-pull-request.yml:23-25`) |
| runtime-secrets | pass | pass | pass | The directory is `.secrets/`, every tracked entry is a `<name>.example` plus `README.md`, `.gitignore:356-358` un-ignores exactly those, and every example is cataloged in `.secrets/README.md` |
| linter-parity | defect | defect | defect | Two defects, see Defects. The root configs are single and CI-driven: `.markdownlint-cli2.jsonc`, `cspell.json`, `.editorconfig` with `.editorconfig-checker.json`, `.github/actionlint.yaml`, and `PSScriptAnalyzerSettings.psd1`, all run by the hub `validate-task.yml` the stub calls at `.github/workflows/test-pull-request.yml:19-23`. `CloudInit/` runs ruff, ruff format, pyright, and pytest through the repository hook at `.github/actions/validate/action.yml:13-41`. `Notify/` is gated nowhere in CI, and the tree carries no local hook |
| recurring-violations | drift | pass | drift | LF throughout, paired defaults at `.editorconfig:19-21` and `.gitattributes:3`, CRLF only for `*.bat`/`*.cmd` (`.editorconfig:40-41`, `.gitattributes:6-7`), and `cspell.json:3` sets `en-US`. The CI prose gate is diff-scoped and green on ptr727/HomeAutomation-Config#434 and ptr727/HomeAutomation-Config#435. A whole-tree run finds a legacy backlog of 429 findings in 30 files, all of them in the repository's own files and none in carried content. Enumerated below |
| readme-structure | pass | pass | pass | The mechanical run reports no README letter. Build and Distribution carries Build Status, Releases, and Release Notes (`README.md:5-28`), License is last (`README.md:1284`), and the tagline at `README.md:3` is one link-free sentence mirrored exactly by the GitHub About description and `HISTORY.md:3` |
| agent-instruction-set | pass | pass | pass | The mechanical run reports no verbatim-section, verbatim-tree, or template-reference finding after ptr727/HomeAutomation-Config#434. `AGENTS.md` carries exactly the three declared sections (`AGENTS.md:9,31,76`), and `.github/copilot-instructions.md:35` carries the Disproved Claims heading with the canonical rules and no repository entries |
| workflow (WORKFLOW.md 5A/5B) | pass | pass | pass | Enumerated below. One least-privilege gap that no D-guarantee names is recorded as a drift finding rather than graded here |

`csharp`, `nuget`, `pypi`, `console`, `docker`, `dotnet-publish`, `hugo`: N/A, since the repository ships no package, image, or site. `python`: N/A by the registry, which declares `source-only` alone, although the tree carries two Python projects (`CloudInit/pyproject.toml`, `Notify/pyproject.toml`). That classification question is raised under Proposed Registry / Spec Updates rather than resolved here.

## Workflow Assertions (5A and 5B)

- **D1, pull request gate.** `.github/workflows/test-pull-request.yml:7-10` triggers on `pull_request` into `main` and `develop` plus `workflow_dispatch`, with no `push` trigger, which is the release-model no-build shape now that every change reaches `develop` through a pull request. One always-run aggregator (`:27-39`) fails unless `validate` succeeded, and its name is the ruleset-bound context.
- **D4, gated publish.** `.github/workflows/publish-release.yml:3-4` is `workflow_dispatch` only, matching `releaseTrigger: dispatch-only`. The `plan` job (`:16-22`) decides once through the pinned `publish-plan-task.yml`, the same validation gate the pull request runs precedes the publish (`:25-31`), and the publish gates on both (`:38`) with `ref: ${{ github.sha }}` pinning the dispatch-time commit (`:44`). Trace S7 predicts `main` -> `X.Y.Z` stable and `develop` -> a `-g<sha>` prerelease, with no artifact to reap on a source-only chain.
- **D7.1, serialization.** The publisher uses the global group `${{ github.workflow }}` with `cancel-in-progress: false` (`publish-release.yml:8-10`).
- **D7.2, permissions at the entry point.** The caller grants `contents: write` and `actions: write` at the one job reaching the release task (`publish-release.yml:40-42`) and `contents: read` at each `validate` job.
- **D8.1, merge-bot.** `.github/workflows/merge-bot-pull-request.yml` is the thin caller of the hub `merge-bot-task.yml` on `pull_request_target`, concurrency keyed on the PR number (`:11-13`), `permissions: {}` (`:16`).
- **D8.2, Dependabot.** `.github/dependabot.yml:8-26` declares `github-actions` for `main` and `develop`, the whole implied set, since no `.devcontainer` is present and the two Python trees are not Dependabot-managed. The repository is private, so the self-hosted-runner routing check applies: the newest Dependabot run (2026-09-24) ran on a GitHub-hosted `ubuntu-latest` runner, executed its steps, and succeeded.
- **Repository-owned workflow.** `.github/workflows/test-homelab-runner.yml` is dispatch-only, read-only (`:6-7`), and gated to the owner's login (`:17`), exercising the self-hosted runner whose labels `.github/actionlint.yaml` declares, a path collision `spec/divergences.json` already accepts. It touches no D-guarantee.

## Defects (most severe first)

1. **`Notify/` has no CI gate, and the repository hook states that it is the whole Python gate.** `Notify/pyproject.toml:4-16` configures ruff and strict mypy, and `Notify/tests/test_render_report.py` is a unittest suite, all documented as local commands at `OPERATIONS.md:13`. The hub `validate-task.yml` at the pinned `d28ed04` detects Python only through a root-level `hashFiles('pyproject.toml')`, so every hub Python step skips here, and the repository hook runs `CloudInit/` alone (`.github/actions/validate/action.yml:18-41`) while its header says "This hook is the whole Python gate for this repository" (`.github/actions/validate/action.yml:4`). Input: a change to `Notify/render_report.py` that breaks ruff, mypy, or its tests. Observed: CI green. Expected per `parity.lang`: the tree's configs shared by editor, CLI, and CI. Letter and intent both miss. Tracked downstream as ptr727/HomeAutomation-Config#368, and the hub-side root-relative detection is part of hub #1134.
2. **No local hook is wired (`parity.hooks`).** The tracked tree on `main` carries neither `.husky/pre-commit` nor `.pre-commit-config.yaml`, and no other file invokes the diff-scoped prose gate or the eol check through `hub-fetch-run.py`. AUDIT.md section 4 grades a repository with none wired as a defect, and the repository's own carried `CODESTYLE.md:21` states the gate is not the repository's free choice to skip. Tracked downstream as ptr727/HomeAutomation-Config#436.

## Drift Findings

- **The four intent advisories are date-only.** `.editorconfig`, `.editorconfig-checker.json`, `.gitattributes`, and `version.json` are each byte-identical to the hub's own copy at `2c5802f`, so there is nothing to carry. The advisory does not compare against that copy. It measures each file against its `intentRef` document (`spec/files.json:18-25`), `GOVERNANCE.md` for the first three and `WORKFLOW.md` for `version.json`, and it fires because that document changed after the repository's last commit on the file. A config file never matches the document it is measured against, so the advisory's identity skip cannot clear it, and the only other way to clear it is a commit to a file that needs no change. That is hub #727, so these four advisories recur on every run until it lands and are not a repository finding.
- **`PSScriptAnalyzerSettings.psd1` is an untriaged path collision.** The file is the repository's own PSScriptAnalyzer configuration for its Windows host scripts (`PSScriptAnalyzerSettings.psd1:3-8`), which the hub gate reads from the root, so the `hub-only` hit is a collision rather than a carry, and deleting it would change what the gate enforces. The remedy is an `accepted` entry in `spec/divergences.json`, the shape `.github/actionlint.yaml` and `.github/actions/validate/action.yml` already have for this repository. Tracked as hub #1718.
- **Two workflows leave the token grant to the repository default, and that default is write.** Neither `.github/workflows/test-pull-request.yml` nor `.github/workflows/publish-release.yml` declares a workflow-level `permissions:` block, where the hub's reference stubs declare `permissions: {}` (`catalog/snippets/workflows/test-pull-request.yml:17`, `catalog/snippets/workflows/publish-release.yml:22`). The aggregator job (`test-pull-request.yml:27-39`) and the `plan` job (`publish-release.yml:16-22`) therefore run under the repository's default workflow token, which the Actions settings API reads as `write` with pull request approval allowed. No D-guarantee names a workflow-level block, so this is a letter miss against the reference rather than a contract defect. The fix is `permissions: {}` at the top of both files, tracked downstream as ptr727/HomeAutomation-Config#437, and the setting half is escalated below.
- **Whole-tree prose backlog, fixed as each file is next edited.** 429 findings from the hub `scripts/prose_lint.py` at `2c5802f` over a `main` export, 30 files, all repository-owned: `comment-wrap` 208, `charset` 81, `comment-case` 63, `semicolon` 35, `dash` 23, `home-path` 17, `dupword` 2. The heaviest files are `PhotoCleaner/PhotoCleaner.sh` (129), `Hailo/install-hailo.sh` (59), `Docker/stacks/immich/immich-heic-decode-failures.md` (47), and `README.md` (32). The `home-path` hits sit at `ARCHITECTURE.md:71`, `Docker/stacks/automation/compose.yml:313`, `Docker/stacks/immich/immich-heic-decode-failures.md:347`, `HARDWARE.md:620-624,969-970`, and `README.md:272,1016,1379-1380`, and are the ones worth reading first, since each is an absolute home path committed to the tree. The diff-scoped CI gate keeps the backlog from growing.
- **The repository records its own open question about `version.json`.** `TODO.md:5` notes that the release floor `2.0` is the hub's rather than one the repository chose, and that a repository shipping no package does not keep `nugetPackageVersion`. The carried file is byte-identical to the hub canonical, so this is the repository's own pending decision rather than drift, recorded so a reader does not mistake the byte match for a settled choice.

## Convergence in Flight

- ptr727/HomeAutomation-Config#431 (merged into `develop` at `03e9c0e`) and ptr727/HomeAutomation-Config#432 (merged to `main` at `70f223b`): the repository's own description of the release model, following the registry change.
- ptr727/HomeAutomation-Config#434 (merged into `develop` at `9321ceb`): the resync of carried fleet content against the hub, 10 files, reviewed by Copilot with the required aggregator green.
- ptr727/HomeAutomation-Config#435 (merged as `5a7b1b1`): the promotion this run reads, reviewed by Copilot with CI green. CodeRabbit runs on the Free plan here and posts a walkthrough only, so Copilot plus CI is the review gate.
- ptr727/HomeAutomation-Config#368 (open): the `Notify/` CI gate, defect 1.
- ptr727/HomeAutomation-Config#436 (open): the local hook, defect 2.
- ptr727/HomeAutomation-Config#437 (open): `permissions: {}` in the two callers.
- ptr727/HomeAutomation-Config#438 (open): the tracking issue enumerating every residual delta from this run.

## Proposed Registry / Spec Updates

- **`spec/divergences.json`: add an `accepted` path-collision entry for `PSScriptAnalyzerSettings.psd1`**, per hub #1718. Not applied here.
- **`spec/divergences.json`: drop HomeAutomation-Config from the `.editorconfig-checker.json` entry.** Its reason says this repository carries a repo-specific `Exclude` list for a `Vantage/` subtree, but the repository's copy is now byte-identical to the canonical and the registry records that subtree as stripped, so the entry's claim about this repository describes finished work. The entry's other repository is outside this run. Not applied here.
- **Registry: decide whether this entry declares `python`.** The tree carries a build-profile project (`CloudInit/pyproject.toml` with `CloudInit/uv.lock`, src layout, and tests) and a lint-only one (`Notify/pyproject.toml`), and a sibling (ESPHome-Config) already declares `python` with a per-type profile. Declaring it would bring the `python` checks into scope and make defect 1 a type finding as well. At the build profile it would also make `CODECOV_TOKEN` required, since `CloudInit/` carries tests, so this is the maintainer's call rather than an edit this run makes.
- **Registry `driftNotes`: no change.** The three notes describe current facts, none asserts outstanding work, and none names a check id.
- **Conformance matrix:** the `operational` row already records this repository's departure. No row covers `source-only` + `release` without a language, so this repository becomes a candidate reference for that shape once it is operational. Not applied here.

## Escalations

Raised rather than resolved, per AUDIT.md section 9.

1. **`repo-config/settings.json` declares no default workflow token permission, so `configure.sh check` cannot see it.** The setting differs across the fleet (read on the hub and on Blog, write here and on PlexCleaner), and a stub that omits `permissions: {}` inherits whichever it is. Either the setting joins the declared settings, or the workflow-level block becomes a named check. Filed as hub #1783.
2. **The four byte-identical intent advisories cannot be cleared**, which is hub #727, still open. Corroborated rather than opened.
3. **The hub `validate-task.yml` detects Python at the root only**, so any repository with a nested Python tree needs a hook, and a hook that covers one tree silently leaves the next one ungated. That is part of hub #1134, still open. Corroborated rather than opened.
