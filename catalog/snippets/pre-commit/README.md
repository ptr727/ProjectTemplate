# Pre-commit snippet

`.pre-commit-config.yaml` is the reference config for the `pre-commit` framework
(pre-commit.com), for a repo that keeps no .NET tool manifest, and for any repo that prefers it to
Husky.Net. The host toolchain is the discriminator, not the
repo's own languages: Husky.Net is a `dotnet` tool, and this framework installs independently
of one. In a repo with Python it runs `ruff format --check` and `ruff check`, plus that
repo's declared type checker, `pyright` or `mypy`, matching whichever `python-codestyle` says
its CI runs. Each tool runs via
`uvx`, native tooling, never Docker. `uvx` needs no project dependency, matching CI's own
invocation for the lint-only profile (`CODESTYLE.md` "Two profiles"). A repo on the build
profile with a `uv.lock` may swap in `uv run <tool>` per entry to pin the project's own
version instead. The config also runs the same two shared doc gates the Husky.Net snippet
carries: the diff-scoped prose/comment-style gate and the whole-tree line-ending check. Those two
are what every repo owes regardless of language. A repo with no Python drops the `ruff-format`,
`ruff-check` and `type-check` hooks and keeps the doc gates, which is what a Docker, config, or
docs repo wires.

Copy `../hub-fetch-run.py` alongside `.pre-commit-config.yaml` (repo root) for the doc gates
to run: it fetches those two checks fresh from `ptr727/ProjectTemplate`'s `main` branch and
runs them, rather than vendoring or pinning a copy. A pin that no tool keeps current goes
stale by construction, and CI (this repo's own, and the hub's) is the backstop for a change
that lands broken on `main` before it does real damage locally. These are two more network
fetches alongside the Docker pulls the VS Code Lint tasks already do. A fetch failure fails
the commit rather than silently skipping the gate.

Install and enable with `uv tool install pre-commit` once, then `pre-commit install`.
`pre-commit` itself is never added as a project dependency: the lint-only profile has no
project environment to add it to, and `uv tool install` gives a persistent command independent
of any project. The hooks use `uvx` to run tools independently of the project.
If `pre-commit install` reports the command not found right after installing it, `uv tool
install`'s own bin directory is not yet on `PATH`: run `uv tool update-shell` and restart or
re-source the shell, or add the directory `uv tool dir --bin` prints directly.
Full linting (workflow YAML, Markdown,
spelling, EditorConfig) stays out of the hook: it runs in CI as pinned action wrappers, and on
demand via the VS Code **Lint** tasks. A Python repo takes those from
`catalog/snippets/configs/vscode-tasks-python.json` (Docker at `:latest`), and a repo with no
Python takes the Lint group alone rather than that file's Python tasks. Either way they carry
the same prose/EOL gates in whole-repo mode for on-demand full-tree validation, not just the
diff-scoped commit-time run.

No LF pin is needed for `.pre-commit-config.yaml` itself: it is plain YAML, not a shebang
script, so the fleet's `[*]` `.editorconfig`/`.gitattributes` default already covers it.
