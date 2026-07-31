# Conformance Matrix

Tracks, per supported repo **shape** - the project type(s) plus the workflow model (`operational` is a `workflowModel` overlay, not a `spec/project-types.json` type) - whether a **context-free agent stands it up cold** from the hub docs to an audit-passing state ([`STANDUP.md`][standup] "Self-Test"), and the date (`YYYY-MM-DD`; `-` = not yet audited) of the shape's most recent audit ([`AUDIT.md`][audit]). A shape that cannot be stood up cold is a documentation defect, not an agent failure - iterate the docs until it can.

`Cold-standup` values: `passing` (a fresh agent reaches operational), `gaps` (reaches partial; the note records the first doc gap), `not-tested` (self-test not yet run for this shape).

The primary shapes are stood up as whole repos; the **composable targets** (`nuget`, `pypi`, `docker`) layer a publish leaf onto a base repo and are exercised as part of a base shape's standup, not alone.

## Primary Shapes

| Shape | Reference repo | Cold-standup | Last audited | First gap / notes |
|---|---|---|---|---|
| `python` + `source-only` | Financial-Modeling | not-tested | - | Reference for the source-release (dispatch-only) profile; the downstream standup issue is open. |
| `csharp` + `console` | - | not-tested | - | |
| `csharp` + `docker` | - | not-tested | - | |
| `csharp` + `python` | PlexCleaner | not-tested | - | First mixed-language shape (#339). Python is a stdlib-only `uvx` **scripts** profile subtree (`RegressionTests/`): no `uv.lock`, `pyproject.toml` lint/type config only, mypy checker, `python.uvlock.pinned` + `python.coverage.codecov` N/A; `codecov.yml` stays required for the C# side. Both language rule-sets apply (CODESTYLE.md "Two profiles"). |
| `homeassistant` | - | not-tested | - | Standalone-config conventions (home-assistant/core); scored by the `ha.*` checks. |
| `eda` | - | not-tested | - | Data-zip release, pull consumer. |
| `upstream-wrapper` | - | not-tested | - | Tag from a committed state file, not SemVer2. |
| `codegen` | - | not-tested | - | Deterministic matrix over both branches. |
| `docs` | ProjectTemplate | not-tested | - | Governance hub; CI is lint-only. |
| `operational` config | - | not-tested | - | `workflowModel: operational`, direct signed commits to `develop`, promotion-PR gate. Carries a required `OPERATIONS.md` (`appliesTo: ["operational"]`, presence-checked) for its runbooks. Blog (#456) is the next standup and the first cold test of `STANDUP.md` step 0. |

## Composable Targets

| Target | Exercised via | Cold-standup | Notes |
|---|---|---|---|
| `nuget` | a `csharp` library base | not-tested | OIDC Trusted Publishing; no stored key. |
| `pypi` | a `python` library base | not-tested | OIDC; `environment: pypi`, `skip-existing: true`. |
| `docker` | any base with a Dockerfile | not-tested | Registry layer cache; always re-push. |

## Updating a Row

1. Run the [`STANDUP.md`][standup] self-test for the shape (fresh agent, docs only).
2. Run [`AUDIT.md`][audit] against the result; set `Cold-standup` and `Last audited`.
3. If the result is not `passing`, record the first doc gap and fix it in the hub (docs or manifests), then re-run.

<!-- Repo -->

[audit]: ../AUDIT.md
[standup]: ../STANDUP.md
