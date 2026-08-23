# Husky snippet

`pre-commit` is the reference git pre-commit hook (installed under `.husky/` by Husky). It
runs **language formatting/lint and the fleet's shared doc gates**: CSharpier and
`dotnet format` style via `dotnet husky run` for .NET, or `ruff format --check` / `ruff check` /
the repo's type checker for a Python repo (native tooling, no Docker, the same checks the
`../pre-commit/.pre-commit-config.yaml` snippet runs), plus the diff-scoped prose/comment-style
gate and the whole-tree line-ending check. Copy `../hub-fetch-run.py` alongside `pre-commit` for
the doc gates to run: it fetches those two checks fresh from `ptr727/ProjectTemplate`'s `main`
branch and runs them, rather than vendoring or pinning a copy. A pin nothing keeps current
goes stale by construction, and CI (this repo's own, and the hub's) is the backstop for a
change that lands broken on `main` before it does real damage locally. These are two more
network fetches alongside the Docker pulls the Lint tasks below already do. A fetch failure
fails the commit rather than silently skipping the gate.

Full linting (workflow YAML, Markdown, spelling, EditorConfig) is **not** run in the hook. It
runs in CI as pinned action wrappers, and on demand via the VS Code **Lint** tasks in
`catalog/snippets/configs/vscode-tasks.json` (Docker at `:latest`), which also carries the
same prose/EOL gates in whole-repo mode for on-demand full-tree validation, not just the
diff-scoped commit-time run. Keeping the Docker-dependent doc linters out of the hook is what
keeps it fast, and Docker is the dependency it stays free of: the hook still needs network
for the two `hub-fetch-run.py` calls above, so a fully offline clone fails the commit loudly
on the fetch rather than silently skipping the doc gates.

A copied `.husky/pre-commit` is an extensionless shebang script, so pin it to **LF** in
`.gitattributes` (`.husky/pre-commit text eol=lf`), git-level enforcement independent of the
editor. The fleet's `[*]` `.editorconfig` default already gives it LF, no path-specific
override needed. A CRLF shebang breaks execution.

Both language blocks run unconditionally, with no tool-presence guard: a repo that keeps a
block declares that tool required, so a missing one fails the commit loudly rather than
skipping the check silently. Drop the `dotnet husky run` block in a non-.NET repo, and drop
the Python block in a non-Python repo, rather than leaving it in place to no-op.
