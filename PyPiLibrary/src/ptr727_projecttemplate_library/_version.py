"""Single-source-of-truth for the package version.

Hatchling reads ``__version__`` from this module via ``[tool.hatch.version]``.
For tag-driven versioning, swap this for ``hatch-vcs`` and configure the build
backend to derive the version from git tags.
"""

__version__ = "0.0.0"
