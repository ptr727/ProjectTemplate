# VS Code Workspace Catalog

The shared `.code-workspace` set for the fleet: the standard extensions every repo recommends, the language-specific additions, and the settings that go with them. Each piece is a copyable JSON fragment in this directory. `base.jsonc` carries the standard set, and `dotnet.jsonc`, `python.jsonc`, and `docker.jsonc` carry the per-type additions. A repo's `<Repo>.code-workspace` composes `base.jsonc` plus the fragments for the languages and targets it ships. Discovered from the fleet's workspace files.

## Standard Extensions (every repo)

- **`davidanson.vscode-markdownlint`** - Markdown lint, sharing `.markdownlint-cli2.jsonc`.
- **`streetsidesoftware.code-spell-checker`** - cSpell, sharing `cspell.json`.
- **`editorconfig.editorconfig`** - applies `.editorconfig`.
- **`yzhang.markdown-all-in-one`** - Markdown editing and the auto-generated Table of Contents.
- **`fanaticpythoner.better-todo-tree`** - surfaces TODO/FIXME markers.
- **`github.vscode-github-actions`** - GitHub Actions authoring.
- **`arahata.linter-actionlint`** - actionlint for workflow YAML.
- **`timonwong.shellcheck`** - shellcheck for shell scripts.
- **`anthropic.claude-code`** - the coding agent.

## Language and Target Additions

- **.NET / C#** (`dotnet.jsonc`): `ms-dotnettools.csdevkit`, `csharpier.csharpier-vscode`; format-on-save for `[csharp]` via CSharpier.
- **Python** (`python.jsonc`): `ms-python.python`, `ms-python.vscode-pylance`, `charliermarsh.ruff`, `ms-python.mypy-type-checker`; format-on-save and import organization for `[python]` via Ruff.
- **Docker** (`docker.jsonc`): `ms-azuretools.vscode-docker`.

## Settings

- **Table of Contents**: `"markdown.extension.toc.levels": "2..3"`. The Markdown All in One extension includes H2 and H3 headings and updates the TOC on save.
- **Format on save** per language: C# via `csharpier.csharpier-vscode`, Python via `charliermarsh.ruff` with import organization.
- **cSpell and markdownlint** read the repo's `cspell.json` and `.markdownlint-cli2.jsonc` (linter parity).
- Trim trailing whitespace except in Markdown and plaintext, and sign off commits (`git.alwaysSignOff`).

## Composing a Workspace

A repo's `<Repo>.code-workspace` is a single-folder workspace (`"folders": [{ "path": "." }]`) that merges `base.jsonc` with the per-type fragments for the languages and targets it ships: the merged `extensions.recommendations` is the standard set plus each type's additions, and the merged `settings` are the shared editor defaults plus each type's formatter block. This repo's own `ProjectTemplate.code-workspace` carries `base.jsonc` only, since it ships no application language.
