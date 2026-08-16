# Workflow snippets

The reusable build/publish workflow tasks a code-shipping repo runs. They are **inert reference here**: this repo is source-only and keeps just the orchestrator set (`test-pull-request`, `publish-release`, `validate-task`, `merge-bot-pull-request`) in `.github/workflows/`, plus the hub-hosted reusable tasks a downstream repo reaches rather than carries (`merge-bot-task`, `get-version-task`, `publish-plan-task`, `publish-docker-readme-task`, `check-upstream-version-task`, `deploy-site-task`, `run-codegen-pull-request-task`, per [`docs/reusable-workflows.md`][reusable-workflows]). Each row below names the canonical implementation of one or more `WORKFLOW.md` guarantees, whether the file lives in this directory or is hub-hosted and reached by pin. The audit asserts a downstream repo's own Actions satisfy those guarantees, not that they match these bytes.

A caller stub for a hub-hosted task carries no snippet of its own once the task ships: its `uses:` line would pin a commit no release carries yet, which the pin gate rejects. [`docs/reusable-workflows.md`][reusable-workflows] "Adopting the Type-Specific Tasks" carries the stub shapes instead, and the catalog gains each snippet back one release after its task ships. `get-version-task.yml` and `publish-plan-task.yml` below carry no snippet at all, for a different reason: each is called as a job inside a larger stub rather than reached by its own top-level caller.

| File | Role | WORKFLOW.md guarantees |
| --- | --- | --- |
| `merge-bot-pull-request.yml` | Caller stub for the hub-hosted merge-bot task, pinned to a hub release, the shape every repo carries | D8.1, D8.3, D8.4 |
| `build-release-task.yml` | Multi-target release orchestrator: get-version, validate-release, github-release plus per-target build jobs | D3, D4, D5, D6 |
| `.github/workflows/get-version-task.yml` | Hub-hosted and reached by pin from a leaf or publisher rather than carried, with no caller-stub snippet since it is called as a job inside a larger stub, per [`docs/reusable-workflows.md`][reusable-workflows] | D3 |
| `.github/workflows/publish-plan-task.yml` | Hub-hosted and reached by pin from every publish-release job rather than carried, with no caller-stub snippet since it is called as a job inside a larger stub, per [`docs/reusable-workflows.md`][reusable-workflows] | D4 |
| `build-executable-task.yml` | Console/executable per-runtime publish, aggregate to one release asset | D5, D6, and section 6 Console walkthrough |
| `build-nugetlibrary-task.yml` | Build + `dotnet nuget push` (OIDC), upload release asset | D3.4, D4.4, D6, and section 6 NuGet walkthrough |
| `build-pypilibrary-task.yml` | Build PyPI package, with publishing split to an OIDC job | D3.4, D4, D7.2, and section 6 PyPI walkthrough |
| `build-docker-task.yml` | Multi-arch image build + push, registry layer cache | D4.4, D6, D9.4, and section 6 Docker walkthrough |

<!-- Repo -->

[reusable-workflows]: ../../../docs/reusable-workflows.md
