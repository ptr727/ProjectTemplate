# ProjectTemplate <!-- omit from toc -->

Governance, agent-orchestration, and workflow-audit hub for a fleet of related repositories.

## Build and Distribution <!-- omit from toc -->

- **Source Code**: [GitHub][projecttemplate-link] - source, issues, discussions, and CI/CD pipelines.
- **Versioned Releases**: [GitHub Releases][releases-link] - version-tagged source archives.

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

- Governance, agent-orchestration, and workflow-audit hub for the fleet: shared rules, a machine-readable spec, a fleet registry, per-repo audit reports, and an audit-agent instruction set. Ships no application code.

See [Release History][history] for the full history.

## Table of Contents <!-- omit from toc -->

- [What This Repo Is](#what-this-repo-is)
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
- [TODO](#todo)
- [License](#license)

## What This Repo Is

This repo is the single home for the shared rules the fleet follows, a machine-readable spec those rules are checked against, a registry of the projects, and an audit-agent instruction set. It ships no application code. Each project owns its own implementation and is **audited** against the ground truth here - to the letter (exact file, section, or config) or to intent (an equivalent outcome).

- **[AGENTS.md][agents]** - cross-cutting rules for AI coding agents: git, branching, release model, doc style, the recurring-violation rules (comments, ASCII charset, US spelling, line endings), PR review etiquette, and workflow YAML conventions.
- **[CODESTYLE.md][codestyle]** - code style for .NET and Python.
- **[WORKFLOW.md][workflow]** - the CI/CD workflow contract (behavioral guarantees D1-D9) and its audit methodology.
- **[AUDIT.md][audit]** - how an agent audits a repository against the spec and reports drift.
- **[spec/][spec]** - the machine-readable ground truth: project-type requirements, the file/section baseline, required/forbidden secrets, and the preferred README structure.
- **[registry/repos.json][repos]** - the fleet registry: every project, its type(s), publish mechanism, and status (cataloged or standardization backlog).
- **[repo-config/][repo-config]** - branch rulesets and the apply script (kept out of `.github/`, which is Actions-owned), plus the GitHub setup reference.
- **[catalog/][catalog]** - reusable reference snippets (workflow tasks, config exemplars, devcontainers) the audit compares implementations against.
- **[reports/][reports]** - per-repo audit output.

## How This Repo Operates

ProjectTemplate follows the same model it documents, and audits its own rules against itself (it classifies as the source-only project type in [WORKFLOW.md][workflow]).

- **Branching.** Persistent `main` and `develop`, each with its own ruleset. This repo uses the default `release` workflow model: commit on feature branches only, feature branch to `develop` is squash-merged, `develop` to `main` is a merge commit, and `develop` is forward-only (no `main -> develop` back-merges). Live-service config repos instead use the `operational` model (registry `workflowModel`) - direct signed commits to `develop`, promoted to `main` by an occasional PR. See [GOVERNANCE.md "Branching Model"][governance-branching-model].
- **CI is lint-only.** There is no build or unit test; the PR gate runs markdownlint, cspell, JSON validation (`jq` parses `registry/`, `spec/`, and `repo-config/`, plus the `spec/validate.py` cross-reference and shape checks), and actionlint, and exposes the ruleset-bound `Check pull request workflow status job` aggregator. The same lint configs (`.markdownlint-cli2.jsonc`, `cspell.json`) drive the editor extensions, the CLI, and CI.
- **Review loop.** Every PR is reviewed by GitHub Copilot, and the agent drives the review loop to green and merges only with explicit maintainer permission. See [GOVERNANCE.md "PR Review Etiquette"][governance-pr-review-etiquette].
- **Release.** A `develop -> main` merge is promoted through a GitHub release (tag plus a source zip, README, and LICENSE); versioning is NBGV-driven from [version.json][version]. See [WORKFLOW.md][workflow].

## Rules

A human-readable index of the rules agents enforce, implement, and audit. The authority for each is [AGENTS.md][agents], [CODESTYLE.md][codestyle], and [WORKFLOW.md][workflow]; the machine-checkable form lives in [spec/][spec].

### Always

- Sign every commit (SSH or GPG).
- Branch feature -> develop (squash) -> main (merge commit); develop is forward-only.
- Drive every PR through the Copilot review loop and merge only with maintainer approval.
- Write US English and ASCII only (no em-dash, straight quotes).
- Write docs and comments in the present tense, describing only the current state - never as a change from a prior one.
- Keep comments concise and only for the non-obvious, and never grow them on edit.
- Follow `.editorconfig` line endings (CRLF default, LF for shell and Docker) and preserve a file's endings on edit.
- One logical paragraph per line, with a trailing `\` for an intentional hard break.
- Pin every GitHub Action to a commit SHA with a version comment.
- Share one lint config per tool across the editor, the CLI, and CI.
- Run the repo's whole lint gate before pushing, not just the parts that look relevant.
- Make gates fail loud - a gate that stops gating must error or annotate, never pass silently.
- Favor VS Code tasks and launch configs for building, running, and testing over ad-hoc shell scripts.

### Never

- Never force-push or rewrite shared history.
- Never treat a merge as a release; publishing is a separate, explicit step.
- Never blanket-delete a workflow run's artifacts.
- Never store a static key when OIDC Trusted Publishing is available.

### If a C# Project

- Carry the shared `[*.cs]` block in `.editorconfig` and build with zero warnings.

### If a Python Project

- Configure ruff and a type checker in `pyproject.toml` - pyright strict, or mypy in CI with pyright editor-only; whichever runs in CI is the gate.

### If Both C# and Python

- Both sections above apply; a repo can be both (a C# app plus a Python subtree). The Python is either a full uv project (`uv.lock`, `uv run`) or a stdlib-only `uvx` scripts subtree (no `uv.lock`, `pyproject.toml` carries lint/type config only). See [CODESTYLE.md][codestyle] "Two profiles".

### If Publishing a Package (NuGet or PyPI)

- Publish via OIDC Trusted Publishing, never a stored API key.

### If a Docker Image

- Cache layers to a registry tag (never `type=gha`) and publish the size-limited Docker Hub README separately.

### For a README or Human-Facing Doc

- Follow the section order in [spec/readme-structure.md][readme-structure] and put every URI as a grouped, alphabetized reference link at the bottom.

### For Workflows

- Make GitHub Actions satisfy the [WORKFLOW.md][workflow] contract (guarantees D1-D9), which the audit verifies.

## Development Environment Setup

Contributors sign every commit. See [docs/ssh-signing.md][ssh-signing] for SSH commit-signing setup, [docs/host-setup.md][host-setup] for host prerequisites, and [docs/devcontainer.md][devcontainer] for devcontainer SSH-agent forwarding. Run the linters before pushing (see [AGENTS.md "Running the Linters Locally"][governance-running-the-linters-locally-known-working-invocations]).

## TODO

Running backlog (kept here, in a committed file, so the guidance survives across environments where agent memory does not).

- Run the first per-repo audits and populate [reports/][reports] for the seven cataloged repos.
- Classify the standardization-backlog repos in [registry/repos.json][repos] (marked `classificationPending`) on first audit.
- Canonicalize Python linter-config placement on `pyproject.toml` (one cataloged repo uses standalone `.ruff.toml` + `pyrightconfig.json`); track as a drift finding, fix downstream.
- Consider renaming this repo to reflect the audit-catalog identity (updates badge and link URLs across the fleet).
- Adopt the OCI annotation keys (`org.opencontainers.image.*`) for Docker image metadata across the Docker repos, replacing the ad-hoc and `org.label-schema.*` labels (from #363).
- Sweep `ManagePackageVersionsCentrally` placement to `Directory.Packages.props` fleet-wide (PlexCleaner sets it in `Directory.Build.props`, off the CODESTYLE canonical).
- Finish onboarding hardening (from #310): make the `AUDIT.md` audit a required onboarding step and run the per-type cold-start self-tests tracked in `reports/conformance-matrix.md` (`STANDUP.md` is already in place).
- Refresh the README (it has gone stale) and evaluate a lower-maintenance structure - for example a per-section index that points into each doc with a one-line description, keeping the README as the adoption and audit-instruction entry point with pointers to the other docs. A per-section index trades brevity for a sync obligation: it must track what the docs contain.
- Add a linter-only Python project type for codegen/boilerplate Python - code that runs during another tool's build to emit generated source (e.g. ESPHome codegen that produces enriched C++ at compile time), so it ships no unit tests and no coverage and needs only the linter. Keep it distinct from the existing `python` type, which is utility code that can and should carry unit tests and coverage (as in PlexCleaner). Until it exists, ESPHome-Config stays `source-only` and its `+python` reclassification is deferred - accept its one outstanding validation finding meanwhile.
- Standardize `OPERATIONS.md` as the fleet's topical doc for repo-specific operational content extracted from a carried `AGENTS.md` (runbooks, backup/log/debug procedures, tool-usage notes, config layout) - the operational-repo analogue of `ARCHITECTURE.md`. HomeAssistant-Config and ESPHome-Config both extract to it. Record it in the section model's topical-doc guidance so extraction has a predictable target.
- Add a fleet-standard clang-format config for the `cpp` type: a catalog snippet plus a CODESTYLE C++ section defining the style, the C++ analogue of the shared ruff config, so the `cpp` clang-format check references one canonical style rather than each repo inventing its own. Base it on the ESPHome-Config agent's proposed `.clang-format`.

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
[governance-branching-model]: ./GOVERNANCE.md#branching-model
[governance-pr-review-etiquette]: ./GOVERNANCE.md#pr-review-etiquette
[governance-running-the-linters-locally-known-working-invocations]: ./GOVERNANCE.md#running-the-linters-locally-known-working-invocations
[audit]: ./AUDIT.md
[catalog]: ./catalog/
[codestyle]: ./CODESTYLE.md
[devcontainer]: ./docs/devcontainer.md
[history]: ./HISTORY.md
[host-setup]: ./docs/host-setup.md
[license]: ./LICENSE
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
