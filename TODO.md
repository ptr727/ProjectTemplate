# TODO

Running backlog for this repo, kept in a committed file so the guidance survives across environments where agent memory does not.

- Run the first per-repo audits and populate [reports/][reports] for the seven cataloged repos.
- Classify the standardization-backlog repos in [registry/repos.json][repos] (marked `classificationPending`) on first audit.
- Canonicalize Python linter-config placement on `pyproject.toml` (one cataloged repo uses standalone `.ruff.toml` + `pyrightconfig.json`), track as a drift finding, fix downstream.
- Consider renaming this repo to reflect the audit-catalog identity (updates badge and link URLs across the fleet).
- Adopt the OCI annotation keys (`org.opencontainers.image.*`) for Docker image metadata across the Docker repos, replacing the ad-hoc and `org.label-schema.*` labels (from #363).
- Sweep `ManagePackageVersionsCentrally` placement to `Directory.Packages.props` fleet-wide (PlexCleaner sets it in `Directory.Build.props`, off the CODESTYLE canonical).
- Finish onboarding hardening (from #310): make the `AUDIT.md` audit a required onboarding step and run the per-type cold-start self-tests tracked in [reports/conformance-matrix.md][matrix] (`STANDUP.md` is already in place).
- Refresh the README (it has gone stale) and evaluate a lower-maintenance structure, for example a per-section index that points into each doc with a one-line description, keeping the README as the adoption and audit-instruction entry point with pointers to the other docs. A per-section index trades brevity for a sync obligation: it must track what the docs contain.
- Add a linter-only Python project type for codegen/boilerplate Python, code that runs during another tool's build to emit generated source (e.g. ESPHome codegen that produces enriched C++ at compile time), so it ships no unit tests and no coverage and needs only the linter. Keep it distinct from the existing `python` type, which is utility code that can and should carry unit tests and coverage (as in PlexCleaner). Until it exists, ESPHome-Config stays `source-only` and its `+python` reclassification is deferred, so accept its one outstanding validation finding meanwhile.
- Add a fleet-standard clang-format config for the `cpp` type: a catalog snippet plus a CODESTYLE C++ section defining the style, the C++ analogue of the shared ruff config, so the `cpp` clang-format check references one canonical style rather than each repo inventing its own. Base it on the ESPHome-Config agent's proposed `.clang-format`.
- Sweep the 13 `dash` and `semicolon` findings in `README.md`. They are deferred rather than dropped, because two changes to that file are in flight and a third overlapping edit would conflict with both for no gain.
- Clean the comment shape in `.editorconfig`, `.gitattributes` and `.gitignore` (44 `comment-wrap` and `comment-case` findings). These are the first files every new repo copies, so until they are fixed a new repo learns the shape the rules forbid.
- Sweep the 54 `comment-wrap` and `comment-case` findings in `repo-config/configure.sh`. It is carried `verbatim`, so a downstream copy is byte-matched and cannot fix them locally, which makes this the hub's whole-class sweep rather than a next-edit correction.

<!-- Repo -->

[matrix]: ./reports/conformance-matrix.md
[repos]: ./registry/repos.json
[reports]: ./reports/
