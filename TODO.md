# TODO

Running backlog for this repo, kept in a committed file so the guidance survives across environments where agent memory does not.

- Populate [reports/][reports] for the cataloged repos that still have no audit, since a registry `status` of `cataloged` asserts a result that only a committed report evidences. Eight repos have one.
- Canonicalize Python linter-config placement on `pyproject.toml` (one cataloged repo uses standalone `.ruff.toml` + `pyrightconfig.json`), track as a drift finding, fix downstream.
- Consider renaming this repo to reflect the audit-catalog identity (updates badge and link URLs across the fleet).
- Adopt the OCI annotation keys (`org.opencontainers.image.*`) for Docker image metadata across the Docker repos, replacing the ad-hoc and `org.label-schema.*` labels (from #363).
- Sweep `ManagePackageVersionsCentrally` placement to `Directory.Packages.props` fleet-wide (PlexCleaner sets it in `Directory.Build.props`, off the CODESTYLE canonical).
- Finish onboarding hardening (from #310): make the `AUDIT.md` audit a required onboarding step and run the per-type cold-start self-tests tracked in [reports/conformance-matrix.md][matrix] (`STANDUP.md` is already in place).
- Refresh the README (it has gone stale) and evaluate a lower-maintenance structure, for example a per-section index that points into each doc with a one-line description, keeping the README as the adoption and audit-instruction entry point with pointers to the other docs. A per-section index trades brevity for a sync obligation: it must track what the docs contain.
- Add a linter-only Python project type for codegen/boilerplate Python, code that runs during another tool's build to emit generated source (e.g. ESPHome codegen that produces enriched C++ at compile time), so it ships no unit tests and no coverage and needs only the linter. Keep it distinct from the existing `python` type, which is utility code that can and should carry unit tests and coverage (as in PlexCleaner). Until it exists, ESPHome-Config stays `source-only` and its `+python` reclassification is deferred, so accept its one outstanding validation finding meanwhile.
- Add a fleet-standard clang-format config for the `cpp` type: a catalog snippet plus a CODESTYLE C++ section defining the style, the C++ analogue of the shared ruff config, so the `cpp` clang-format check references one canonical style rather than each repo inventing its own. Base it on the ESPHome-Config agent's proposed `.clang-format`.
- Re-vendor `repo-config/configure.sh` across the fleet. The hub swept it to one sentence per line, and it is carried `verbatim` with `appliesTo: "*"`, so every repo already holding a copy is byte-mismatched against the hub until it takes the new one.

<!-- Repo -->

[matrix]: ./reports/conformance-matrix.md
[reports]: ./reports/
