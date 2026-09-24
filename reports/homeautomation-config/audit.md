# Audit: HomeAutomation-Config

- **Audited branch:** main (`9c16921`), the merge of the `develop` promotion ptr727/HomeAutomation-Config#441 (`develop` at `e8544a0`)
- **Types:** source-only (from registry), release workflow model
- **Verdict:** operational. No defect, mechanized or hand-judged. What remains is two drift findings tracked on the hub, the repository's own prose backlog, and one pending repository decision
- **Date:** 2026-09-24
- **Run stamp:** `audit run 2026-09-24T22:39:33Z | hub 03ed883`

Third full run, replacing the second run of the same day in whole. That run graded `main` at `5a7b1b1`, the promotion ptr727/HomeAutomation-Config#435, not operational on two hand-judged `linter-parity` defects, the ungated `Notify/` tree and the missing local hook, and recorded one drift finding on the workflow token grant. ptr727/HomeAutomation-Config#440 fixed all three and was promoted to `main` in ptr727/HomeAutomation-Config#441, with no release dispatched, by the maintainer's choice. This run grades `main` at that promotion merge. Everything the fix did not touch was re-read rather than carried forward: the settings check, the secrets, the develop drift, and the whole-tree prose backlog.

The mechanical run reports five advisories and no defect, letter, or error finding:

```text
audit run 2026-09-24T22:39:33Z | hub 03ed883
== HomeAutomation-Config (source-only; release) @ main@9c16921 ==
  DRIFT  intent: .editorconfig ... hub canonical changed later at 2026-09-23 (f747d1e)
  DRIFT  intent: .editorconfig-checker.json ... (f747d1e)
  DRIFT  intent: .gitattributes ... (f747d1e)
  DRIFT  intent: version.json ... hub canonical changed later at 2026-09-18 (5de7af3)
  DRIFT  hub-only: PSScriptAnalyzerSettings.psd1 is undeclared in spec/files.json and this repo carries it
1 repo(s) audited; 0 defect/letter/error finding(s).
```

The two checks the runner cannot evaluate, `parity.hooks` and the nested Python gate, were read by hand and both pass. See the `linter-parity` row.

## Develop Drift

