# ProjectTemplate <!-- omit from toc -->

Agent enablement for a fleet of repositories: autonomy and repeatable quality inside guardrails.

## Build and Distribution <!-- omit from toc -->

- **Source Code**: [GitHub][projecttemplate-link] for source, issues, discussions, and CI/CD pipelines.
- **Versioned Releases**: [GitHub Releases][releases-link] for version-tagged source archives.

### Build Status <!-- omit from toc -->

[![Releases Build][releases-build-shield]][actions-link]\
[![Last Commit][last-commit-shield]][commits-link]\
[![License][license-shield]][license]

### Releases <!-- omit from toc -->

[![GitHub Release][github-release-shield]][releases-link]\
[![GitHub Pre-Release][github-pre-release-shield]][releases-link]

### Release Notes <!-- omit from toc -->

**Version: 2.0**:

**Summary**:

- Agent enablement for the fleet: shared rules, a machine-readable spec, a fleet registry, per-repo audit reports, and an audit-agent instruction set, so an agent works autonomously inside guardrails that prove the result. Ships no application code.

See [Release History][history] for the full history.

## Table of Contents <!-- omit from toc -->

- [What This Repo Is](#what-this-repo-is)
- [What It Achieves](#what-it-achieves)
- [How This Repo Operates](#how-this-repo-operates)
- [Rules](#rules)
  - [Always](#always)
  - [Never](#never)
  - [If a C# Project](#if-a-c-project)
  - [If a Python Project](#if-a-python-project)
  - [If Publishing a Package (NuGet or PyPI)](#if-publishing-a-package-nuget-or-pypi)
  - [If a Docker Image](#if-a-docker-image)
  - [For a README or Human-Facing Doc](#for-a-readme-or-human-facing-doc)
  - [For Workflows](#for-workflows)
- [Development Environment Setup](#development-environment-setup)
- [License](#license)

## What This Repo Is

**The purpose is agent enablement.** An AI coding agent is fast and inconsistent, so a fleet built by one drifts a different way in every repository, and the drift stays invisible until something breaks where it matters. What this repo makes repeatable is the outcome: an agent stands a repository up, changes it, and releases it on its own, and lands in the same known-good shape every time. The guardrails are what make granting that autonomy sound rather than reckless.

**Guardrails here enable rather than restrain.** A rule earns its place by removing a decision an agent would otherwise make differently every time, or by making a failure loud that would otherwise pass green. Write safety bounds what an agent can reach outside the project in front of it, the review loop closes before anything merges, and the audit proves the result instead of accepting the agent's report of it. Autonomy extends exactly as far as the verification reaches.

**Nothing here is finished.** Every rule traces to a specific failure, nearly all of them observed in this fleet rather than imagined, and a procedure that lets a new one through is corrected as part of the work that found it. The ground truth improves by being used.

This repo is the single home for those rules, a machine-readable spec they are checked against, a registry of the projects, and an audit-agent instruction set. It ships no application code. Each project owns its own implementation and is **audited** against the ground truth here, to the letter (exact file, section, or config) or to intent (an equivalent outcome).

- **[AGENTS.md][agents]** - the agent entry point: context and delegation rules, plus the map from a task to the section that governs it.
- **[GOVERNANCE.md][governance]** - cross-cutting rules for AI coding agents: git, branching, release model, doc style, the recurring-violation rules (comments, ASCII charset, US spelling, line endings), PR review etiquette, and workflow YAML conventions.
- **[CODESTYLE.md][codestyle]** - code style for .NET and Python.
- **[WORKFLOW.md][workflow]** - the CI/CD workflow contract (behavioral guarantees D1-D9) and its audit methodology.
- **[AUDIT.md][audit]** - how an agent audits a repository against the spec and reports drift.
- **[spec/][spec]** - the machine-readable ground truth: project-type requirements, the file/section baseline, required/forbidden secrets, and the preferred README structure.
- **[registry/repos.json][repos]** - the fleet registry: every project, its type(s), publish mechanism, and status (cataloged or standardization backlog).
- **[repo-config/][repo-config]** - branch rulesets and the apply script (kept out of `.github/`, which is Actions-owned), plus the GitHub setup reference.
- **[catalog/][catalog]** - reusable reference snippets (workflow tasks, config exemplars, devcontainers) the audit compares implementations against.
- **[reports/][reports]** - per-repo audit output.

## What It Achieves

Keeping a fleet of repositories consistent has always been a tax paid in review attention, and it stops scaling at the point where one person can no longer hold every repo in their head. An agent changes that arithmetic in both directions at once. It can apply a convention across every repository in an afternoon, and it can spread a mistake exactly as fast. What makes the speed worth having is a ground truth an agent can read, a gate that proves the result rather than reporting it, and a boundary naming the decisions that are never the agent's to make. Each objective below is a standing capability, with the machinery that delivers it named so the claim is checkable.

- **Workflow consistency, by contract rather than by copy.** Every repo satisfies one behavioral CI/CD contract ([WORKFLOW.md][workflow], guarantees D1 to D9) instead of inheriting one YAML file it then edits. The fixed part is the orchestration seam, meaning job names, the ruleset-bound required check, and the artifact handoff. What a repo builds inside that seam is its own, so a Hugo site and a NuGet package satisfy the same contract without pretending to be the same pipeline.
- **Technical consistency that does not depend on anyone remembering it.** One line-ending policy, one comment shape, one character set, one US-English convention, and one config per linter shared by the editor, the CLI, and CI. A rule that holds in review therefore holds on a laptop and in the pipeline, because all three read the same file rather than three copies that drift apart.
- **Best practices promoted once, not re-litigated per repo.** A practice that proves itself becomes fleet law in [GOVERNANCE.md][governance] or [CODESTYLE.md][codestyle] and is carried, rather than being rediscovered and re-argued in the next repository. Every rule here traces to a specific failure that actually happened, which is why the collection is opinionated and small rather than exhaustive.
- **Feedback loops that close on the procedure, not the instance.** [AUDIT.md][audit] reads a live repo and reports drift, the per-repo reports in [reports/][reports] record it, and a repo that cannot be stood up from the docs alone is a documentation defect tracked in the [conformance matrix][matrix]. When a downstream agent hits something the procedure did not cover, the fix lands in the procedure so the next repo never meets it.
- **Onboarding a new language or deployment target is a spec change.** Thirteen project types are declared today in [spec/project-types.json][project-types]. Adding one means declaring its detection, its checks, and the files it carries, then proving a context-free agent can stand it up cold. No fleet-wide rewrite, and no per-repo improvisation.
- **Re-deployment that is measured and traceable.** Versions come from git history through NBGV rather than a hand-edited number, a release is always a deliberate act and never a side effect of a merge, and staleness is detected by **content hash against the hub's own past revisions**, so the audit can say whether a repo is behind the canonical or has forked it. A version stamp is a claim a repo can keep while editing the content underneath, so it is never trusted for that answer.
- **Every carried unit declares how much freedom it grants.** This is the distinction that makes the whole thing tolerable to work in, and it is a field on each [spec/files.json][files] entry rather than something a reader infers from the file's shape. An entry that names no level takes `presence`, the most permissive one, so silence grants freedom rather than withholding it:

  | Level | The obligation | Who owns the content |
  | --- | --- | --- |
  | `verbatim` | Byte-identical to canonical, after governed normalization | The hub. A paraphrase is a defect, not an adaptation. |
  | `interface` | Honor a named contract, checked by name and wiring | The repo owns the body entirely. |
  | `intent` | Reach the same outcome, judged by meaning | The repo owns the wording and shape. |
  | `presence` | The unit exists | The repo owns all of it. |

- **The human contributes where domain expertise is decisive, and only there.** The maintainer keeps what an agent cannot know or must not decide: creating a repository, granting a write outside the owner boundary, changing a ruleset, approving every merge, and every judgment about the domain a repo actually serves. The agent takes the mechanical scale-out, which is the part that does not benefit from human attention and degrades under it. A repo's own knowledge also has a declared destination rather than an improvised one, chosen by what the content is: `CODESTYLE.md` for conventions beyond the carried rules, `ARCHITECTURE.md` for how a code repo is built, `OPERATIONS.md` for how a live-service repo is run, and `TODO.md` for its backlog. Which of those a repo carries follows from what it is, so this hub holds the two that apply to it. Domain expertise therefore lands somewhere declared instead of being diluted into a carried file that the next re-vendor overwrites.

## How This Repo Operates

ProjectTemplate follows the same model it documents, and audits its own rules against itself (it classifies as the source-only project type in [WORKFLOW.md][workflow]).

- **Branching.** Persistent `main` and `develop`, each with its own ruleset. This repo uses the default `release` workflow model: commit on feature branches only, feature branch to `develop` is squash-merged, `develop` to `main` is a merge commit, and `develop` is forward-only (no `main -> develop` back-merges). Live-service config repos instead use the `operational` model (registry `workflowModel`), with direct signed commits to `develop`, promoted to `main` by an occasional PR. See [GOVERNANCE.md "Branching Model"][governance-branching-model].
- **CI is lint-only.** There is no build or unit test. The PR gate runs markdownlint, cspell, JSON validation (`jq` parses `registry/`, `spec/`, and `repo-config/`, plus the `spec/validate.py` cross-reference and shape checks), and actionlint, and exposes the ruleset-bound `Check pull request workflow status job` aggregator. The same lint configs (`.markdownlint-cli2.jsonc`, `cspell.json`) drive the editor extensions, the CLI, and CI.
- **Review loop.** Every PR is reviewed by GitHub Copilot, and the agent drives the review loop to green and merges only with explicit maintainer permission. See [GOVERNANCE.md "PR Review Etiquette"][governance-pr-review-etiquette].
- **Release.** A `develop -> main` merge is promoted through a GitHub release (tag plus a source zip, README, and LICENSE). Versioning is NBGV-driven from [version.json][version]. See [WORKFLOW.md][workflow].

## Rules

A human-readable index of the rules agents enforce, implement, and audit. The authority for each is [GOVERNANCE.md][governance], [CODESTYLE.md][codestyle], and [WORKFLOW.md][workflow]. The machine-checkable form lives in [spec/][spec].

### Always

- Sign every commit (SSH or GPG).
- Branch feature -> develop (squash) -> main (merge commit), and develop is forward-only.
- Drive every PR through the Copilot review loop and merge only with maintainer approval.
- Write US English and ASCII only (no em-dash, straight quotes).
- Write docs and comments in the present tense, describing only the current state, never as a change from a prior one.
- Keep comments concise and only for the non-obvious, and never grow them on edit.
- Follow `.editorconfig` line endings (CRLF default, LF for shell and Docker) and preserve a file's endings on edit.
- One logical paragraph per line, with a trailing `\` for an intentional hard break.
- Pin every GitHub Action to a commit SHA with a version comment.
- Share one lint config per tool across the editor, the CLI, and CI.
- Run the repo's whole lint gate before pushing, not just the parts that look relevant.
- Make gates fail loud, since a gate that stops gating must error or annotate, never pass silently.
- Favor VS Code tasks and launch configs for building, running, and testing over ad-hoc shell scripts.

### Never

- Never force-push or rewrite shared history.
- Never treat a merge as a release. Publishing is a separate, explicit step.
- Never blanket-delete a workflow run's artifacts.
- Never store a static key when OIDC Trusted Publishing is available.

### If a C# Project

- Carry the shared `[*.cs]` block in `.editorconfig` and build with zero warnings.

### If a Python Project

- Configure ruff and a type checker in `pyproject.toml`, either pyright strict or mypy in CI with pyright editor-only. Whichever runs in CI is the gate.

### If Both C# and Python

- Both sections above apply, and a repo can be both (a C# app plus a Python subtree). The Python is either a full uv project (`uv.lock`, `uv run`) or a stdlib-only `uvx` scripts subtree (no `uv.lock`, `pyproject.toml` carries lint/type config only). See [CODESTYLE.md][codestyle] "Two profiles".

### If Publishing a Package (NuGet or PyPI)

- Publish via OIDC Trusted Publishing, never a stored API key.

### If a Docker Image

- Cache layers to a registry tag (never `type=gha`) and publish the size-limited Docker Hub README separately.

### For a README or Human-Facing Doc

- Follow the section order in [spec/readme-structure.md][readme-structure] and put every URI as a grouped, alphabetized reference link at the bottom.

### For Workflows

- Make GitHub Actions satisfy the [WORKFLOW.md][workflow] contract (guarantees D1-D9), which the audit verifies.

## Development Environment Setup

Contributors sign every commit. See [docs/ssh-signing.md][ssh-signing] for SSH commit-signing setup, [docs/host-setup.md][host-setup] for host prerequisites, and [docs/devcontainer.md][devcontainer] for devcontainer SSH-agent forwarding. Run the linters before pushing (see [GOVERNANCE.md "Running the Linters Locally"][governance-running-the-linters-locally-known-working-invocations]).

## License

See [LICENSE][license].

<!-- Shields -->

[github-pre-release-shield]: https://img.shields.io/github/v/release/ptr727/ProjectTemplate?include_prereleases&label=GitHub%20Pre-Release&logo=github
[github-release-shield]: https://img.shields.io/github/v/release/ptr727/ProjectTemplate?logo=github&label=GitHub%20Release
[last-commit-shield]: https://img.shields.io/github/last-commit/ptr727/ProjectTemplate?logo=github&label=Last%20Commit
[license-shield]: https://img.shields.io/github/license/ptr727/ProjectTemplate?label=License
[releases-build-shield]: https://img.shields.io/github/actions/workflow/status/ptr727/ProjectTemplate/publish-release.yml?event=schedule&logo=github&label=Releases%20Build

<!-- Repo -->

[agents]: ./AGENTS.md
[audit]: ./AUDIT.md
[catalog]: ./catalog/
[codestyle]: ./CODESTYLE.md
[devcontainer]: ./docs/devcontainer.md
[files]: ./spec/files.json
[governance]: ./GOVERNANCE.md
[governance-branching-model]: ./GOVERNANCE.md#branching-model
[governance-pr-review-etiquette]: ./GOVERNANCE.md#pr-review-etiquette
[governance-running-the-linters-locally-known-working-invocations]: ./GOVERNANCE.md#running-the-linters-locally-known-working-invocations
[history]: ./HISTORY.md
[host-setup]: ./docs/host-setup.md
[license]: ./LICENSE
[matrix]: ./reports/conformance-matrix.md
[project-types]: ./spec/project-types.json
[readme-structure]: ./spec/readme-structure.md
[repo-config]: ./repo-config/
[reports]: ./reports/
[repos]: ./registry/repos.json
[spec]: ./spec/
[ssh-signing]: ./docs/ssh-signing.md
[version]: ./version.json
[workflow]: ./WORKFLOW.md

<!-- External -->

[actions-link]: https://github.com/ptr727/ProjectTemplate/actions
[commits-link]: https://github.com/ptr727/ProjectTemplate/commits
[projecttemplate-link]: https://github.com/ptr727/ProjectTemplate
[releases-link]: https://github.com/ptr727/ProjectTemplate/releases
