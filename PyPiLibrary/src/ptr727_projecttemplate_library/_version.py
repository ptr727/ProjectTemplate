"""Single-source-of-truth for the package version.

Hatchling reads ``__version__`` from this module via ``[tool.hatch.version]``.

The ``0.0.0`` value below is a local-development placeholder so ``uv build``
works outside CI. The release pipeline replaces the ``__version__`` line
(in place, preserving this docstring) with a PEP 440 version computed from
NBGV's ``AssemblyFileVersion`` (``Major.Minor.Patch.BuildNumber``) just
before ``uv build``. The format is **branch-aware**:

- ``main`` push  → ``Major.Minor.Patch.BuildNumber`` (PEP 440 release).
  ``pip install <pkg>`` picks this up by default. Equals the .NET
  assemblies' ``FileVersion`` stamp.
- ``develop`` push → ``Major.Minor.Patch.BuildNumber.dev0`` (PEP 440 dev
  release). The BuildNumber stays in the release segment so develop's
  segment grows past main's per commit — that's what makes
  ``pip install --pre <pkg>`` actually prefer the develop build over the
  main release. Without ``--pre``, pip filters the ``.dev`` suffix and
  picks the main release. Matches how NuGet/Docker mark develop as
  prerelease. Edge case: in the window between a release merge to main
  and the next commit on develop, develop's BuildNumber equals main's
  (or is one lower), so ``--pre`` still resolves to the main release
  until a new develop commit lands. Self-healing.

.NET's ``AssemblyVersion`` (the binary-compat identity, a separate NBGV
output) and NuGet ``PackageVersion`` / Docker tags (NBGV ``SemVer2`` —
PEP 440 doesn't accept its prerelease / build-metadata suffixes) all
carry different strings across artifact families. All four still derive
from the same NBGV computation against ``version.json`` + git history
and correspond to the same commit (main pushes publish release
versions; develop pushes publish PEP 440 dev releases / NBGV
prereleases). See
``.github/workflows/build-pypilibrary-task.yml`` (the "Compute PyPI
version step" and "Write version into _version.py step").

If you fork this template and want a different versioning scheme, replace
both this file's contents and the workflow step that rewrites it. Two common
alternatives: ``hatch-vcs`` (git-tag-driven, no NBGV dependency) or manual
edits per release.
"""

__version__ = "0.0.0"