`develop` vs `main`: ahead 0, behind 17. `git diff origin/develop origin/main` is empty, so the trees are identical and the 17 commits are promotion-merge ancestry, the benign artifact a merge-commit promotion always leaves. No drift finding: `main` carries no content `develop` lacks.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| source-only (`sourceonly.release.tagonly`) | pass | pass | pass | `.github/workflows/publish-release.yml:38-56` reaches the hub `build-release-task.yml` (pinned at `d28ed04`, 2.0.657) with `github: true`, every `enable_*` input false, and `expect_release_assets: false`, so the release is the tag, the source archive, README, and LICENSE |
| source-only (`sourceonly.nbgv.retained`) | pass | pass | pass | `version.json:1-10` is retained and byte-identical to the hub canonical. The pinned release task computes the version once through `get-version-task.yml`, the fleet's single NBGV run |
| branch-model | pass | pass | pass | `repo-config/configure.sh check ptr727/HomeAutomation-Config release` from hub `main` reported "Configuration matches" on 2026-09-24 at hub `03ed883`: both rulesets match `repo-config/main.json` and the PR-gated `repo-config/develop.json`, whose required context `Check pull request workflow status job` (`repo-config/main.json:42`, `repo-config/develop.json:45`) is the aggregator's own name at `.github/workflows/test-pull-request.yml:31`. `branch.operational.lintci` and `branch.operational.prtriggers` are N/A on the release model |
| carried-scope | drift | pass | drift | One `hub-only` hit, `PSScriptAnalyzerSettings.psd1`. It is the repository's own content at a path the hub also uses: its exclusion comments name the `host-install/windows/` scripts (`PSScriptAnalyzerSettings.psd1:3-8`) and it differs from the hub's copy. No `spec/divergences.json` entry triages it yet, tracked as hub #1718. See Drift Findings |
| repo-setup | pass | pass | pass | Both the Actions and the Dependabot store hold exactly the two baseline App-token names `spec/secrets.json` requires, no forbidden or unclaimed name, matching the empty registry `requiredSecrets` for a `github-release` target with mechanism `none`. The merge-bot passes both by name (`.github/workflows/merge-bot-pull-request.yml:23-25`) |
| runtime-secrets | pass | pass | pass | The directory is `.secrets/`, every tracked entry is a `<name>.example` plus `README.md`, `.gitignore:356-358` un-ignores exactly those, and every example is cataloged in `.secrets/README.md` |
| linter-parity | pass | pass | pass | The root configs are single and CI-driven: `.markdownlint-cli2.jsonc`, `cspell.json`, `.editorconfig` with `.editorconfig-checker.json`, `.github/actionlint.yaml`, and `PSScriptAnalyzerSettings.psd1`, all run by the hub `validate-task.yml` the stub calls at `.github/workflows/test-pull-request.yml:22-26`. Both nested Python trees are gated by the repository hook, which the hub's root-only detection makes necessary. `CloudInit/` runs ruff, ruff format, pyright and pytest through its `uv.lock` (`.github/actions/validate/action.yml:19-42`). `Notify/` runs ruff, ruff format, mypy and its unittest suite through `uvx` and `uv run --no-project` (`.github/actions/validate/action.yml:44-62`), and the run for ptr727/HomeAutomation-Config#440 executed all four and ran 19 tests. `parity.hooks`: `.pre-commit-config.yaml:38-47` runs the diff-scoped prose gate and the eol check through `hub-fetch-run.py`, byte-identical to `catalog/snippets/hub-fetch-run.py`, and `.pre-commit-config.yaml:8-37` adds both trees' lint, format and type checks, which pass clean over the whole tree |
| recurring-violations | drift | pass | drift | LF throughout, paired defaults at `.editorconfig:19-21` and `.gitattributes:3`, CRLF only for `*.bat`/`*.cmd` (`.editorconfig:40-41`, `.gitattributes:6-7`), and `cspell.json:3` sets `en-US`. The CI prose gate is diff-scoped and green on ptr727/HomeAutomation-Config#440 and ptr727/HomeAutomation-Config#441. A whole-tree run finds a legacy backlog of 429 findings in 30 files, all of them in the repository's own files and none in carried content. Enumerated below |
| readme-structure | pass | pass | pass | The mechanical run reports no README letter. Build and Distribution carries Build Status, Releases, and Release Notes (`README.md:5-28`), License is last (`README.md:1284`), and the tagline at `README.md:3` is one link-free sentence mirrored exactly by the GitHub About description and `HISTORY.md:3` |
| agent-instruction-set | pass | pass | pass | The mechanical run reports no verbatim-section, verbatim-tree, or template-reference finding after ptr727/HomeAutomation-Config#434. `AGENTS.md` carries exactly the three declared sections (`AGENTS.md:9,31,76`), and `.github/copilot-instructions.md:35` carries the Disproved Claims heading with the canonical rules and no repository entries |
| workflow (WORKFLOW.md 5A/5B) | pass | pass | pass | Enumerated below. Both entry points declare `permissions: {}` at workflow level |

`csharp`, `nuget`, `pypi`, `console`, `docker`, `dotnet-publish`, `hugo`: N/A, since the repository ships no package, image, or site. `python`: N/A by the registry, which declares `source-only` alone, although the tree carries two Python projects (`CloudInit/pyproject.toml`, `Notify/pyproject.toml`). The maintainer keeps it undeclared, as recorded under Proposed Registry / Spec Updates.

## Workflow Assertions (5A and 5B)

