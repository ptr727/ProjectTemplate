# AGENTS.md Section Model

Companion to [fidelity-model.md][fidelity-model]. That doc defines how a carried *file* is verified. This one defines how the *sections* of `AGENTS.md` carry, and records the fidelity of each. It is the ground truth an agent or human consults before adding, removing, or re-typing a section, not a judgment re-derived each session.

## Why sections have fidelity

`AGENTS.md` is the fleet's cross-cutting rule book, and its sections are not equal. Most state a rule that is identical in every repo. A few describe the repo they live in. Treating them all as carried-by-intent is what lets a rule silently paraphrase, weaken, or vanish, which is the recurring drift this model exists to stop. So each section carries a declared fidelity, checked by the audit against the hub canonical.

## The categories

A section is one of the following. Fidelity is declared in [files.json][files], never inferred.

- **verbatim** - universal fleet-law rule *text*, byte-identical in every repo after EOL and action-pin normalization. The audit content-hashes each downstream copy against the hub's `## <heading>` block. A verbatim section may carry clauses only some repos exercise (for example "a source-only repo carries no build task"). The *text* is still identical everywhere - applicability is per-repo, the wording is not.
- **intent** - the section *describes this particular repo* (its own directory tree, its own devcontainer and toolchain), so its content legitimately varies. The heading must be present, the body is judged by meaning rather than hashed.

`appliesTo` scope is orthogonal to fidelity. A section may apply to a subset of repos (for example `operational`) and is checked only for repos in that subset.

**The default is verbatim.** `AGENTS.md` holds rule text, and rule text is universal. The repo-specific *values* live in other files (`.editorconfig`, `.devcontainer/`, the real tree), never in this prose. `intent` is the deliberate exception for a section that is inherently a description of one repo.

## The classification

| Section | Fidelity | Reason |
| --- | --- | --- |
| Foundational Principles | verbatim | the governing rationale, universal |
| Repository Boundaries and Write Safety | verbatim | universal write-safety law |
| Git and Commit Rules | verbatim | universal git law |
| Branching Model | verbatim | universal (repo-specific history SHAs removed so it can carry) |
| Release Model | verbatim | universal release contract, all target shapes described inline |
| Operational Repositories | verbatim, `appliesTo: operational` | universal for operational repos, N/A for release repos |
| Repository Onboarding and Conformance | verbatim | universal audit and registry obligations |
| Pull Request Title and Commit Message Conventions | verbatim | universal, with generic examples |
| Documentation Style Conventions | verbatim | all rule text, including the Line Endings *rule* (a repo's `.editorconfig` value is not here) |
| Verification Discipline | verbatim | universal verification law |
| PR Review Etiquette | verbatim | universal review-loop contract |
| Communicating with the User | verbatim | universal |
| Workflow YAML Conventions | verbatim | universal conventions, applied to whatever workflows a repo has |
| Supported Development Platforms | verbatim | universal |
| Editor and Tasks | verbatim | universal editor convention (standard set plus per-language additions) |
| Repository Details | verbatim | universal About-panel convention |
| Devcontainer | intent | describes this repo's toolchain and devcontainer, genuinely per-repo |
| Repository Layout | intent | describes this repo's directory tree, genuinely per-repo |

## Changing the structure carries review weight

The set of sections, and each section's fidelity, is itself governed.

- **Adding a section** is a fleet-wide act - it declares a new rule every repo must carry. Add it to `AGENTS.md`, to `files.json`, and to the table above in the same change, with its fidelity chosen deliberately.
- **Changing a verbatim section** re-vendors it across the whole fleet. The audit drift wave that follows is the mechanism working, not a regression.
- **Changing a section's fidelity** (intent to verbatim or back) is a governance decision, recorded here with its reason.
- **A downstream repo's extra section** the hub does not declare is drift to reconcile, not a local liberty. Either the rule belongs fleet-wide, so promote it here, or it is repo-specific content that does not belong in a carried section.

## Enforcement

`files.json` declares each section's fidelity. [validate.py][validate] proves every declared verbatim section resolves in the hub `AGENTS.md`. [audit.py][audit] checks each repo's copy - presence for `intent`, byte-match for `verbatim` - and classifies a mismatch as stale (re-vendor) or modified (review).

<!-- Internal -->

[audit]: ./audit.py
[fidelity-model]: ./fidelity-model.md
[files]: ./files.json
[validate]: ./validate.py
