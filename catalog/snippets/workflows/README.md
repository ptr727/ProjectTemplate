# Workflow snippets

The reusable build/publish workflow tasks a code-shipping repo runs. They are **inert reference here** - this repo is source-only and keeps just the orchestrator set (`test-pull-request`, `publish-release`, `validate-task`, `merge-bot-pull-request`) in `.github/workflows/`. Each file below is the canonical implementation of one or more `WORKFLOW.md` guarantees; the audit asserts a downstream repo's own Actions satisfy those guarantees, not that they match these bytes.

| File | Role | WORKFLOW.md guarantees |
| --- | --- | --- |
| `build-release-task.yml` | Multi-target release orchestrator: get-version, validate-release, github-release plus per-target build jobs | D3, D4, D5, D6 |
| `get-version-task.yml` | NBGV version/tag computation (reusable) | D3 |
| `publish-plan-task.yml` | Single-source release-gate decision (publish? stable?) reused by every publish-release job | D4 |
| `build-executable-task.yml` | Console/executable per-runtime publish, aggregate to one release asset | D5, D6; section 6 Console walkthrough |
| `build-nugetlibrary-task.yml` | Build + `dotnet nuget push` (OIDC), upload release asset | D3.4, D4.4, D6; section 6 NuGet walkthrough |
| `build-pypilibrary-task.yml` | Build PyPI package; publish split to an OIDC job | D3.4, D4, D7.2; section 6 PyPI walkthrough |
| `build-docker-task.yml` | Multi-arch image build + push, registry layer cache | D4.4, D6, D9.4; section 6 Docker walkthrough |
| `build-datebadge-task.yml` | BYOB date/last-build badge on the default branch | D4; section 3 Release Model |
| `publish-docker-readme-task.yml` | Push the size-limited Docker Hub overview | D2.4; section 6 Docker walkthrough |
| `deploy-site.yml` | Dispatch entry point for a site deploy: environment choice, per-environment concurrency, ref gate, shared validation | D2.1, D2.3, D7.1 |
| `deploy-site-task.yml` | Build a site and ship it to a filesystem on a host the project owns, then verify against the running host | D4.6, D5.6, D7.2 (section 6 static-site walkthrough) |
| `check-upstream-version-task.yml` | Upstream-version tracker for wrapper repos | D3.5, D8.3 |
| `run-codegen-pull-request-task.yml` | Deterministic codegen executor (per-branch PR) | D8.2 |
| `run-periodic-codegen-pull-request.yml` | Scheduled codegen trigger over both branches | D8.2 |
