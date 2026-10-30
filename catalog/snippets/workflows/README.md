# Workflow snippets

The reusable build and publish workflow tasks serve code-shipping repositories. They are **inert reference here** because this repository is source-only. The hub's `.github/workflows/` directory contains both its own entry-point workflows and the reusable tasks downstream repositories reach by pin. Each row below names the canonical caller snippet for one or more `WORKFLOW.md` guarantees. The table intentionally lists snippets rather than every hub-hosted task. The audit checks that downstream Actions satisfy those guarantees, not that they match these bytes. Release-chain hooks are composite actions because each hook is repository-owned content. They are `dotnet-publish`, `build-nuget`, `build-pypi`, `docker-prepare`, and `docker-build-base`.

A caller stub for a hub-hosted task carries no snippet of its own until the task ships in a release. The stub's `uses:` line would pin a commit no release carries yet, which the pin gate rejects. `test-pull-request.yml` and `run-periodic-codegen-pull-request.yml` gained theirs once `2.0.352` released `validate-task.yml` and `run-codegen-pull-request-task.yml`. `publish-release.yml` gained its own once a release carried the renamed .NET publish interface and the package push had moved to the caller's own job. `test-pull-request-smoke.yml` gained its own later, pinned at landing to the same `2.0.587` release as `publish-release.yml`. [`docs/reusable-workflows.md`][reusable-workflows] "Adopting the Type-Specific Tasks" carries the remaining stub shapes as reference until each has a snippet of its own. `get-version-task.yml` and `publish-plan-task.yml` below carry no snippet at all, for a different reason. Each is called as a job inside a larger stub rather than reached by its own top-level caller.

| File | Role | WORKFLOW.md guarantees |
| --- | --- | --- |
| `merge-bot-pull-request.yml` | Caller stub for the hub-hosted merge-bot task, pinned to a hub release, the shape every repo carries | D8.1, D8.3, D8.4 |
| `test-pull-request.yml` | Caller stub for the hub-hosted `validate-task.yml`, the no-build operational trigger shape, pinned to a hub release | D1.2, D1.5 |
| `test-pull-request-smoke.yml` | Caller stub for the hub-hosted `validate-task.yml` and a `build-release-task.yml` smoke build, the release-with-smoke trigger shape, pinned to a hub release and carried downstream as `test-pull-request.yml` | D1.1, D1.2, D1.3, D1.4, D1.5, D7.4 |
| `run-periodic-codegen-pull-request.yml` | Caller stub for the hub-hosted `run-codegen-pull-request-task.yml`, pinned to a hub release, the same per-repo shape a codegen repo carries today | D8.2 |
| `publish-release.yml` | Caller stub for the hub-hosted release chain, the NuGet-library shape, pinned to a hub release. The package push is in the caller's own job, so the OIDC `job_workflow_ref` claim names the publishing repository | D4.1, D5.2, D7.1, D7.2 |
| `.github/workflows/get-version-task.yml` | Hub-hosted and reached by pin from a leaf or publisher rather than carried. It has no caller-stub snippet, since it is called as a job inside a larger stub, per [`docs/reusable-workflows.md`][reusable-workflows] | D3 |
| `.github/workflows/publish-plan-task.yml` | Hub-hosted and reached by pin from every publish-release job rather than carried. It has no caller-stub snippet, since it is called as a job inside a larger stub, per [`docs/reusable-workflows.md`][reusable-workflows] | D4 |

<!-- Repo -->

[reusable-workflows]: ../../../docs/reusable-workflows.md
