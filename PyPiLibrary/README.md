# PyPiLibrary

Python PyPi template — companion to the .NET `NuGetLibrary` in this repo. Published to PyPI as [`ptr727-projecttemplate-library`](https://pypi.org/project/ptr727-projecttemplate-library/).

## Stack

- **Build backend** — [`hatchling`](https://hatch.pypa.io/latest/) via `pyproject.toml`
- **Env / deps / publish** — [`uv`](https://docs.astral.sh/uv/) (Astral)
- **Lint + format** — [`ruff`](https://docs.astral.sh/ruff/)
- **Type checker** — [`pyright`](https://microsoft.github.io/pyright/)
- **Tests** — [`pytest`](https://docs.pytest.org/)
- **Publish** — [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) via `pypa/gh-action-pypi-publish` (no API token in repo secrets)

## Layout

```text
PyPiLibrary/
    pyproject.toml
    README.md
    src/
        ptr727_projecttemplate_library/
            __init__.py
            _version.py
            example.py
    tests/
        __init__.py
        test_example.py
```

## Local Development

The repo's [devcontainer](../docs/devcontainer.md) installs `uv` automatically and runs `uv sync` for this project on first open. To work outside the devcontainer:

```shell
# from the repo root
cd PyPiLibrary
uv sync                          # creates .venv, installs deps + dev group
uv run ruff check                # lint
uv run ruff format --check       # formatting check
uv run pyright                   # type check
uv run pytest                    # tests
uv build                         # wheel + sdist into ./dist
```

## Publishing

Releases are produced by `.github/workflows/build-pypilibrary-task.yml` (called from `build-release-task.yml` to build, lint, type-check, test, and upload the wheel + sdist as a workflow-run artifact). Publishing is a separate top-level `publish-pypi` job in `publish-release.yml` that downloads the artifact by name and runs [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — no `PYPI_API_TOKEN` secret is involved. The publish job has `id-token: write` only at that single job level, so the test-pull-request flow (which calls the same build task during PR validation) doesn't need to propagate that permission through the reusable workflow chain.

First-time setup (one-time, on PyPI):

1. PyPI → **Account settings** → **Publishing** → **Add a new pending publisher**.
2. Project name: `ptr727-projecttemplate-library`. Owner: `ptr727`. Repo: `ProjectTemplate`. Workflow: `publish-release.yml`. Environment: `pypi`.
3. GitHub repo → **Settings** → **Environments** → create `pypi` environment (optionally with required reviewers).
4. The first successful release converts the pending publisher to a real publisher.

## Template Adoption

When deriving a new project from this template:

- Replace the package name `ptr727-projecttemplate-library` (in `pyproject.toml`, this README, and CI) with your name.
- Rename `src/ptr727_projecttemplate_library/` to your import name.
- Re-register the trusted publisher on PyPI under the new project name.
- **Wire up a versioning scheme before the first publish.** `_version.py` ships with `__version__ = "0.0.0"` as a placeholder. The publish workflow uses `skip-existing: true` so the workflow won't fail on duplicate uploads — but **no new versions will land on PyPI** until you replace `0.0.0` with something that increments. Common options:
  - [`hatch-vcs`](https://github.com/ofek/hatch-vcs) — derive the version from git tags. Add it to `[build-system].requires` and switch `[tool.hatch.version]` to `source = "vcs"`. Pairs well with tag-driven releases.
  - **Read from `version.json`** — the .NET side uses Nerdbank.GitVersioning which reads from `version.json`. A small custom Hatchling plugin or a CI step can pull the version into `_version.py` so .NET and Python ship with matching versions.
  - **Manual bumps** — edit `_version.py` in each release PR. Simplest, but easy to forget.

If you don't want a Python project at all, delete the `PyPiLibrary/` folder, the `build-pypilibrary-task.yml` workflow, the `build-pypilibrary` job in `build-release-task.yml`, the `publish-pypi` job in `publish-release.yml`, and the `uv` block in `dependabot.yml`.
