# WORKFLOW.md

The single guide for CI/CD **workflows** (GitHub Actions). It is a deliberate mixture of code
style, architecture, a **behavioral contract** (expected inputs and outputs), and a **test
methodology**. Code style lives in [`CODESTYLE.md`](./CODESTYLE.md); this file is its sibling for
everything under [`.github/workflows/`](./.github/workflows/).

Its defining principle: **it describes required outcomes, not a required implementation.** Two
repos may implement the same guarantee with different YAML. A workflow is correct when it
**satisfies the contract** in section 4 and is **defect-free against the expected inputs and
outputs** - not when it matches the template byte for byte. The conventions in section 2 are how
we keep workflows legible and reviewable; the contract in section 4 is what they must *do*.

Given this document, an agent must be able to do three things to any project:

1. **Audit** - statically check the workflows against the conventions (section 2) and the
   structural facts each guarantee implies (section 5A).
2. **Test** - trace the expected inputs/outputs (section 5B) and, where warranted, drive a live
   probe (section 5C).
3. **Assess** - render a verdict: **operational** (every applicable guarantee holds and every
   scenario's observed output equals the expected) or **not operational** (any mismatch - which is
   a *defect*, not a style nit).

> **Reconciliation pending.** Some conventions in section 2 currently also appear in `AGENTS.md`
> ("Workflow YAML Conventions") and the release behavior in `AGENTS.md` "Release Model". This is
> intentional short-term duplication while this document is ratified. Once ratified, the canonical
> copy moves here and `AGENTS.md` points to it (the `CODESTYLE.md` pattern); treat any difference
> as "this file is being adopted," not drift.

The guarantees below are distilled from real failures observed in practice. They are stated as the
**failure-mode each prevents**, so the document stays portable to any project.

## 1. Purpose and how to use this document

- **Contract, not implementation.** Conform to the *outcomes* in section 4. Shape, job names, and
  file layout may differ between repos; the input/output behavior may not.
- **Operational is binary.** A workflow is operational only if every applicable guarantee holds.
  A single input/output mismatch is a defect and makes the workflow non-operational, regardless of
  how clean the YAML looks.
- **The three verbs.** Audit (static), Test (trace + probe), Assess (verdict). Section 5 gives an
  agent the exact procedure.
- **Provenance.** Each guarantee encodes a failure that has actually shipped. The failure-mode is
  named so the *why* survives even when the implementation changes.

## 2. Workflow style conventions

These are prescriptive style/legibility rules. They are cheap to check and keep workflows
reviewable; they are necessary but not sufficient (a perfectly styled workflow can still violate
the section 4 contract).

- **Action pinning.** Pin **every** action - first-party (`actions/*`) and third-party - to a
  commit SHA with a trailing `# vX.Y.Z` comment, so a tag swap cannot change executed code while
  Renovate/Dependabot can still bump it. Use `# vX` (major-only) only when the upstream floating
  major tag has no specific patch SHA. The single documented no-pin exception is a tool whose tag
  stream lags `master` such that tag-tracking would propose a downgrade (in this template,
  `dotnet/nbgv@master`); do not invent new exceptions, including for repo-owned build-layer leaves.
- **Filename.** Reusable workflows (`on: workflow_call`) end in `-task.yml`. Entry-point workflows
  (`on: push` / `pull_request` / `schedule` / `workflow_dispatch`) do **not** use `-task`; they end
  with what they do (`-pull-request.yml`, `-release.yml`). The suffix is semantic: a `-task.yml` is
  meant to be `uses:`-d, never triggered directly. Names are lowercase, hyphen-separated.
- **Workflow `name:`.** Reusable workflow names end in **"task"** (`Build PyPI library task`);
  entry-point names end in **"action"** (`Publish project release action`). The UI label then tells
  you at a glance whether you are looking at an orchestrator or a callee.
- **Job and step `name:`.** Every job `name:` ends in **"job"**; every step `name:` ends in
  **"step"**. **Exception:** a job whose `name:` is bound as a required-status-check `context:` in a
  branch ruleset keeps that exact name verbatim - renaming silently breaks required-check
  enforcement.
