# Workflow snippets

The reusable build/publish workflow tasks a code-shipping repo runs. They are **inert reference here**: this repo is source-only and keeps just the orchestrator set (`test-pull-request`, `publish-release`, `validate-task`, `merge-bot-pull-request`) in `.github/workflows/`, plus the hub-hosted reusable tasks a downstream repo reaches rather than carries (`merge-bot-task`, `get-version-task`, `publish-plan-task`, `build-release-task`, `build-docker-task`, per [`docs/reusable-workflows.md`][reusable-workflows]). Each row below names the canonical implementation of one or more `WORKFLOW.md` guarantees, whether the file lives in this directory or is hub-hosted and reached by pin. The audit asserts a downstream repo's own Actions satisfy those guarantees, not that they match these bytes. The release-chain hooks (`build-executable`, `build-nuget`, `build-pypi`, `docker-prepare`, `docker-build-base`) are composite actions rather than snippets here, since a hook is per-repo content and this catalog carries only what a repo copies whole.

| File | Role | WORKFLOW.md guarantees |
| --- | --- | --- |
| `merge-bot-pull-request.yml` | Caller stub for the hub-hosted merge-bot task, pinned to a hub release, the shape every repo carries | D8.1, D8.3, D8.4 |
| `.github/workflows/get-version-task.yml` | Hub-hosted and reached by pin from a leaf or publisher rather than carried, with no caller-stub snippet since it is called as a job inside a larger stub, per [`docs/reusable-workflows.md`][reusable-workflows] | D3 |
| `.github/workflows/publish-plan-task.yml` | Hub-hosted and reached by pin from every publish-release job rather than carried, with no caller-stub snippet since it is called as a job inside a larger stub, per [`docs/reusable-workflows.md`][reusable-workflows] | D4 |
| `.github/workflows/build-release-task.yml` | Hub-hosted release chain, reached by pin from a `publish-release.yml` caller stub's `publish` job, with the caller-stub catalog snippet landing once the release that first ships it exists, per [`docs/reusable-workflows.md`][reusable-workflows] "Rollout" | D3, D4, D5, D6 |
| `.github/workflows/build-docker-task.yml` | Hub-hosted Docker leg, reached by pin from a repo needing only the Docker build, with the caller-stub catalog snippet landing once the release that first ships it exists, per [`docs/reusable-workflows.md`][reusable-workflows] "Rollout" | D4, D6, and section 6 Docker walkthrough |
| `build-datebadge-task.yml` | BYOB date/last-build badge on the default branch | D4, and section 3 Release Model |
| `publish-docker-readme-task.yml` | Push the size-limited Docker Hub overview | D2.4, and section 6 Docker walkthrough |
| `deploy-site.yml` | Dispatch entry point for a site deploy: environment choice, per-environment concurrency, ref gate, shared validation | D2.1, D2.3, D7.1 |
| `deploy-site-task.yml` | Build a site and ship it to a filesystem on a host the project owns, then verify against the running host | D4.6, D5.6, D7.2 (section 6 static-site walkthrough) |
| `check-upstream-version-task.yml` | Upstream-version tracker for wrapper repos | D3.5, D8.3 |
| `run-codegen-pull-request-task.yml` | Deterministic codegen executor (per-branch PR) | D8.2 |
| `run-periodic-codegen-pull-request.yml` | Scheduled codegen trigger over both branches | D8.2 |

<!-- Repo -->

[reusable-workflows]: ../../../docs/reusable-workflows.md
