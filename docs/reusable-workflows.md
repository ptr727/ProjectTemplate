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
- [Adopting the Gates](#adopting-the-gates)
- [Adopting the Pure Functions](#adopting-the-pure-functions)
- [Adopting the Release Chain](#adopting-the-release-chain)
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

1. **The hub reusable workflow**, at `.github/workflows/<name>-task.yml` in the hub. It follows [GOVERNANCE.md "Workflow YAML Conventions"][governance-workflow-yaml-conventions], so the file ends `-task.yml` and its `name:` ends "task". It owns the job graph, the permissions each job needs, the validate-at-entry step, the artifact seam, retention, and the ruleset-bound aggregator name. It checks out the caller's repo by default. When it needs its own defaults or scripts, it checks out the hub at `${{ job.workflow_repository }}`, `${{ job.workflow_sha }}` under `.hub/`, the repository and exact commit the caller pinned, so a fork of the hub checks out itself rather than a hard-coded upstream.
2. **The hook**, a composite action at `.github/actions/<hook>/action.yml` in the caller's repo. A hub job resolves it in one order: the caller's path when `hashFiles('.github/actions/<hook>/action.yml')` is non-empty, else the hub default at the same name under `.hub/`. A required hook with no default fails its job with `::error::` naming the missing path.
3. **The caller stub**, downstream, under thirty lines. The audit grades it at `interface` fidelity: the caller job key, the hub task the `uses:` names, and the secrets it maps are the contract, and the `with:` block is the repo's own.
4. **The hub's own use.** The hub calls its own task files by `./` path, so every hub pull request exercises the reusable file at least at parse level, and fully for the workflows the hub itself runs.

### The Hook Contract

A hook receives the fixed inputs [WORKFLOW.md "Reusable-Task Parameter Contract"][workflow-reusable-task-parameter-contract] gives a leaf today. Those are `ref`, `branch`, `smoke` where relevant, and the NBGV version outputs where a build needs them. It reports back through step outputs and, for a release target, through the existing artifact seam, `release-asset-<branch>-<target>`, which the hub's `github-release` job collects by pattern. A hook may use marketplace actions, which is the reason a hook is a composite action rather than a script. A toolchain setup, a Docker build, or a coverage upload is a `uses:` step, and a shell script cannot carry one. Action pins inside a hook follow the SHA-pinning rule like any other workflow content, and Dependabot bumps them in the repo that carries the hook.

### Pinning

A downstream `uses:` reads `ptr727/ProjectTemplate/.github/workflows/<name>-task.yml@<sha> # <tag>`. The SHA is the hub `main` commit a release tag points at, and the comment is that tag. Hub tags carry no `v`, so the comment reads `# 2.0.334` rather than `# v2.0.334`. A `develop` SHA is not bumpable, because Dependabot compares the pinned commit against tags and a commit no tag names has no version to bump from, which PhotoCleaner's `prose-gate` pin documents in place. Dependabot's `github-actions` ecosystem keeps reusable-workflow references current the same way it keeps action pins current, so a released pin bumps on the same schedule as every other action in the repo. The first such bump in the fleet is the live proof of that sentence.

A downstream pull request may pin a hub feature-branch SHA to test a hub change that is still in flight, and re-pins to a released `main` SHA before it merges. `scripts/repo_gate.py check_sha_pin` reads the owner and repository out of the reference and confirms the SHA resolves, for a reusable workflow exactly as for an action.

The sequencing consequence is that a hub task lands on `develop`, promotes to `main`, and is released before any downstream carries a bumpable pin. The catalog snippet for a caller stub therefore lands one release after the task it names, since a snippet under `catalog/snippets/workflows/` is scanned by the same pin gate and cannot carry a placeholder SHA. The [Rollout][rollout] section carries that ordering as checkboxes per stage.

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
| `validate-task.yml` | `validate` (a repo's own domain checks, beyond the fleet doc-lint block and the generic unit-test job) | no-op |
| `get-version-task.yml`, `publish-plan-task.yml` | none | not applicable |
| `build-release-task.yml` | `build-executable`, `build-nuget`, `build-pypi` | executable, nuget and pypi defaults from today's snippets |
| `build-docker-task.yml` | `docker-prepare` (extra tags, build-args, matrix), `docker-build-base` | vanilla single-target from `image`, base build required when `build-base` |
| `publish-docker-readme-task.yml` | `docker-readme-transform` | publish `Docker/README.md` or `README.md` as-is |
| `check-upstream-version-task.yml` | `resolve-upstream` | none, required |
| `deploy-site-task.yml`, `codegen-task.yml` | `deploy`, `codegen` | none, required |

## The Docker Family

The five live `build-docker-task.yml` copies share an identical core. It is QEMU and Buildx setup, a Docker Hub login on every build for the higher rate limit, and `docker/build-push-action` with a `type=registry` `buildcache-<branch>` cache. It tags `latest` or `develop` plus `SemVer2`, passes a `LABEL_VERSION` build-arg, and pushes the Docker Hub description on a `main` publish. What varies is data or a pre-step, never the core.

- **Vanilla single-target** repos differ only in the image name and the build-arg list. That is data, carried in the stub's `with:`.
- **Upstream-pinned** repos read a committed upstream version file before the build and add a `:<upstream-version>` tag and version build-args. That is a `docker-prepare` hook.
- **Multi-image** repos read a matrix file, optionally build base images first, then build each image with its own tags, args and cache repository. That is a `docker-prepare` hook emitting the matrix, plus a `docker-build-base` hook the task calls when `build-base` is set.

The hub task takes `push`, `ref`, `branch`, `smoke`, the NBGV version outputs, `image`, an optional `matrix` (a JSON list of `{name, tags, build-args, context, dockerfile, cache-repo}`, defaulting to the single entry `image` implies), and `build-base`. The core job body stays hub-owned, so the cache policy, the multi-arch platform selection (`linux/amd64,linux/arm64` on a non-smoke `main` build), and the login-on-smoke are decided once. The in-job description push earlier per-repo copies carried is not part of the hub core (see the readme paragraph below).

Docker Hub README publishing is a hub task of its own, `publish-docker-readme-task.yml`, with the size-limited overview, the repository list, and a `docker-readme-transform` hook in place of today's `transform-run` string input. The in-job description push in the build task is dropped in its favor, so the readme publishes once per release rather than once per image build. Upstream dependency monitoring is one hub task, `check-upstream-version-task.yml`, with a `resolve-upstream` hook in place of today's `resolver-command` string input and an `auto-merge` input. A tracker whose bump must wait for a human sets `auto-merge: false`, which gives the pull request a head prefix the merge-bot rules do not match. The rebuild-on-upstream-change trigger stays in the caller stub as a `push` filtered to the state file. Multi-stage Dockerfile builds are inconsistent across the Docker repos, and that is Dockerfile content rather than workflow content, so it is tracked as a type-level improvement beside this work rather than inside it.

## Rollout

This section is the tracker a session resumes from, and git is its only persistence. Every item is a checkbox with the evidence that closed it, a pull request, a commit, or a release tag, written into the item by the change that closed it. A session picking this work up reads this section first, takes the first unchecked item whose stage is open, verifies its claim against the current tree before acting on it, does the work in its own worktree, and ticks the item in the same pull request. Nothing here is ticked by intention: an item is ticked when the thing it names is on `develop`, or, for an adoption, on the named repo's ground-truth branch. [`TODO.md`][todo] "Hub-Hosted Reusable Workflows" carries the reasoning behind each stage, the open questions and what is settled, and this section carries the state.

A stage carries three kinds of item, plus a proof item where a claim needs a live run. **Hub** is the hub pull request that ships the task and its stub, and the catalog snippet that follows the release. **Release** is the promotion and release that gives the task a pinnable `main` commit, since a downstream stub pins a released tag and nothing can adopt before one exists. **Adoption** is one checkbox per repo, ticked when that repo's ground-truth branch carries the stub and the audit reports no `interface` finding on the file. **Proof** is a checkbox for a behavior only a live run demonstrates, ticked with the run URL. Stage 0 is the merge-bot's hub and release items plus its two proofs, and stage 1 is its adoption, split so the adoption list is a stage of its own. The exit metric per stage comes from [reports/workflow-reuse.md][workflow-reuse-report]: every downstream copy of the stage's files becomes a caller, so the callers column equals the copies column, and the report's downstream line total, 10,964 on the first run, falls toward the stubs plus the genuinely repo-specific hooks. Regenerate that report in the pull request that ticks a stage's last adoption, so the number and the tick land together.

### Stage 0: Design, Measurement, and the Merge-Bot Task

- [x] `docs/reusable-workflows.md`, `spec/workflow_reuse.py`, `reports/workflow-reuse.md`, `.github/workflows/merge-bot-task.yml`, the hub's own caller stub, and the manifest contract for `merge-bot-pull-request.yml`, on `develop` in #744 (`f33fa7e`).
- [x] Promoted to `main` in #746 (`20616e0`) and released as `2.0.338`, the first tag carrying `merge-bot-task.yml`.
- [x] The catalog caller snippet `catalog/snippets/workflows/merge-bot-pull-request.yml`, pinned to `20616e0a70613ad8727d567990f5d0e082f5275c # 2.0.338`, in #748, the pull request that added this section.
- [ ] The first Dependabot pull request against hub `develop` after `f33fa7e` merges through `merge-bot-task.yml`, proving the callee reads the caller's `pull_request_target` payload, the explicit `secrets:` map, App-token minting in a callee, and `permissions: {}` at the caller. Tick with the run URL. If it fails on the token grant, the fallback is at the caller, since a callee cannot widen what its caller grants: replace `permissions: {}` with `contents: read`, the least scope, and widen only to what the failing run names.
- [ ] The first Dependabot pull request against hub `main` after `20616e0` merges with `--merge`. Tick with the run URL.

### Stage 1: Merge-Bot Adoption

Adoptable since `2.0.338`. Each repo replaces the whole of its `.github/workflows/merge-bot-pull-request.yml` with the stub in [Adopting the Merge-Bot][adopting-the-merge-bot], on its own feature branch, and the audit's `missing required job 'merge-bot'` finding on that file is the work list. The pilot goes first and records what the hub cannot prove, cross-repository resolution of the pin, the `rules` input where the repo has a tracker, and the first Dependabot bump of the pin, as proof items here.

- [x] PhotoCleaner (pilot, chosen as a release-model repo with Dependabot, C#, executable and Docker targets and a fresh resync, so what it shows is the mechanism): adopted on `develop` in ptr727/PhotoCleaner#53 at `a3158ce` and promoted to `main`, its ground-truth branch, in ptr727/PhotoCleaner#54 at `4efcae8`, both on 2026-08-15, where `python3 spec/audit.py PhotoCleaner` reports no `interface` finding on the file. The live proofs it owes, cross-repository resolution of the pin with a Dependabot PR to `develop` merged with `--squash` through the callee, and Dependabot bumping the pin, are the two proof items directly below, ticked with their evidence when they happen.
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
- [ ] `reports/workflow-reuse.md` regenerated with `merge-bot-pull-request.yml` showing callers equal to copies.

### Stage 2: The Gates

Hub: `validate-task.yml` hosts a `lint` job (the fleet doc-lint block, language lint by tree detection, the prose gate, and the repo gate), a generic `unit-test` job (a `dotnet test` or a `uv run pytest`, skipped cleanly where the caller carries no test project), and a `validate` job resolving the `validate` hook for a repo's own domain checks, which decides #729 in the one place the `uvx` tools are pinned or floated. There is no `test-pull-request-task.yml`: the ruleset-bound aggregator stays in the caller stub by design, and a task wrapping one line that calls `validate-task.yml` hosts nothing generic, so the stub shapes live in [Adopting the Gates][adopting-the-gates] instead, with the trigger shape, operational or release, settling #585. This stage is where the hook fallback is first proven live: the hub carries its own `validate` hook (its registry and spec check, its script self-tests, its fleet-skills check, and its unclassified-character report), so a hub pull request exercises the override path, and a repo with no hook of its own exercises the default.

- [x] Hub pull request on `develop` with the task, the hub's own hook and default, the manifest contracts, and the catalog snippets left for the release that follows, [#760][pr-760].
- [ ] Promoted and released, tag recorded here.
- [ ] Catalog snippets for both stub shapes in [Adopting the Gates][adopting-the-gates] pinned to that release.
- [x] Hook override path observed on a hub pull request run, [proof run][override-path-run] (runs `./.github/actions/validate`, no hub checkout). Default path awaits a repo with no `validate` hook of its own.
- [ ] PhotoCleaner (pilot, release trigger shape with smoke, the same repo that piloted stage 1)
- [ ] HomeAutomation-Config (second pilot, operational trigger shape)
- [ ] The remaining repos, one checkbox each added when the pilots close, since the sweep list is every cataloged repo.
- [ ] `reports/workflow-reuse.md` regenerated with `validate-task.yml` at 0 copies (a hub-only file no repo carries) and `test-pull-request.yml` showing callers equal to copies.

### Stage 3: The Pure Functions

Hub: `get-version-task.yml` and `publish-plan-task.yml` hosted, and the downstream copies deleted on adoption. PlexCleaner gains the `plan` job D4.1 requires by adopting rather than by a copy.

- [x] Hub pull request on `develop`, #759.
- [ ] Promoted and released, tag recorded here.
- [ ] Catalog snippet for a caller stub, after the release: a caller of either task is a job inside a repo's own `publish-release.yml` or a future `build-release-task.yml` rather than a standalone top-level workflow, so today's honest answer is no snippet, only the `with:`/`uses:` lines in [Adopting the Pure Functions](#adopting-the-pure-functions). Revisit if an adopting repo's shape argues otherwise.
- [ ] Adoption, one checkbox per carrier added when the hub pull request merges: today `get-version-task.yml` has 8 carriers and `publish-plan-task.yml` 3, with ESPHome-NonRoot and NxWitness carrying both.
  - [ ] ESPHome-NonRoot (`get-version-task.yml` and `publish-plan-task.yml`)
  - [ ] NxWitness (`get-version-task.yml` and `publish-plan-task.yml`)
  - [ ] PhotoCleaner (`get-version-task.yml`)
  - [ ] PlexCleaner (`get-version-task.yml`, and gains the `plan` job D4.1 requires by adopting `publish-plan-task.yml` rather than by a copy)
  - [ ] VSCode-Server-DotNetCore (`get-version-task.yml`)
  - [ ] KiCadLibrary (`get-version-task.yml`)
  - [ ] aiopurpleair (`get-version-task.yml`)
  - [ ] homeassistant-purpleair (`get-version-task.yml`)
  - [ ] Utilities (`publish-plan-task.yml`)
- [ ] `reports/workflow-reuse.md` regenerated with `get-version-task.yml` and `publish-plan-task.yml` at 0 copies, since both are hub-only files no repo carries.

### Stage 4: The Release Chain and the Docker Core

Hub: `build-release-task.yml` with `build-executable`, `build-nuget`, `build-pypi` hooks, and `build-docker-task.yml` per [The Docker Family][the-docker-family]. The three no-asset release shapes collapse into `expect_release_assets`. No `publish-release-task.yml` ships: a caller stub's trigger policy and its `plan`, `validate`, `publish`, and `publish-pypi` jobs each reach one hub task directly, and none of that wiring is generic enough across the fleet's five trigger shapes (dispatch-only Docker schedule, push-gated NuGet/PyPI, KiCad's branch-matrix dispatch) to host as a further reusable workflow without becoming another set of caller-owned inputs. The `release-assets` hook (extra files beyond a target's own) stays unshipped too: no cataloged repo needs it today, and adding an unexercised hook is deferred until one does.

`build-release-task.yml` inlines its own `get-version` and `validate-release` jobs (no `./` nesting of the sibling `get-version-task.yml`) and duplicates the Docker core from `build-docker-task.yml` in its own `build-docker` job for the same reason: a hub task cannot reach a sibling hub task by a `./` path, since that path resolves against the caller's repository. `build-docker-task.yml` ships as its own hub task rather than being folded away, for a caller that wants the Docker leg without the rest of the release chain. The two carry the same job body and are kept in lockstep by hand, recorded in each file's own header comment. The `build-executable`, `build-nuget`, and `build-pypi` hub defaults take a `project-file` (or `project-dir`) input beyond the fixed `ref`/`branch`/`smoke` set, since the vanilla project layout (Console/Console.csproj, NuGetLibrary/NuGetLibrary.csproj, `PyPiLibrary/`) is a convention a real repo's own project name never matches. `build-release-task.yml` forwards its own caller-facing `executable_project`, `nuget_project`, and `pypi_project_dir` inputs into that same default's `project-file`/`project-dir` input. This mirrors the Docker hook's own `image` input rather than widening the fixed contract.

- [x] Hub pull request on `develop` in #762.
- [ ] Promoted and released, tag recorded here.
- [ ] PhotoCleaner (pilot, vanilla Docker plus executable)
- [ ] PlexCleaner (second, the same shape)
- [ ] VSCode-Server-DotNetCore (vanilla Docker only)
- [ ] ESPHome-NonRoot (`docker-prepare` hook for the upstream pin)
- [ ] NxWitness (matrix hook and `build-base`)
- [ ] The NuGet, PyPI and remaining release repos, one checkbox each added when the pilots close.
- [ ] A smoke build through `build-release-task.yml` observed on the PhotoCleaner or PlexCleaner pilot's pull request, run URL recorded here.
- [ ] A real publish through `build-release-task.yml` observed on the PhotoCleaner or PlexCleaner pilot, run URL recorded here.
- [ ] `reports/workflow-reuse.md` regenerated with `build-release-task.yml` and `build-docker-task.yml` at 0 copies (hub-only files) and `publish-release.yml` showing callers equal to copies.

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

Two copies today filter Dependabot by ecosystem and semver tier before merging. [WORKFLOW.md D8.1][workflow-d8] says every Dependabot tier auto-merges and the required checks are the gate, so those two repos drop the filter on adoption unless the [Open Decisions][open-decisions] below settle otherwise.

## Adopting the Gates

Adoptable once `validate-task.yml` is released. A downstream repo replaces its own `validate-task.yml` job bodies and its `test-pull-request.yml`'s inline lint job with one of the two stub shapes below, and deletes the copy of `validate-task.yml` per the `retire` disposition in `spec/divergences.json`. The pin is the release that first carries the task, shown here as a placeholder since no release exists yet: `@<hub-main-commit-sha> # <release-tag>`. `publish-release.yml`'s own `validate` job takes the same `uses:` line.

**No-build repos** carry the operational trigger shape [WORKFLOW.md "Branch Model"][workflow] states and [#585][issue-585] settles: a direct push to `develop` runs CI advisory (no required check binds the direct-commit allowance), and a `pull_request` to `main` or `develop` runs it pre-merge and actionable. A release-model repo with no build target takes the same stub with a `pull_request: branches: [main, develop]` trigger instead, since it has no direct-commit allowance to keep advisory.

```yaml
name: Test pull request action

# Thin caller: the gate is the hub's reusable validate-task.yml, which every fleet repo reaches rather than carries.
# Operational trigger shape (WORKFLOW.md "Branch Model"): a push to develop runs CI advisory, and a pull_request
# to main or develop runs it pre-merge and actionable, which is what makes D1.2 hold on this model too (#585).
on:
  push:
    branches: [develop]
  pull_request:
    branches: [main, develop]
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions: {}

jobs:

  validate:
    name: Validate sources job
    uses: ptr727/ProjectTemplate/.github/workflows/validate-task.yml@<hub-main-commit-sha> # <release-tag>
    permissions:
      contents: read

  # GitHub Actions does not support required status checks on conditional jobs, so a single always-run aggregator gates the merge.
  # Its name is the ruleset-bound required status-check context: rename it and the ruleset context together.
  check-workflow-status:
    name: Check pull request workflow status job
    runs-on: ubuntu-latest
    needs: [validate]
    if: always()
    steps:
      - name: Check workflow results step
        run: |
          set -Eeuo pipefail
          if [[ "${{ needs.validate.result }}" != "success" ]]; then
            echo "Job 'validate' did not succeed (${{ needs.validate.result }}); refusing to pass."
            exit 1
          fi
```

**Release repos with a smoke build** carry the standard `pull_request` trigger, a `changes` paths-filter job (WORKFLOW.md D1.1: each of the repo's own targets gets a filter entry, and `.github/workflows/**` is excluded per D1.4), and a `smoke-build` job. The smoke build calls the repo's own `./.github/workflows/build-release-task.yml` by local path rather than a hub task, since that orchestrator is not hosted until [Stage 4][stage-4].

```yaml
name: Test pull request action

on:
  pull_request:
    branches: [main, develop]
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions: {}

jobs:

  # Add one filter entry per target this repo builds; a touched target must never fall through unfiltered (D1.1).
  changes:
    name: Detect changed targets job
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
    outputs:
      release: ${{ steps.filter.outputs.release }}
    steps:
      - name: Checkout code step
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Filter changed paths step
        id: filter
        uses: dorny/paths-filter@fbd0ab8f3e69293af611ebaee6363fc25e6d187d # v4.0.1
        with:
          filters: |
            release:
              - '!.github/workflows/**'

  validate:
    name: Validate sources job
    uses: ptr727/ProjectTemplate/.github/workflows/validate-task.yml@<hub-main-commit-sha> # <release-tag>
    permissions:
      contents: read

  # Never publishes and never uploads (D1.3): smoke: true disables every publish path in build-release-task.
  smoke-build:
    name: Smoke build job
    needs: [changes]
    if: needs.changes.outputs.release == 'true'
    uses: ./.github/workflows/build-release-task.yml
    secrets: inherit
    with:
      smoke: true
      github: false
      dockerhub: false
      # On a pull_request event github.ref_name is the PR ref (for example 123/merge), never the target branch,
      # so the logical branch reads base_ref first and only falls back to ref_name on a non-PR trigger.
      branch: ${{ github.base_ref || github.ref_name }}

  # Treats a skipped smoke-build (an unchanged target) as pass, and blocks on failure or cancelled (D1.5, D7.4).
  check-workflow-status:
    name: Check pull request workflow status job
    runs-on: ubuntu-latest
    needs: [changes, validate, smoke-build]
    if: always()
    steps:
      - name: Check workflow results step
        run: |
          set -Eeuo pipefail
          for result in "changes:${{ needs.changes.result }}" "validate:${{ needs.validate.result }}" "smoke-build:${{ needs.smoke-build.result }}"; do
            name="${result%%:*}"
            value="${result#*:}"
            if [[ "$value" != "success" && "$value" != "skipped" ]]; then
              echo "::error::Job '$name' did not succeed ($value)."
              exit 1
            fi
          done
```

## Adopting the Pure Functions

Neither `get-version-task.yml` nor `publish-plan-task.yml` has a caller-stub snippet of its own, since a caller reaching either one is a job inside a repo's own `publish-release.yml` or a future `build-release-task.yml`, not a standalone top-level workflow. A repo whose publisher reads NBGV's version outputs directly, without carrying the whole release orchestrator, reaches `get-version-task.yml` by pin in place of its own copy:

```yaml
  get-version:
    name: Get version information job
    uses: ptr727/ProjectTemplate/.github/workflows/get-version-task.yml@<hub-main-commit-sha> # <release-tag>
    with:
      ref: ${{ github.ref }}
    # Outputs: SemVer2, AssemblyVersion, AssemblyFileVersion, AssemblyInformationalVersion, GitCommitId, Prerelease.
```

A repo whose publisher needs the release-gate decision reaches `publish-plan-task.yml` by pin the same way:

```yaml
  plan:
    name: Plan release job
    uses: ptr727/ProjectTemplate/.github/workflows/publish-plan-task.yml@<hub-main-commit-sha> # <release-tag>
    with:
      event_name: ${{ github.event_name }}
      actor: ${{ github.actor }}
      ref_name: ${{ github.ref_name }}
    # Outputs: publish, stable.
```

`build-release-task.yml` (stage 4) does not call `get-version-task.yml` this way. It inlines the get-version and validate-release jobs rather than nesting a sibling hub task, per the design's no-`./`-nesting rule: a local `uses:` inside a called workflow would resolve against the top-level caller's repository, not the hub. Only a caller that reads the version outputs on their own, without the rest of the release orchestrator, reaches `get-version-task.yml` directly, and a downstream `publish-release.yml` stub that does exactly that is the shape shown above.

## Adopting the Release Chain

A downstream repo replaces the whole of its `.github/workflows/build-release-task.yml`, and every per-target leaf task it carries (`build-executable-task.yml`, `build-nugetlibrary-task.yml`, `build-pypilibrary-task.yml`, `build-docker-task.yml`), with a caller stub in its own `publish-release.yml` reaching the hub tasks by pin. `test-pull-request.yml`'s smoke job calls `build-release-task.yml` the same way, with `smoke: true` and the paths-filter's `enable_*` outputs. Nothing here lands as a catalog snippet in this pull request, since a caller stub's pin can only name a released hub commit and no release yet carries `build-release-task.yml` ([Pinning][pinning]). The snippet follows the release that first ships it, tracked in the [Rollout][rollout] section below.

The stub keeps its own trigger policy exactly as today: `workflow_dispatch` plus a main-only weekly `schedule` for a Docker repo, or `workflow_dispatch` plus a paths-filtered `push` to `main` for a NuGet or PyPI repo whose merges should auto-publish. What moves to the hub is the release-gate decision, the build/version/publish job graph, and the Docker core, never the trigger. This is the full shape, a NuGet-library repo whose merges publish:

```yaml
name: Publish project release action

on:
  push:
    branches: [main]
    paths:
      - 'Widget/**'
      - 'version.json'
      - 'Directory.Build.props'
      - 'Directory.Packages.props'
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}
  cancel-in-progress: false

jobs:

  # Single source of the release-gate decision (publish? stable?), reused by every job below.
  plan:
    name: Plan release job
    uses: ptr727/ProjectTemplate/.github/workflows/publish-plan-task.yml@<sha> # <tag>
    with:
      event_name: ${{ github.event_name }}
      actor: ${{ github.actor }}
      ref_name: ${{ github.ref_name }}

  # The same reusable gate the PR runs, on the branch tip; runs only when a publish will happen.
  validate:
    name: Validate job
    needs: [plan]
    if: ${{ needs.plan.outputs.publish == 'true' }}
    uses: ptr727/ProjectTemplate/.github/workflows/validate-task.yml@<sha> # <tag>
    secrets:
      CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }}

  # Build, version, validate, push, and release the triggering branch. Grants the write scopes the hub
  # task needs (it declares none of its own except where a job genuinely writes, per its own header).
  publish:
    name: Publish project release job
    needs: [plan, validate]
    if: ${{ needs.plan.outputs.publish == 'true' }}
    uses: ptr727/ProjectTemplate/.github/workflows/build-release-task.yml@<sha> # <tag>
    secrets:
      NUGET_USERNAME: ${{ secrets.NUGET_USERNAME }}
    permissions:
      contents: write
      id-token: write
      actions: write
    with:
      ref: ${{ github.sha }}
      branch: ${{ github.ref_name }}
      smoke: false
      github: true
      nuget: true
      enable_docker: false
      enable_pypi: false
      enable_executable: false
      nuget_project: ./Widget/Widget.csproj
```

A Docker repo's stub adds `schedule: - cron: '0 2 * * MON'` to the trigger block, sets `enable_docker: true`, `dockerhub: true`, and `docker_image: ptr727/widget`, and maps `DOCKER_HUB_USERNAME`/`DOCKER_HUB_ACCESS_TOKEN` under `secrets:` instead of `NUGET_USERNAME`. A PyPI repo keeps `enable_pypi: true` and drops `id-token: write` from the `publish` job's grant, since PyPI publishing stays a separate job at the caller stub, `id-token: write` belonging at that one entry point, verbatim in shape to today's:

```yaml
  publish-pypi:
    name: Publish PyPI library job
    needs: [validate, publish]
    runs-on: ubuntu-latest
    environment:
      name: pypi
      url: https://pypi.org/project/<package>/
    permissions:
      id-token: write
      contents: read
    steps:
      - name: Download build artifacts step
        uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
        with:
          name: pypilibrary-build-${{ github.ref_name }}
          path: ./dist
      - name: Publish to PyPI step
        uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2
        with:
          packages-dir: ./dist
          skip-existing: true
```

No `publish-release-task.yml` ships alongside `build-release-task.yml`: the four jobs above are each a thin call to one hub task or a verbatim OIDC upload, and the trigger policy that ties them together genuinely differs enough across the fleet's shapes (dispatch-only Docker schedule, push-gated NuGet or PyPI, KiCadLibrary's branch-matrix dispatch) that hosting it would just move the same `with:` block one file over rather than removing it.

## What a Pilot Proves

The hub's own stub proves most of the mechanics on the first Dependabot pull request after the task lands on `develop`. That run shows the callee reading the caller's `github.event.*` under `pull_request_target`. It shows an explicit `secrets:` map reaching the callee and the App token minting inside one. It shows `permissions: {}` at the caller not failing the callee at startup, and `--squash` running on `develop`. A Dependabot pull request against `main` after promotion proves `--merge`, and a maintainer push to a bot branch proves the disable job. A hub feature branch cannot test itself, since under `pull_request_target` the callee resolves from the base branch, so the proof follows the merge rather than preceding it.

Four things the hub cannot prove fall to the first downstream adopter. They are cross-repository resolution of the owner-scoped `uses:` reference, Dependabot bumping a `# <tag>` pin on a reusable workflow, and the `rules` input end to end on a repo with a tracker. The fourth is `merge-app` itself, since nothing opens App pull requests against the hub. A pilot records each of those as observed in its own audit report rather than assumed here.

## Open Decisions

- **`delete-branch` default.** `false` matches the hub's behavior, and seven repos opt in today. A fleet default of `true` is one edit to the task and removes seven `with:` blocks. The repository setting that protects `develop` from a promotion is unaffected either way, since a bot branch is never `develop`.
- **The Dependabot semver-major filter.** Two repos skip a nuget semver-major bump. Either it drops on adoption per D8.1, or the task grows a `skip-semver-major-ecosystems` input with a `dependabot/fetch-metadata` step run under the App token. Decide before those two repos adopt, everything else adopts unaffected.
- **A `requiredHubUses` audit contract.** The interface check today asserts the task filename token in the caller job. A field asserting the full owner-scoped form on a downstream copy and the `./` form on the hub is a small schema extension. It waits for the first adoption to show whether the token check misses anything.

<!-- Sections -->

[adopting-the-gates]: #adopting-the-gates
[adopting-the-merge-bot]: #adopting-the-merge-bot
[open-decisions]: #open-decisions
[pinning]: #pinning
[rollout]: #rollout
[stage-4]: #stage-4-the-release-chain-and-the-docker-core
[the-docker-family]: #the-docker-family

<!-- Repo -->

[governance-hub-hosted-tooling]: ../GOVERNANCE.md#hub-hosted-tooling
[governance-workflow-yaml-conventions]: ../GOVERNANCE.md#workflow-yaml-conventions
[issue-585]: https://github.com/ptr727/ProjectTemplate/issues/585
[override-path-run]: https://github.com/ptr727/ProjectTemplate/actions/runs/31950332387/job/95172710046
[pr-760]: https://github.com/ptr727/ProjectTemplate/pull/760
[secrets]: ../spec/secrets.json
[todo]: ../TODO.md
[workflow]: ../WORKFLOW.md
[workflow-d8]: ../WORKFLOW.md#d8---bots--automation
[workflow-reusable-task-parameter-contract]: ../WORKFLOW.md#reusable-task-parameter-contract
[workflow-reuse-report]: ../reports/workflow-reuse.md
