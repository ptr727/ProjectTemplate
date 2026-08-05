# Conformance Matrix

Tracks, per supported repo **shape** - the project type(s) plus the workflow model (`operational` is a `workflowModel` overlay, not a `spec/project-types.json` type) - whether a **context-free agent stands it up cold** from the hub docs to an audit-passing state ([`STANDUP.md`][standup] "Self-Test"), and the date (`YYYY-MM-DD`; `-` = not yet audited) of the shape's most recent audit ([`AUDIT.md`][audit]). A shape that cannot be stood up cold is a documentation defect, not an agent failure - iterate the docs until it can.

`Cold-standup` values: `passing` (a fresh agent reaches operational), `gaps` (reaches partial; the note records the first doc gap), `not-tested` (self-test not yet run for this shape).

The primary shapes are stood up as whole repos; the **composable targets** (`nuget`, `pypi`, `docker`) layer a publish leaf onto a base repo and are exercised as part of a base shape's standup, not alone.

## Primary Shapes

| Shape | Reference repo | Cold-standup | Last audited | First gap / notes |
|---|---|---|---|---|
| `python` + `source-only` | Financial-Modeling | not-tested | - | Reference for the source-release (dispatch-only) profile; the downstream standup issue is open. |
| `hugo` + `source-only` + `release` | Blog | not-tested | 2026-08-03 | Hugo static site (#456, #558), stood up 2026-08-01 and cataloged 2026-08-03. Release and deploy are independent surfaces: a dispatch-only publisher cuts the tag, and a separate dispatch deploys to a `self-hosted` filesystem per environment. Reclassified off the interim `source-only`-alone declaration when the type landed. Two deviations found by the type's own checks are open against the repo (ptr727/Blog#28, ptr727/Blog#29): the vendored theme records no upstream ref, and the generator pin is duplicated across two workflows. It is also the case that shaped `hugo.deploy.retention`: its deploy credential is confined write-only, so the deploy can neither prune nor read the destination back, and the prune is a host-side timer its runbook records as host-owned. The first draft of that check demanded an in-pipeline assertion and would have pushed a correct design to widen a deliberately narrow credential. The audit report predates the deploy and is due a re-run. |
| `csharp` + `console` | - | not-tested | - | |
| `csharp` + `docker` | - | not-tested | - | |
| `csharp` + `python` | PlexCleaner | not-tested | - | First mixed-language shape (#339). Python is a stdlib-only `uvx` **scripts** profile subtree (`RegressionTests/`): no `uv.lock`, `pyproject.toml` lint/type config only, mypy checker, `python.uvlock.pinned` + `python.coverage.codecov` N/A; `codecov.yml` stays required for the C# side. Both language rule-sets apply (CODESTYLE.md "Two profiles"). |
| `homeassistant` | - | not-tested | - | Standalone-config conventions (home-assistant/core); scored by the `ha.*` checks. |
| `eda` | - | not-tested | - | Data-zip release, pull consumer. |
| `upstream-wrapper` | - | not-tested | - | Tag from a committed state file, not SemVer2. |
| `codegen` | - | not-tested | - | Deterministic matrix over both branches. |
| `docs` | ProjectTemplate | not-tested | - | Governance hub; CI is lint-only. |
| `operational` config | - | not-tested | - | `workflowModel: operational`, direct signed commits to `develop`, promotion-PR gate. Its `develop` ruleset carries no `pull_request` rule, so the branch discipline rests on the instruction rather than the gate. |

## Composable Targets

| Target | Exercised via | Cold-standup | Notes |
|---|---|---|---|
| `nuget` | a `csharp` library base | not-tested | OIDC Trusted Publishing; no stored key. |
| `pypi` | a `python` library base | not-tested | OIDC; `environment: pypi`, `skip-existing: true`. |
| `docker` | any base with a Dockerfile | not-tested | Registry layer cache; always re-push. |
| `self-hosted` | a `hugo` base | not-tested | rsync over SSH into a per-environment release directory, with an atomic pointer flip. Retention takes either D5.6 shape: the deploy asserts the count where its credential can observe the destination, and the host owns it where that credential is confined write-only, which is the case on the first member. Credentials are per-environment GitHub Environment secrets and variables rather than repository secrets, so `spec/secrets.json` declares the mechanism with an empty `requires` and the audit cannot see whether the environments are configured. |

## Updating a Row

1. Run the [`STANDUP.md`][standup] self-test for the shape (fresh agent, docs only).
2. Run [`AUDIT.md`][audit] against the result; set `Cold-standup` and `Last audited`.
3. If the result is not `passing`, record the first doc gap and fix it in the hub (docs or manifests), then re-run.

<!-- Repo -->

[audit]: ../AUDIT.md
[standup]: ../STANDUP.md
