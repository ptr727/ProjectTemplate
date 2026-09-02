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
- [Adopting the Type-Specific Tasks](#adopting-the-type-specific-tasks)
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

1. **The hub reusable workflow**, at `.github/workflows/<name>-task.yml` in the hub. It follows [GOVERNANCE.md "Workflow YAML Conventions"][governance-workflow-yaml-conventions], so the file ends `-task.yml` and its `name:` ends "task". It owns the job graph, the permissions each job needs, the validate-at-entry step, the artifact seam, retention, and the ruleset-bound aggregator name. It checks out the caller's repo by default. A hub-owned action or sibling workflow uses GitHub's `$/` self-repository syntax, which resolves it at the reusable workflow's commit. When a gate needs an implementation file, its composite action owns that file and invokes it through `GITHUB_ACTION_PATH`, so the workflow does not need a second hub checkout.
2. **The hook**, a composite action at `.github/actions/<hook>/action.yml` in the caller's repo. A hub job resolves it in one order: the caller's path when `hashFiles('.github/actions/<hook>/action.yml')` is non-empty, else the hub default through `$/.github/actions/<default>`. A required hook with no default fails its job with `::error::` naming the missing path.
3. **The caller stub**, downstream, under thirty lines. The audit grades it at `interface` fidelity: the caller job key, the hub task the `uses:` names, and the secrets it maps are the contract, and the `with:` block is the repo's own.
4. **The hub's own use.** The hub calls its own task files by `./` path, so every hub pull request exercises the reusable file at least at parse level, and fully for the workflows the hub itself runs.

### The Hook Contract

A hook receives the fixed inputs [WORKFLOW.md "Reusable-Task Parameter Contract"][workflow-reusable-task-parameter-contract] gives a leaf today. Those are `ref`, `branch`, `smoke` where relevant, and the NBGV version outputs where a build needs them. It reports back through step outputs and, for a release target, through the existing artifact seam, `release-asset-<branch>-<target>`, which the hub's `github-release` job collects by pattern. A package hook also uploads the artifact its publish job consumes: a NuGet hook uploads `nuget-build-<branch>` as well as its `release-asset-*`, and a PyPI hook uploads `pypi-build-<branch>` and no release asset at all. A NuGet or PyPI hook has to carry that exact package-artifact name or its publish job finds nothing. A hook may use marketplace actions, which is the reason a hook is a composite action rather than a script. A toolchain setup, a Docker build, or a coverage upload is a `uses:` step, and a shell script cannot carry one. Action pins inside a hook follow the SHA-pinning rule like any other workflow content, and Dependabot bumps them in the repo that carries the hook.

### Pinning

A downstream `uses:` reads `ptr727/ProjectTemplate/.github/workflows/<name>-task.yml@<sha> # <tag>`. The SHA is the hub `main` commit a release tag points at, and the comment is that tag. Hub tags carry no `v`, so the comment reads `# 2.0.334` rather than `# v2.0.334`. A `develop` SHA is not bumpable, because Dependabot compares the pinned commit against tags and a commit no tag names has no version to bump from, which PhotoCleaner's `prose-gate` pin documents in place. Dependabot's `github-actions` ecosystem keeps reusable-workflow references current the same way it keeps action pins current, so a released pin bumps on the same schedule as every other action in the repo. The first such bump in the fleet is the live proof of that sentence.

A downstream pull request may pin a hub feature-branch SHA to test a hub change that is still in flight, and re-pins to a released `main` SHA before it merges. `scripts/repo_gate.py check_sha_pin` reads the owner and repository out of the reference and confirms the SHA resolves, for a reusable workflow exactly as for an action.

The sequencing consequence is that a hub task lands on `develop`, promotes to `main`, and is released before any downstream carries a bumpable pin. The catalog snippet for a caller stub therefore lands one release after the task it names, since a snippet under `catalog/snippets/workflows/` is scanned by the same pin gate and cannot carry a placeholder SHA. The [Rollout][rollout] section carries that ordering as checkboxes per stage.

### Secrets and Permissions

Every hub task declares the secrets it needs by name under `on.workflow_call.secrets`, and a caller maps each one explicitly. Most are `required: true`. A mechanism's secret is `required: false` where the task treats it as one of several opt-in targets, such as `DOCKER_HUB_USERNAME`/`DOCKER_HUB_ACCESS_TOKEN` in `build-release-task.yml`. A package-registry credential is not among them, since `NUGET_USERNAME` and the PyPI OIDC exchange are read by the caller stub's own `publish-nuget` / `publish-pypi` job rather than passed into the task, per [Adopting the Release Chain][adopting-the-release-chain]. The same names are `required: true` in a task built around that one mechanism instead, such as `DOCKER_HUB_USERNAME`/`DOCKER_HUB_ACCESS_TOKEN` in `build-docker-task.yml`. Whether `secrets: inherit` is used is decided by the call's own boundary, not by the fleet's preference. [GitHub documents the keyword][gh-reusing-workflows] for a caller in the same organization or enterprise as the called workflow, and the fleet is a personal account. So a cross-repository call to a hub task names each secret it passes, and `inherit` is never used on one. A call whose job needs none passes no `secrets:` key, which is what the [Adopting the Gates][adopting-the-gates] `validate` stub does. A call by local path stays inside one repository. There the caller's own secret store is the one the called workflow reads, so `inherit` is available. Availability is not a reason to use it, and this repository's own local-path calls name their secrets or pass none. The [Adopting the Gates][adopting-the-gates] smoke-build stub carries the local-path shape for the same reason. Both shapes run in the fleet today, one repo carrying a local-path `inherit` call beside a cross-repository call that names its secrets, and another proving an inherited value reaches a publishing task that authenticates from it. The declared names are the ones [`spec/secrets.json`][secrets] already declares for the mechanism the task implements, so the secret audit and the workflow agree by construction. An environment-scoped secret is the exception. `DEPLOY_SSH_PRIVATE_KEY` and the `SITE_AUTH_TOKEN_ID`/`SITE_AUTH_TOKEN` pair beside it cross a GitHub Environment boundary `spec/secrets.json` has no vocabulary for, per its `deploy-ssh` mechanism note.

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
| `build-release-task.yml` | `dotnet-publish`, `build-nuget`, `build-pypi` | `dotnet-publish-default`, `nuget-build-default`, `pypi-build-default` |
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
- [x] The first Dependabot pull request against hub `develop` after `f33fa7e` merges through `merge-bot-task.yml`, proving the callee reads the caller's `pull_request_target` payload, the explicit `secrets:` map, App-token minting in a callee, and `permissions: {}` at the caller. If it fails on the token grant, the fallback is at the caller, since a callee cannot widen what its caller grants: replace `permissions: {}` with `contents: read`, the least scope, and widen only to what the failing run names. Dependabot PR #771 merged to `develop` with `--squash` in [run-771][run-771]. The callee job `Merge dependabot pull request job` succeeded, so `permissions: {}` held and the fallback was not needed.
- [x] The first Dependabot pull request against hub `main` after `20616e0` merges with `--merge`. Dependabot PR #770 merged to `main` with `--merge` in [run-770][run-770]. Both pull requests were merged by `app/ptr727-codegen`.

### Stage 1: Merge-Bot Adoption

Adoptable since `2.0.338`. Each repo replaces the whole of its `.github/workflows/merge-bot-pull-request.yml` with the stub in [Adopting the Merge-Bot][adopting-the-merge-bot], on its own feature branch, and the audit's `missing required job 'merge-bot'` finding on that file is the work list. The pilot goes first and records what the hub cannot prove, cross-repository resolution of the pin, the `rules` input where the repo has a tracker, and the first Dependabot bump of the pin, as proof items here.

- [x] PhotoCleaner (pilot, chosen as a release-model repo with Dependabot, .NET publish and Docker targets and a fresh resync, so what it shows is the mechanism): adopted on `develop` in ptr727/PhotoCleaner#53 at `a3158ce` and promoted to `main`, its ground-truth branch, in ptr727/PhotoCleaner#54 at `4efcae8`, both on 2026-08-15, where `python3 spec/audit.py PhotoCleaner` reports no `interface` finding on the file. The live proofs it owes, cross-repository resolution of the pin with a Dependabot PR to `develop` merged with `--squash` through the callee, and Dependabot bumping the pin, are the two proof items directly below, ticked with their evidence when they happen.
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
- [x] Promoted to `main` in #774 (`0b07a59d`) and released as `2.0.352`, the first tag carrying `validate-task.yml`.
- [ ] Catalog snippets for both stub shapes in [Adopting the Gates][adopting-the-gates] pinned to that release. The no-build shape has one, `catalog/snippets/workflows/test-pull-request.yml`. The release-with-smoke shape still calls its own repo's `build-release-task.yml` by `./` path rather than the hub's, so it carries no catalog-ready pin yet, and this item stays open until it does.
- [x] Hook override path observed on a hub pull request run, [proof run][override-path-run] (runs `./.github/actions/validate`, no hub checkout). Default path observed on PhotoCleaner's adoption pull request, [pilot smoke run][pilot-smoke-run], where the hub's `validate-default` ran because that repo carries no `validate` hook. The follow-up self-reference pilot also runs the bundled prose and repository gates through `$/.github/actions/` without checking out the hub.
- [x] PhotoCleaner (pilot, release trigger shape with smoke, the same repo that piloted stage 1): ptr727/PhotoCleaner#55 on `develop` (`c80cb29`), promoted in ptr727/PhotoCleaner#56 (`fa91db0`), both on 2026-08-16. `test-pull-request.yml` calls the hub validate task and no repo hook was needed.
- [ ] HomeAutomation-Config (second pilot, operational trigger shape)
- [ ] The remaining repos, one checkbox each added when the pilots close, since the sweep list is every cataloged repo.
- [ ] `reports/workflow-reuse.md` regenerated with `validate-task.yml` at 0 copies (a hub-only file no repo carries) and `test-pull-request.yml` showing callers equal to copies.

### Stage 3: The Pure Functions

Hub: `get-version-task.yml` and `publish-plan-task.yml` hosted, and the downstream copies deleted on adoption. PlexCleaner gains the `plan` job D4.1 requires by adopting rather than by a copy.

- [x] Hub pull request on `develop`, #759.
- [x] Promoted to `main` in #774 (`0b07a59d`) and released as `2.0.352`, the first tag carrying `get-version-task.yml` and `publish-plan-task.yml`.
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

Hub: `build-release-task.yml` provides the `dotnet-publish`, `build-nuget`, and `build-pypi` hooks. `build-docker-task.yml` follows [The Docker Family][the-docker-family]. The three no-asset release shapes collapse into `expect_release_assets`. No `publish-release-task.yml` ships. A caller stub's `plan`, `validate`, `publish`, `publish-nuget`, and `publish-pypi` jobs each reach one hub task directly or push what the task built. That wiring varies across the fleet's five trigger shapes, so another reusable workflow would become caller-owned inputs. The `release-assets` hook for extra files stays unshipped because no cataloged repo needs it.

`build-release-task.yml` reaches `get-version-task.yml` and `build-docker-task.yml` through `$/`, so both sibling tasks resolve at the same hub commit the downstream caller pins. It keeps `validate-release` inline because that gate belongs to the release orchestrator. `build-docker-task.yml` also ships as a task in its own right for a caller that wants only the Docker leg. The `dotnet-publish-default`, `nuget-build-default`, and `pypi-build-default` actions require explicit project paths. Each default action validates its required inputs when selected, while caller-provided hooks remain free to use different inputs.

- [x] Hub pull request on `develop` in #762.
- [x] Promoted to `main` in #774 (`0b07a59d`) and released as `2.0.352`, the first tag carrying `build-release-task.yml` and `build-docker-task.yml`. The first release attempt, on `82fecef`, ended in `startup_failure` in [run-startup-failure][run-startup-failure] because `build-nuget` and `github-release` declared job-level permissions. #772 fixed it before #774 promoted.
- [x] PhotoCleaner (pilot, vanilla Docker plus .NET publish): ptr727/PhotoCleaner#55 on `develop` (`c80cb29`), promoted in ptr727/PhotoCleaner#56 (`fa91db0`). The migration deleted five carried task files. It mapped every value to a task input (`dotnet_publish_project`, `docker_image`) and needed no repo hook. The [pilot publish run][pilot-publish-run] released `1.1.11` with the .NET publish asset and Docker image. That run exposed an archive naming regression between the hub default and the repo leaf. The fix derives the name from the project file, with `dotnet_publish_asset_name` as an override. The next PhotoCleaner release proves the fix.
- [ ] PlexCleaner (second, the same shape)
- [ ] VSCode-Server-DotNetCore (vanilla Docker only)
- [ ] ESPHome-NonRoot (`docker-prepare` hook for the upstream pin)
- [ ] NxWitness (matrix hook and `build-base`)
- [ ] The NuGet, PyPI and remaining release repos, one checkbox each added when the pilots close.
- [x] The [pilot smoke run][pilot-smoke-run] exercised `build-release-task.yml` on PhotoCleaner's pull request. Hub defaults ran get-version, validate-release, `dotnet-publish`, `docker-prepare`, and `build-docker`. NuGet, PyPI, and the base build skipped.
- [x] Cross-repository `$/` resolution observed on PhotoCleaner pull request #58 in [self-reference smoke run][self-reference-smoke-run]: nested get-version and Docker tasks, the .NET publish default, and the Docker prepare default all resolved from the pinned hub feature commit and passed.
- [x] A real publish through `build-release-task.yml` observed on the PhotoCleaner pilot, [pilot publish run][pilot-publish-run]: release `1.1.11` on `fa91db0` with `Publish GitHub release job` and `Build Docker image job` both succeeding.
- [x] The first real (non-smoke) NuGet publish through the chain, from ptr727/Utilities at `f3b4cc9` (`2.0.526`), failed the NuGet.org token exchange `HTTP 401` because the OIDC `job_workflow_ref` claim named this hub's `build-release-task.yml` rather than the publishing repository's own workflow. The build itself passed and every later job skipped, so the run published nothing and left no partial release. PhotoCleaner, the pilot, sets `enable_nuget: false`, so the NuGet leg had never run for real. Fixed by moving the push to the caller stub's own `publish-nuget` job, the shape `build-pypi` already used, per [Adopting the Release Chain][adopting-the-release-chain]. A smoke build never reaches either push, so no pull request can catch this class and each adopter's first real release is where it surfaces. Each NuGet adopter owes a stub edit with its next pin bump, dropping `nuget: true`, the `NUGET_USERNAME` secret mapping and `id-token: write` from its `publish` job and adding the `publish-nuget` job, since those two names are no longer declared on the task and a pin bump without the edit startup-fails.
- [ ] Proof: the next PhotoCleaner release names its .NET publish asset `PhotoCleaner.7z`, which the asset-name fix in this repository derives from the project file. Tick with the release.
- [ ] `reports/workflow-reuse.md` regenerated with `build-release-task.yml` and `build-docker-task.yml` at 0 copies (hub-only files) and `publish-release.yml` showing callers equal to copies.

### Stage 5: The Type-Specific Tasks

Hub: `publish-docker-readme-task.yml` with a `docker-readme-transform` hook, `check-upstream-version-task.yml` with a `resolve-upstream` hook and an `auto-merge` input, `deploy-site-task.yml` with a `deploy` hook, and `run-codegen-pull-request-task.yml` with a `codegen` hook. `build-datebadge-task.yml` is retired rather than hosted, TODO.md already tracks deleting the retired badge from its one remaining carrier. The `operational-vs-release-workflow` skill's note that the target list stays per repo is retired here, once the release-chain stage that makes it true has shipped.

- [x] Hub pull request on `develop` in #761.
- [x] Promoted to `main` in #774 (`0b07a59d`) and released as `2.0.352`, the first tag carrying `publish-docker-readme-task.yml`, `check-upstream-version-task.yml`, `deploy-site-task.yml`, and `run-codegen-pull-request-task.yml`.
- [ ] Adoption, one checkbox per carrier added when the hub pull request merges.
  - [ ] VSCode-Server-DotNetCore (`publish-docker-readme-task.yml`)
  - [ ] NxWitness (`publish-docker-readme-task.yml`, moving the readme job out of `publish-release.yml`, and `run-codegen-pull-request-task.yml` with its scheduler)
  - [ ] ESPHome-NonRoot (`check-upstream-version-task.yml`, both trackers, the second with `auto-merge: false`)
  - [ ] Blog (`deploy-site-task.yml`, keeping `deploy-site.yml` as its own caller)
  - [ ] LanguageTags (`run-codegen-pull-request-task.yml` and its scheduler)
  - [ ] KiCadLibrary (deletes `build-datebadge-task.yml` and its caller job outright, per TODO.md's retired-badge cleanup, adopting no new stub)
- [ ] Catalog snippets for `publish-docker-readme-task.yml`, `check-upstream-version-task.yml`, `deploy-site.yml`, `deploy-site-task.yml`, and `run-codegen-pull-request-task.yml` pinned to the release that first carries each task. `catalog/snippets/workflows/run-periodic-codegen-pull-request.yml` now exists, since [Codegen](#adopting-the-type-specific-tasks) already states it keeps the same per-repo shape as today. The other four stay open: `publish-docker-readme-task.yml` and `check-upstream-version-task.yml` are each a job embedded in a repo's own workflow rather than a standalone top-level caller with a snippet of its own, and `deploy-site.yml` carries no manifest-wide snippet by design, since each site's own shape varies around the shared `deploy` job.
- [ ] `reports/workflow-reuse.md` regenerated, and the fleet total's callers equal to the sum of the stubs the fleet needs.
- [x] The environment-secret handoff in the deploy-site adoption, the task's own `environment:` binding (not the caller's, which cannot carry one, [issue #942][issue-942]) resolving `DEPLOY_SSH_PRIVATE_KEY` from the caller's environment store across a cross-repository `uses:`. [Confirmed against Blog's own `staging` environment][run-cross-repo-secret-probe]: `DEPLOY_SSH_PRIVATE_KEY` resolved (masked, non-empty), and only the separately-tracked `SITE_BASE_URL` naming mismatch was missing.
- [x] The `deploy` hook's `verify` mode gained a same-shaped secret handoff as `DEPLOY_SSH_PRIVATE_KEY` above it. Blog's `checks/check-live-urls.sh` raised the case, against its own staging environment's token-gated auth. `deploy-site-task.yml` declares an optional, generic `SITE_AUTH_TOKEN_ID`/`SITE_AUTH_TOKEN` pair and forwards it into the `verify` invocation as `env:`, the named-pair shape decided in [issue #929][issue-929]. Which product gates a given environment, and how a caller maps its secrets to these two names, stays that repo's concern, not the hub's.
- [ ] The default `docker-readme-transform` action resolving through `$/` at the caller's pinned hub commit, observed on a caller with no override hook. Tick with the run URL.
- [ ] `$/` recognized by a released actionlint, so the scoped `.github/actionlint.yaml` ignores can drop.

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

A downstream repo replaces its own `validate-task.yml` job bodies and its `test-pull-request.yml`'s inline lint job with one of the two stub shapes, and deletes the copy of `validate-task.yml` per the `retire` disposition in `spec/divergences.json`. The [no-build caller snippet][no-build-caller-snippet] is the complete no-build shape and carries an adoptable released pin. The catalog snapshot stays at that bootstrap pin as later releases ship. After adoption, the first downstream Dependabot run advances the pin in `.github/workflows/test-pull-request.yml`. The release-with-smoke shape below keeps the placeholder `@<hub-main-commit-sha> # <release-tag>` until it has a catalog snippet of its own. A downstream `publish-release.yml` uses the same released pin for its `validate` job. The hub workflow uses its local task path instead.

**No-build repos** carry the operational trigger shape [WORKFLOW.md "Branch Model"][workflow] states and [#585][issue-585] settles: a direct push to `develop` runs CI advisory (no required check binds the direct-commit allowance), and a `pull_request` to `main` or `develop` runs it pre-merge and actionable. A release-model repo with no build target takes the same stub with a `pull_request: branches: [main, develop]` trigger instead, since it has no direct-commit allowance to keep advisory.

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

A repo that vendors a theme or imports content it does not author narrows the Lint Markdown step's glob instead. Blog carries a WordPress archive and the PaperMod theme, for instance. `.markdownlint-cli2.jsonc` is declared `"fidelity": "verbatim", "whole": true` in `spec/files.json`, so it is not locally editable:

```yaml
  validate:
    name: Validate sources job
    uses: ptr727/ProjectTemplate/.github/workflows/validate-task.yml@<hub-main-commit-sha> # <release-tag>
    permissions:
      contents: read
    with:
      markdown-exclude-globs: |
        !content/**
        !themes/*/**
```

`validate-task.yml` appends each line after `**/*.md` in the Lint Markdown step's own `globs:` block, unvalidated. A negated glob excludes, the intended use, but a non-negated one adds to what is linted rather than narrowing it.

The repo gate's `sha-pin` and `eol-coverage` checks read the same tracked-file list, hitting the same wall for a vendored subtree carrying its own CI. PaperMod's own workflows pin actions by floating tag, which Blog does not author. Per `themes/README.md`'s byte-identical-to-upstream invariant, Blog does not locally edit them either. `repo-gate-exclude-globs` narrows that scan the same way, one git pathspec pattern per line:

```yaml
    with:
      repo-gate-exclude-globs: |
        themes/PaperMod/**
```

`validate-task.yml` passes each line straight through to the `repo-gate` composite action's own `exclude-globs` input. That input turns each line into a `--exclude` argument for `repo_gate.py`. Unlike `markdown-exclude-globs`, a line here is never negated: it is always a pathspec to drop from `git ls-files`. So `themes/PaperMod/**` excludes, rather than `!themes/PaperMod/**`.

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

`build-release-task.yml` also calls `get-version-task.yml` through `$/`, so the sibling resolves at the release task's pinned hub commit. A caller that needs the version outputs without the rest of the release orchestrator reaches `get-version-task.yml` directly, using the pinned owner-scoped form shown above.

## Adopting the Release Chain

A downstream repo replaces its carried release orchestrator and per-target leaf tasks with a caller stub in its own `publish-release.yml` reaching the hub tasks by pin. `test-pull-request.yml`'s smoke job calls `build-release-task.yml` the same way, with `smoke: true` and the paths-filter's `enable_*` outputs. The renamed .NET publish interface is not yet in a hub release, so the full shape below keeps a pin placeholder and the catalog snippet is withheld until that release exists ([Pinning][pinning]). The task declares no job-level `permissions:` of its own, because a called job's block is validated against the caller's grant before its `if:` runs and would fail a caller that does not grant it at startup. The caller therefore grants only what its enabled paths write with: `contents: write` and `actions: write` when it sets `github: true` on a non-smoke run (the release upload and the artifact cleanup), and nothing beyond `contents: read` on a build-only or smoke run, where a Dependabot pull request holds a read-only token. No call to this task ever needs `id-token: write`, because neither package push happens inside it, for the reason the next paragraph gives.

Neither package push runs inside the hub task, and that is a constraint rather than a preference. NuGet.org and PyPI trusted publishing both validate the OIDC token's `job_workflow_ref` claim against the repository that owns the package, and that claim names the workflow file the job actually ran from. A job running from a hub task therefore carries `ptr727/ProjectTemplate/.github/workflows/build-release-task.yml@<sha>`, and the token exchange is rejected, NuGet.org answering `HTTP 401` with `does not start with <owner>/<repo>/.github/workflows/` and PyPI rejecting the same shape under its own code. A caller hook does not avoid it, since a composite action runs inside the hub's job and leaves the claim unchanged. The hub task instead builds the package and uploads it as `nuget-build-<branch>` or `pypi-build-<branch>`, and the caller stub's own `publish-nuget` or `publish-pypi` job downloads that artifact and pushes, so the claim names the publishing repository. A smoke build never reaches either push, which is why a pull request cannot catch this and the first real release is where it surfaces.

The trusted-publishing policy on NuGet.org and PyPI therefore keeps naming the publishing repository and its own `publish-release.yml`, and adopting the chain does not change it. Pointing a policy at the hub's workflow file instead would let any repository calling that task publish that package, so it is not the fix.

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

# GITHUB_TOKEN gets no scope by default, and each job below grants only what its hub task writes with.
permissions: {}

jobs:

  # Single source of the release-gate decision (publish or not, stable or not), reused by every job below.
  plan:
    name: Plan release job
    uses: ptr727/ProjectTemplate/.github/workflows/publish-plan-task.yml@<hub-main-commit-sha> # <release-tag>
    with:
      event_name: ${{ github.event_name }}
      actor: ${{ github.actor }}
      ref_name: ${{ github.ref_name }}

  # The same reusable gate the PR runs, on the branch tip, running only when a publish will happen.
  validate:
    name: Validate job
    needs: [plan]
    if: ${{ needs.plan.outputs.publish == 'true' }}
    uses: ptr727/ProjectTemplate/.github/workflows/validate-task.yml@<hub-main-commit-sha> # <release-tag>
    permissions:
      contents: read
    secrets:
      CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }}

  # Build, version, validate, and release the triggering branch.
  # Grants the write scopes the hub task needs (it declares none of its own except where a job genuinely writes, per its own header).
  publish:
    name: Publish project release job
    needs: [plan, validate]
    if: ${{ needs.plan.outputs.publish == 'true' && needs.validate.result == 'success' }}
    uses: ptr727/ProjectTemplate/.github/workflows/build-release-task.yml@<hub-main-commit-sha> # <release-tag>
    permissions:
      contents: write
      actions: write
    with:
      ref: ${{ github.sha }}
      branch: ${{ github.ref_name }}
      smoke: false
      github: true
      enable_docker: false
      enable_pypi: false
      enable_dotnet_publish: false
      nuget_project: ./Widget/Widget.csproj

  # The push lives here rather than in the hub task so the OIDC token's job_workflow_ref claim names this repository.
  # A skipped publish job skips this one too, so no separate release gate is needed.
  publish-nuget:
    name: Publish NuGet library job
    needs: [validate, publish]
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
      actions: write
    steps:
      - name: Download build artifacts step
        uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
        with:
          name: nuget-build-${{ github.ref_name }}
          path: ./nuget
      - name: Setup .NET SDK step
        uses: actions/setup-dotnet@a98b56852c35b8e3190ac28c8c2271da59106c68 # v6.0.0
        with:
          dotnet-version: 10.x
      # Trades the GitHub OIDC token for a short-lived NuGet key, so there is no stored API key.
      - name: NuGet login step
        id: nuget-login
        uses: NuGet/login@8d196754b4036150537f80ac539e15c2f1028841 # v1.2.0
        with:
          user: ${{ secrets.NUGET_USERNAME }}
      # Pushing the .nupkg also pushes the co-located .snupkg to nuget.org's symbol server, since no --no-symbols flag is set.
      - name: Push to NuGet.org step
        env:
          NUGET_API_KEY: ${{ steps.nuget-login.outputs.NUGET_API_KEY }}
        run: |
          set -Eeuo pipefail
          dotnet nuget push ./nuget/*.nupkg \
            --source https://api.nuget.org/v3/index.json \
            --api-key "$NUGET_API_KEY" \
            --skip-duplicate
      - name: Delete consumed NuGet build artifact step
        continue-on-error: true
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -Eeuo pipefail
          if ! ids=$(gh api "repos/$GITHUB_REPOSITORY/actions/runs/${{ github.run_id }}/artifacts" --paginate \
            --jq ".artifacts[] | select(.name == \"nuget-build-${{ github.ref_name }}\") | .id"); then
            echo "::warning::Could not list NuGet build artifacts. The retention-days backstop will reap them."
            ids=""
          fi
          for id in $ids; do
            if ! gh api --method DELETE "repos/$GITHUB_REPOSITORY/actions/artifacts/$id"; then
              echo "::warning::Failed to delete artifact $id. The retention-days backstop will reap it."
            fi
          done
```

A Docker repo's stub adds `schedule: - cron: '0 2 * * MON'` to the trigger block, sets `enable_docker: true`, `dockerhub: true`, `docker_image: ptr727/widget`, `enable_nuget: false` (dropping `nuget_project` with it) and `expect_release_assets: false`, maps `DOCKER_HUB_USERNAME`/`DOCKER_HUB_ACCESS_TOKEN` under the `publish` job's `secrets:`, and drops the `publish-nuget` job, since Docker Hub has no OIDC equivalent and the hub task pushes the image itself. A PyPI repo sets `enable_pypi: true` with `pypi_project_dir` and `pypi_version_file`, `enable_nuget: false` (dropping `nuget_project`) and `expect_release_assets: false`, since PyPI contributes no release asset, and swaps `publish-nuget` for the same shape one registry over, verbatim as today's:

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
      actions: write
    steps:
      - name: Download build artifacts step
        uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
        with:
          name: pypi-build-${{ github.ref_name }}
          path: ./dist
      - name: Publish to PyPI step
        uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2
        with:
          packages-dir: ./dist
          skip-existing: true
      - name: Delete consumed PyPI build artifact step
        continue-on-error: true
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -Eeuo pipefail
          if ! ids=$(gh api "repos/$GITHUB_REPOSITORY/actions/runs/${{ github.run_id }}/artifacts" --paginate \
            --jq ".artifacts[] | select(.name == \"pypi-build-${{ github.ref_name }}\") | .id"); then
            echo "::warning::Could not list PyPI build artifacts. The retention-days backstop will reap them."
            ids=""
          fi
          for id in $ids; do
            if ! gh api --method DELETE "repos/$GITHUB_REPOSITORY/actions/artifacts/$id"; then
              echo "::warning::Failed to delete artifact $id. The retention-days backstop will reap it."
            fi
          done
```

No `publish-release-task.yml` ships alongside `build-release-task.yml`: the jobs above are each a thin call to one hub task or a verbatim OIDC upload, and the trigger policy that ties them together genuinely differs enough across the fleet's shapes (dispatch-only Docker schedule, push-gated NuGet or PyPI, KiCadLibrary's branch-matrix dispatch) that hosting it would just move the same `with:` block one file over rather than removing it.

## Adopting the Type-Specific Tasks

Stage 5 hosts four more tasks: `publish-docker-readme-task.yml`, `check-upstream-version-task.yml`, `deploy-site-task.yml`, and `run-codegen-pull-request-task.yml`. The codegen pin below is filled in, `0b07a59d7c65d07d8df275a96deaf2e06cbefd51 # 2.0.352`, since `run-periodic-codegen-pull-request.yml` now has a catalog snippet, the same per-repo shape it carries today. The other three pins stay the placeholder `<sha>` and `<tag>`: `publish-docker-readme-task.yml` and `check-upstream-version-task.yml` are each a job embedded in a repo's own workflow rather than a standalone top-level caller with a snippet of its own, and `deploy-site.yml` carries no manifest-wide snippet by design, since each site's own shape varies around the shared `deploy` job. This section stays the adoption reference for those three until a follow-on catalog change lands, per [Pinning](#pinning).

`build-datebadge-task.yml` is retired rather than hosted. TODO.md "Delete the retired `byob.yarr.is` last-build badge" already settles that the badge service is deprecated and the badge is deleted rather than replaced, so no repo adopts a caller stub for it and KiCadLibrary's deletion is that same cleanup, not a migration to a hub task.

**Docker Hub readme.** A repo publishing an image replaces its `publish-docker-readme-task.yml` job body with a caller stub reaching the hub task. A `docker-readme-transform` hook, `.github/actions/docker-readme-transform/action.yml`, sets a `readme-filepath` step output naming the file to push, and is needed only to render the readme first or to override the hub default, which publishes `Docker/README.md` if present else `README.md` as-is.

```yaml
  publish-docker-readme:
    name: Publish Docker Hub readme job
    permissions:
      contents: read
    uses: ptr727/ProjectTemplate/.github/workflows/publish-docker-readme-task.yml@<sha> # <tag>
    with:
      branch: ${{ github.ref_name }}
    secrets:
      DOCKER_HUB_USERNAME: ${{ secrets.DOCKER_HUB_USERNAME }}
      DOCKER_HUB_ACCESS_TOKEN: ${{ secrets.DOCKER_HUB_ACCESS_TOKEN }}
```

The caller grants `contents: read` explicitly, since the task's own jobs declare that scope for their checkout steps and a callee can only keep or reduce what its caller grants, never widen it. A multi-image repo passes `manifest` and `manifest-jq` in place of relying on the single-repository default, the same as today. NxWitness derives its Docker Hub repository list from `./Make/Matrix.json` inline in `publish-release.yml` today rather than through a standalone file, so its adoption also moves that job to the stub above.

**Upstream-version tracker.** A repo tracking an upstream release replaces `check-upstream-version-task.yml`'s job body with a caller stub, and carries a required `resolve-upstream` hook, `.github/actions/resolve-upstream/action.yml`, setting a `versions` step output, a JSON object of name -> version.

```yaml
  check-upstream-version:
    name: Check upstream version job
    uses: ptr727/ProjectTemplate/.github/workflows/check-upstream-version-task.yml@<sha> # <tag>
    secrets:
      CODEGEN_APP_CLIENT_ID: ${{ secrets.CODEGEN_APP_CLIENT_ID }}
      CODEGEN_APP_PRIVATE_KEY: ${{ secrets.CODEGEN_APP_PRIVATE_KEY }}
```

ESPHome-NonRoot carries two trackers today. `check-upstream-version.yml` adopts the stub above as-is. `check-upstream-dependency.yml`, whose bump waits for a human because its head deliberately does not match a merge-bot rule, adopts a second instance of the same stub with `with: { branches: '["develop"]', bump-branch-prefix: upstream-dependency, auto-merge: false }` and a `resolve-upstream` hook shaped around its apt-package snapshot, setting `versions` to `{"docker_base_packages": "<sorted, comma-joined package list>"}` rather than a name-to-version map. The generic title and body this produces read less specifically than today's bespoke "packages added/removed" wording, which is the cost of folding a bespoke tracker into the shared task.

**Deploy-site.** A site repo keeps `deploy-site.yml` as a per-repo caller (it has no manifest-wide catalog snippet either, since its `uses:` names the hub, and it still carries the dispatch, the ref gate, and the shared validation call), but its `deploy` job reaches the hub-hosted `deploy-site-task.yml`. A job calling a reusable workflow cannot itself carry an `environment:` key ([GitHub's supported-keywords list][gh-reusing-workflows] omits it, and GitHub rejects the combination outright), so the caller's `deploy` job carries none. `DEPLOY_SSH_PRIVATE_KEY` still crosses correctly: the task's own `deploy` job binds `environment: ${{ inputs.environment }}` on itself, and per GitHub's own cross-repository behavior (the `github` context, and an OIDC token's `sub` claim, always attribute the environment to the *caller*), that resolves against the caller's own GitHub Environment store, not the hub's. The caller's `secrets: DEPLOY_SSH_PRIVATE_KEY: ${{ secrets.DEPLOY_SSH_PRIVATE_KEY }}` line is still required (the task declares this a required `workflow_call.secrets` input, and GitHub rejects a call omitting a required one), but what value the task's own job actually sees for it is governed by the task's own `environment:` binding, not by anything the caller's own (environment-less) job context could resolve. Confirmed both against GitHub's documented reusable-workflow secrets mechanics and with a live cross-repository run against Blog's own `staging` environment ([issue #942][issue-942]). `secrets: inherit` was never the alternative here regardless. It is not used on a cross-repository call at all, per [Secrets and Permissions][secrets-and-permissions], and it would not carry an environment-scoped secret across one either. A required `deploy` hook, `.github/actions/deploy/action.yml`, is invoked three times (`build`, `prune`, `verify`) so the site keeps its own generator, precompression, and URL contract while the upload-then-flip sequence stays hub-owned. The hook declares all four inputs the three invocations use between them, `mode`, `bundle-path`, `release-id`, and `environment`, since a composite action rejects an invocation that supplies an input it does not declare, even one a different mode leaves unset. Each invocation also passes the GitHub Environment variables that mode needs (`SITE_BASE_URL` to `build` and `verify`, `DEPLOY_SSH_USER` and `DEPLOY_SSH_HOST` to `prune`) as plain `env:` vars, since a composite action's own steps are not guaranteed to read the caller's `vars` context directly. `verify` additionally receives an optional `SITE_AUTH_TOKEN_ID`/`SITE_AUTH_TOKEN` secret pair the same way, forwarded whenever the caller maps it. A site whose live check sits behind its own token-gated auth is the reason it exists.

Blog is the reference adoption, and its real inventory is two scripts, not three: `deploy/make-release.sh` assembles, hard-links, stamps, and installs a release into whatever root it is pointed at, and `checks/check-live-urls.sh` verifies one against a running server. There is no `deploy/prune-releases.sh`. `build` mode wraps `make-release.sh` pointed at the hub-passed `bundle-path` rather than a live root, alongside whatever generator setup the hook itself needs, Hugo and brotli in Blog's case, that `make-release.sh` assumes are already on `PATH`. The script's own tail, a swap of a local `current` symlink to the release it just wrote and a check that the swap hard-linked something against whatever `current` pointed at before, runs entirely against that ephemeral `bundle-path`, so it is local bookkeeping rather than a second real deploy. It is also what leaves `bundle-path/current` in place for the hub task's own build-mode assertion to find. Because `bundle-path` is empty at the start of every run, that local `current` never resolves to anything and the hard-link check never has a previous release to compare against, so it is inert in CI. The `build` hook's own `current` is never the live one either way: only the hub-owned Upload release and Flip current steps that follow touch the real `/<environment>/` root, so the boundary the upload-then-flip sequence draws is between `bundle-path` and the environment, not a seam inside `make-release.sh` itself.

`prune` mode is a no-op for Blog. Its deploy credential is a forced `rsync` command confined write-only, so it can neither list nor delete the remote destination, and retention there is owned by the host's own daily timer instead, which Blog's own `OPERATIONS.md` records by name next to that ownership line. A site whose credential can observe its own destination prunes for real in this mode instead, the case `deploy-site-task.yml`'s own comment on that step already anticipates.

`verify` mode wraps `checks/check-live-urls.sh`, reading the `SITE_BASE_URL` `env:` var the same way `build` mode does. It also gets the optional `SITE_AUTH_TOKEN_ID`/`SITE_AUTH_TOKEN` pair, since Blog's staging environment sits behind its own token-gated auth proxy. Which product gates that environment is Blog's concern to document in its own repository, not the hub's. So is how Blog's own caller maps its secrets to those two generic names. A further adopter behind a different product's token gate needs no hub change. Its own caller just maps its own secret names to `SITE_AUTH_TOKEN_ID`/`SITE_AUTH_TOKEN`.

A hook, not a fixed path convention, is still the better contract even at two scripts: Blog's own generator setup (`install-hugo`, the mtime restore) and its staging auth check are exactly the per-site variation a hardcoded script name could not absorb.

```yaml
  deploy:
    name: Deploy job
    needs: [validate]
    permissions:
      contents: read
    uses: ptr727/ProjectTemplate/.github/workflows/deploy-site-task.yml@<sha> # <tag>
    with:
      environment: ${{ inputs.environment }}
    secrets:
      DEPLOY_SSH_PRIVATE_KEY: ${{ secrets.DEPLOY_SSH_PRIVATE_KEY }}
```

**Codegen.** A repo generating checked-in files from an external source replaces `run-codegen-pull-request-task.yml`'s job body with a caller stub, and carries a required `codegen` hook, `.github/actions/codegen/action.yml`, running only the generator invocation. `run-periodic-codegen-pull-request.yml` stays a per-repo caller stub the same shape it is today, calling the hub task by its pinned `uses:` in place of `./`. The full file is now `catalog/snippets/workflows/run-periodic-codegen-pull-request.yml`.

```yaml
  run-codegen:
    name: Run codegen and pull request job
    uses: ptr727/ProjectTemplate/.github/workflows/run-codegen-pull-request-task.yml@0b07a59d7c65d07d8df275a96deaf2e06cbefd51 # 2.0.352
    secrets:
      CODEGEN_APP_CLIENT_ID: ${{ secrets.CODEGEN_APP_CLIENT_ID }}
      CODEGEN_APP_PRIVATE_KEY: ${{ secrets.CODEGEN_APP_PRIVATE_KEY }}
```

The task is .NET-specific orchestration. It installs the .NET SDK, runs the caller's `codegen` hook, restores the repository's .NET tools, formats with CSharpier, and opens the branch pull requests. The hook carries only the repository's generator invocation, such as `dotnet run --project ...`. It receives no legacy generator-specific secret.

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
[adopting-the-release-chain]: #adopting-the-release-chain
[open-decisions]: #open-decisions
[pinning]: #pinning
[rollout]: #rollout
[secrets-and-permissions]: #secrets-and-permissions
[stage-4]: #stage-4-the-release-chain-and-the-docker-core
[the-docker-family]: #the-docker-family

<!-- Repo -->

[gh-reusing-workflows]: https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations
[governance-hub-hosted-tooling]: ../GOVERNANCE.md#hub-hosted-tooling
[governance-workflow-yaml-conventions]: ../GOVERNANCE.md#workflow-yaml-conventions
[issue-585]: https://github.com/ptr727/ProjectTemplate/issues/585
[issue-929]: https://github.com/ptr727/ProjectTemplate/issues/929
[issue-942]: https://github.com/ptr727/ProjectTemplate/issues/942
[no-build-caller-snippet]: ../catalog/snippets/workflows/test-pull-request.yml
[override-path-run]: https://github.com/ptr727/ProjectTemplate/actions/runs/31950332387/job/95172710046
[pilot-publish-run]: https://github.com/ptr727/PhotoCleaner/actions/runs/31977092102
[pilot-smoke-run]: https://github.com/ptr727/PhotoCleaner/actions/runs/31974932749
[self-reference-smoke-run]: https://github.com/ptr727/PhotoCleaner/actions/runs/32047594855
[pr-760]: https://github.com/ptr727/ProjectTemplate/pull/760
[run-cross-repo-secret-probe]: https://github.com/ptr727/Blog/actions/runs/32618245296
[run-770]: https://github.com/ptr727/ProjectTemplate/actions/runs/31972611554
[run-771]: https://github.com/ptr727/ProjectTemplate/actions/runs/31972622149
[run-startup-failure]: https://github.com/ptr727/ProjectTemplate/actions/runs/31972504539
[secrets]: ../spec/secrets.json
[todo]: ../TODO.md
[workflow]: ../WORKFLOW.md
[workflow-d8]: ../WORKFLOW.md#d8---bots--automation
[workflow-reusable-task-parameter-contract]: ../WORKFLOW.md#reusable-task-parameter-contract
[workflow-reuse-report]: ../reports/workflow-reuse.md
