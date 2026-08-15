# Audit: HomeAutomation-Config

- **Audited branch:** main (`e10a2cf`), with the convergence in flight read at `develop` (`c8252c7`)
- **Types:** source-only (from registry), operational workflow model
- **Verdict:** main not operational (stale carried content), `develop` operational on every mechanized check
- **Date:** 2026-08-15
- **Run stamps:** `audit run 2026-08-15T14:58:02Z | hub d54862a` (main) and `audit run 2026-08-15T14:58:25Z | hub d54862a | branch override develop`

First committed report for this repository. It was resynced against the hub's `main` (`0e84805`) on this date per `RESYNC.md`, the resync landed on `develop` as ptr727/HomeAutomation-Config#51, and the promotion to `main` is open as ptr727/HomeAutomation-Config#52. So `main` at the time of writing still reads the pre-resync findings, and `develop` is the state `main` takes when that promotion merges. This report grades the two apart rather than letting either stand for the other.

## Develop Drift

`develop` vs `main`: `develop` is ahead by 15 content commits since the last promotion (#36), and `main` carries no content `develop` lacks (the trial `git merge-tree` of the two is conflict-free). The commit-count gap in the other direction is the promotion-merge ancestry artifact and is **benign**. The content drift is real and is the resync itself, closed by the promotion.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| source-only (`sourceonly.release.tagonly`) | pass | pass | pass | `.github/workflows/publish-release.yml:61-69` inlines `softprops/action-gh-release` (SHA-pinned) with `LICENSE` and `README.md` as the only files, so the release is the tag, GitHub's source archive, README, and LICENSE. No `build-*-task.yml` and no `expect_release_assets` |
| source-only (`sourceonly.nbgv.retained`) | pass | pass | pass | `version.json` retained (hub byte form after #51), and `dotnet/nbgv@master` computes the tag inline at `publish-release.yml:55`, the same unpinned form the hub's own publisher and catalog snippet use by design |
| branch-model | pass | pass | pass | Both rulesets live and matching the carried payloads by normalized diff (`repo-config/main.json`, `repo-config/operational/develop.json`), confirmed by `repo-config/configure.sh check ptr727/HomeAutomation-Config operational` from this checkout and by the repository's own `AUDIT.md` snippets, all reporting in sync |
| carried-scope | pass | pass | pass | `repo-config/configure.sh` deleted in #40, no `retire`-dispositioned hub path remains. Two `investigate`-dispositioned hits stand (`.github/workflows/publish-release.yml`, `validate-task.yml`), which is a hub triage rather than a repository finding, see Escalations |
| repo-setup | pass | pass | pass | `spec/secrets.json` carries `baseline` plus `note` only, the shape `docs/repo-config-carry.md` states for a source-only repository whose only publish target maps to no mechanism (the dead `targetMechanisms` map dropped in #51). `configure.sh check` reports the Dependabot security features enabled |
| linter-parity | pass | pass | pass | `.github/workflows/validate-task.yml:30-50` runs markdownlint-cli2, cspell (README and HISTORY scope), actionlint, and editorconfig-checker as pinned action wrappers or Docker, one config per linter at the root. `test-pull-request.yml:31` carries the ruleset-bound `Check pull request workflow status job` |
| recurring-violations | pass | pass | pass | Hub prose lint over the resync diff is clean. LF throughout, per the registry `lineEndings: lf` and the repository's `.editorconfig`. Three pre-existing whole-tree `dead-path` mentions remain (`GOVERNANCE.md:29`, `GOVERNANCE.md:282`, `OPERATIONS.md:306`), the first two inside carried text and all three left for the files' next edit per the fix-as-edited policy |
| readme-structure | pass on `develop` | pass | pass | The seven README letters the `main` run reports (license shield placement, `-link` suffixes, `github-link` naming, group membership) are all closed on `develop` since #40, where the run reports none |
| agent-instruction-set | pass on `develop` | pass | pass | Every `AGENTS.md` and `GOVERNANCE.md` verbatim section matches the current canonical after #51. Before the re-vendor, each differing line in every stale region was traced to a past hub commit, so no repository-local rule sat inside a verbatim region. `CODESTYLE.md` carries the hub's skill-pointer form with the repository's own shell rules kept as a subsection of Shell. `.github/copilot-instructions.md` carries the current runbook with an empty Disproved Claims ledger |
| workflow (WORKFLOW.md 5A/5B) | pass | pass | pass | Operational model: `test-pull-request.yml:9-12` runs on `push` to `develop` (advisory) and `pull_request` into `main` (the enforced gate). `publish-release.yml:4` is `workflow_dispatch` only, matching `releaseTrigger: dispatch-only`. `merge-bot-pull-request.yml` uses `actions/create-github-app-token` (SHA-pinned) with the client-id input, matching the baseline mechanism note. `dependabot.yml` declares `github-actions` for both `main` and `develop` |

csharp, nuget, pypi, python, console, docker, hugo: N/A (no packaging, no application code, no site).

## Defects (most severe first)

None on `develop`. On `main`, the whole class of stale carried content (2 `AGENTS.md` and 13 `GOVERNANCE.md` verbatim sections, `.markdownlint-cli2.jsonc`, the carried `configure.sh`, and the README letters) is what #40, #43, #50, and #51 already closed, and it is a defect only until #52 merges.

## Drift Findings

- `.editorconfig` and `.gitattributes` carry an intent advisory on both branches. Both are the repository's LF adaptation (registry `lineEndings: lf`), and every hub change since the copies' last commit is CRLF-model or hub-tree specific (Python and Dockerfile pins this tree has no files for). Judged current by meaning, and the advisory will keep firing until the hub's `.editorconfig` stops moving, which is a property of the intent tier rather than of this repository.
- `.github/workflows/publish-release.yml` and `.github/workflows/validate-task.yml` report `hub-only` with an `investigate` disposition in `spec/divergences.json`. Both are the repository's own interface workflows honoring the named contract, and neither can be acted on until the hub settles the fidelity and `appliesTo` call the ledger records as pending.

## Convergence in Flight

- ptr727/HomeAutomation-Config#51 (merged into `develop` at `c8252c7`): the resync, one commit per drift class, driven to a Copilot review on its head, one finding fixed and resolved. Coverage read 9 of 11 files on both rounds, which the maintainer accepted at merge.
- ptr727/HomeAutomation-Config#52 (open): the `develop` to `main` promotion. Copilot reviewed the head, one finding deferred to ptr727/HomeAutomation-Config#53 (Duplicacy credentials on a command line in `Duplicacy/init-storage.sh`, content from #45 rather than from the resync), thread resolved, checks 6/6, coverage 72 of 76 files, left for the maintainer to merge.

## Proposed Registry / Spec Updates

- Delete the third `driftNote` ("README self-flags previously-committed secrets"): the README no longer mentions secrets, and #46 and #49 moved every real secret out of the checkout to `~/.secrets/`, so the note describes finished work. Applied in the same change.
- Fill the `operational` config row of `reports/conformance-matrix.md` with this repository as its reference, since it is now the first operational repository with a committed report. Applied in the same change.

## Escalations

Raised rather than resolved, per AUDIT.md section 9. Each was already on file from a sibling resync the same day, so this run corroborates rather than opens them.

1. **The hub's canonical `.github/copilot-instructions.md` names `ptr727/ProjectTemplate` in intent-fidelity prose**, in "A Shape Nothing Recognizes Blocks the Loop and Earns an Issue" ("File an issue on the hub, `ptr727/ProjectTemplate`"). The template-reference scan exempts only verbatim sections and the hub itself, so every downstream carry of the current canonical trips `carried:` on its next audit. #51 reworded the line to name the hub through `AGENTS.md` "Fleet Bootstrap", and PlexCleaner's carry holds the same line. The fix belongs in the canonical wording. Tracked as #720.
2. **`spec/divergences.json` still dispositions `publish-release.yml` and `validate-task.yml` as `investigate` with no tracking**, so every repository carrying either reports two `hub-only` findings that no repository can act on. The fidelity and `appliesTo` decision the ledger records as pending is the remedy. Tracked as #669.
3. **The `dead-path` prose check flags a carried mention of the hub-hosted `repo-config/configure.sh`** (`GOVERNANCE.md` "Repository Boundaries and Write Safety" is verbatim carried text) in any repository that has deleted its copy, which is now every converged repository. The mention is a pointer to a hub-hosted file, the shape "Documentation Style Conventions" permits, so the check wants the same hub-hosted exemption the template-reference scan gained. Tracked as #721, where it has already failed a downstream promotion gate.
