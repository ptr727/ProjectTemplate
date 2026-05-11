# PyPiLibrary

Python PyPI template — companion to the .NET `NuGetLibrary` in this repo. Published to PyPI as [`ptr727-projecttemplate-library`](https://pypi.org/project/ptr727-projecttemplate-library/).

## Stack

- **Build backend** — [`hatchling`](https://hatch.pypa.io/latest/) via `pyproject.toml`
- **Env / deps / publish** — [`uv`](https://docs.astral.sh/uv/) (Astral)
- **Lint + format** — [`ruff`](https://docs.astral.sh/ruff/)
- **Type checker** — [`pyright`](https://microsoft.github.io/pyright/)
- **Tests** — [`pytest`](https://docs.pytest.org/)
- **Publish** — [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) via `pypa/gh-action-pypi-publish` (no API token in repo secrets)
- **Version** — [Nerdbank.GitVersioning](https://github.com/dotnet/Nerdbank.GitVersioning) (NBGV) shared with the .NET side. CI overwrites `_version.py` with NBGV's `AssemblyFileVersion` before `uv build`, so each release ships .NET and Python at the same numeric version.

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

Prerequisite: enable **2FA** on the PyPI account (TOTP or hardware key). PyPI requires it before any trusted publisher can be registered.

1. **PyPI** → **Account settings** → **Publishing** → **Add a new pending publisher** ([direct link](https://pypi.org/manage/account/publishing/)). If the project already exists on PyPI, go to the project page → **Manage** → **Publishing** → **Add a new publisher** instead — the "pending" form is only for projects that don't exist yet. Fields:
   - **PyPI project name**: `ptr727-projecttemplate-library`
   - **Owner**: `ptr727`
   - **Repository name**: `ProjectTemplate`
   - **Workflow filename**: `publish-release.yml`
   - **Environment name**: `pypi`
2. **GitHub repo** → **Settings** → **Environments** → **New environment** → `pypi`. The environment owns deploy-time guardrails:
   - **Deployment branch rule** → **Selected branches and tags** → add `main`. Without this, a push to any branch could mint an OIDC token claiming to publish PyPI; with it, only pushes to `main` are eligible. **This step is mandatory — Trusted Publishing without a branch restriction is a documented security anti-pattern.**
   - (Optional) add yourself as a **required reviewer** so each publish requires a click — useful belt-and-suspenders against an accidental release.
3. The first successful release converts the pending publisher to a real publisher. After that the same OIDC exchange validates against the real publisher on every release.

Troubleshooting:

- `invalid-publisher: ... Publisher with matching claims was not found` — the publisher hasn't been registered yet, or one of the five claim fields (owner, repo, workflow filename, environment name, project name) doesn't match. Re-check step 1.
- `manifest unknown` from `docker:` pulling `ghcr.io/pypa/gh-action-pypi-publish` — the SHA pinned in `publish-release.yml` doesn't correspond to a release tag with a published GHCR image. Pin to the SHA that the upstream tag (`# vX.Y.Z` comment) actually points at on `pypa/gh-action-pypi-publish`.

Fallback (API token instead of Trusted Publishing): drop the `id-token: write` permission from the `publish-pypi` job, add `password: ${{ secrets.PYPI_API_TOKEN }}` to the `pypa/gh-action-pypi-publish` step, and store the token as a repo secret. Also pass `attestations: false` since attestations require the OIDC token. The OIDC path is preferred — no long-lived secret in the repo — so use the token method only when Trusted Publishing isn't an option.

## Template Adoption

When deriving a new project from this template:

- Replace the package name `ptr727-projecttemplate-library` (in `pyproject.toml`, this README, and CI) with your name.
- Rename `src/ptr727_projecttemplate_library/` to your import name.
- Re-register the trusted publisher on PyPI under the new project name.
- **Pick a versioning scheme.** The template defaults to **NBGV-driven** versioning shared with the .NET side: `_version.py` holds `__version__ = "0.0.0"` as a local-development placeholder, and the CI step **"Write version into _version.py step"** in [`build-pypilibrary-task.yml`](../.github/workflows/build-pypilibrary-task.yml) overwrites it with NBGV's `AssemblyFileVersion` (always `Major.Minor.Patch.BuildNumber`, all numeric, PEP 440 valid) just before `uv build`. The wheel and sdist uploaded to PyPI therefore share the version of the matching NuGet/Docker/executable artifacts for that commit. If you want a different scheme, replace both `_version.py` and the workflow step. Two common alternatives:
  - [`hatch-vcs`](https://github.com/ofek/hatch-vcs) — derive the version from git tags. Add it to `[build-system].requires` and switch `[tool.hatch.version]` to `source = "vcs"`. Drop the CI overwrite step. Pairs well with tag-driven releases and removes the NBGV dependency.
  - **Manual bumps** — edit `_version.py` in each release PR. Simplest, but easy to forget. Drop the CI overwrite step.

  The publish workflow uses `skip-existing: true` so a re-upload of the same version is a no-op instead of a failure — useful when iterating on releases without bumping NBGV.

If you don't want a Python project at all, delete the `PyPiLibrary/` folder, the `build-pypilibrary-task.yml` workflow, the `build-pypilibrary` job in `build-release-task.yml`, the `publish-pypi` job in `publish-release.yml`, and the `uv` block in `.github/dependabot.yml`.
