# Code Style and Formatting Rules — Python

This file is the style guide for the **Python project** in this repo: [`PyPiLibrary/`](./). It does NOT apply to the .NET projects — see [`CODESTYLE.md`](../CODESTYLE.md) at the repo root for those.

Cross-cutting rules (PR titles, branching, US English, markdown style, workflow YAML, PR review etiquette) live in [`AGENTS.md`](../AGENTS.md) and apply to both languages. This file only documents what's specific to Python.

## Toolchain

| Tool | Role | Config |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | env, deps, build, publish | `pyproject.toml` `[dependency-groups]`, `uv.lock` |
| [hatchling](https://hatch.pypa.io/latest/) | build backend | `pyproject.toml` `[build-system]` |
| [ruff](https://docs.astral.sh/ruff/) | lint + format + import sort | `pyproject.toml` `[tool.ruff]` |
| [pyright](https://microsoft.github.io/pyright/) | type checker | `pyproject.toml` `[tool.pyright]` |
| [pytest](https://docs.pytest.org/) | test runner | `pyproject.toml` `[tool.pytest.ini_options]` |

`pyright` is consumed in two places: as a dev dependency (`uv run pyright` for CI/scripted runs) and via VS Code's **Pylance** extension (which embeds pyright). The standalone `ms-pyright.pyright` extension is in `unwantedRecommendations` because Pylance covers it. `mypy` is **not used** here — don't introduce it.

## Local Development Loop

From inside `PyPiLibrary/`:

```sh
uv sync                          # creates .venv, installs deps + dev group
uv run ruff format               # auto-format
uv run ruff check --fix          # auto-fix lint
uv run ruff check                # verify lint clean
uv run ruff format --check       # verify format clean
uv run pyright                   # verify types
uv run pytest                    # run tests
uv build                         # produce wheel + sdist in ./dist
```

CI runs the same commands via [`.github/workflows/build-pypilibrary-task.yml`](../.github/workflows/build-pypilibrary-task.yml). Husky.Net pre-commit hooks (configured in [`.husky/task-runner.json`](../.husky/task-runner.json)) run `ruff format` and `ruff check` against staged Python files when `uv` is on PATH.

## Layout

`src` layout — keeps the package out of the repo root and prevents accidental imports of unbuilt code:

```text
PyPiLibrary/
    pyproject.toml
    README.md
    CODESTYLE.md           # this file
    uv.lock                # committed for reproducible CI
    src/
        ptr727_projecttemplate_library/
            __init__.py
            _version.py
            <modules>.py
    tests/
        __init__.py
        test_<module>.py
```

## Code Style

### Formatting and Linting

- **`ruff format` is authoritative.** Don't argue with the formatter; if it reformats your code, that's the final form. Configure (line length, target version) in `pyproject.toml` `[tool.ruff]`, not via inline `# fmt:` directives.
- **Run `ruff check --fix` before committing.** Most ruff lint rules have safe autofixes; let the tool handle them. The configured rule families are listed under `[tool.ruff.lint]` `select`. Add new rule families project-wide rather than scattering inline `# noqa` markers.
- **`# noqa` is a last resort.** When you must use one, scope it narrowly (`# noqa: E501`, not bare `# noqa`) and add a short comment on the same line explaining why. False-positive patterns that recur across the codebase belong in `[tool.ruff.lint]` `ignore` or per-file `[tool.ruff.lint.per-file-ignores]`, with a comment.

### Comments

- **Inline `#` comments**: keep tight and local. One line is preferred, but multi-line is fine when you need to document a non-obvious implementation constraint, a local trade-off, or coupling that future edits could easily break. Keep that rationale next to the affected block so the reviewer/maintainer sees it at edit-time.
- **Don't explain *what* the code does** — well-named identifiers handle that. Don't reference the current task ("added for X", "used by Y"); that belongs in the PR description.

### Docstrings

- Follow [PEP 257](https://peps.python.org/pep-0257/). Focus docstrings primarily on the **behavior contract** (what callers and tests can rely on), public semantics, and edge-case expectations. Implementation-local rationale belongs in inline `#` comments, not docstrings.
- A short one-liner is fine for trivial functions and tests with self-documenting names.
- For non-trivial behavior — non-obvious test scenarios, contracts a test pins, edge cases callers must know about, design trade-offs that are load-bearing for future maintainers — write a one-line summary, blank line, then a details paragraph. Multi-paragraph docstrings are fine when the contract earns it.
- Design notes belong **in the code** (docstrings or inline comments). They do NOT belong in [`HISTORY.md`](../HISTORY.md) — that file is end-user release notes, not a design log.

### Type Hints

- **All public APIs are typed.** Pyright runs on `src/` in strict mode (`[tool.pyright]` `strict = ["src"]`); tests run in standard mode.
- **Use modern syntax**: `list[int]` not `List[int]`, `dict[str, X]` not `Dict[str, X]`, `X | None` not `Optional[X]`, `from __future__ import annotations` only when needed for forward references.
- **Don't add `# type: ignore` to silence pyright errors without a comment** explaining the constraint. If a recurring false positive needs suppression, configure it project-wide in `[tool.pyright]`.

### Naming

- `snake_case` for functions, methods, variables, modules, package directories.
- `PascalCase` for classes, type aliases, type vars, enum members.
- `UPPER_SNAKE_CASE` for module-level constants.
- Single leading underscore for module-private; double leading underscore for name-mangled (rare — usually means rethink the design).

### Imports

- **Let ruff sort imports.** `[tool.ruff.lint]` `select` includes the `I` rule family (isort-equivalent). Don't hand-sort.
- Standard library first, then third-party, then first-party (the project itself), each block separated by a blank line — ruff enforces this automatically.
- Avoid wildcard imports (`from x import *`) outside `__init__.py` re-exports.

### Patterns to Avoid

- **Don't add backward-compat shims, `# removed` markers, or rename-to-`_` for unused vars** — just delete. Git history is the audit trail.
- **Don't add error handling for impossible cases.** Trust internal code; only validate at boundaries (user input, parsed config, external APIs).
- **Don't use exceptions for expected control flow.** Exceptions are for *unexpected* states.
- **Don't suppress errors silently** (`except Exception: pass`). Either handle the specific exception and document why it's safe, or let it propagate.

## Tests

- `pytest` with the configuration in `[tool.pytest.ini_options]`. Default invocation: `uv run pytest`.
- One test file per module under test, named `test_<module>.py`.
- Test functions named `test_<scenario>_<expected_behavior>` — descriptive, not numbered.
- Use fixtures (defined in `conftest.py` for shared ones, or per-test for narrowly-scoped) instead of setup/teardown methods.
- **Avoid mocking when fakes work.** Hand-rolled fakes that implement the protocol you depend on are usually clearer and break less than `unittest.mock` magic.
- **Test edge cases that the docstring promises**, not implementation details. If the test breaks when you refactor *without changing behavior*, the test is asserting on an implementation detail.

## Versioning

`_version.py` ships with `__version__ = "0.0.0"` as a placeholder. The publish workflow uses `skip-existing: true` so the workflow won't fail, but no new PyPI versions will land until you wire `_version.py` to something that increments. See the **Template Adoption** section of [`README.md`](./README.md) for the three usual options (`hatch-vcs`, version.json bridge, manual bumps).

## Linter Cleanliness

Before pushing or opening a PR:

- VS Code's **Problems** pane should be quiet for the files you touched. The relevant linters are ruff (via the `charliermarsh.ruff` extension) and pyright (via the `ms-python.python` extension's bundled Pylance).
- The CI gate is `uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest` — same as the local commands above, run from `PyPiLibrary/`.
- For markdown files in this directory, follow the markdown style rules in [AGENTS.md](../AGENTS.md). The repo's markdownlint config applies; fix violations at the source rather than disabling rules.

## Adopting This Template Without Python

If your derived project does not need a Python side, delete the entire `PyPiLibrary/` folder, the `build-pypilibrary` job in `build-release-task.yml`, the `publish-pypi` job in `publish-release.yml`, the `build-pypilibrary-task.yml` workflow, the `uv` block in `.github/dependabot.yml`, the Python entries in `.husky/task-runner.json`, and the Python settings/extension recommendations in `ProjectTemplate.code-workspace` and `.devcontainer/devcontainer.json`. The .NET side stands alone.
