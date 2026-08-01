# Agent Instruction Section Model

Companion to [fidelity-model.md][fidelity-model]. That doc defines how a carried *file* is verified. This one defines how the *sections* of the agent instruction set carry, and records the fidelity of each. It is the ground truth an agent or human consults before adding, removing, or re-typing a section, not a judgment re-derived each session.

The set spans two files. [`AGENTS.md`][agents] is the entry point every agent reads first, and carries only the rules that apply to every task (context and delegation) plus the map to the rest. [`GOVERNANCE.md`][governance] carries the topical rule text, one section per topic. The split exists so an agent loads the section a task needs instead of the whole rule book, and it changes nothing about how a section is classified or checked.

## Why sections have fidelity

`GOVERNANCE.md` is the fleet's cross-cutting rule book, and its sections are not equal. Most state a rule that is identical in every repo. A few describe the repo they live in. Treating them all as carried-by-intent is what lets a rule silently paraphrase, weaken, or vanish, which is the recurring drift this model exists to stop. So each section carries a declared fidelity, checked by the audit against the hub canonical.

## The categories

A section is one of the following. Fidelity is declared in [files.json][files], never inferred.

- **verbatim** - universal fleet-law rule *text*, byte-identical in every repo after EOL and action-pin normalization. The audit content-hashes each downstream copy against the hub's `## <heading>` block. A verbatim section may carry clauses only some repos exercise (for example "a source-only repo carries no build task"). The *text* is still identical everywhere, since applicability varies by repo while the wording does not.
- **intent** - the section *describes this particular repo* (its own directory tree, its own devcontainer and toolchain), so its content legitimately varies. The heading must be present, the body is judged by meaning rather than hashed.

`appliesTo` scope is orthogonal to fidelity. A section may apply to a subset of repos (for example `operational`) and is checked only for repos in that subset.

**The default is verbatim.** These files hold rule text, and rule text is universal. The repo-specific *values* live in other files (`.editorconfig`, `.devcontainer/`, the real tree), never in this prose. `intent` is the deliberate exception for a section that is inherently a description of one repo.

## The classification

