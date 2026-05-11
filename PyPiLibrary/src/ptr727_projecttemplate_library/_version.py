"""Single-source-of-truth for the package version.

Hatchling reads ``__version__`` from this module via ``[tool.hatch.version]``.

The ``0.0.0`` value below is a local-development placeholder so ``uv build``
works outside CI. The release pipeline overwrites this file with the
Nerdbank.GitVersioning ``AssemblyFileVersion`` (computed from ``version.json``
+ git history, shared with the .NET side) just before ``uv build``, so the
wheel and sdist uploaded to PyPI carry the same numeric version as the
matching NuGet, Docker, and executable artifacts for the same release commit.
See ``.github/workflows/build-pypilibrary-task.yml`` (the "Write version into
_version.py step").

If you fork this template and want a different versioning scheme, replace
both this file's contents and the workflow step that rewrites it. Two common
alternatives: ``hatch-vcs`` (git-tag-driven, no NBGV dependency) or manual
edits per release.
"""

__version__ = "0.0.0"