- **Concurrency.** Top-level workflows declare
  `concurrency: { group: '${{ github.workflow }}-${{ github.ref }}', cancel-in-progress: true }`.
  Document any exception inline (see D7 for the publisher's global, no-cancel group and the
  merge-bot's ordered, no-cancel group).
- **Shells.** Every multi-line bash `run:` starts with `set -euo pipefail`.
- **Conditionals.** Multi-line `if:` uses the folded scalar `if: >-` (a literal block `if: |`
  embeds newlines into the boolean expression and is wrong).
- **Boolean inputs.** A boolean input used by both `workflow_call` and `workflow_dispatch` is
  declared in **both** trigger blocks. `workflow_call` delivers a real boolean; `workflow_dispatch`
  delivers the **string** `"true"`/`"false"`. Any `if:` consuming one must compare both forms:
  `if: ${{ inputs.foo == true || inputs.foo == 'true' }}`.
- **Reusable-workflow permissions.** Job-level `permissions:` are validated **before** `if:`
  evaluates, so even a skipped job needs valid permissions declared. Grant least privilege; for a
  reusable callee that needs a scope (e.g. `actions: write` to delete artifacts), the **caller**
  grants it at the `uses:` job.
- **Allowlist `success` and `skipped` explicitly** when chaining across optional dependencies.
  `!= 'failure'` lets `cancelled` through; use `(needs.X.result == 'success' || needs.X.result == 'skipped')`.
- **Docker layer cache.** Cache to/from a registry tag (`type=registry`, e.g. `buildcache-<branch>`),
  never the Actions cache (`type=gha`). Multi-image repos use a per-image buildcache tag.
- **Line endings.** Workflow YAML follows `.editorconfig` (CRLF in this template). Committed JSON
  state files follow the repo's JSON line-ending rule. Preserve endings on every edit.

## 3. Architecture

### Two layers: orchestration vs build

The pipeline splits into a generic **orchestration layer** and a repo-owned **build layer**.

- **Orchestration** (generic, intended to be synced verbatim): the publish plan and branch matrix,
  the version step, the release-tagging/asset-attaching job, the date-badge job, and the
  aggregator shape of the PR workflow. It is target-agnostic and should not need per-repo edits.
- **Build** (repo-owned): the `build-<target>-task.yml` leaf tasks. A derived project owns and
  replaces these; it curates only the *list* of leaf jobs the orchestrator calls.

### The seam contract

A target contributes a file to the GitHub release by uploading a workflow artifact named
`release-asset-<branch>-<target>`. The release job collects **every** matching artifact by
**pattern** (`pattern: release-asset-<branch>-*` + `merge-multiple: true`), never by an
`artifact-ids:` that names a specific build job's output. This is canonical for **every** repo,
single-target included: name your one asset to the pattern and the verbatim release job globs it.
Switching a single-target repo to an `artifact-id` output forks the release download and breaks the
verbatim carry.

### Reusable-task parameter contract

Every `build-*-task.yml` and the release task take: `ref` (git ref to check out/version), `branch`
(the **logical** branch that drives config/tags/prerelease), and where relevant `smoke`.
Branch-derived config keys off `inputs.branch`, **never** `github.ref_name` - the publisher's
matrix builds the non-default branch from a run whose `github.ref_name` is the default branch, so
`ref_name` would be wrong. Artifact names are branch-suffixed so both matrix legs coexist in one
run.

### Versioning

NBGV computes the version. It MUST version from the **checked-out branch**, not the runner's CI ref
(set `IGNORE_GITHUB_REF=true`; `GITHUB_REF` is reserved and a step `env:` cannot override it). The
default branch is the public-release ref (`publicReleaseRefSpec`), so it builds a clean public
`X.Y.Z`; every other branch builds a prerelease `X.Y.Z-g<sha>`. `version.json`'s `version` is the
major.minor floor; NBGV appends the git height as the patch. A package build derives its registry
version from the same source (PyPI uses a PEP 440 `.devN` suffix off the default branch). A repo
that wraps an upstream release may drive its build/image version from an external committed
`name -> version` state file while NBGV still tags the GitHub release.

### Validate-at-entry

When a workflow's inputs must satisfy a cross-input or input-versus-derived-state invariant, assert
it **once** in a dedicated entry job/step that the downstream jobs `needs:`, failing fast before any
build or publish. One gate with a clear `::error::` beats partial checks scattered through later
jobs.

### Resource lifecycle

Workflow artifacts are an **intra-run handoff** only; durable copies live on the release/registry.
Each transfer artifact is deleted by exact name/pattern **at its point of consumption**, the delete
is **gated to the same condition as the consumer**, it is **best-effort**, and **every** upload sets
`retention-days: 1` as the failure-path backstop. The run is **never** blanket-deleted. See D5.

### Fast PR feedback

PRs validate fast and never publish. A paths-filter detects changed targets and smoke-builds only
those; unit tests always run; smoke builds compile/lint/test but upload nothing and push nothing.
A single required aggregator gates the merge. See D1.

### Release model

Two-phase by default: PRs smoke-test, merges do not publish. The publisher (weekly schedule +
manual dispatch) builds and publishes **both** branches via a matrix; its `push` trigger publishes
only when an opt-in repository variable is set. Every release is a tag on the built commit plus a
source zip, README, and LICENSE; targets amend it with `release-asset-*` files or push to their own
registry. An unchanged version re-pushes nothing (no-op republish); Docker re-pushes by design to
pick up base-image refreshes. See D3/D4.

### Output seam by destination

Pick each output's path by **where the artifact goes**, not by language:

- **File on the GitHub release** (zip, binary, packaged library): one leaf task per output, each
  uploading `release-asset-<branch>-<name>`.
- **Package-registry push** (NuGet, PyPI): the leaf builds and the package is published to its
  registry. NuGet pushes from the leaf *and* uploads a `release-asset-*`; PyPI is split - the leaf
  only builds + uploads its build artifact, and a separate publish job does the OIDC upload (so
  `id-token: write` is granted at one entry point) and contributes **no** `release-asset-*`.
- **Image-registry push** (Docker): the leaf pushes multi-arch tags and contributes no
  `release-asset-*`.
- **Source-only** (validate + tag): no package/image leaf - only validation, optionally one
  `release-asset-*`, and the verbatim release orchestration.

## 4. Behavioral contract - expected outcomes

The required behaviors, organized by domain. Each is a **MUST**, stated as input -> output plus the
failure-mode it prevents. A workflow that violates any applicable guarantee is **not operational**.

### D1 - PR fast-feedback (smoke)

- **D1.1 Only changed targets build.** Input: a PR touching some targets. Output: the paths-filter
  marks exactly those targets, and only their smoke builds run; unchanged targets skip. *Prevents:
  rebuilding everything on every PR, and the inverse - a changed target slipping through unbuilt.*
- **D1.2 Unit tests always run.** Input: any PR. Output: the unit-test job runs unconditionally.
- **D1.3 Smoke never publishes and never uploads.** Input: a smoke build (`smoke: true`). Output:
  full compile/lint/test, but **no** registry/image push, **no** release, and **no** artifact
  uploads (every `upload-artifact` is gated `!smoke`). *Prevents: a PR publishing, and orphaned
  artifacts churning the storage quota.*
- **D1.4 Workflow-file changes are not smoke-built.** Input: a PR changing only
  `.github/workflows/**`. Output: the paths-filter excludes workflow files, so smoke-build skips
  (a filter cannot tell a logic change from a version-bump). *Implication: there is no CI
  workflow-lint; workflow edits MUST be linted locally (actionlint).*
- **D1.5 One required aggregator gates merge.** Input: any PR. Output: a single aggregator job
  must **succeed** (not merely "not fail"), `needs:` the changes job, treat a **skipped** smoke
  build as pass, and **block** on any `failure`/`cancelled`. Its name is ruleset-bound and MUST NOT
  be renamed. *Prevents: a paths-filter error letting a target-changing PR merge with its smoke
  build silently skipped.*

### D2 - Input/state validation at entry

- **D2.1 Validate before expensive work.** Input: a workflow whose inputs carry a cross-input or
  input-versus-derived-state invariant. Output: a dedicated entry job/step asserts it and fails
  fast with `::error::` before builds; downstream jobs `needs:` it. *Prevents: cryptic failures
  deep in matrix expansion.*
- **D2.2 Release branch matches version classification.** Input: a real (non-smoke) release build.
  Output: the gate fails loudly if the default branch carries a prerelease suffix, **or** a
  non-default branch carries none. It strips `+buildmetadata` before testing for the prerelease `-`
  (only a `-` in the core/prerelease segment counts), and it is **skipped on smoke** (a smoke build
  checks out a detached PR head and always versions as prerelease). *Prevents: a non-default leg
  published as stable; a build-metadata hyphen false-positive; the gate blocking every
  default-base promotion PR.*
- **D2.3 Publish only from the default branch.** Input: a `workflow_dispatch` (or schedule) publish.
  Output: a dispatch from a non-default ref fails fast. *Prevents: the matrix building the other
  branch leg from the wrong ref and shipping a malformed non-prerelease "Latest".*
- **D2.4 Mutually-exclusive / paired inputs are validated.** Input: a workflow with either/or or
  must-pair inputs (e.g. a repo-list vs a manifest+jq pair). Output: a half-filled or conflicting
  combination fails fast. *Prevents: a silent fall-through from a partially specified input.*

### D3 - Versioning and classification

- **D3.1 Version from the checked-out branch.** Input: a matrix publish dispatched from the default
  branch, each leg checking out its own branch. Output: each leg's version reflects **its** branch
  (`IGNORE_GITHUB_REF=true`), so the non-default leg stays a prerelease. *Prevents: every leg being
  classified as the public ref because the CI ref is the default branch.*
- **D3.2 Default = public, others = prerelease.** Output: default branch -> `X.Y.Z`
  (`PublicRelease=true`); any other branch -> `X.Y.Z-g<sha>` (`PublicRelease=false`).
- **D3.3 Version floor + git height.** Output: `version.json` sets the major.minor floor; NBGV
  appends the git height as the patch. The floor is bumped only for a functional change, by the
  maintainer, in the PR that introduces it - never on a cadence. (No `pathFilters`, so any commit
  advances the height.)
- **D3.4 Registry versions follow the same classification.** Output: NuGet default = stable, others
  = prerelease; PyPI default = release, others = a PEP 440 `.devN` build.
- **D3.5 Wrapper repos may use an external version.** Output: a repo wrapping an upstream release
  drives its build/image version from a committed `name -> version` state file, while NBGV's version
  still tags the GitHub release. *Prevents: a wrapper being forced onto its own NBGV version for the
  immutable image tag.*

### D4 - Release / publish

- **D4.1 Two-phase by default.** Output: PRs smoke-test; merges do **not** publish unless the opt-in
  variable is set; the publisher's schedule and dispatch always publish both branches.
- **D4.2 Tag the built commit.** Output: the release's `target_commitish` is the **built commit's
  SHA** (NBGV's commit id), never `github.sha` (wrong branch in the publisher matrix) or a moving
  branch ref. *Prevents: the release tag landing on the default branch instead of the built tree.*
- **D4.3 Release contents.** Output: every release is a tag on the built commit plus the auto
  source zip, README, and LICENSE; file-producing targets attach `release-asset-*`. The prerelease
  flag equals `branch != default`.
- **D4.4 No-op republish.** Input: a re-run whose version is unchanged. Output: nothing is
  re-pushed - the release-create step is skipped when the tag already exists (and refreshed only on
  `workflow_dispatch`); registry pushes use skip-duplicate/skip-existing. **Docker always
  re-pushes** by design (base-image refresh). *Prevents: duplicate releases and wasted pushes.*

### D5 - Resource cleanup

- **D5.1 Delete at the point of consumption.** Output: the job that downloads a transfer artifact
  deletes it (by exact name/pattern) right after consuming it - including per-runtime intermediates
  consumed by an aggregation job. *Prevents: transfer artifacts accumulating against the storage
  quota.*
- **D5.2 Gate the delete to the consumer's condition.** Output: the delete runs under the **same**
  condition as the consuming step (e.g. only when a release was actually created/refreshed). *Prevents:
  deleting freshly built artifacts on a no-op re-run where nothing consumed them.*
- **D5.3 Best-effort.** Output: cleanup is `continue-on-error`, tolerates a failed artifact
  listing, and deletes **all** matching ids. *Prevents: a cleanup hiccup reddening a job whose
  publish already succeeded.*
- **D5.4 Retention backstop.** Output: **every** `upload-artifact` sets `retention-days: 1`, so a
  job that dies before its consumer leaves nothing beyond a day - no separate terminal cleanup job
  is needed.
- **D5.5 Never blanket-delete.** Output: cleanup MUST NOT enumerate and delete the run's whole
  artifact set (`.artifacts[].id`). *Prevents: destroying diagnostic/log artifacts and the
  build-records actions emit automatically - exactly what you need to debug a failed run.*

### D6 - Seam / architecture conformance

- **D6.1 Pattern handoff.** Output: the release job downloads assets by `pattern:`/`merge-multiple:`,
  not `artifact-ids:`; targets upload `release-asset-<branch>-<target>`. Canonical for single-target
  repos too. *Prevents: a single-target repo forking the verbatim release download.*
- **D6.2 Branch drives config.** Output: branch-derived config reads `inputs.branch`, never
  `github.ref_name`. *Prevents: the publisher matrix mislabeling the non-default leg.*
- **D6.3 Branch-suffixed artifacts.** Output: artifact names are branch-suffixed so both matrix legs
  coexist in one run.
- **D6.4 Per-target subsetting is clean.** Output: dropping a target removes its leaf task, its job
  and `needs` entry in the release task, its path-filter entry, and (for the split PyPI publish) its
  publish job - and leaves the release orchestration untouched.

### D7 - Concurrency, permissions, safety

- **D7.1 Publisher serializes.** Output: the publisher uses a **global, ref-independent** concurrency
  group with `cancel-in-progress: false`, so a scheduled run and a manual dispatch cannot run
  concurrently and double-push, and a mid-flight publish is never cancelled into a partial release.
- **D7.2 Skipped jobs still need valid permissions.** Output: every reusable job declares valid
  `permissions:` (validated before `if:`); a callee's extra scope (e.g. `actions: write` for
  cleanup) is granted by the caller. *Prevents: `startup_failure` on a skipped job, or a cleanup
  step lacking permission.*
- **D7.3 Boolean inputs both forms.** Output: boolean inputs are declared in both trigger blocks and
  compared against `true` and `'true'`.
- **D7.4 Optional-dependency chaining.** Output: cross-job conditions allowlist `success`/`skipped`
  explicitly rather than `!= 'failure'`.

### D8 - Bots / automation

- **D8.1 Merge-bot.** Output: enables auto-merge on `opened`/`reopened`; dispatches `--squash` or
  `--merge` by the PR's base ref; disables auto-merge on a maintainer-pushed `synchronize`; and its
  concurrency is keyed on the **PR number**, not `github.ref`. *Prevents: two PRs colliding in
  auto-merge.*
- **D8.2 CodeGen and Dependabot.** Output: codegen runs as a matrix over both branches and is
  deterministic from an external source (re-runnable, convergent); Dependabot targets both branches,
  with security PRs against the default branch.
- **D8.3 Upstream-version tracker.** Output: a scheduled resolver prints a JSON `name -> version`
  object to a committed state file, opens a rolling per-branch bump PR naming only the keys that
  moved, the merge-bot auto-merges it, and the bump ships on the **next** publish.

### D9 - Style / static (see section 2)

- **D9.1** Every action is SHA-pinned with a version comment (sole exception: the documented
  lagging-tag tool).
- **D9.2** File, workflow, job, and step names follow the suffix rules; ruleset-bound names are
  verbatim.
- **D9.3** Bash `run:` blocks start `set -euo pipefail`; multi-line `if:` uses `>-`.
- **D9.4** Docker layer cache targets a registry tag, not `type=gha`.
- **D9.5** Line endings follow `.editorconfig`.

## 5. Test methodology

An agent verifies a project against this contract in three escalating modes, then renders a verdict.

### 5A. Static audit (no execution)

Read the workflow files plus `version.json` and assert the observable structural fact behind each
D-guarantee. Each item is pass/fail with a `file:line` citation. The core checklist:

- **D1:** a paths-filter `changes` job exists and excludes `.github/workflows/**`; smoke calls the
  build task with `smoke: true`, `github: false` (and no registry/image push); every
  `upload-artifact` in the build tasks is gated `!smoke`; the required aggregator `needs:` `changes`
  and fails on `failure`/`cancelled` while passing on `skipped`.
- **D2:** an entry validation job/step exists for each complex-input workflow; the release gate
  checks both directions, strips `+buildmetadata`, and skips on smoke; the publisher rejects a
  non-default-ref dispatch; either/or inputs are validated.
- **D3:** the version step sets `IGNORE_GITHUB_REF=true`; `version.json` has `publicReleaseRefSpec`
  on the default branch only; PyPI applies a `.devN` off-default.
- **D4:** `target_commitish` is the built commit id; `prerelease` equals `branch != default`; the
  release-create step is gated on `exists == false || workflow_dispatch`; registry pushes use
  skip-duplicate/skip-existing.
- **D5:** each transfer artifact has a delete step at its consumer, **gated to the consumer's
  condition**, `continue-on-error: true`, looping all ids; **every** upload sets
  `retention-days: 1`; **no** `.artifacts[].id` blanket delete exists anywhere.
- **D6:** the release download uses `pattern:`/`merge-multiple:` (no `artifact-ids:`); config reads
  `inputs.branch` (grep for `github.ref_name` in branch-derived config is a finding); artifact
  names are branch-suffixed.
- **D7:** the publisher concurrency group is ref-independent with `cancel-in-progress: false`;
  reusable jobs declare permissions; boolean `if:` uses both forms.
- **D8/D9:** merge-bot concurrency keys on PR number; actions are SHA-pinned; names/suffixes and
  shells/conditionals follow section 2.

### 5B. End-to-end trace scenarios (no execution, deterministic from the YAML)

For each scenario, evaluate every job's `if:`/`needs:` against the inputs and emit the predicted
**run/skip + version + release + artifact-end-state** table, then compare to the expected. These
are deterministic from the workflow text - the cheapest way to catch a behavioral defect. Minimum
set (each lists the domains it exercises):

| # | Input | Expected output | Exercises |
| --- | --- | --- | --- |
| S1 | PR touching a build target (default-base) | `changes` flags it; `unit-test` runs; that target's smoke build runs (`smoke:true`); no push, **no uploads**; validate-release **skipped (smoke), succeeds**; release job **skipped**; aggregator **success**; version = prerelease; no release; no dangling artifacts | D1, D2.2, D3 |
| S2 | PR changing only docs | smoke-build **skipped**; `unit-test` runs; aggregator **success**; no build/release | D1.1, D1.5 |
| S3 | PR changing only `.github/workflows/**` | filter excludes -> smoke-build **skipped**; aggregator **success** (lint locally) | D1.4 |
| S4 | PR base = default branch, carrying a build target (promotion) | smoke versions as prerelease (detached head); validate-release **skipped (smoke)** so the default-branch arm does **not** fire; aggregator **success**; promotion not blocked | D1.3, D2.2 |
| S5 | push to non-default branch, opt-in variable unset | `setup` -> publish=false; nothing publishes | D4.1 |
| S6 | push to non-default branch, opt-in variable set | publish=true for that branch; it publishes a **prerelease** | D3, D4 |
| S7 | scheduled/dispatched publish from default branch | matrix builds both legs: non-default leg -> `X.Y.Z-g<sha>`, release `prerelease=true`, registry prerelease, assets consumed-then-deleted; default leg -> `X.Y.Z`, release `prerelease=false`, registry stable, badge/readme run; **no dangling artifacts** | D3, D4, D5, D6, D7 |
| S8 | `workflow_dispatch` from a non-default ref | `setup` **fails fast** with the guard error | D2.3 |
| S9 | re-run publish, version unchanged (tag exists) | schedule -> release-create **skipped**, paired delete **skipped**, registry pushes no-op; dispatch -> refresh; Docker re-pushes; no duplicate release | D4.4, D5.2 |
| S10 | a build whose branch and version classification disagree | validate-release **fails loud**; build/publish skip; nothing bad ships | D2.2 |

### 5C. Live probe (where warranted)

- Open a trivial-change PR touching one target and confirm S1 in the run.
- Drive a `smoke: true` push-probe of the build task for **both** `branch: <default>` and
  `branch: <non-default>` and assert the version classification (clean vs prerelease) and that the
  gate passes - **without publishing**. This proves D3.1/D2.2 end to end at near-zero risk.
- Inspect the latest real publish's logs for `PublicRelease`/`SemVer2` per leg and confirm the
  artifact lifecycle (uploaded, consumed, deleted; none left behind).

### Assessment

The workflow is **operational** iff every applicable 5A item passes **and** every 5B scenario's
observed output equals the expected (confirmed by 5C where a live signal is available). Any mismatch
is a **defect** -> **not operational**. Recommended agent procedure:

1. **Audit** with 5A; record each pass/fail with `file:line`.
2. **Trace** S1-S10 with 5B; diff predicted vs expected tables.
3. **Probe** with 5C only for guarantees a static trace cannot fully settle (live version
   classification, real artifact lifecycle).
4. **Verdict:** operational / not operational, with the failing guarantee(s) and the input that
   triggers each.

## 6. Per-project-type test walkthroughs

The template covers several project shapes. Each maps the same S1-S10 onto its targets; the
differences are which leaf tasks exist and what each produces. Walking these is the self-check that
the contract holds for every shape.

- **Console / executable application.** Target produces `release-asset-<branch>-executable` (a
  zip of `dotnet publish` output) from a per-runtime matrix; the per-runtime intermediates
  (`publish-<branch>-<runtime>`) are themselves transfer artifacts and MUST be consume-then-deleted
  by the aggregation job (D5.1). Test: S1 with a console change smoke-builds a reduced runtime
  subset and uploads nothing; S7 attaches the executable zip, prerelease on the non-default leg and
  Latest on the default leg.
- **NuGet library.** Target both pushes (`dotnet nuget push --skip-duplicate`) and uploads
  `release-asset-<branch>-nugetlibrary`. Test: S7 non-default leg publishes a **prerelease** package
  (`X.Y.Z-g<sha>`, `isPrerelease=true`) plus the asset; default leg publishes a stable `X.Y.Z`; S9
  re-run is a `--skip-duplicate` no-op.
- **PyPI library.** Leaf builds + uploads `pypilibrary-build-<branch>`; a **separate** publish job
  does the OIDC Trusted-Publishing upload (`id-token: write`, an environment gate) and then
  **consume-then-deletes** the build artifact; PyPI contributes no `release-asset-*`. Test: S7
  default leg publishes a release version, non-default a PEP 440 `.devN`; S9 re-run is a
  `skip-existing` no-op; confirm the build artifact is deleted after publish (D5.1).
- **Docker image.** Leaf pushes multi-arch tags with a registry buildcache; no `release-asset-*`
  (so `expect_release_assets` is false - the release is tag + source/README/LICENSE only); the
  Docker-Hub readme and date-badge run only when the default branch publishes. Docker **always**
  re-pushes (D4.4). A wrapper repo drives the immutable tag from an external version (D3.5). Test:
  S7 default leg pushes `latest` + the version tag and updates the readme/badge; non-default pushes
  the develop tag; S9 still re-pushes the image.
- **Data / asset library.** A single leaf: validate -> zip -> upload
  `release-asset-<branch>-library`; it drops the nuget/pypi/executable/docker jobs and the PyPI
  publish job and keeps the release orchestration verbatim. Test: S7 attaches the zip, prerelease on
  the non-default leg.
- **Source-only / no build.** No package/image leaf; validation lives in the PR workflow; the
  release is a tag + source zip + README + LICENSE (zero or one `release-asset-*`). Test: S1 runs
  validation only; S7 cuts a release with no build asset.
