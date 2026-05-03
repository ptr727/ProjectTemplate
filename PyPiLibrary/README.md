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

Releases are produced by `.github/workflows/build-pypilibrary-task.yml`, called from `build-release-task.yml` when `pypi: true` is passed by `publish-release.yml`. The publish job uses [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — no `PYPI_API_TOKEN` secret is involved; the workflow exchanges its OIDC token for a short-lived PyPI upload token.

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
- Update `_version.py` versioning policy to match your release cadence (Nerdbank.GitVersioning is .NET-only; for Python use a tag-driven scheme like [`hatch-vcs`](https://github.com/ofek/hatch-vcs) or manual bumps in `_version.py`).

If you don't want a Python project at all, delete the `PyPiLibrary/` folder, the `build-pypilibrary-task.yml` workflow, and the `pip` block in `dependabot.yml`.
