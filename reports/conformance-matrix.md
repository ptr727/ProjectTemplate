# Conformance Matrix

Tracks, per supported repo type, whether a **context-free agent stands it up cold** from the hub docs to an audit-passing state ([`STANDUP.md`][standup] "Self-Test"), and the date of the type's most recent audit ([`AUDIT.md`][audit]). A type that cannot be stood up cold is a documentation defect, not an agent failure - iterate the docs until it can.

`Cold-standup` values: `passing` (a fresh agent reaches operational), `gaps` (reaches partial; the note records the first doc gap), `not-tested` (self-test not yet run for this type).

The primary types are stood up as whole repos; the **composable targets** (`nuget`, `pypi`, `docker`) layer a publish leaf onto a base repo and are exercised as part of a base type's standup, not alone.

## Primary Types

| Type | Reference repo | Cold-standup | Last audited | First gap / notes |
|---|---|---|---|---|
| `python` + `source-only` | Financial-Modeling | not-tested | - | Reference for the source-release (dispatch-only) profile; the downstream standup issue is open. |
| `csharp` + `console` | - | not-tested | - | |
| `csharp` + `docker` | - | not-tested | - | |
| `homeassistant` | - | not-tested | - | Standalone-config conventions (home-assistant/core); scored by the `ha.*` checks. |
| `eda` | - | not-tested | - | Data-zip release, pull consumer. |
| `upstream-wrapper` | - | not-tested | - | Tag from a committed state file, not SemVer2. |
| `codegen` | - | not-tested | - | Deterministic matrix over both branches. |
| `docs` | ProjectTemplate | not-tested | - | Governance hub; CI is lint-only. |
| `operational` config | - | not-tested | - | `workflowModel: operational`; direct signed commits to `develop`, promotion-PR gate. |

## Composable Targets

| Target | Exercised via | Cold-standup | Notes |
|---|---|---|---|
| `nuget` | a `csharp` library base | not-tested | OIDC Trusted Publishing; no stored key. |
| `pypi` | a `python` library base | not-tested | OIDC; `environment: pypi`, `skip-existing: true`. |
| `docker` | any base with a Dockerfile | not-tested | Registry layer cache; always re-push. |

## Updating a Row

1. Run the [`STANDUP.md`][standup] self-test for the type (fresh agent, docs only).
2. Run [`AUDIT.md`][audit] against the result; set `Cold-standup` and `Last audited`.
3. If the result is not `passing`, record the first doc gap and fix it in the hub (docs or manifests), then re-run.

<!-- Repo -->

[audit]: ../AUDIT.md
[standup]: ../STANDUP.md
