# Catalog

Reusable reference snippets: concrete config artifacts a repo can copy or compare against. These are
**reference material, not run here** - this repo ships no build, so the workflow snippets below are not active. Each is the canonical shape the audit (`AUDIT.md`) checks a downstream implementation against.

- `snippets/workflows/` - the reusable build/publish workflow tasks that a code-shipping repo runs (this docs repo keeps only the source-only orchestrator set in `.github/workflows/`). See `snippets/workflows/README.md` for the mapping from each file to the `WORKFLOW.md` guarantees it implements.
- `snippets/configs/` - config exemplars: `vscode-tasks.json` (.NET clean-compile task group) and `vscode-tasks-python.json` (the Python equivalent - `ruff`/type-check/`pytest`, all `type: process` so no `&&` chaining breaks Windows PowerShell 5.1), `dependabot.yml` (multi-ecosystem dual-target reference), `docker-hub-readme.md` (the size-limited Docker Hub overview, distinct from the project `README.md`).
- `snippets/devcontainer/` - `.devcontainer` definitions for the .NET and Python toolchains.
- `snippets/vscode/` - the composable `.code-workspace` fragments: `base.jsonc` (standard set) plus `dotnet.jsonc`, `python.jsonc`, `docker.jsonc` per-type additions. See `snippets/vscode/README.md`.
