# Audit: Blog

- **Audited branch:** main (`2b132e4`)
- **Types:** `hugo`, `source-only` (from registry)
- **Verdict:** operational
- **Date:** 2026-08-05
- **Run stamp:** `audit run 2026-08-05T21:57:38Z | hub 01507a0`

Second full run, replacing the 2026-08-03 report in whole rather than editing it, per the run-stamp discipline in [`AUDIT.md`][audit] section 8. The previous run graded the repo as `source-only` against a hub that had no static-site type, and it recorded that it was due a re-run once the deploy existed. That is what this is. The `hugo` type and the `self-hosted` composable target have since landed, the deploy has run against both environments, and every `hugo.*` check is judged here for the first time.

The three deviations the first run recorded against the repo are closed. [ptr727/Blog#27][blog-27] (the remote release tree was never pruned), [ptr727/Blog#28][blog-28] (the vendored theme recorded no upstream ref), and [ptr727/Blog#29][blog-29] (the generator pin was duplicated across two workflows) each have a fix on `main` and are cited as evidence below. No defect is open. Everything remaining is the hub having advanced past what this repo carries, plus one finding that is the hub's own to fix rather than the repo's.

## Develop Drift

`develop` vs `main`: ahead 0, behind 6. `git diff origin/develop origin/main` is empty, so the two branches carry identical trees and the six commits are the promotion's PR commits plus its merge commit. That is the benign ancestry artifact a merge-commit promotion always leaves. No drift finding: `main` carries no content `develop` lacks.

## Dimensions

| Dimension | Letter | Intent | Verdict | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| hugo | pass | pass | pass | All nine checks pass, enumerated below |
| branch-model | pass | pass | pass | Both branches protected, exactly one ruleset per name, and both match `repo-config/develop.json` and `repo-config/main.json` by normalized diff. The required-status-check context is the fleet canonical `Check pull request workflow status job` (`repo-config/main.json:41`, `repo-config/develop.json:44`) and matches the aggregator's own name (`.github/workflows/test-pull-request.yml:26`). General settings diff clean against `repo-config/settings.json`, and the two state-dependent settings hold: the repo is public with discussions on, and `default_branch` is `main` |
| repo-setup | pass | pass | pass | No forbidden secret, no stale secret. `requiredSecrets` is empty and correct: the GitHub release needs no credential, and the deploy's credentials are per-environment. See the caveat below, because a clean verdict here says less than it looks like it does |
| linter-parity | pass | pass | pass | One config per linter, each driving CI: markdownlint-cli2, cspell, actionlint, editorconfig-checker, then shellcheck and shfmt, config validation, the site build, and the URL contract (`.github/workflows/validate-task.yml:28-88`) |
| recurring-violations | drift | pass | drift | 17 prose findings across 7 of the repo's own files, none in carried or vendored content. Line endings are LF and bound by git rather than only by the editor (`.gitattributes`). Enumerated below |
| readme-structure | pass | pass | pass | Sections present and in spec order, with Getting Started legitimately omitted and Installation and Usage N/A for a repo that ships no installable artifact. Build and Distribution carries all three subsections (`README.md:12,17,22`). The intro is 76 characters, link-free, and a single sentence (`README.md:3`), the GitHub About description mirrors it exactly, and `HISTORY.md:1-3` matches the README title and intro |
| workflow (WORKFLOW.md 5A/5B) | pass | pass | pass | Enumerated below |
| agent-instruction-set | drift | pass | drift | Seven verbatim units behind the canonical, all of them stale copies rather than absences, so each states its rule in an older form. Enumerated below |

`csharp`, `nuget`, `pypi`, `python`, `console`, `docker`: N/A. The repo builds a static site and ships no package or image. It carries Python helper scripts under `checks/`, which is not enough to make it a `python` repo, since there is no package, no `pyproject.toml`, and no ruff or pyright surface for the dimension to check.

**What a clean `repo-setup` does not cover.** The deploy's credentials are GitHub Environment secrets and variables rather than repository secrets, and [`spec/secrets.json`][secrets] has no vocabulary for an environment scope. The registry therefore declares an empty `requiredSecrets`, which is correct rather than a gap, because listing the names there would make the audit demand them in the repository Actions store where they deliberately are not. The consequence is that this dimension passing is no evidence at all that either environment is configured. What proves that is a deploy run reaching its verification step, which has happened for both environments and is outside what the audit can see.

## Hugo Dimension, Check by Check

| Check | Verdict | Evidence |
| --- | --- | --- |
| `hugo.build.strict` | pass | `hugo --gc --minify --panicOnWarning` at `.github/workflows/validate-task.yml:83` and the identical command at `deploy/make-release.sh:94`, so the gate and the deploy build the same way rather than in two variants |
| `hugo.urls.parity` | pass | `checks/check-url-parity.py:16-30` asserts a floor on each of the three lists before comparing, and `checks/check-live-urls.sh:18-25` does the same against the running site. The floors sit under the current counts (328 render, 917 redirect, 778 legacy media), so a list may grow but cannot collapse into a vacuous pass |
| `hugo.output.uncommitted` | pass | `public/` and `resources/_gen/` are gitignored (`.gitignore:5-6`) and neither is tracked. The Markdown glob excludes the imported archive, the vendored theme, and the render (`.github/workflows/validate-task.yml:31-35`), and cspell is scoped to `README.md` and `HISTORY.md` |
| `hugo.generator.pinned` | pass | Version and SHA-256 are pinned together at `.github/actions/install-hugo/action.yml:26-27`, verified by `sha256sum --check --strict` before install (line 33), and the extended build is asserted from the binary rather than inferred from the file name (lines 36-38). The pin is declared once, in the composite action both callers use (`validate-task.yml:76`, `deploy-site-task.yml:79`), and is not exposed as an overridable input. This closes [ptr727/Blog#29][blog-29], which is the stronger fix: the check asks that something assert two copies agree, and removing the second copy makes agreement structural |
| `hugo.vendored.provenance` | pass | `themes/README.md:10-16` records the upstream repository, the exact commit, its upstream date, its `git describe` form, and the license, and lines 23-26 record the two local edits against it. The record sits outside the vendored directory deliberately, so replacing that directory on an update does not take the record with it. This closes [ptr727/Blog#28][blog-28] |
| `hugo.deploy.environment` | pass | The deploy job binds `environment: ${{ inputs.environment }}` (`deploy-site-task.yml:62`) and takes the host, user, base URL, and known-hosts entry from that environment's variables and secrets, so the workflow file names no host, path, or address. A separate `assert-environment` job re-asserts the name (lines 38-55), which is what the check asks for, because the environment binding resolves before any step runs and a `workflow_call` caller is not bound by the dispatch choice list a human sees |
| `hugo.deploy.atomic` | pass | The release installs under its own immutable id beside the retained ones, and `--delete` is omitted on the upload precisely because at an environment root it would remove rollback targets (`deploy-site-task.yml:123,132-136`). `--link-dest` points at `current`, which still resolves to the previous release at that moment. The pointer moves as a separate step (lines 140-150), and locally the same flip is a temporary link renamed over the old one (`deploy/make-release.sh:206-210`), which is a single rename rather than a replace in place |
| `hugo.deploy.verified` | pass | The terminal step observes the running host rather than the transport's exit status (`deploy-site-task.yml:155-163`). `checks/check-live-urls.sh` asserts which environment answered from a response header (lines 130-139) and that the rules answering are the release just installed (lines 154-182), polling to a bounded timeout because the config reload is asynchronous. Unreachability is reported distinctly from an HTTP status in three separate places (lines 101-105, 145-152, 163-167), and the preflight separates a bad credential from a vanished site (lines 109-126) |
| `hugo.deploy.retention` | pass | `OPERATIONS.md:170` declares the count and names the host timer that owns the prune, and records that the release `current` resolves to is retained unconditionally without consuming one of the ten. `OPERATIONS.md:172` records why nothing prunes on the deploy path, which is that the deploy key then needs no delete capability. This is the second of the two shapes D5.6 allows, and it is the correct one here: the credential is confined write-only and cannot observe the destination, so an in-pipeline assertion would mean widening it. Note that `deploy/make-release.sh:212-230` does prune, but in CI it runs against `${RUNNER_TEMP}/bundle`, so it is a local scratch prune and satisfies nothing on its own. The host timer is what the verdict rests on. This closes [ptr727/Blog#27][blog-27] |

## Workflow Assertions (5A and 5B)

- **D1, PR fast feedback.** The pull request gate runs the reusable validation and gates the merge on one always-run aggregator whose name is the ruleset-bound context (`test-pull-request.yml:15-19,25-29`).
- **D4, release and publish.** The publisher is dispatch-only, refuses a ref that is not `main` or `develop` (`publish-release.yml:34-40`), pins the dispatch-time commit so a push landing after dispatch cannot release unvalidated (line 46), versions with NBGV (line 53), and derives prerelease from the ref (line 64). It runs the same validation gate first (lines 15-19). Proven end to end: release `1.0.11` on 2026-08-01 from `main`, and prerelease `1.0.17-g4b2def3ee9` on 2026-08-04 from `develop`.
- **D4, deploy.** The deploy is a separate `workflow_dispatch` from the release, so a redeploy of an unchanged commit mints no tag (`deploy-site.yml:3-12`). Production is refused from any ref other than `main`, compared against the full ref rather than `ref_name`, because a tag and a branch sharing a short name are separate namespaces and the `ref_name` form would accept a tag named `main` pointing anywhere (`deploy-site.yml:25-37`). The gate runs first, before anything is installed or written.
- **D7, concurrency and permissions.** Both dispatch workflows queue rather than cancel, each for a stated reason: a cancelled deploy leaves a release uploaded and unflipped, and a cancelled publish leaves a half-created GitHub release (`deploy-site.yml:14-17`, `publish-release.yml:6-10`). Both assertion jobs declare `permissions: {}`, since neither reads the repository.
- **Dependabot.** `github-actions` is declared and dual-targets `main` and `develop`, which is the whole implied set: the site has no package manifest, and the theme is vendored rather than pulled by a manager.

## Defects

None.

## Drift Findings

### Carried Content Behind the Canonical

Seven verbatim units are stale, and none is absent. Every one is the hub advancing after this repo's last re-vendor, which is the propagation this model expects rather than anything the repo did. They fall into two groups.

**The Markdown capitalization settlement ([#566][pr-566]).** Three units differ from the canonical only in the case of the word Markdown in prose: `GOVERNANCE.md > Documentation Style Conventions`, `GOVERNANCE.md > Repository Details`, and `.markdownlint-cli2.jsonc` (in two comments). No rule changed.

**Four rule additions this repo has not yet carried.** These are substantive, and the repo currently states each rule in a form that is missing the new part:

1. `GOVERNANCE.md > Repository Boundaries and Write Safety` is missing the rule that a refused write is reported and never re-shaped, and that the maintainer's say-so does not lift a refusal by the harness ([#569][pr-569]).
2. `GOVERNANCE.md > Communicating with the User` is missing both the form-follows-surface qualifier on clickable links and the whole rule that work blocked on the user is raised as a direct interactive prompt whose options are the actions themselves ([#561][pr-561]).
3. `GOVERNANCE.md > Release Model` is missing the filesystem-deploy leaf bullet, which is the one that describes this repo's own deploy shape, including the retention rule the `hugo.deploy.retention` verdict above is judged against ([#558][issue-558], [#560][pr-560]).
4. `GOVERNANCE.md > Branching Model` is missing the issue-closing-keyword rule, which moved here out of Release Model, so this repo carries it in the old location and will lose it on the next Release Model re-vendor if the two are done separately ([#563][issue-563]).

Item 4 is the one to sequence carefully. A re-vendor that takes the new Release Model without also taking the new Branching Model drops the rule entirely rather than leaving it stale.

### Prose

17 findings from `scripts/prose_lint.py` over the repo with `content/`, `public/`, `themes/`, and `resources/` excluded. All 17 are in files this repo authors.

| Check | Count | Locations |
| --- | --- | --- |
| semicolon | 6 | `OPERATIONS.md:80,176,186`, `TODO.md:31,104`, `deploy/README.md:110` |
| spelling | 5 | `OPERATIONS.md:231`, `deploy/README.md:197,219,231`, `deploy/make-release.sh:51` |
| comment-wrap | 4 | `.github/workflows/deploy-site-task.yml:35`, `checks/check-live-urls.sh:77,99,149` |
| comment-case | 1 | `.github/workflows/deploy-site-task.yml:139` |
| charset-unknown | 1 | `layouts/rss.xml:51` |

Each was read rather than counted. The six semicolons all join independent clauses, which is the form the rule bans. The five spellings are British forms with US equivalents. The four wraps are comment sentences continuing onto a second line. The one case finding opens a comment sentence on a lowercase command name. The `charset-unknown` finding is not a violation and is carried to Escalations below, unchanged from the previous run.

## Non-Findings

**`GOVERNANCE.md > Repository Onboarding and Conformance` is absent, and correctly so.** [`spec/section-model.md`][section-model] line 51 declares it hub-only and not carried, so its absence here is conformance rather than drift. Recorded because a section-count comparison against the hub surfaces it and it reads like a gap.

**The `Project Conventions` section is gone.** The previous run raised it as an undeclared `AGENTS.md` section and as escalation 1. The repo has since moved that content into the topical docs that own it, and its `AGENTS.md` now carries exactly the three declared sections. The escalation is resolved and is not repeated below.

## Escalations

Three, raised rather than resolved, per [`AUDIT.md`][audit] section 9. The first is new and is a hub defect rather than a repo one.

### 1. The Template-Reference Check Contradicts the Byte-Locked Fleet Bootstrap Section

[`spec/audit.py`][audit-runner] lines 691-695 flag any carried `AGENTS.md`, `GOVERNANCE.md`, or `.github/copilot-instructions.md` that contains the hub's name anywhere in the file, on the reasoning that the coordination flow is machinery a consumer should not see. That reasoning is sound and the check catches real cases.

It has no exemption for `AGENTS.md > Fleet Bootstrap`, which the hub declares `verbatim` in [`spec/files.json`][files] and whose first sentence names the hub repository by path. Naming it is the section's entire function: it is the byte-locked entry point that tells an agent where the canonical rules live when nothing else present says so. Blog's line 11 is byte-identical to the hub's own `AGENTS.md:11`, and it is the file's only occurrence of the name.

So the finding on this repo is unclearable by construction. The only way to satisfy the check is to delete or alter a section the hub byte-locks, which the verbatim check would then flag instead. This is not specific to Blog. It fires on any repo that has carried the current canonical, and it will therefore spread across the fleet as the carry propagates rather than staying a single-repo curiosity. The fix belongs in the check: scan the file with the `Fleet Bootstrap` block excised, so a reference outside that section is still caught. The finding above is recorded as a hub defect and is not counted against this repo's verdict.

### 2. A Downstream Repo Holding a Report at the Hub's Own Report Path

Blog carries `reports/Blog/audit.md`, a self-audit dated 2026-08-05 against hub `3b802b9`. [`AUDIT.md`][audit] section 8 states that the hub authors the report and that a report written by the repo being audited is a claim rather than evidence, and it forbids the route it anticipated, which is a downstream repo opening a pull request against the hub. It says nothing about a downstream repo keeping such a report in its own tree, and `spec/files.json` neither declares nor forbids the path.

Two things follow. The path is the same relative path the hub uses for its evidence, so a reader with both trees open has two documents named `reports/Blog/audit.md` reaching verdicts on the same repository, and only one of them is evidence under the model. The downstream copy is also stale by exactly the mechanism section 8 describes: it declares `types: ["source-only"]`, which the registry superseded before the file was written, so its own header disagrees with the registry it cites.

The question for the spec is whether a repo self-auditing in its own tree is sanctioned, and if it is, under what name. The content is genuinely useful, because the repo checks things the hub cannot see, its environment secrets among them. The collision is with the path and with the word audit, not with the practice. Raised rather than resolved, since the answer changes `spec/files.json` for every repo rather than just this one.

### 3. The Character-Set Tiers Do Not Classify the COPYRIGHT SIGN

Unchanged from the previous run and still open. `layouts/rss.xml:51` emits the sign as U+00A9 in generated feed output, where the alternative is an ASCII transliteration in a machine-read document. The tiers in `GOVERNANCE.md` "Character Set" have no entry for it, so the linter reports `charset-unknown` rather than pass or fail. Every repo generating a feed or a rendered document hits the same gap.

## Proposed Registry / Spec Updates

- **Registry: no change.** The entry already declares `types: ["hugo", "source-only"]`, both publish targets, `dispatch-only`, and `lineEndings: "lf"`. Its four `driftNotes` all describe current, live deviations, none asserts outstanding work, and none names a check id, so nothing is retired by this run.
- **Conformance matrix: the `hugo` row is updated with this run's date and findings** in the same change as this report.
- **`spec/audit.py`: exempt the `Fleet Bootstrap` block from the template-reference scan**, per escalation 1. This is a hub defect with a fleet-wide blast radius and is the one item here that should not wait.
- **`spec/files.json` or `AUDIT.md` section 8: decide the downstream self-report question**, per escalation 2.
- **`GOVERNANCE.md` "Character Set": classify U+00A9**, per escalation 3.

## Convergence

Per [`AUDIT.md`][audit] section 10, the drift above is applied to the target by pull request, one focused PR per drift class, and the maintainer merges. Two classes are open here:

1. **Re-vendor the seven stale units**, taking Branching Model and Release Model together for the reason in item 4 above.
2. **Fix the 17 prose findings**, which are all in this repo's own authored files.

Neither is started. The repo also carries one open item of its own, [ptr727/Blog#33][blog-33], which is a retest of the deploy transport's SSH options against the real host. That is the repo's to close and is recorded here only so a reader is not surprised by it.

<!-- Repo -->
[audit]: ../../AUDIT.md
[audit-runner]: ../../spec/audit.py
[files]: ../../spec/files.json
[secrets]: ../../spec/secrets.json
[section-model]: ../../spec/section-model.md

<!-- External -->
[blog-27]: https://github.com/ptr727/Blog/issues/27
[blog-28]: https://github.com/ptr727/Blog/issues/28
[blog-29]: https://github.com/ptr727/Blog/issues/29
[blog-33]: https://github.com/ptr727/Blog/issues/33
[issue-558]: https://github.com/ptr727/ProjectTemplate/issues/558
[issue-563]: https://github.com/ptr727/ProjectTemplate/issues/563
[pr-560]: https://github.com/ptr727/ProjectTemplate/pull/560
[pr-561]: https://github.com/ptr727/ProjectTemplate/pull/561
[pr-566]: https://github.com/ptr727/ProjectTemplate/pull/566
[pr-569]: https://github.com/ptr727/ProjectTemplate/pull/569
