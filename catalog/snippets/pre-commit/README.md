# Pre-commit snippet

`.pre-commit-config.yaml` is the reference config for the `pre-commit` framework
(pre-commit.com), for a repo that keeps no .NET tool manifest declaring Husky.Net.
It is also the reference config for any repo that prefers it to Husky.Net. The toolchain the repo
already keeps is the discriminator, not the repo's own languages. Husky.Net runs from a .NET tool manifest declaring it, and this
framework needs none.
In a repo with Python, it runs `ruff format --check` and `ruff check`. It also runs that repo's
declared type checker, `pyright` or `mypy`, matching whichever `python-codestyle` says its CI
runs. Each tool runs via `uvx`, native tooling, never Docker. `uvx` needs no project dependency, matching CI's own
invocation for the lint-only profile (`CODESTYLE.md` "Two profiles"). A repo on the build
profile with a `uv.lock` may swap in `uv run <tool>` per entry to pin the project's own
version instead. The config also runs the same two shared doc gates the Husky.Net snippet
carries: the diff-scoped prose/comment-style gate and the whole-tree line-ending check. Those two
are what every repo owes regardless of language. The prose gate's `--diff HEAD` sees the staged
state under this runner rather than the working tree.
This is because `pre-commit` sets unstaged changes aside before it runs a hook.
So a partially staged file is judged on its staged edits alone here, where
the Husky.Net shape judges all of them. That set-aside covers tracked files only. So the gate
reads the paths `pre-commit` passes it rather than scanning `.`, which keeps an untracked file out
of the check. Those are the staged paths, or the conflicted ones while a merge is in progress.
`require_serial` keeps `pre-commit` from running the gate in parallel batches, each of which would
fetch it again. A path named that way is judged even inside a generated tree. So the
hook's `exclude` mirrors the gate's default `generated_trees`, and a repo adding to that list adds
the same names there. The pull request gate reads every line the branch changes against its base. A repo with no Python drops the `ruff-format`,
`ruff-check` and `type-check` hooks and keeps the doc gates.
That is what a Docker, config, or docs repo wires.

Copy `../hub_fetch_run.py` alongside `.pre-commit-config.yaml` (repo root) for the doc gates
to run. The script fetches those two checks fresh from `ptr727/ProjectTemplate`'s `main` branch and
runs them, rather than vendoring or pinning a copy. A pin that no tool keeps current goes
stale by construction. CI (this repo's own, and the hub's) is the backstop for a change
that lands broken on `main` before it does real damage locally. The prose gate adds one
`api.github.com` commit lookup to its fetch, with a `git ls-remote` against `github.com`
where that fails. An allow-list permitting only
`raw.githubusercontent.com` gets an unresolved-commit fallback. These are network calls
alongside the Docker pulls the VS Code Lint tasks already do. A fetch failure fails the commit
rather than silently skipping the gate.

Install and enable with `uv tool install pre-commit` once, then `pre-commit install`.
`pre-commit` itself is never added as a project dependency.
The lint-only profile has no
project environment to add it to, and `uv tool install` gives a persistent command independent
of any project. The hooks use `uvx` to run tools independently of the project.
If `pre-commit install` reports the command not found right after installing it, `uv tool
install`'s own bin directory is not yet on `PATH`.
Run `uv tool update-shell` and restart or
re-source the shell, or add the directory `uv tool dir --bin` prints directly.
Full linting (workflow YAML, Markdown,
spelling, EditorConfig) stays out of the hook. It runs in CI as pinned action wrappers, and on
demand via the VS Code **Lint** tasks. A Python repo takes those from
`catalog/snippets/configs/vscode-tasks-python.json` (Docker at `:latest`).
A repo with no
Python takes the Lint group alone rather than that file's Python tasks. Either way they carry
the prose gate in whole-repo mode, for on-demand full-tree validation beyond the diff-scoped
commit-time run. Their `Lint: EOL` task runs the same whole-tree EOL check the hook runs.

No LF pin is needed for `.pre-commit-config.yaml` itself: it is plain YAML, not a shebang
script, so the fleet's `[*]` `.editorconfig`/`.gitattributes` default already covers it.
