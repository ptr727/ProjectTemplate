"""Single-source-of-truth for the package version.

Hatchling reads ``__version__`` from this module via ``[tool.hatch.version]``.

The ``0.0.0`` value below is a local-development placeholder so ``uv build``
works outside CI. The release pipeline replaces the ``__version__`` line
(in place, preserving this docstring) with NBGV's ``AssemblyFileVersion``
(``Major.Minor.Patch.BuildNumber`` — always numeric, PEP 440 valid) just
before ``uv build``, so the wheel and sdist uploaded to PyPI carry the same
version string that's stamped into the .NET assembly metadata as
``FileVersion``. .NET's ``AssemblyVersion`` (the binary-compat identity, a
separate NBGV output) and NuGet ``PackageVersion`` / Docker tags (NBGV
``SemVer2`` — PEP 440 doesn't accept its prerelease / build-metadata
suffixes) all carry different strings. All four artifacts still derive from
the same NBGV computation against ``version.json`` + git history and
correspond to the same release commit. See
``.github/workflows/build-pypilibrary-task.yml`` (the "Write version into
_version.py step").

If you fork this template and want a different versioning scheme, replace
both this file's contents and the workflow step that rewrites it. Two common
alternatives: ``hatch-vcs`` (git-tag-driven, no NBGV dependency) or manual
edits per release.
"""

__version__ = "0.0.0"