- **D1, pull request gate.** `.github/workflows/test-pull-request.yml:7-10` triggers on `pull_request` into `main` and `develop` plus `workflow_dispatch`, with no `push` trigger, which is the release-model no-build shape now that every change reaches `develop` through a pull request. One always-run aggregator (`:30-42`) fails unless `validate` succeeded, and its name is the ruleset-bound context.
- **D4, gated publish.** `.github/workflows/publish-release.yml:3-4` is `workflow_dispatch` only, matching `releaseTrigger: dispatch-only`. The `plan` job (`:19-25`) decides once through the pinned `publish-plan-task.yml`, the same validation gate the pull request runs precedes the publish (`:28-34`), and the publish gates on both (`:41`) with `ref: ${{ github.sha }}` pinning the dispatch-time commit (`:47`). Trace S7 predicts `main` -> `X.Y.Z` stable and `develop` -> a `-g<sha>` prerelease, with no artifact to reap on a source-only chain.
- **D7.1, serialization.** The publisher uses the global group `${{ github.workflow }}` with `cancel-in-progress: false` (`publish-release.yml:8-10`).
- **D7.2, permissions at the entry point.** Both callers declare `permissions: {}` at workflow level (`test-pull-request.yml:17`, `publish-release.yml:13`), so the aggregator and the `plan` job, which grant nothing of their own, get no token scope. The publisher grants `contents: write` and `actions: write` at the one job reaching the release task (`publish-release.yml:43-45`) and `contents: read` at each `validate` job. The pinned `publish-plan-task.yml` uses no token.
- **D8.1, merge-bot.** `.github/workflows/merge-bot-pull-request.yml` is the thin caller of the hub `merge-bot-task.yml` on `pull_request_target`, concurrency keyed on the PR number (`:11-13`), `permissions: {}` (`:16`).
- **D8.2, Dependabot.** `.github/dependabot.yml:8-26` declares `github-actions` for `main` and `develop`, the whole implied set, since no `.devcontainer` is present and the two Python trees are not Dependabot-managed. The repository is private, so the self-hosted-runner routing check applies: the newest Dependabot run (2026-09-24) ran on a GitHub-hosted `ubuntu-latest` runner, executed its steps, and succeeded.
- **Repository-owned workflow.** `.github/workflows/test-homelab-runner.yml` is dispatch-only, read-only (`:6-7`), and gated to the owner's login (`:17`), exercising the self-hosted runner whose labels `.github/actionlint.yaml` declares, a path collision `spec/divergences.json` already accepts. It touches no D-guarantee.

## Defects (most severe first)

None.

## Drift Findings

- **The four intent advisories are date-only.** `.editorconfig`, `.editorconfig-checker.json`, `.gitattributes`, and `version.json` are each byte-identical to the hub's own copy at `03ed883`, so there is nothing to carry. The advisory does not compare against that copy. It measures each file against its `intentRef` document (`spec/files.json:18-25`), `GOVERNANCE.md` for the first three and `WORKFLOW.md` for `version.json`, and it fires because that document changed after the repository's last commit on the file. A config file never matches the document it is measured against, so the advisory's identity skip cannot clear it, and the only other way to clear it is a commit to a file that needs no change. That is hub #727, so these four advisories recur on every run until it lands and are not a repository finding.
- **`PSScriptAnalyzerSettings.psd1` is an untriaged path collision.** The file is the repository's own PSScriptAnalyzer configuration for its Windows host scripts (`PSScriptAnalyzerSettings.psd1:3-8`), which the hub gate reads from the root, so the `hub-only` hit is a collision rather than a carry, and deleting it would change what the gate enforces. The remedy is an `accepted` entry in `spec/divergences.json`, the shape `.github/actionlint.yaml` and `.github/actions/validate/action.yml` already have for this repository. Tracked as hub #1718.
- **Whole-tree prose backlog, fixed as each file is next edited.** 429 findings from the hub `scripts/prose_lint.py` at `03ed883` over a `main` export, 30 files, all repository-owned and unchanged from the previous run: `comment-wrap` 208, `charset` 81, `comment-case` 63, `semicolon` 35, `dash` 23, `home-path` 17, `dupword` 2. The heaviest files are `PhotoCleaner/PhotoCleaner.sh` (129), `Hailo/install-hailo.sh` (59), `Docker/stacks/immich/immich-heic-decode-failures.md` (47), and `README.md` (32). The `home-path` hits sit at `ARCHITECTURE.md:71`, `Docker/stacks/automation/compose.yml:313`, `Docker/stacks/immich/immich-heic-decode-failures.md:347`, `HARDWARE.md:620-624,969-970`, and `README.md:272,1016,1379-1380`, and are the ones worth reading first, since each is an absolute home path committed to the tree. The diff-scoped CI gate keeps the backlog from growing.
- **The repository records its own open question about `version.json`.** `TODO.md:5` notes that the release floor `2.0` is the hub's rather than one the repository chose, and that a repository shipping no package does not keep `nugetPackageVersion`. The carried file is byte-identical to the hub canonical, so this is the repository's own pending decision rather than drift, recorded so a reader does not mistake the byte match for a settled choice.

