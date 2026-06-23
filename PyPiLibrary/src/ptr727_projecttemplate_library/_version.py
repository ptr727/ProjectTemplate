"""Single-source-of-truth for the package version.

Hatchling reads ``__version__`` via ``[tool.hatch.version]``. ``0.0.0`` is a
local-dev placeholder; before ``uv build`` the release pipeline rewrites the
``__version__`` line with a branch-aware PEP 440 version: ``main`` ->
``Major.Minor.Patch.BuildNumber`` (release), ``develop`` -> the same with a
``.dev0`` suffix, so ``pip install --pre`` prefers the develop build. See
``.github/workflows/build-pypilibrary-task.yml``.
"""

__version__ = "0.0.0"
