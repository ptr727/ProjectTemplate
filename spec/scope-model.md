# Scope Model

How every governance rule is scoped, so the carried docs are granular single-scope pieces composed per repo, not large pieces with internal carve-outs a reader must piece out. This is a hub-only doc: it governs the carrying machinery ([`spec/files.json`][files], [`spec/files.schema.json`][files-schema], [`spec/audit.py`][audit]) and is not itself carried to the fleet.

## Two Axes

A rule has a physical home, and a repo rule has a reach as well.

- **Axis A, home.** A rule lives on the **host** (per-machine, `~/.claude`, `host-setup/`, loading in every session regardless of repo and covering ad-hoc work outside any project) or in the **repo** (it travels with a repo and can assume repo context). A rule that must hold in both places is stated in both and kept in sync deliberately, because the populations differ. The write-safety rules are the worked example, living in the host `~/.claude/CLAUDE.md` and the carried `AGENTS.md` at once.
- **Axis B, reach** (repo rules only). A repo rule is **hub-only** (meaningful only in this coordinator repo: the registry, the spec, the audit, fleet coordination), **all-downstream** (every derived repo), or **type-specific** (only repos matching a selector). Hub-only rules are simply absent from the carried baseline. All-downstream and type-specific rules are carried, gated by an `appliesTo` selector.

## Selectors

A selector is one token from one of four **disjoint** namespaces. Because the namespaces share no token, a single flat `appliesTo` list is unambiguous.

| Namespace | Tokens | Source of truth |
| --- | --- | --- |
| project type | `csharp` `nuget` `pypi` `python` `cpp` `console` `docker` `homeassistant` `eda` `codegen` `upstream-wrapper` `source-only` `hugo` `docs` | [`spec/project-types.json`][project-types] |
| workflow model | `release` `operational` | [`registry/repos.schema.json`][repos-schema] |
| release trigger | `two-phase` `publish-on-merge` `dispatch-only` `none` | [`registry/repos.schema.json`][repos-schema] |
| consumer model | `push` `pull` | [`registry/repos.schema.json`][repos-schema] |

A repo's **selector set** is its `types` plus its `workflowModel`, `releaseTrigger`, and `consumerModel`. `workflowModel` and `releaseTrigger` resolve as the repo value, then `defaults`, then the fleet default (`release`, `two-phase`). `consumerModel` has no fleet default. [`spec/validate.py`][validate] requires it on every cataloged repo, so a cataloged repo always contributes one. `validate.py` also enforces that every `appliesTo` token resolves to a known selector and that no project type collides with a reserved token, and [`spec/audit.py`][audit] resolves the set in `repo_selectors`.

## appliesTo Semantics

`appliesTo` appears on a [`spec/files.json`][files] entry (which files a repo carries) and, per the section-object form in [`spec/files.schema.json`][files-schema], on an individual `sections` element (which sections within a carried file apply).

- **`*`** means all repos.
- A list is **disjunctive (any-of)**: `["csharp", "operational"]` reads "csharp OR operational". Cross-axis **AND is not expressible**, and that is deliberate. A single-scope piece carries one selector, so the need for AND is the signal to split the piece further, not to write a two-token entry.
- Entry-level and section-level `appliesTo` compose with **AND**: a section applies only if its file is carried by the repo *and* the section's own selector matches.

## Documenting a Whole-Carried File's Section Scopes

A file carried `whole` (no `sections` allowlist) still has single-scope sections, and the applicability gate resolves an inapplicable section to N/A at read time, so no split is needed. Record the mapping here rather than mechanizing it.

- [`CODESTYLE.md`][codestyle]: **General** is all-downstream, **.NET** is `csharp`, **Python** is `python`. A non-`csharp` repo reads the .NET section as N/A, a non-`python` repo the Python section.

<!-- Repo -->
[audit]: ./audit.py
[codestyle]: ../CODESTYLE.md
[files]: ./files.json
[files-schema]: ./files.schema.json
[project-types]: ./project-types.json
[repos-schema]: ../registry/repos.schema.json
[validate]: ./validate.py
