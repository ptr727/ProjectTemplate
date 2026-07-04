# Workflow snippets

The reusable build/publish workflow tasks a code-shipping repo runs. They are **inert reference here** -
this repo is source-only and keeps just the orchestrator set (`test-pull-request`, `publish-release`,
`build-release-task`, `get-version-task`, `merge-bot-pull-request`) in `.github/workflows/`. Each file
below is the canonical implementation of one or more `WORKFLOW.md` guarantees; the audit asserts a
downstream repo's own Actions satisfy those guarantees, not that they match these bytes.

| File | Role | WORKFLOW.md guarantees |
| --- | --- | --- |
| `build-executable-task.yml` | Console/executable per-runtime publish, aggregate to one release asset | D5, D6; §6 Console walkthrough |
| `build-nugetlibrary-task.yml` | Build + `dotnet nuget push` (OIDC), upload release asset | D3.4, D4.4, D6; §6 NuGet walkthrough |
| `build-pypilibrary-task.yml` | Build PyPI package; publish split to an OIDC job | D3.4, D4, D7.2; §6 PyPI walkthrough |
| `build-docker-task.yml` | Multi-arch image build + push, registry layer cache | D4.4, D6, D9.4; §6 Docker walkthrough |
| `build-datebadge-task.yml` | BYOB date/last-build badge on the default branch | D4; §3 Release Model |
| `publish-docker-readme-task.yml` | Push the size-limited Docker Hub overview | D2.4; §6 Docker walkthrough |
| `check-upstream-version-task.yml` | Upstream-version tracker for wrapper repos | D3.5, D8.3 |
| `run-codegen-pull-request-task.yml` | Deterministic codegen executor (per-branch PR) | D8.2 |
| `run-periodic-codegen-pull-request.yml` | Scheduled codegen trigger over both branches | D8.2 |
