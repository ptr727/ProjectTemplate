---
name: python-codestyle
description: >-
  Governs Python code style for ptr727/ProjectTemplate fleet repos: the build-versus-lint-only
  profile split, the uv/ruff/pyright/mypy/pytest toolchain, src layout, formatting and linting,
  comment and docstring conventions, type hints, naming, imports, patterns to avoid, test
  conventions, and versioning. Use this whenever writing, reviewing, or editing a .py file, a
  pyproject.toml, or a uv.lock, whenever deciding whether a Python subtree is a shippable project
  or a lint-only scripts tree, whenever choosing pyright versus mypy for a repo's CI gate, or
  whenever writing or reviewing a pytest test. Triggers even when the task looks like a small
  local fix ("just add a helper function", "silence this lint warning", "add a dependency"),
  because the profile split, the ruff-is-authoritative rule, and the ban on backward-compat
  shims or impossible-case error handling are each easy to violate one file at a time. Applies
  only to a repo's Python side, a repo with no Python has no use for this Skill.
---

# Python Codestyle

## Why this exists

This is the Python-specific half of the fleet's code style guide, kept in one place instead of
re-derived per repo or per session. CODESTYLE.md's General section still owns the rules every
language shares (clean-compile verification as a concept, the suppression-scope order, tooling
casing in prose), this Skill is everything specific to a Python project on top of that: the two
profiles, the toolchain, layout, and the language-level conventions.

## Adapt before propagating

The rules below describe the default Python profile: a package that publishes to PyPI,
type-checked by pyright in strict mode, dependencies in `[dependency-groups]`. A derived repo
often differs, and when it does, adapt these fields to match the repo's actual toolchain rather
than copying verbatim (a verbatim copy that misdescribes the repo is inaccurate and gets rejected
in review). The axes that commonly vary per repo:

- **Type checker in CI**: pyright strict, mypy in CI with pyright editor-only (Pylance), or both.
  Whichever runs in CI is the one the clean-compile and the CI gate invoke.
- **Dependency declaration**: `[dependency-groups]`, or PEP 621 `[project.optional-dependencies]`
  (dev tools installed with `uv sync --extra <group>`).
- **Versioning / publishing**: a published package (`_version.py` plus a version source,
  `uv build`, and a PyPI publish step), or a source-only repo with a static `version` and no
  publish step (see Versioning below).
- **Disabled markdownlint rules**: repo-specific, `.markdownlint-cli2.jsonc` at the repo root is
  the source of truth, not any example rule named here.
- **VS Code config home**: editor settings/extensions may live in `.vscode/*.json` or the
  `<Repo>.code-workspace`, while tasks/launch/debug configs can only be external `.vscode/*.json`
  (they cannot live in the workspace file). The repo's own `tasks.json` sits wherever it keeps it,
  and the canonical task definitions it is written against are the hub `vscode-tasks-python.json`
  snippet, which resolves the same way from every repo.

## Two profiles

A repo's Python is one of two shapes, declared as the `build` or `lint-only` profile and validated
against the `pyproject.toml` shape. Most of this Skill (uv project, `uv.lock`, `uv run`, src
layout, pytest coverage) describes the Project shape (the `build` profile). The two differ by
whether the Python has third-party runtime dependencies, which shows up structurally in
`pyproject.toml`, so the fleet's audit reads the shape there:

