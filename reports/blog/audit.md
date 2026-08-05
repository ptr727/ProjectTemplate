# Audit: Blog

- **Audited branch:** main (`6855ddb`)
- **Types:** source-only (from registry)
- **Verdict:** operational
- **Date:** 2026-08-03
- **Run stamp:** `audit run 2026-08-03T22:34:03Z | hub 3a7cc64`
- **Partly superseded 2026-08-05, re-run owed.** This run graded the repo as `source-only` against a hub with no static-site type. Since then the deploy has run, the `hugo` type and the `self-hosted` target landed, and the registry declares `types: ["hugo", "source-only"]`, so the **Types** line above and every `hugo.*` dimension this report does not have are stale rather than wrong. What the run observed is left as it was observed, per the run-stamp discipline: the annotation under Proposed Registry / Spec Updates records what has since been applied, and the next full run replaces this file rather than editing it.

First audit of this repo. It was stood up on 2026-08-01 and reached a proven release path the same day, but it was never added to [`registry/repos.json`][repos], so no hub tool had measured it until now and [`reports/divergences.md`][divergences] under-reported the fleet by exactly this repo. The registry entry lands with this report. Nothing here is a defect: every finding is the hub advancing after the carry, which is the propagation job [#536][pr-536] exists to make possible.

## Develop Drift

`develop` vs `main`: ahead 1, behind 1. The behind-1 is the promotion merge commit, which is the benign ancestry artifact merge-commit promotions always leave. The ahead-1 is a Dependabot action-SHA bump the merge bot landed after promotion. A second run with `--branch develop` reports the identical nine findings, so `develop` carries no conformance content `main` lacks.

## Already Owed by the Hub's Own Develop

This audit reads hub `main` (`3a7cc64`), which AUDIT.md section 1 makes the ground truth. Re-running it from a tree at hub `develop` (`362aec8`) reports two additional re-vendors and two DEFECTs, all four from [#545][pr-545] taking `bypass_actors` out of the three ruleset payloads about an hour before this run. They are recorded here rather than counted, because measuring a repo against un-promoted hub content reports work in flight as a conformance failure. What the re-run observed was:

- `repo-config/develop.json` and `repo-config/main.json` become stale carries, re-vendored the same mechanical way as the rest.
- The live `develop` and `main` rulesets then diverge from the payloads, because both still carry the `RepositoryRole` admin bypass the new payloads no longer declare. Closing that is a repository-settings change on a protected branch, so it is the maintainer's to apply, not an agent's.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| branch-model | pass | pass | pass | Both branches protected, exactly one ruleset per name, and both match `repo-config/develop.json` and `repo-config/main.json` by normalized diff. The required-status-check context is the fleet canonical `Check pull request workflow status job` (`repo-config/main.json:48`, `repo-config/develop.json:51`) and matches the aggregator's own name (`.github/workflows/test-pull-request.yml:26`) |
| repo-setup | pass | pass | pass | The baseline App-token pair is present in both the Actions and Dependabot stores, no forbidden name is configured, and no stale secret. `requiredSecrets` is empty, correct for a source-only repo publishing only a GitHub release |
| linter-parity | pass | pass | pass | One config per linter, each driving CI: markdownlint-cli2, cspell, actionlint, editorconfig-checker, shellcheck and shfmt, then the Hugo build and the URL-contract check (`.github/workflows/validate-task.yml:34-101`) |
| recurring-violations | drift | pass | drift | Three prose findings across the repo's own files, none in carried content. Line endings are LF and bound by git rather than only by the editor (`.gitattributes:7,16`) |
| readme-structure | pass | pass | pass | Sections present and in spec order, with Installation and Usage legitimately N/A for source-only. The intro is 77 characters, link-free, single-sentence (`README.md:3`), and the GitHub About description mirrors it exactly |
| workflow (WORKFLOW.md 5A/5B) | pass | pass | pass | The PR gate runs the reusable validation and gates the merge on one always-run aggregator (`test-pull-request.yml:25-26`). The publisher is dispatch-only, refuses a ref that is not `main` or `develop` (`publish-release.yml:34`), pins the dispatch-time commit so a later push cannot release unvalidated, versions with NBGV (`publish-release.yml:52-53`), and derives prerelease from the ref (`publish-release.yml:64`). Proven end to end: release `1.0.11`, 2026-08-01 |
| agent-instruction-set | drift | drift | drift | Eight verbatim units behind the canonical, two of them absent rather than stale, plus one undeclared section. Enumerated below |

csharp, nuget, pypi, python, console, docker: N/A. The repo builds a Hugo site and ships no package or image. It carries Python helper scripts under `checks/`, which is not enough to make it a `python` repo: there is no package, no `pyproject.toml`, and no ruff or pyright surface for the dimension to check.

## Defects

None.

## Drift Findings

**Carried content behind the canonical.** Six of these are stale copies and two never arrived, and the distinction matters because a stale copy still states the rule in an older form while an absent one states nothing at all:

1. `AGENTS.md > Fleet Bootstrap` is **absent**. Added by [#536][pr-536] after this repo carried its baseline, so the repo holds no statement of where the canonical rules live or how to route by its own state.
2. `GOVERNANCE.md > Representative Data in Agent-Authored Text` is **absent**. The rule against agent-authored text quoting the maintainer's own data is not present in this repo in any form.
3. `AGENTS.md > Context and Delegation Discipline` is stale. Missing the rule that a wait separates three outcomes and says which one it reached.
4. `AGENTS.md > Where the Rules Live` is stale. Missing the table row routing to `Representative Data in Agent-Authored Text`, consistent with finding 2.
5. `GOVERNANCE.md > Git and Commit Rules` is stale. Missing "Commit means commit and push".
6. `GOVERNANCE.md > Verification Discipline` is stale. Missing "A launched process is not a result, and a cause nobody observed is not a diagnosis".
7. `GOVERNANCE.md > PR Review Etiquette` is stale. Missing the whole `### Every Finding Ends in an Action` subsection, so the repo carries the review loop without the five outcomes that close a finding.
8. `repo-config/configure.sh` is stale. It predates the payload-driven check mode ([#540][pr-540], [#543][pr-543]), so its check mode compares the pull-request merge methods and the required-status-check contexts by name rather than comparing every parameterized rule's whole parameters object in both directions. It passes clean over drift the current canonical would catch.

**Undeclared section.** `AGENTS.md > Project Conventions` (`AGENTS.md:9`) is not a section [`spec/files.json`][files] declares. Its four rules are genuinely repo-specific (the append-only URL contract, never populating media over HTTP, `content/` as an archive, and a gate proving itself by failing) and none duplicates a verbatim section. Left in place pending the spec question raised below, which is what the section model asks for when reconciliation is not obvious.

**Prose.** Three findings from `scripts/prose_lint.py` run over the repo excluding `content/`, `public/`, `themes/`, and `resources/`: a comment opening in lowercase (`.github/workflows/merge-bot-pull-request.yml:20`), a semicolon in prose (`OPERATIONS.md:142`), and a COPYRIGHT SIGN that the character-set tiers do not classify (`layouts/rss.xml:51`).

## Proposed Registry / Spec Updates

- Add the `Blog` registry entry. Applied in this change: `source-only`, `release` workflow model, `dispatch-only` release trigger, `lineEndings: "lf"`, and `driftNotes` recording the two declared deviations plus the interim classification.
- Revisit `publish[]` once the VPS deploy exists. It declares the GitHub release only, because that is the only channel that currently ships. [#456][issue-456] holds the static-site type pending this repo's measured deploy shape. **Applied 2026-08-05**: the deploy has run, [#456][issue-456] and [#558][issue-558] are answered, the `hugo` type and the `self-hosted` target exist, and the registry entry now declares `types: ["hugo", "source-only"]` with both publish targets. The interim classification driftNotes are removed and three type checks are recorded against the repo instead, two of which are already fixed downstream.

## Escalations

Two spec questions, raised rather than resolved, per AUDIT.md section 9.

1. **The hub contradicts itself on whether a repo may carry its own `AGENTS.md` section.** [`AGENTS.md`][agents] states that "a project's project-specific conventions and public-API/behavioral contracts (e.g. a 'Library API Conventions' section) live in that project's own `AGENTS.md`", while [`spec/section-model.md`][section-model] states that "a downstream repo's extra section the hub does not declare is drift to reconcile, not a local liberty" and lists four destinations for repo-specific content, none of them `AGENTS.md`. A repo following the first is flagged by the audit for violating the second. Blog is the case: its `Project Conventions` section is exactly the shape the first sanctions.
2. **The character-set tiers do not classify the COPYRIGHT SIGN.** `layouts/rss.xml:51` emits the sign as U+00A9 in generated RSS output, where the alternative is an ASCII transliteration in a machine-read feed. The tiers in GOVERNANCE.md "Character Set" have no entry for it, so the linter reports `charset-unknown` rather than pass or fail, and every repo generating a feed or a rendered document will hit the same gap.

<!-- Repo -->
[agents]: ../../AGENTS.md
[divergences]: ../divergences.md
[files]: ../../spec/files.json
[repos]: ../../registry/repos.json
[section-model]: ../../spec/section-model.md

<!-- External -->
[issue-456]: https://github.com/ptr727/ProjectTemplate/issues/456
[issue-558]: https://github.com/ptr727/ProjectTemplate/issues/558
[pr-536]: https://github.com/ptr727/ProjectTemplate/pull/536
[pr-540]: https://github.com/ptr727/ProjectTemplate/pull/540
[pr-543]: https://github.com/ptr727/ProjectTemplate/pull/543
[pr-545]: https://github.com/ptr727/ProjectTemplate/pull/545
