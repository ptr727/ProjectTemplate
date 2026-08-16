# Husky snippet

`pre-commit` is the reference git pre-commit hook (installed under `.husky/` by Husky). It runs **language formatting and style only**: CSharpier and `dotnet format` style via `dotnet husky run` for .NET, or ruff for a Python repo. Native tooling and no Docker is what keeps it fast.

Full linting (line endings, workflow YAML, Markdown, spelling) is **not** run in the hook. It runs in CI as pinned action wrappers, and on demand via the VS Code **Lint** tasks in `catalog/snippets/configs/vscode-tasks.json` (Docker at `:latest`). Keeping the doc linters out of the hook is what keeps it simple.

A copied `.husky/pre-commit` is an extensionless shebang script, so pin it to **LF** in `.gitattributes` (`.husky/pre-commit text eol=lf`), git-level enforcement independent of the editor. The fleet's `[*]` `.editorconfig` default already gives it LF, no path-specific override needed. A CRLF shebang breaks execution. Drop the `dotnet husky run` line in a non-.NET repo.
