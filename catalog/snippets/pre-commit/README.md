# Pre-commit snippet

`.pre-commit-config.yaml` is the reference config for the Python `pre-commit` framework
(pre-commit.com), for a Python repo with no `.husky/` tree of its own. It runs `ruff format
--check`, `ruff check`, and this repo's declared type checker (`pyright` or `mypy`, match
whichever `python-codestyle` says this repo's CI runs) via `uvx`, native tooling, no Docker,
plus the same two shared doc gates the Husky.Net snippet carries: the diff-scoped
prose/comment-style gate and the whole-tree line-ending check. `uvx` runs each tool from its
own latest release with no project dependency required, matching CI's own invocation for the
lint-only profile (`CODESTYLE.md` "Two profiles"). A repo on the build profile with a
`uv.lock` may swap in `uv run <tool>` per entry to pin the project's own version instead.

Copy `../hub-fetch-run.py` alongside `.pre-commit-config.yaml` (repo root) for the doc gates
to run: it fetches those two checks fresh from `ptr727/ProjectTemplate`'s `main` branch and
runs them, rather than vendoring or pinning a copy. A pin nothing keeps current goes stale by
construction, and CI (this repo's own, and the hub's) is the backstop for a change that lands
broken on `main` before it does real damage locally. These are two more network fetches
alongside the Docker pulls the VS Code Lint tasks already do. A fetch failure fails the
commit rather than silently skipping the gate.

Install and enable with `uv tool install pre-commit` once, then `pre-commit install`.
`pre-commit` itself is never added as a project dependency: the lint-only profile has no
project environment to add it to, and `uv tool install` gives a persistent, PATH-available
command independent of any project, the same footing `uvx` gives the tools the hooks run.
Full linting (workflow YAML, Markdown,
spelling, EditorConfig) stays out of the hook: it runs in CI as pinned action wrappers, and on
demand via the VS Code **Lint** tasks in `catalog/snippets/configs/vscode-tasks-python.json`
(Docker at `:latest`), which also carries the same prose/EOL gates in whole-repo mode for
on-demand full-tree validation, not just the diff-scoped commit-time run.

No LF pin is needed for `.pre-commit-config.yaml` itself: it is plain YAML, not a shebang
script, so the fleet's `[*]` `.editorconfig`/`.gitattributes` default already covers it.
