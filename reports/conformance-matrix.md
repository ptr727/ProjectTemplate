# Conformance Matrix

Tracks, per supported repo **shape**, whether a **context-free agent stands it up cold** from the hub docs to an audit-passing state ([`STANDUP.md`][standup] "Self-Test"), and the date of the shape's most recent audit ([`AUDIT.md`][audit]). A shape is the project type(s) plus the workflow model, where `operational` is a `workflowModel` overlay rather than a `spec/project-types.json` type. The date is `YYYY-MM-DD`, and a `-` cell means not yet audited. A shape that cannot be stood up cold is a documentation defect rather than an agent failure, so iterate the docs until it can.

`Cold-standup` values: `passing` (a fresh agent reaches operational), `gaps` (reaches partial, and the note records the first doc gap), `not-tested` (self-test not yet run for this shape).

The primary shapes are stood up as whole repos. The **composable targets** (`nuget`, `pypi`, `docker`) layer a publish leaf onto a base repo and are exercised as part of a base shape's standup, not alone.

## Primary Shapes

| Shape | Reference repo | Cold-standup | Last audited | First gap / notes |
|---|---|---|---|---|
| `python` + `source-only` | Financial-Modeling | not-tested | - | Reference for the source-release (dispatch-only) profile. The downstream standup issue is open. |
| `hugo` + `source-only` + `release` | Blog | not-tested | 2026-08-05 | Hugo static site (#456, #558), stood up 2026-08-01 and cataloged 2026-08-03. Release and deploy are independent surfaces: a dispatch-only publisher cuts the tag, and a separate dispatch deploys to a `self-hosted` filesystem per environment. Reclassified off the interim `source-only`-alone declaration when the type landed. **The 2026-08-05 run is the first to judge the `hugo` checks**, since the 2026-08-03 one predated the type and graded the repo as `source-only` alone. All nine pass, the repo is operational, and the three deviations the first run recorded are closed (ptr727/Blog#27, ptr727/Blog#28, ptr727/Blog#29). Two drift classes stay open, both of them the hub having advanced: seven stale verbatim carries and 17 prose findings. This is the shape `hugo.deploy.retention` was written against: its deploy credential is confined write-only, so the deploy can neither prune nor read the destination back, and the prune is a host-side timer its runbook records as host-owned. The first draft of that check demanded an in-pipeline assertion and would have pushed a correct design to widen a deliberately narrow credential. The run also surfaced a hub defect rather than a repo one, carried as escalation 1 of the report: the template-reference check had no exemption for the byte-locked `Fleet Bootstrap` section, whose first sentence must name the hub, so it fired unclearably on every repo that had carried the current canonical. Fixed since, across all three surfaces that stated the rule: the scan in `spec/audit.py` excises a file's verbatim sections before looking for the name, and `GOVERNANCE.md` "Documentation Style Conventions" and `recurring.norepoxref` both carry the exception and its boundary. It cleared exactly two findings fleet-wide and kept the other eleven. |
| `csharp` + `console` | - | not-tested | - | |
| `csharp` + `docker` | - | not-tested | - | |
| `csharp` + `python` | PlexCleaner | not-tested | 2026-08-15 | First mixed-language shape (#339). Python is a stdlib-only `uvx` **scripts** profile subtree (`RegressionTests/`): no `uv.lock`, `pyproject.toml` lint/type config only, mypy checker, `python.uvlock.pinned` + `python.coverage.codecov` N/A, and `codecov.yml` stays required for the C# side. Both language rule-sets apply (CODESTYLE.md "Two profiles"). **The 2026-08-15 run is the first to judge the `python` checks**, since the 2026-07-04 one predated the type declaration and graded the repo as `csharp` + `console` + `docker` alone. The repo is operational: every mechanized check passes, and the two workflow divergences still standing (no `changes` paths-filter, a dispatch guard that skips rather than failing fast) are letter misses whose intent holds. Its two open drift items are prose and configuration rather than pipeline, and the pass also settled the two `investigate` gap dispositions the whole fleet was carrying. |
| `homeassistant` | - | not-tested | - | Standalone-config conventions (home-assistant/core), scored by the `ha.*` checks. |
| `eda` | - | not-tested | - | Data-zip release, pull consumer. |
| `upstream-wrapper` | - | not-tested | - | Tag from a committed state file, not SemVer2. |
| `codegen` | - | not-tested | - | Deterministic matrix over both branches. |
| `docs` | ProjectTemplate | not-tested | - | Governance hub, and CI is lint-only. |
| `operational` config | HomeAutomation-Config | not-tested | 2026-08-15 | `workflowModel: operational`, direct signed commits to `develop`, promotion-PR gate. Its `develop` ruleset carries no `pull_request` rule, so the branch discipline rests on the instruction rather than the gate. HomeAutomation-Config is the first operational repository with a committed report (`reports/homeautomation-config/audit.md`): `source-only` plus `operational`, clean on every mechanized check on `main` after its 2026-08-15 resync and promotion. Its `.editorconfig` and `.gitattributes` are an LF adaptation that the intent advisory keeps flagging by construction. |

## Composable Targets

| Target | Exercised via | Cold-standup | Notes |
|---|---|---|---|
| `nuget` | a `csharp` library base | not-tested | OIDC Trusted Publishing, with no stored key. |
| `pypi` | a `python` library base | not-tested | OIDC, with `environment: pypi`, `skip-existing: true`. |
| `docker` | any base with a Dockerfile | not-tested | Registry layer cache, and always re-push. |
| `self-hosted` | a `hugo` base | not-tested | rsync over SSH into a per-environment release directory, with an atomic pointer flip. Retention takes either D5.6 shape: the deploy asserts the count where its credential can observe the destination, and the host owns it where that credential is confined write-only, which is the case on the first member. Credentials are per-environment GitHub Environment secrets and variables rather than repository secrets, so `spec/secrets.json` declares the mechanism with an empty `requires` and the audit cannot see whether the environments are configured. |

## Updating a Row

1. Run the [`STANDUP.md`][standup] self-test for the shape (fresh agent, docs only).
2. Run [`AUDIT.md`][audit] against the result, then set `Cold-standup` and `Last audited`.
3. If the result is not `passing`, record the first doc gap and fix it in the hub (docs or manifests), then re-run.

<!-- Repo -->

[audit]: ../AUDIT.md
[standup]: ../STANDUP.md
