# Workflow snippets

The reusable build and publish workflow tasks serve code-shipping repositories. They are **inert reference here** because this repository is source-only. The hub's `.github/workflows/` directory contains both its own entry-point workflows and the reusable tasks downstream repositories reach by pin. Each row below names the canonical caller snippet for one or more `WORKFLOW.md` guarantees. The table intentionally lists snippets rather than every hub-hosted task. The audit checks that downstream Actions satisfy those guarantees, not that they match these bytes. Release-chain hooks are composite actions because each hook is repository-owned content. They are `dotnet-publish`, `build-nuget`, `build-pypi`, `docker-prepare`, and `docker-build-base`.

A caller stub for a hub-hosted task carries no snippet of its own until the task ships in a release: its `uses:` line would pin a commit no release carries yet, which the pin gate rejects. `test-pull-request.yml` and `run-periodic-codegen-pull-request.yml` gained theirs once `2.0.352` released `validate-task.yml` and `run-codegen-pull-request-task.yml`. The release caller snippet is withheld until a release carries the renamed .NET publish interface. [`docs/reusable-workflows.md`][reusable-workflows] "Adopting the Gates", "Adopting the Release Chain", and "Adopting the Type-Specific Tasks" carry the remaining stub shapes as reference until each has a snippet of its own. `get-version-task.yml` and `publish-plan-task.yml` below carry no snippet at all, for a different reason: each is called as a job inside a larger stub rather than reached by its own top-level caller.

| File | Role | WORKFLOW.md guarantees |
| --- | --- | --- |
| `merge-bot-pull-request.yml` | Caller stub for the hub-hosted merge-bot task, pinned to a hub release, the shape every repo carries | D8.1, D8.3, D8.4 |
| `test-pull-request.yml` | Caller stub for the hub-hosted `validate-task.yml`, the no-build operational trigger shape, pinned to a hub release | D1.2, D1.5 |
| `run-periodic-codegen-pull-request.yml` | Caller stub for the hub-hosted `run-codegen-pull-request-task.yml`, pinned to a hub release, the same per-repo shape a codegen repo carries today | D8.2 |
| `.github/workflows/get-version-task.yml` | Hub-hosted and reached by pin from a leaf or publisher rather than carried, with no caller-stub snippet since it is called as a job inside a larger stub, per [`docs/reusable-workflows.md`][reusable-workflows] | D3 |
| `.github/workflows/publish-plan-task.yml` | Hub-hosted and reached by pin from every publish-release job rather than carried, with no caller-stub snippet since it is called as a job inside a larger stub, per [`docs/reusable-workflows.md`][reusable-workflows] | D4 |

<!-- Repo -->

[reusable-workflows]: ../../../docs/reusable-workflows.md