## Convergence in Flight

- ptr727/HomeAutomation-Config#440 (merged into `develop` at `e8544a0`): the `Notify/` CI gate, the local hook, and `permissions: {}` in the two callers, closing ptr727/HomeAutomation-Config#368, ptr727/HomeAutomation-Config#436 and ptr727/HomeAutomation-Config#437. Reviewed by Copilot with the required aggregator green.
- ptr727/HomeAutomation-Config#441 (merged as `9c16921`): the promotion this run reads, reviewed by Copilot with CI green. Its two findings claimed `uvx` has no `--directory` option and were declined with a constructed case showing it does. CodeRabbit runs on the Free plan here and posts a walkthrough only, so Copilot plus CI is the review gate.
- ptr727/HomeAutomation-Config#438 (open): the tracking issue for the previous run's residual deltas. Its done condition is a re-audit grading `main` operational with the report re-committed on the hub, which this report meets, so it closes when this report merges. The hub-side drift above stays tracked on hub #1718 and hub #727.

## Proposed Registry / Spec Updates

- **`spec/divergences.json`: add an `accepted` path-collision entry for `PSScriptAnalyzerSettings.psd1`**, per hub #1718. Not applied here.
- **`spec/divergences.json`: drop HomeAutomation-Config from the `.editorconfig-checker.json` entry.** Its reason says this repository carries a repo-specific `Exclude` list for a `Vantage/` subtree, but the repository's copy is now byte-identical to the canonical and the registry records that subtree as stripped, so the entry's claim about this repository describes finished work. The entry's other repository is outside this run. Not applied here.
- **Registry: `python` stays undeclared, by the maintainer's decision** recorded on ptr727/HomeAutomation-Config#438. The entry keeps `source-only` alone, although the tree carries a build-profile project (`CloudInit/`) and a lint-only one (`Notify/`), and the repository hook gates both.
- **Registry `driftNotes`: no change.** The three notes describe current facts, none asserts outstanding work, and none names a check id.
- **Conformance matrix:** the `operational` row already records this repository's departure. No row covers `source-only` + `release` without a language, so this repository is a candidate reference for that shape. Not applied here.

## Escalations

Raised rather than resolved, per AUDIT.md section 9.

1. **`repo-config/settings.json` declares no default workflow token permission, so `configure.sh check` cannot see it.** The setting differs across the fleet (read on the hub and on Blog, write here and on PlexCleaner), and a stub that omits `permissions: {}` inherits whichever it is. Either the setting joins the declared settings, or the workflow-level block becomes a named check. Filed as hub #1783.
2. **The four byte-identical intent advisories cannot be cleared**, which is hub #727, still open. Corroborated rather than opened.
3. **The hub `validate-task.yml` detects Python at the root only**, so any repository with a nested Python tree needs a hook, and a hook that covers one tree silently leaves the next one ungated. That is part of hub #1134, still open. Corroborated rather than opened.
