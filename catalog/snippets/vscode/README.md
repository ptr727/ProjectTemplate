# VS Code Workspace Catalog

The shared `.code-workspace` set for the fleet: the standard extensions every repo recommends, the language-specific additions, and the settings that go with them. A repo's `<Repo>.code-workspace` composes the standard set plus the additions for the languages and targets it ships. Discovered from the fleet's workspace files.

## Standard Extensions (every repo)

- `davidanson.vscode-markdownlint` - Markdown lint, sharing `.markdownlint-cli2.jsonc`.
- `streetsidesoftware.code-spell-checker` - cSpell, sharing `cspell.json`.
- `editorconfig.editorconfig` - applies `.editorconfig`.
- `yzhang.markdown-all-in-one` - Markdown editing and the auto-generated Table of Contents.
- `fanaticpythoner.better-todo-tree` - surfaces TODO/FIXME markers.
- `github.vscode-github-actions` - GitHub Actions authoring.
- `arahata.linter-actionlint` - actionlint for workflow YAML.
- `timonwong.shellcheck` - shellcheck for shell scripts.
- `anthropic.claude-code` - the coding agent.

## Language and Target Additions

- **.NET / C#**: `ms-dotnettools.csdevkit`, `csharpier.csharpier-vscode`.
- **Python**: `ms-python.python`, `ms-python.vscode-pylance`, `charliermarsh.ruff`, `ms-python.mypy-type-checker`.
- **Docker**: `ms-azuretools.vscode-docker`.

## Settings

- **Table of Contents**: `"markdown.extension.toc.levels": "2..3"` - the Markdown All in One extension includes H2 and H3 headings and updates the TOC on save.
- **Format on save** per language: C# via `csharpier.csharpier-vscode`; Python via `charliermarsh.ruff` with import organization.
- **cSpell and markdownlint** read the repo's `cspell.json` and `.markdownlint-cli2.jsonc` (linter parity).
- Trim trailing whitespace except in Markdown and plaintext; sign off commits (`git.alwaysSignOff`).

## Composing a Workspace

A repo's `<Repo>.code-workspace` is a single-folder workspace (`"folders": [{ "path": "." }]`) whose `extensions.recommendations` is the standard set plus its language and target additions, and whose `settings` carry the TOC level, the per-language formatters, and the shared editor defaults. This repo's own `ProjectTemplate.code-workspace` carries the standard set only, since it ships no application language.
