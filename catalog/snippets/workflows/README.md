# Workflow snippets

The reusable build/publish workflow tasks a code-shipping repo runs. They are **inert reference here**: this repo is source-only and keeps just the orchestrator set (`test-pull-request`, `publish-release`, `validate-task`, `merge-bot-pull-request`) in `.github/workflows/`, plus the hub-hosted reusable tasks a downstream repo reaches rather than carries (`merge-bot-task`, `get-version-task`, `publish-plan-task`, `build-release-task`, `build-docker-task`, `publish-docker-readme-task`, `check-upstream-version-task`, `deploy-site-task`, `run-codegen-pull-request-task`, per [`docs/reusable-workflows.md`][reusable-workflows]). Each row below names the canonical implementation of one or more `WORKFLOW.md` guarantees, whether the file lives in this directory or is hub-hosted and reached by pin. The audit asserts a downstream repo's own Actions satisfy those guarantees, not that they match these bytes. The table does not yet carry a row for every name in the parenthetical above: `build-release-task.yml`, `build-docker-task.yml`, `publish-docker-readme-task.yml`, `check-upstream-version-task.yml`, `deploy-site-task.yml`, and `run-codegen-pull-request-task.yml` are hub-hosted with no row here, for the reason the next paragraph gives. The release-chain hooks (`build-executable`, `build-nuget`, `build-pypi`, `docker-prepare`, `docker-build-base`) are composite actions rather than snippets here, since a hook is per-repo content and this catalog carries only what a repo copies whole.

A caller stub for a hub-hosted task carries no snippet of its own once the task ships: its `uses:` line would pin a commit no release carries yet, which the pin gate rejects. [`docs/reusable-workflows.md`][reusable-workflows] "Adopting the Release Chain" and "Adopting the Type-Specific Tasks" carry the stub shapes instead, and the catalog gains each snippet back one release after its task ships. `get-version-task.yml` and `publish-plan-task.yml` below carry no snippet at all, for a different reason: each is called as a job inside a larger stub rather than reached by its own top-level caller.

| File | Role | WORKFLOW.md guarantees |
| --- | --- | --- |
| `merge-bot-pull-request.yml` | Caller stub for the hub-hosted merge-bot task, pinned to a hub release, the shape every repo carries | D8.1, D8.3, D8.4 |
| `.github/workflows/get-version-task.yml` | Hub-hosted and reached by pin from a leaf or publisher rather than carried, with no caller-stub snippet since it is called as a job inside a larger stub, per [`docs/reusable-workflows.md`][reusable-workflows] | D3 |
| `.github/workflows/publish-plan-task.yml` | Hub-hosted and reached by pin from every publish-release job rather than carried, with no caller-stub snippet since it is called as a job inside a larger stub, per [`docs/reusable-workflows.md`][reusable-workflows] | D4 |

<!-- Repo -->

[reusable-workflows]: ../../../docs/reusable-workflows.md
