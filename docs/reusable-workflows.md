# Hub-Hosted Reusable Workflows (Hub-Only)

The design for moving the fleet's standard GitHub Actions workflows out of every repo and into the hub, once, as reusable workflows a downstream repo reaches rather than carries. This doc is **hub-only** and is not carried downstream. It states the target model, the hook contract, the pin and secrets policy, the staged rollout with its completion state, and the measurement that tracks the burn-down. [GOVERNANCE.md "Hub-Hosted Tooling"][governance-hub-hosted-tooling] is the law this doc applies to workflows. [WORKFLOW.md][workflow] keeps the CI/CD contract every hosted workflow still has to satisfy.

## Table of Contents <!-- omit from toc -->

- [Why](#why)
- [Measured State](#measured-state)
- [Target Model](#target-model)
  - [Layers](#layers)
  - [The Hook Contract](#the-hook-contract)
  - [Pinning](#pinning)
  - [Secrets and Permissions](#secrets-and-permissions)
  - [The Hub Is Its Own First Consumer](#the-hub-is-its-own-first-consumer)
- [Hook Catalog](#hook-catalog)
- [The Docker Family](#the-docker-family)
- [Rollout](#rollout)
  - [Stage 0: Design, Measurement, and the Merge-Bot Task](#stage-0-design-measurement-and-the-merge-bot-task)
  - [Stage 1: Merge-Bot Adoption](#stage-1-merge-bot-adoption)
  - [Stage 2: The Gates](#stage-2-the-gates)
  - [Stage 3: The Pure Functions](#stage-3-the-pure-functions)
  - [Stage 4: The Release Chain and the Docker Core](#stage-4-the-release-chain-and-the-docker-core)
  - [Stage 5: The Type-Specific Tasks](#stage-5-the-type-specific-tasks)
- [Adopting the Merge-Bot](#adopting-the-merge-bot)
- [What a Pilot Proves](#what-a-pilot-proves)
- [Open Decisions](#open-decisions)

## Why

A standard workflow copied into twenty repos is twenty files that go stale at twenty different rates. A fix to a shared job is a fleet sweep rather than one edit, and a defect in a snippet seeds itself into every repo that adopted it. The carry-versus-reach test in [GOVERNANCE.md "Hub-Hosted Tooling"][governance-hub-hosted-tooling] already decides this for scripts: a file holding no per-repo content is a copy whose only future is to go stale. A workflow whose job graph is identical across repos of a type is that file.

The audit today grades a carried workflow at `interface` fidelity, meaning it checks the job keys and the artifact seam and leaves the body owned. That was the strongest contract the schema could express for a copied file. Two of the most-copied files, `publish-release.yml` and `validate-task.yml`, could not even take that, because their job lists come in six and twelve shapes across the fleet. Hosting the job graph in the hub is the contract the schema could not express.

## Measured State

`python3 spec/workflow_reuse.py --report` reads every cataloged repo's `.github/workflows/` from its ground-truth branch. It compares each file against the hub canonical of the same name, after the normalization the verbatim engine applies. It clusters the copies of each canonical into variants and writes [reports/workflow-reuse.md][workflow-reuse-report]. That report is the burn-down. Its fleet total names the files, the lines, the share of lines byte-identical to a canonical, and the files that already reach a hub reusable workflow or composite action. The migration is done when the copies of each standard workflow reach zero and the callers reach the number of repos that need it.

The first run, at hub `7c67328` on 2026-08-15 before the merge-bot moved, read 108 workflow files and 10,964 lines across 20 downstream repos. 5,385 of those lines, 49 percent, were byte-identical to a hub canonical, and the rest is mostly a per-repo edit of the same canonical rather than independent code. One file reaches the hub, PhotoCleaner's `validate-task.yml` consuming the `prose-gate` action. Every other copy carries its job bodies.

The identical share is read against the hub's current canonical, so it falls twice for one workflow: once when the hub canonical becomes a caller stub, since every copy still carrying the job bodies stops matching it, and again when the copies adopt the stub and their lines leave the fleet. The committed report is the second reading for the merge-bot, at hub `47c0c28`, where the identical share is 4,201 lines, 38 percent, with the files, the lines, and the callers unchanged. The three numbers to watch across phases are therefore the files, the lines, and the callers, and the identical share is the duplication that remains inside them.

## Target Model

A workflow whose job graph is identical across repos of a type is reached, not carried. The hub hosts it as a `workflow_call` reusable workflow. A downstream repo carries a caller stub, meaning the trigger, the concurrency group, one pinned `uses:`, an explicit `secrets:` map, and a `with:` block for the inputs it sets. Where a repo has behavior of its own, it carries a composite action at the conventional hook path. Everything else a repo carries today for that workflow is deleted.

### Layers

1. **The hub reusable workflow**, at `.github/workflows/<name>-task.yml` in the hub. It follows [GOVERNANCE.md "Workflow YAML Conventions"][governance-workflow-yaml-conventions], so the file ends `-task.yml` and its `name:` ends "task". It owns the job graph, the permissions each job needs, the validate-at-entry step, the artifact seam, retention, and the ruleset-bound aggregator name. It checks out the caller's repo by default. When it needs its own defaults or scripts, it checks out the hub at `${{ github.job_workflow_sha }}` under `.hub/`, which is the commit the caller pinned.
2. **The hook**, a composite action at `.github/actions/<hook>/action.yml` in the caller's repo. A hub job resolves it in one order: the caller's path when `hashFiles('.github/actions/<hook>/action.yml')` is non-empty, else the hub default at the same name under `.hub/`. A required hook with no default fails its job with `::error::` naming the missing path.
3. **The caller stub**, downstream, under thirty lines. The audit grades it at `interface` fidelity: the caller job key, the hub task the `uses:` names, and the secrets it maps are the contract, and the `with:` block is the repo's own.
4. **The hub's own use.** The hub calls its own task files by `./` path, so every hub pull request exercises the reusable file at least at parse level, and fully for the workflows the hub itself runs.

### The Hook Contract

A hook receives the fixed inputs [WORKFLOW.md "Reusable-Task Parameter Contract"][workflow-reusable-task-parameter-contract] gives a leaf today. Those are `ref`, `branch`, `smoke` where relevant, and the NBGV version outputs where a build needs them. It reports back through step outputs and, for a release target, through the existing artifact seam, `release-asset-<branch>-<target>`, which the hub's `github-release` job collects by pattern. A hook may use marketplace actions, which is the reason a hook is a composite action rather than a script. A toolchain setup, a Docker build, or a coverage upload is a `uses:` step, and a shell script cannot carry one. Action pins inside a hook follow the SHA-pinning rule like any other workflow content, and Dependabot bumps them in the repo that carries the hook.

### Pinning

A downstream `uses:` reads `ptr727/ProjectTemplate/.github/workflows/<name>-task.yml@<sha> # <tag>`. The SHA is the hub `main` commit a release tag points at, and the comment is that tag. Hub tags carry no `v`, so the comment reads `# 2.0.334` rather than `# v2.0.334`. A `develop` SHA is not bumpable, because Dependabot compares the pinned commit against tags and a commit no tag names has no version to bump from, which PhotoCleaner's `prose-gate` pin documents in place. Dependabot's `github-actions` ecosystem keeps reusable-workflow references current the same way it keeps action pins current, so a released pin bumps on the same schedule as every other action in the repo. The first such bump in the fleet is the live proof of that sentence.

A downstream pull request may pin a hub feature-branch SHA to test a hub change that is still in flight, and re-pins to a released `main` SHA before it merges. `scripts/repo_gate.py check_sha_pin` reads the owner and repository out of the reference and confirms the SHA resolves, for a reusable workflow exactly as for an action.

The sequencing consequence is that a hub task lands on `develop`, promotes to `main`, and is released before any downstream carries a bumpable pin. The catalog snippet for a caller stub therefore lands one release after the task it names, since a snippet under `catalog/snippets/workflows/` is scanned by the same pin gate and cannot carry a placeholder SHA. The [Rollout](#rollout) section carries that ordering as checkboxes per stage.

### Secrets and Permissions

Every hub task declares the secrets it needs by name under `on.workflow_call.secrets`, each `required: true`, and a caller maps each one explicitly. `secrets: inherit` is not used, since it is documented for a caller in the same organization or enterprise as the called workflow and the fleet is a personal account. The declared names are the ones [`spec/secrets.json`][secrets] already declares for the mechanism the task implements, so the secret audit and the workflow agree by construction.

A hub task declares no job-level `permissions:` where every write goes through the App token, and the caller sets `permissions: {}`. A called workflow can only keep or reduce the caller's grant. A callee job naming a scope the caller did not grant fails at startup even when its `if:` is false. Declaring nothing in the callee is therefore the shape that cannot fail against any caller, and it gives `GITHUB_TOKEN` no scope. A task whose job genuinely writes with `GITHUB_TOKEN`, such as a release upload, declares that scope in the callee job and documents it in the stub's comment so the caller grants it.

### The Hub Is Its Own First Consumer

The hub's own `.github/workflows/` carries the caller stubs it needs, each calling its task by `./` path. The stub is byte-shaped like a downstream stub apart from that one line, so a change to a task is felt in the hub's own CI first. This is also what makes the hook fallback path run on every hub pull request. The hub carries no hook of its own for a job with a default, so the default runs here on every change.

## Hook Catalog

The target set. A row exists once its hub task ships, and until then the row is the plan.

| Hub task | Hooks, at `.github/actions/<hook>` in the caller | Hub default |
| --- | --- | --- |
| `merge-bot-task.yml` | none, extra bot rules are a `with:` input | not applicable |
| `validate-task.yml` | `validate` (repo tests and lint beyond the fleet doc-lint block) | no-op |
| `test-pull-request-task.yml` | none, wires validate, smoke and the aggregator, `smoke` is a boolean input | not applicable |
| `get-version-task.yml`, `publish-plan-task.yml` | none | not applicable |
| `build-release-task.yml` | `build-executable`, `build-nuget`, `build-pypi`, `release-assets` (extra files) | executable, nuget and pypi defaults from today's snippets |
| `build-docker-task.yml` | `docker-prepare` (extra tags, build-args, matrix), `docker-build-base` | vanilla single-target from `image`, base build required when `build-base` |
| `publish-docker-readme-task.yml` | `docker-readme-transform` | publish `Docker/README.md` or `README.md` as-is |
| `publish-release-task.yml` | none, trigger policy stays in the caller stub and reaches the plan job as `event_name`, `actor` and `ref_name` | not applicable |
| `check-upstream-version-task.yml` | `resolve-upstream` | none, required |
| `deploy-site-task.yml`, `codegen-task.yml` | `deploy`, `codegen` | none, required |

## The Docker Family

The five live `build-docker-task.yml` copies share an identical core. It is QEMU and Buildx setup, a Docker Hub login on every build for the higher rate limit, and `docker/build-push-action` with a `type=registry` `buildcache-<branch>` cache. It tags `latest` or `develop` plus `SemVer2`, passes a `LABEL_VERSION` build-arg, and pushes the Docker Hub description on a `main` publish. What varies is data or a pre-step, never the core.

- **Vanilla single-target** repos differ only in the image name and the build-arg list. That is data, carried in the stub's `with:`.
- **Upstream-pinned** repos read a committed upstream version file before the build and add a `:<upstream-version>` tag and version build-args. That is a `docker-prepare` hook.
- **Multi-image** repos read a matrix file, optionally build base images first, then build each image with its own tags, args and cache repository. That is a `docker-prepare` hook emitting the matrix, plus a `docker-build-base` hook the task calls when `build-base` is set.

The hub task takes `push`, `ref`, `branch`, `smoke`, the NBGV version outputs, `image`, an optional `matrix` (a JSON list of `{name, tags, build-args, context, dockerfile, cache-repo}`, defaulting to the single entry `image` implies), and `build-base`. The core job body stays hub-owned, so the cache policy, the multi-arch platform selection (`linux/amd64,linux/arm64` on a non-smoke `main` build), the login-on-smoke, and the description push are decided once.

Docker Hub README publishing is a hub task of its own, `publish-docker-readme-task.yml`, with the size-limited overview, the repository list, and a `docker-readme-transform` hook in place of today's `transform-run` string input. The in-job description push in the build task is dropped in its favor, so the readme publishes once per release rather than once per image build. Upstream dependency monitoring is one hub task, `check-upstream-version-task.yml`, with a `resolve-upstream` hook in place of today's `resolver-command` string input and an `auto-merge` input. A tracker whose bump must wait for a human sets `auto-merge: false`, which gives the pull request a head prefix the merge-bot rules do not match. The rebuild-on-upstream-change trigger stays in the caller stub as a `push` filtered to the state file. Multi-stage Dockerfile builds are inconsistent across the Docker repos, and that is Dockerfile content rather than workflow content, so it is tracked as a type-level improvement beside this work rather than inside it.

## Rollout

This section is the tracker a session resumes from, and git is its only persistence. Every item is a checkbox with the evidence that closed it, a pull request, a commit, or a release tag, written into the item by the change that closed it. A session picking this work up reads this section first, takes the first unchecked item whose stage is open, verifies its claim against the current tree before acting on it, does the work in its own worktree, and ticks the item in the same pull request. Nothing here is ticked by intention: an item is ticked when the thing it names is on `develop`, or, for an adoption, on the named repo's ground-truth branch. [`TODO.md`][todo] "Hub-Hosted Reusable Workflows" carries the reasoning behind each stage, the open questions and what is settled, and this section carries the state.

A stage carries three kinds of item, plus a proof item where a claim needs a live run. **Hub** is the hub pull request that ships the task and its stub, and the catalog snippet that follows the release. **Release** is the promotion and release that gives the task a pinnable `main` commit, since a downstream stub pins a released tag and nothing can adopt before one exists. **Adoption** is one checkbox per repo, ticked when that repo's ground-truth branch carries the stub and the audit reports no `interface` finding on the file. **Proof** is a checkbox for a behavior only a live run demonstrates, ticked with the run URL. Stage 0 is the merge-bot's hub and release items plus its two proofs, and stage 1 is its adoption, split so the adoption list is a stage of its own. The exit metric per stage comes from [reports/workflow-reuse.md][workflow-reuse-report]: downstream copies of the stage's files fall to zero, callers rise to the number of repos that need the workflow, and downstream workflow lines fall from 10,964 toward the stubs plus the genuinely repo-specific hooks. Regenerate that report in the pull request that ticks a stage's last adoption, so the number and the tick land together.

### Stage 0: Design, Measurement, and the Merge-Bot Task

- [x] `docs/reusable-workflows.md`, `spec/workflow_reuse.py`, `reports/workflow-reuse.md`, `.github/workflows/merge-bot-task.yml`, the hub's own caller stub, and the manifest contract for `merge-bot-pull-request.yml`, on `develop` in #744 (`f33fa7e`).
- [x] Promoted to `main` in #746 (`20616e0`) and released as `2.0.338`, the first tag carrying `merge-bot-task.yml`.
- [x] The catalog caller snippet `catalog/snippets/workflows/merge-bot-pull-request.yml`, pinned to `20616e0a70613ad8727d567990f5d0e082f5275c # 2.0.338`, in #748, the pull request that added this section.
- [ ] The first Dependabot pull request against hub `develop` after `f33fa7e` merges through `merge-bot-task.yml`, proving the callee reads the caller's `pull_request_target` payload, the explicit `secrets:` map, App-token minting in a callee, and `permissions: {}` at the caller. Tick with the run URL. If it fails on the token grant, the fallback is at the caller, since a callee cannot widen what its caller grants: replace `permissions: {}` with `contents: read`, the least scope, and widen only to what the failing run names.
- [ ] The first Dependabot pull request against hub `main` after `20616e0` merges with `--merge`. Tick with the run URL.

### Stage 1: Merge-Bot Adoption

Adoptable since `2.0.338`. Each repo replaces the whole of its `.github/workflows/merge-bot-pull-request.yml` with the stub in [Adopting the Merge-Bot](#adopting-the-merge-bot), on its own feature branch, and the audit's `missing required job 'merge-bot'` finding on that file is the work list. The pilot goes first and records what the hub cannot prove, cross-repository resolution of the pin, the `rules` input where the repo has a tracker, and the first Dependabot bump of the pin, as proof items here.

- [ ] PhotoCleaner (pilot, chosen as a release-model repo with Dependabot, C#, executable and Docker targets and a fresh resync, so what it shows is the mechanism): adopted on `develop` in ptr727/PhotoCleaner#53 at `a3158ce` on 2026-08-15, where `python3 spec/audit.py PhotoCleaner --branch develop` reports no `interface` finding on the file. Ticks when that repo promotes `develop` to `main`, its ground-truth branch, which is also when the report below counts it as a caller. The three live proofs it owes, cross-repository resolution of the pin, a Dependabot PR to `develop` merged with `--squash` through the callee, and Dependabot bumping the pin, are the proof items in stage 0 and below, ticked with their run URLs when they happen.
- [ ] Proof: the first `pull_request_target` run on PhotoCleaner `develop` after `a3158ce` resolves the owner-scoped `uses:` and merges the Dependabot PR that opened it. Tick with the run URL.
- [ ] Proof: Dependabot opens a `Bump ptr727/ProjectTemplate` PR on PhotoCleaner after the next hub release. Tick with the PR.
- [ ] HomeAutomation-Config (operational model, the direct-to-develop path)
- [ ] homeassistant-purpleair (third, `rules: '[{"head-prefix": "ha-version-bump/", "base": "develop"}]'` and `delete-branch: true`)
- [ ] ESPHome-NonRoot (`delete-branch: true`, built-in upstream-version pairs cover its tracker)
- [ ] NxWitness (`delete-branch: true`, drops the Dependabot semver-major filter per D8.1 unless the open decision lands first)
- [ ] KiCadLibrary (drops the Dependabot semver-major filter per D8.1 unless the open decision lands first)
- [ ] LanguageTags (`delete-branch: true`)
- [ ] aiopurpleair (`delete-branch: true`)
- [ ] MediaTools (`delete-branch: true`)
- [ ] VSCode-Server-DotNetCore (`delete-branch: true`)
- [ ] Blog
- [ ] ESPHome-Config
- [ ] HomeAssistant-Config
- [ ] PlexCleaner
- [ ] Utilities
- [ ] Vantage-Config
- [ ] AudioCleaner (carries no merge-bot today, takes the stub on its next standup or resync, since the manifest applies it to every repo)
- [ ] DevKitCIoT (same)
- [ ] EspDinIoT (same)
- [ ] Financial-Modeling (same)
- [ ] HolidayLights (same)
- [ ] `reports/workflow-reuse.md` regenerated with `merge-bot-pull-request.yml` at 0 copies carrying job bodies and callers equal to the adopters.

### Stage 2: The Gates

Hub: `validate-task.yml` hosts the per-type doc-lint block once and calls the `validate` hook for a repo's own tests, deciding #729 in the one place the `uvx` tools are pinned or floated. `test-pull-request-task.yml` wires validate, smoke and the fixed aggregator name, and the stub carries the trigger shape, release or operational, which settles #585. This stage is where the hook fallback is first proven live: the hub carries no hook, so the default runs on every hub pull request, and the pilot's hook proves the override.

- [ ] Hub pull request on `develop` with both tasks, the hub's own stubs, the manifest contracts, and the catalog snippets left for the release that follows.
- [ ] Promoted and released, tag recorded here.
- [ ] Catalog snippets for both stubs pinned to that release.
- [ ] Hook fallback observed on a hub pull request run (default path) and on the pilot (override path), run URLs recorded here.
- [ ] PhotoCleaner (pilot, release trigger shape with smoke, the same repo that piloted stage 1)
- [ ] HomeAutomation-Config (second pilot, operational trigger shape)
- [ ] The remaining repos, one checkbox each added when the pilots close, since the sweep list is every cataloged repo.
- [ ] `reports/workflow-reuse.md` regenerated with `validate-task.yml` and `test-pull-request.yml` at 0 copies carrying job bodies.

### Stage 3: The Pure Functions

Hub: `get-version-task.yml` and `publish-plan-task.yml` hosted, and the downstream copies deleted on adoption. PlexCleaner gains the `plan` job D4.1 requires by adopting rather than by a copy.

- [ ] Hub pull request on `develop`.
- [ ] Promoted and released, tag recorded here.
- [ ] Adoption, one checkbox per carrier added when the hub pull request merges: today `get-version-task.yml` has 8 carriers and `publish-plan-task.yml` 3.
- [ ] `reports/workflow-reuse.md` regenerated with both at 0 copies.

### Stage 4: The Release Chain and the Docker Core

Hub: `build-release-task.yml` with `build-executable`, `build-nuget`, `build-pypi` and `release-assets` hooks, `publish-release-task.yml`, and `build-docker-task.yml` per [The Docker Family](#the-docker-family). The three no-asset release shapes collapse into `expect_release_assets`.

- [ ] Hub pull request on `develop`.
- [ ] Promoted and released, tag recorded here.
- [ ] PhotoCleaner (pilot, vanilla Docker plus executable)
- [ ] PlexCleaner (second, the same shape)
- [ ] VSCode-Server-DotNetCore (vanilla Docker only)
- [ ] ESPHome-NonRoot (`docker-prepare` hook for the upstream pin)
- [ ] NxWitness (matrix hook and `build-base`)
- [ ] The NuGet, PyPI and remaining release repos, one checkbox each added when the pilots close.
- [ ] `reports/workflow-reuse.md` regenerated with `build-release-task.yml`, `publish-release.yml` and `build-docker-task.yml` at 0 copies.

### Stage 5: The Type-Specific Tasks

Hub: `publish-docker-readme-task.yml` with a `docker-readme-transform` hook, `check-upstream-version-task.yml` with a `resolve-upstream` hook and an `auto-merge` input, deploy-site, codegen and the date badge, each with its hook. The `operational-vs-release-workflow` skill's note that the target list stays per repo is retired here.

- [ ] Hub pull request on `develop`.
- [ ] Promoted and released, tag recorded here.
- [ ] Adoption, one checkbox per carrier added when the hub pull request merges.
- [ ] `reports/workflow-reuse.md` regenerated, and the fleet total's callers equal to the sum of the stubs the fleet needs.

## Adopting the Merge-Bot

A downstream repo replaces the whole of its `.github/workflows/merge-bot-pull-request.yml` with the stub below, which is the catalog snippet `catalog/snippets/workflows/merge-bot-pull-request.yml` byte for byte, and deletes nothing else. The pin is the release that first carried the task, and Dependabot bumps it from there. Its App-signed pull requests keep merging by the built-in rules (`codegen-main` to `main`, `codegen-develop` to `develop`, `upstream-version-main` to `main`, `upstream-version-develop` to `develop`). A repo with a tracker outside those pairs adds one `rules` entry per pair, and a repo that keeps its repository-wide branch auto-delete off and still wants bot branches gone sets `delete-branch: true`.

```yaml
name: Merge bot pull request action

# Thin caller: the merge-bot is the hub's reusable merge-bot-task.yml, which every fleet repo reaches rather than carries.
# The trigger is pull_request_target so the called workflow resolves from the trusted base rather than the PR head, and no job checks out PR code.
on:
  pull_request_target:
    types: [opened, reopened, synchronize]

# Concurrency keys on the PR number rather than on github.ref, which under pull_request_target is the base branch and would serialize every bot PR against it, so each PR queues independently.
# The cancel-in-progress setting is false so a follow-up synchronize does not cancel an in-flight opened run before it enables auto-merge.
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number }}
  cancel-in-progress: false

# Every write in the called workflow uses the App token, so GITHUB_TOKEN gets no scope.
permissions: {}

jobs:

  merge-bot:
    name: Merge bot pull request job
    uses: ptr727/ProjectTemplate/.github/workflows/merge-bot-task.yml@20616e0a70613ad8727d567990f5d0e082f5275c # 2.0.338
    secrets:
      CODEGEN_APP_CLIENT_ID: ${{ secrets.CODEGEN_APP_CLIENT_ID }}
      CODEGEN_APP_PRIVATE_KEY: ${{ secrets.CODEGEN_APP_PRIVATE_KEY }}
    # A repo with a tracker outside the built-in codegen and upstream-version pairs adds a with: block carrying a rules JSON array of head or head-prefix plus base.
    # A repo that keeps the repository-wide auto-delete off and still wants bot branches gone sets delete-branch true in the same block.
```

A repo that needs either input appends the block to the `merge-bot` job. This is the shape for a repo with a `ha-version-bump/` tracker into `develop` that also wants its bot branches deleted:

```yaml
    with:
      rules: '[{"head-prefix": "ha-version-bump/", "base": "develop"}]'
      delete-branch: true
```

The task's inputs are `app-login` (default `ptr727-codegen[bot]`), `rules` (a JSON array of `{"head": "<exact>"}` or `{"head-prefix": "<prefix>"}` plus `"base"`, default `[]`), and `delete-branch` (default `false`). The merge method follows the base, `develop` squashes and `main` merges, so a rule carries none. An App pull request that matches no rule is annotated with a warning rather than merged, so a renamed tracker branch is visible in the run rather than silent.

Two copies today filter Dependabot by ecosystem and semver tier before merging. [WORKFLOW.md D8.1][workflow-d8] says every Dependabot tier auto-merges and the required checks are the gate, so those two repos drop the filter on adoption unless the [Open Decisions](#open-decisions) below settle otherwise.

## What a Pilot Proves

The hub's own stub proves most of the mechanics on the first Dependabot pull request after the task lands on `develop`. That run shows the callee reading the caller's `github.event.*` under `pull_request_target`. It shows an explicit `secrets:` map reaching the callee and the App token minting inside one. It shows `permissions: {}` at the caller not failing the callee at startup, and `--squash` running on `develop`. A Dependabot pull request against `main` after promotion proves `--merge`, and a maintainer push to a bot branch proves the disable job. A hub feature branch cannot test itself, since under `pull_request_target` the callee resolves from the base branch, so the proof follows the merge rather than preceding it.

Four things the hub cannot prove fall to the first downstream adopter. They are cross-repository resolution of the owner-scoped `uses:` reference, Dependabot bumping a `# <tag>` pin on a reusable workflow, and the `rules` input end to end on a repo with a tracker. The fourth is `merge-app` itself, since nothing opens App pull requests against the hub. A pilot records each of those as observed in its own audit report rather than assumed here.

## Open Decisions

- **`delete-branch` default.** `false` matches the hub's behavior, and seven repos opt in today. A fleet default of `true` is one edit to the task and removes seven `with:` blocks. The repository setting that protects `develop` from a promotion is unaffected either way, since a bot branch is never `develop`.
- **The Dependabot semver-major filter.** Two repos skip a nuget semver-major bump. Either it drops on adoption per D8.1, or the task grows a `skip-semver-major-ecosystems` input with a `dependabot/fetch-metadata` step run under the App token. Decide before those two repos adopt, everything else adopts unaffected.
- **A `requiredHubUses` audit contract.** The interface check today asserts the task filename token in the caller job. A field asserting the full owner-scoped form on a downstream copy and the `./` form on the hub is a small schema extension. It waits for the first adoption to show whether the token check misses anything.

<!-- Repo -->

[governance-hub-hosted-tooling]: ../GOVERNANCE.md#hub-hosted-tooling
[governance-workflow-yaml-conventions]: ../GOVERNANCE.md#workflow-yaml-conventions
[secrets]: ../spec/secrets.json
[workflow]: ../WORKFLOW.md
[workflow-d8]: ../WORKFLOW.md#d8---bots--automation
[workflow-reusable-task-parameter-contract]: ../WORKFLOW.md#reusable-task-parameter-contract
[todo]: ../TODO.md
[workflow-reuse-report]: ../reports/workflow-reuse.md
