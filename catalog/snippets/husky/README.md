# Husky snippet

`pre-commit` is the reference git pre-commit hook (installed under `.husky/` by [Husky](https://typicode.github.io/husky/)). It gives **local lint parity with the CI lint job** so a commit does not surprise the maintainer with a CI-only failure - line endings (`editorconfig-checker`), workflow YAML (`actionlint`), Markdown (`markdownlint-cli2`), and spelling (`cspell`), plus C# formatting via `dotnet husky run`.

The lint tools run through Docker (identical across Windows/WSL/Linux) and **only when the image is already present locally** - the hook never pulls and skips anything unavailable, so it is never a false blocker; CI enforces the same checks regardless. Warm the images with the VS Code **Lint** tasks ([`catalog/snippets/configs/vscode-tasks.json`](../configs/vscode-tasks.json)).

A copied `.husky/pre-commit` is an extensionless shebang script, so pin it to **LF** in both `.gitattributes` (`.husky/pre-commit text eol=lf`) and `.editorconfig` (`[.husky/pre-commit] end_of_line = lf`) - a CRLF shebang breaks execution. Drop the `dotnet husky run` line in a non-.NET repo.