| Section | File | Fidelity | Reason |
| --- | --- | --- | --- |
| Context and Delegation Discipline | `AGENTS.md` | verbatim | universal cost law: session scope, reading, commands, delegation |
| Where the Rules Live | `AGENTS.md` | verbatim | the map from a task to the section that governs it |
| Foundational Principles | `GOVERNANCE.md` | verbatim | the governing rationale, universal |
| Durable Knowledge and Self-Improvement | `GOVERNANCE.md` | verbatim | universal meta-rule: durable knowledge belongs in the committed docs and agents keep them current |
| Repository Boundaries and Write Safety | `GOVERNANCE.md` | verbatim | universal write-safety law |
| Git and Commit Rules | `GOVERNANCE.md` | verbatim | universal git law |
| Branching Model | `GOVERNANCE.md` | verbatim | universal (repo-specific history SHAs removed so it can carry) |
| Release Model | `GOVERNANCE.md` | verbatim | universal release contract, all target shapes described inline |
| Operational Repositories | `GOVERNANCE.md` | verbatim | fleet context (the two workflow models), carried by all so the cross-references to it resolve |
| Pull Request Title and Commit Message Conventions | `GOVERNANCE.md` | verbatim | universal, with generic examples |
| Documentation Style Conventions | `GOVERNANCE.md` | verbatim | all rule text, including the Line Endings *rule* (a repo's `.editorconfig` value is not here) |
| Verification Discipline | `GOVERNANCE.md` | verbatim | universal verification law |
| PR Review Etiquette | `GOVERNANCE.md` | verbatim | universal review-loop contract |
| Communicating with the User | `GOVERNANCE.md` | verbatim | universal |
| Workflow YAML Conventions | `GOVERNANCE.md` | verbatim | universal conventions, applied to whatever workflows a repo has |
| Supported Development Platforms | `GOVERNANCE.md` | verbatim | universal |
| Editor and Tasks | `GOVERNANCE.md` | verbatim | universal editor convention (standard set plus per-language additions) |
| Repository Details | `GOVERNANCE.md` | verbatim | universal About-panel convention |
| Devcontainer | `GOVERNANCE.md` | intent | describes this repo's toolchain and devcontainer, genuinely per-repo |
| Repository Layout | `GOVERNANCE.md` | intent | describes this repo's directory tree, genuinely per-repo |

**Devcontainer content.** A devcontainer is optional infrastructure, not required by any repo type. An operational (live config) repo is edited and deployed live and typically has none, so its Devcontainer section states that plainly. A repo that keeps one (a code repo's toolchain, or an offline-debugging aid for a config repo) describes it. The section is present in every carried `GOVERNANCE.md` so the development model is always answered, even when the answer is none.

**Not carried (hub-only).** `Repository Onboarding and Conformance` lives in the hub's `GOVERNANCE.md` as hub-audit context (reconciling the registry, the STANDUP cold-start, the conformance matrix) but is not a carried section, since a downstream agent never runs those. Its one universal rule, that a repo is done when it passes `AUDIT.md` for its type, is carried in `AUDIT.md` itself. Like the model docs and `STANDUP.md`, it is hub machinery, not fleet law.

## Changing the structure carries review weight

The set of sections, and each section's fidelity, is itself governed.

- **Adding a section** is a fleet-wide act, declaring a new rule every repo must carry. Add it to the file it belongs in, to `files.json` under that file, and to the table above in the same change, with its fidelity chosen deliberately. A rule that applies to every task belongs in `AGENTS.md`, and a topical rule belongs in `GOVERNANCE.md`.
- **Changing a verbatim section** re-vendors it across the whole fleet. The audit drift wave that follows is the mechanism working, not a regression.
- **Changing a section's fidelity** (intent to verbatim or back) is a governance decision, recorded here with its reason.
- **A downstream repo's extra section** the hub does not declare is drift to reconcile, not a local liberty, including a section whose *heading* differs but whose *content* duplicates a verbatim section (compare by content, not heading name). Either the rule belongs fleet-wide, so promote it here, or its unique part is repo-specific content that moves to one of the topical docs below and the duplicate is deleted. The audit lists a repo's undeclared sections as an advisory so the reconciliation is not missed.

## Where repo-specific content goes

A repo's own content is not carried and is not declared here, so extraction needs a predictable destination rather than a judgment call per repo. Four topical docs take it, chosen by what the content *is*:

- [`CODESTYLE.md`][codestyle]: a repo's language and formatting conventions beyond the carried rules.
- `ARCHITECTURE.md`: how a code repo is built, its module layout, data flow, and design decisions.
- `OPERATIONS.md`: how a repo is run, under the headings `Runbooks`, `Backup and Recovery`, `Logs and Debugging`, `Tool Usage`, and `Configuration Layout`. It is the operational analogue of `ARCHITECTURE.md`, and it is where an `AGENTS.md` split puts the repo-specific half.
- `TODO.md`: the repo's running backlog, which keeps open work out of the README's section order where it does not belong and changes on a different cadence from everything around it.

**`OPERATIONS.md` is required for every repo**, declared in [`files.json`][files] as `appliesTo: "*"` and checked for presence only, the same footing as `README.md` and `HISTORY.md`, so its content is entirely the repo's own. It is mandatory rather than advisory because the convention was already emerging unevenly: of the four operational-model repos, two wrote one unprompted and the others scattered the same material across ad-hoc names, which is the improvisation these destinations exist to prevent. That reasoning never depended on the workflow model. Every repo has operational surface, since publishing to a package registry needs trusted-publisher setup, shipping an image needs registry credentials, and serving a site needs a deploy path and a staging story. A repo with nothing to say still carries the file as a stub, meaning those five headings with no content under them, because a stub names the destination and its shape where a blank file names only the destination. This repo's own [`OPERATIONS.md`][operations] is the worked example.

**The workflow model and the need for this file are unrelated axes.** `operational` as a `workflowModel` describes where config lives and how a change reaches `develop`, not whether the repo has runbooks. Keying the file to that selector read a sufficient condition as a necessary one, since an operational-model repo certainly has runbooks while a release repo has them too. Reclassifying a repo between models does not change how much operational surface it has, which is the test that showed the selector was wrong.

**A destination is declared when its content class recurs across repos.** Software architecture recurs, because every code repo has one, so `ARCHITECTURE.md` is declared. A home-device inventory does not recur, so it stays the repo's own file, neither declared nor mandated. The scattering these destinations prevent is the same material landing under different names in different repos, and that has no force for content existing in exactly one repo. Declaring a destination for a one-repo need would grow this list without bound and still lag whatever the next repo invents.

`CODESTYLE.md` is carried by every repo already. `ARCHITECTURE.md` stays **declared but advisory**: every code repo has an architecture, and how much of it earns a separate document is contextual, so mandating it would produce empty files where the design needs none. Declared and required are separate questions, and only a universal need answers both.

`OPERATIONS.md` is agent-instruction content, so it takes the inline-link exception the markdown rules name, not the reference-style default. `ARCHITECTURE.md` is not on that closed list and follows the reference-style rule.

## Migrating a repo onto the split

A repo that carried its governance inside `AGENTS.md` before the router split holds two things in one file: sections that are stale copies of fleet law, and local additions written after a fault the fleet has never seen. Re-vendoring the canonical over the whole file silently deletes the second kind.

**Probe the canonical for each local rule's distinctive phrase.** That is the check that works. A word-overlap or similarity heuristic does not: a repo-specific rule written in ordinary governance vocabulary scores as a reworded duplicate of a rule it has nothing to do with, so the cheap check is confidently wrong in exactly the direction that loses content. Take each candidate rule, pick the phrasing that is peculiar to it, and grep the hub's canonical for that. Absent means it is a local addition, and it is then either promoted here or moved to the repo's topical doc, never dropped because a heuristic called it redundant.

## Enforcement

`files.json` declares each section's fidelity. [validate.py][validate] proves every declared section resolves to a real level-two heading in the hub's own copy of the file that declares it, so a renamed or mistyped section cannot silently stop being checked. [audit.py][audit] checks each repo's copy (presence for `intent`, byte-match for `verbatim`) and classifies a mismatch as stale (re-vendor) or modified (review).

<!-- Internal -->

[agents]: ../AGENTS.md
[audit]: ./audit.py
[codestyle]: ../CODESTYLE.md
[fidelity-model]: ./fidelity-model.md
[files]: ./files.json
[governance]: ../GOVERNANCE.md
[operations]: ../OPERATIONS.md
[validate]: ./validate.py