- **Project** (the `build` profile): the Python has third-party runtime dependencies, or is the
  repo's deliverable. It is a PEP 621 uv project: `[project]` with `dependencies` (dev tools in
  `[project.optional-dependencies]` or `[dependency-groups]`), a `[build-system]`, and a committed
  `uv.lock` (pinned LF, per GOVERNANCE.md's "Line Endings" section). CI runs `uv sync --frozen` +
  `uv run <tool>`, so the lockfile pins tool versions.
- **Scripts** (the `lint-only` profile): stdlib-only utility scripts embedded in a non-Python repo
  (e.g. a Python tooling subtree of a `csharp` app). Run the tools with `uvx` (no project install,
  no lockfile): the `pyproject.toml` carries only tool config (`[tool.ruff]`, `[tool.mypy]`, and
  an optional `[tool.pyright]` editor block), with no `[project]`, no `[build-system]`, and no
  `uv.lock` (that metadata would misrepresent it as a shippable package). mypy is the type-check
  gate (there is no first-party package for pyright strict to anchor on), and a `[tool.pyright]`
  block in standard mode keeps Pylance quiet in the editor, the same mypy-gate/pyright-editor
  split the build profile uses. There is no lockfile, and a `uvx <tool>@<ver>` pin in a `run:`
  step is not something Dependabot tracks, so CI runs `uvx ruff@latest` / `uvx mypy@latest` rather
  than a manual pin that would silently go stale. The fleet rule is to pin only what Dependabot
  auto-updates (SHA-pinned actions, package deps) and otherwise run latest, so the VS Code tasks,
  README, and CI all run the unpinned latest here. `.py` files follow the repo's line-ending
  default (CRLF in a CRLF-default repo, and a shebang-executed script is LF-pinned by path, per
  GOVERNANCE.md's "Line Endings" section). There is no pytest suite, and `unittest` is the runner
  instead. A script that carries a gate still earns tests, written with the standard library's
  `unittest` so they run under bare `python3` with nothing installed, as `test_<script>.py` under
  a `tests/` directory beside the scripts it exercises (`<scripts-dir>/tests/`), kept apart so a
  test never reads as a tool. Within the scripts directory the name carries the kind: a gate that
  checks and exits non-zero on a finding takes a `_lint` or `_gate` suffix, and a utility that
  does work takes none. Any repo carrying Python carries the Python tooling in CI, coverage
  included, this profile too: `uvx ruff@latest check`, `uvx ruff@latest format --check`,
  `uvx mypy@latest`, and the unittest suite
  under `uvx coverage@latest run -m unittest discover -s <scripts-dir>/tests` with
  `coverage report`, informational with no threshold adopted. A co-present `csharp` type still
  carries `codecov.yml` for its own tests.

## Toolchain

| Tool | Role | Config |
|---|---|---|
| [uv][uv-link] | env, deps, build, publish (build/publish only where the repo ships a package) | `pyproject.toml` `[dependency-groups]` or `[project.optional-dependencies]`, `uv.lock` |
| [hatchling][latest-link] | build backend (published packages) | `pyproject.toml` `[build-system]` |
| [ruff][ruff-link] | lint + format + import sort | `pyproject.toml` `[tool.ruff]` |
| [pyright][pyright-link] | type checker (the default, a strict baseline) | `pyproject.toml` `[tool.pyright]` |
| [mypy][mypy-link] | additional/alternate type checker (optional, the CI checker in a mypy-in-CI repo, required for Home Assistant) | `pyproject.toml` `[tool.mypy]` (or per home-assistant/core) |
| [pytest][docs-link] | test runner | `pyproject.toml` `[tool.pytest.ini_options]` |

**Type checking targets strongly typed, deterministic code.** pyright in strict mode is the
default baseline on first-party code (a repo may instead run mypy in CI and keep pyright
editor-only via Pylance, per the next paragraph): `[tool.pyright]` `strict = ["src"]`, or the
integration package for a Home Assistant repo, with tests run in standard mode. pyright is the
anchor because Pylance embeds it, so the editor and the CLI/CI (`uv run pyright`) run the same
engine and never disagree. The standalone `ms-pyright.pyright` extension stays in
`unwantedRecommendations` because Pylance covers it. Relax strictness on third-party code only
when a dependency has no usable types and no alternative (e.g. `pandas`): a targeted, commented
`# pyright: ignore[...]` or a scoped `[tool.pyright]` override, never a blanket relaxation.

**mypy is allowed, and required where the ecosystem demands it, it is not banned.** Running more
than one checker is normal when each serves a purpose (the .NET side pairs CSharpier and
`dotnet format` the same way), and pyright's inference and mypy's plugin ecosystem (e.g.
`pydantic.mypy`) catch different classes of error. A Home Assistant integration runs
`mypy --strict` because the platinum `strict-typing` quality-scale tier requires it, and a
pydantic-heavy library may opt in for the plugin. When a repo uses mypy it runs in CI and the
editor (the `ms-python.mypy-type-checker` extension) so the two stay consistent, and its mypy
command joins the clean-compile. A repo with no such need stays pyright-only, which is lighter and
inherently consistent.

## Local development loop

From inside the Python project directory:

```sh
uv sync                          # creates .venv, installs deps + dev group
uv run ruff format               # auto-format
uv run ruff check --fix          # auto-fix lint
uv run ruff check                # verify lint clean
uv run ruff format --check       # verify format clean
uv run pyright                   # verify types
uv run pytest                    # run tests
uv build                         # produce wheel + sdist in ./dist (published packages only)
```

The Python clean-compile is `uv run ruff format` + `uv run ruff check` + the repo's type checker:
`uv run pyright`, or `uv run mypy src` where mypy is the CI checker, or both where the repo runs
both (see Type checking above). Run it, plus `uv run pytest`, before committing. These are
documented commands, and an optional VS Code tasks mirror (all `type: process`, no `&&` shell
chaining, so it runs the same on any task shell) is in the hub `vscode-tasks-python.json` snippet.
CI runs the same clean-compile commands as the authoritative backstop. Git hooks are opt-in, so
wire `pre-commit` for `ruff` and the type checker yourself if you want local enforcement.

## Layout

`src` layout, which keeps the package out of the repo root and prevents accidental imports of
unbuilt code:

```text
<python-project>/
    pyproject.toml
    README.md
    uv.lock                # committed for reproducible CI
    src/
        <package_name>/
            __init__.py
            _version.py        # published packages; a source-only repo uses a static version instead
            <modules>.py
    tests/
        __init__.py
        test_<module>.py
```

## Code style

### Formatting and linting

- **`ruff format` is authoritative.** Don't argue with the formatter, and if it reformats your
  code, that's the final form. Configure (line length, target version) in `pyproject.toml`
  `[tool.ruff]`, not via inline `# fmt:` directives.
- **Run `ruff check --fix` before committing.** Most ruff lint rules have safe autofixes, let the
  tool handle them. The configured rule families are listed under `[tool.ruff.lint]` `select`. Add
  new rule families project-wide rather than scattering inline `# noqa` markers.
- **`# noqa` is a last resort.** When you must use one, scope it narrowly (`# noqa: E501`, not
  bare `# noqa`) and add a short comment on the same line explaining why. False-positive patterns
  that recur across the codebase belong in `[tool.ruff.lint]` `ignore` or per-file
  `[tool.ruff.lint.per-file-ignores]`, with a comment. Porting an existing codebase is not a
  license to add `ignore` / `per-file-ignores` blocks to mute newly surfaced lint. Fix it.

### Comments

- **Inline `#` comments**: keep tight and local. One line is preferred, but multi-line is fine
  when you need to document a non-obvious implementation constraint, a local trade-off, or
  coupling that future edits could easily break. Keep that rationale next to the affected block so
  the reviewer/maintainer sees it at edit-time.
- **Don't explain what the code does.** Well-named identifiers handle that. Don't reference the
  current task ("added for X", "used by Y"), which belongs in the PR description.

### Docstrings

- Follow [PEP 257][pep-0257-link]. Focus docstrings primarily on the behavior contract (what
  callers and tests can rely on), public semantics, and edge-case expectations.
  Implementation-local rationale belongs in inline `#` comments, not docstrings.
- A short one-liner is fine for trivial functions and tests with self-documenting names.
- For non-trivial behavior (non-obvious test scenarios, contracts a test pins, edge cases callers
  must know about, design trade-offs that are load-bearing for future maintainers), write a
  one-line summary, blank line, then a details paragraph. Multi-paragraph docstrings are fine when
  the contract earns it.
- Design notes belong in the code (docstrings or inline comments). They do NOT belong in
  `HISTORY.md`, which is end-user release notes, not a design log.

### Type hints

- **All public APIs are typed.** The repo's configured type checker runs on `src/` (pyright strict
  via `[tool.pyright]` `strict = ["src"]`, or mypy where that is the CI checker), and tests run in
  the checker's looser/standard mode.
- **Use modern syntax**: `list[int]` not `List[int]`, `dict[str, X]` not `Dict[str, X]`,
  `X | None` not `Optional[X]`, `from __future__ import annotations` only when needed for forward
  references.
- **Don't add `# type: ignore` to silence pyright errors without a comment** explaining the
  constraint. If a recurring false positive needs suppression, configure it project-wide in
  `[tool.pyright]`. A new port doesn't change this, fix freshly surfaced type errors rather than
  muting them.

### Naming

- `snake_case` for functions, methods, variables, modules, package directories.
- `PascalCase` for classes, type aliases, type vars, enum members.
- `UPPER_SNAKE_CASE` for module-level constants.
- Single leading underscore for module-private, double leading underscore for name-mangled (rare,
  and usually means rethink the design).

### Imports

- **Let ruff sort imports.** `[tool.ruff.lint]` `select` includes the `I` rule family
  (isort-equivalent). Don't hand-sort.
- Standard library first, then third-party, then first-party (the project itself), each block
  separated by a blank line, which ruff enforces automatically.
- Avoid wildcard imports (`from x import *`) outside `__init__.py` re-exports.

### Patterns to avoid

- **Don't add backward-compat shims, `# removed` markers, or rename-to-`_` for unused vars**, just
  delete. Git history is the audit trail.
- **Don't add error handling for impossible cases.** Trust internal code, and validate only at
  boundaries (user input, parsed config, external APIs).
- **Don't use exceptions for expected control flow.** Exceptions are for unexpected states.
- **Don't suppress errors silently** (`except Exception: pass`). Either handle the specific
  exception and document why it's safe, or let it propagate.

## Tests

- `pytest` with the configuration in `[tool.pytest.ini_options]`. Default invocation:
  `uv run pytest`.
- One test file per module under test, named `test_<module>.py`.
- Test functions named `test_<scenario>_<expected_behavior>`, descriptive and not numbered.
- Use fixtures (defined in `conftest.py` for shared ones, or per-test for narrowly-scoped) instead
  of setup/teardown methods.
- **Avoid mocking when fakes work.** Hand-rolled fakes that implement the protocol you depend on
  are usually clearer and break less than `unittest.mock` magic.
- **Test edge cases that the docstring promises**, not implementation details. If the test breaks
  when you refactor without changing behavior, the test is asserting on an implementation detail.

## Versioning

**Published packages.** `_version.py` ships with `__version__ = "0.0.0"` as a placeholder. Until
you wire `_version.py` to something that increments (the usual options are `hatch-vcs`, a
version.json bridge, or manual bumps), no new PyPI versions will land, and publishing with
`skip-existing: true` keeps a stuck placeholder version from failing the run.

**Source-only repos** (no PyPI publish, with a source-release on dispatch or no release at all) do
not need `_version.py`: keep a static `version` in `pyproject.toml` `[project]`, or let the
release pipeline's version source (e.g. NBGV plus `version.json`) own the tag. There is no publish
step to guard, so `skip-existing` does not apply.

## Linter cleanliness

Before pushing or opening a PR:

- VS Code's Problems pane should be quiet for the files you touched. The relevant linters are ruff
  (via the `charliermarsh.ruff` extension) and pyright (via the `ms-python.python` extension's
  bundled Pylance).
- The CI gate is `uv run ruff check`, `uv run ruff format --check`, the repo's type checker
  (`uv run pyright` or `uv run mypy src`), and `uv run pytest`, the same commands as the local
  loop above, run from the Python project directory (invoked as separate steps, not `&&`-chained,
  so the runner shell is irrelevant).
- Markdown in this directory follows CODESTYLE.md's repo-wide Markdown and Spelling rules,
  packaged as the `comment-and-doc-style` Skill.

<!-- External -->

[docs-link]: https://docs.pytest.org/
[latest-link]: https://hatch.pypa.io/latest/
[mypy-link]: https://mypy-lang.org/
[pep-0257-link]: https://peps.python.org/pep-0257/
[pyright-link]: https://microsoft.github.io/pyright/
[ruff-link]: https://docs.astral.sh/ruff/
[uv-link]: https://docs.astral.sh/uv/
