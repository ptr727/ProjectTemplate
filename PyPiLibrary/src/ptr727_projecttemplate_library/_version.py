"""Single-source-of-truth for the package version.

Hatchling reads ``__version__`` from this module via ``[tool.hatch.version]``.

The ``0.0.0`` value below is a local-development placeholder so ``uv build``
works outside CI. The release pipeline overwrites this file with the
Nerdbank.GitVersioning ``AssemblyFileVersion`` (``Major.Minor.Patch.BuildNumber``
— always numeric, PEP 440 valid) just before ``uv build``, so the wheel and
sdist uploaded to PyPI carry the same version string that's stamped into the
.NET assembly metadata as ``FileVersion`` / ``AssemblyVersion``. The NuGet
package version and Docker tags use NBGV's ``SemVer2`` instead — PEP 440
doesn't accept ``SemVer2``'s prerelease / build-metadata suffixes, so those
strings will not be byte-identical to PyPI's. All four artifacts derive from
the same NBGV computation against ``version.json`` + git history and so
correspond to the same release commit. See
``.github/workflows/build-pypilibrary-task.yml`` (the "Write version into
_version.py step").

If you fork this template and want a different versioning scheme, replace
both this file's contents and the workflow step that rewrites it. Two common
alternatives: ``hatch-vcs`` (git-tag-driven, no NBGV dependency) or manual
edits per release.
"""

__version__ = "0.0.0"
