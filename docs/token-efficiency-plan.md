# Token Efficiency Plan

The rollout plan for [issue #766][issue-766], optimizing the fleet instruction set for effective
token cost and behavioral accuracy. The objectives come from the issue and its maintainer
comments. The gap analysis measures this repository at commit `5b8e008`, and the phases are
ordered so every reduction lands behind a measurement that can veto it. This doc is **hub-only**
and is not carried downstream, the same way [`docs/token-cost.md`][token-cost] is hub-only,
because it tracks the hub's own campaign rather than a fact a downstream repo needs.

The companion docs are [`docs/token-cost.md`][token-cost], which measured why recurring context
is the cost driver, and [`docs/fleet-map.md`][fleet-map], which holds the skill Gap Register and
Decision Ledger this plan routes boundary decisions through. The first structural reduction,
[PR #773][pr-773], landed before this doc and is recorded as Phase 0 below.

**Maintenance rule.** Check a box in the same pull request that completes its work. Update the
[Metrics](#metrics) log and any status line in that same pull request. Append a discovered
defect to the [Findings Register](#findings-register) rather than fixing it in the same change.
A phase whose boxes are checked without its metrics row is itself stale prose, so this doc is
only trustworthy while that rule holds.

## Table of Contents <!-- omit from toc -->

- [Objectives](#objectives)
- [Cost Model](#cost-model)
- [Gap Analysis](#gap-analysis)
- [Rollout Plan](#rollout-plan)
- [Phased Delivery](#phased-delivery)
- [Metrics](#metrics)
- [Findings Register](#findings-register)
- [Non-Goals and Invariants](#non-goals-and-invariants)

## Objectives

- **O1, lower recurring context.** Reduce the bytes a session re-reads on every request, not the
  bytes a repository stores. The optimization principle is `effective cost ~= size x load
  frequency`, per [issue #766][issue-766] and the session measurements in
  [`docs/token-cost.md`][token-cost].
- **O2, improve accuracy.** Routing precision, rule retention, verification quality, and
  escalation correctness are measured before and after each reduction. A reduction that saves
  tokens and loses behavior is a failure, not a partial success.
- **O3, progressive disclosure as the default shape.** A skill body carries the common execution
  path. Extended mechanics, examples, edge cases, and history live in `references/` and are read
  only when the subcase appears.
- **O4, empirical decisions for locked content.** The byte-locked `AGENTS.md` sections and the
  skill boundary map change only on measured downstream evidence, never on hub-file size alone.
- **O5, preserve the invariants.** Safety, authorization, verification, and merge gates keep
  their exact semantics, decision-point duplication stays, and deterministic `WHEN / DO / UNLESS`
  phrasing never softens into suggestion wording.
- **O6, agent-executable delivery.** Every work item is a checkbox sized for one task, with an
  execution protocol, so agents apply the changes and record the results in this doc.

## Cost Model

Bytes are the deterministic unit. A token estimate divides bytes by roughly four, and the exact
ratio is model-dependent, so this plan records bytes and treats tokens as derived. Five load
tiers, from [issue #766][issue-766] with the measured current sizes:

| Tier | Content | Current size | Loads when |
| --- | --- | ---: | --- |
| 1 | `AGENTS.md` | 17,508 B | Every session, every provider that injects it |
| 1 | `.github/copilot-instructions.md` | 53,188 B | Every Copilot session, whole file |
| 2 | 18 skill descriptions | 19,232 B | Every session, on providers that surface descriptions |
| 3 | 18 skill bodies | 142,377 B | When the skill activates |
| 4 | Skill `references/`, 6 skills | 64,322 B | When the subcase appears |
| 5 | `GOVERNANCE.md` 66,677 B, `WORKFLOW.md` 60,713 B, `CODESTYLE.md` 7,575 B | 134,965 B | Selectively, one section at a time |

Two facts shape the whole plan. First, tier 1 and tier 2 cost multiply with every session, so a
thousand bytes removed from them outweighs ten thousand removed from tier 4. Second, which files
actually sit in tier 1 or tier 2 differs by provider, per the second maintainer comment on
[issue #766][issue-766]. Copilot loads `AGENTS.md` plus the whole runbook file. The opencode
harness injects `AGENTS.md` and every skill description into the system prompt. Claude Code reads
skills through the generated plugin. Codex reads the `.agents/skills/` tree directly. Phase 4
measures the real per-provider composition before any tier 1 or tier 2 reduction is sized.

## Gap Analysis

Measured at commit `5b8e008` on `develop`, after [PR #773][pr-773]. Each gap names the phase that
closes it.

| # | Gap | Evidence | Phase |
| --- | --- | --- | --- |
| G1 | The Copilot Review Runbook embeds hand-run mechanics that duplicate the hub-hosted helper | The runbook section is 47,624 B of the 53,188 B file, 90%, while [`scripts/pr_review.py`][pr-review-script] already implements status, wait, reply, and resolve | [3](#phase-3-copilot-runbook-reduction) |
| G2 | Twelve of eighteen skills carry no `references/`, and four bodies remain over 11 KB | `comment-and-doc-style` 14,726 B, `operational-vs-release-workflow` 12,340 B, `git-commit-conventions` 11,869 B, `dotnet-codestyle` 11,562 B, with `pr-review-conduct` next at 9,801 B | [2](#phase-2-skill-body-progressive-disclosure) |
| G3 | Skill descriptions carry rationale, history, and mechanics instead of routing only | 19,232 B across 18 descriptions, averaging 1,068 B, with the longest at 1,375 B (`operational-vs-release-workflow`) | [5](#phase-5-skill-description-compression) |
| G4 | `Why This Exists` narratives repeat as runtime cost on every activation | Incident history in `agent-conduct`, `repo-worktree`, `comment-and-doc-style`, and the language skills, per finding 3 of [issue #766][issue-766] | [2](#phase-2-skill-body-progressive-disclosure) |
| G5 | No behavioral benchmark exists to veto a reduction | The evaluation set proposed in [issue #766][issue-766] is specified but not authored, and no routing record exists | [1](#phase-1-baseline-and-benchmark) |
| G6 | No repeatable per-layer measurement of the instruction set itself | The tables in this doc were hand-derived once, with no `scripts/` inventory tool to re-run per phase | [1](#phase-1-baseline-and-benchmark) |
| G7 | No downstream or per-provider context measurement | The second maintainer comment on [issue #766][issue-766] asks for exactly this table before any locked-content decision | [4](#phase-4-downstream-and-provider-measurement) |
| G8 | `AGENTS.md` is 89% byte-locked verbatim sections | 15,583 B of 17,508 B sits in the three verbatim sections named in [`spec/files.json`][files-json], so only the 1,925 B intro is freely editable | [6](#phase-6-agentsmd-reduction-decision) |
| G9 | Skill boundaries are unexamined against load cost | Split and fold candidates exist, listed under Phase 7, but no measurement supports either direction | [7](#phase-7-skill-boundary-review) |
| G10 | Propagation cost is unplanned for each artifact class | Skill edits reach machines through `skills_install.py`, carried-file edits reach repos through RESYNC re-vendor, and verbatim edits need a fleet sweep, three different channels | All |

## Rollout Plan

The execution protocol for every checkbox in this doc.

**One checkbox, one task, one pull request.** Isolate into a worktree on a feature branch before
the first edit, per the [`repo-worktree`][repo-worktree-skill] Skill. A multi-checkbox phase is a
sequence of tasks, not one task. A change touches one skill at a time, so a behavioral
regression attributes to one edit.

**Bookkeeping lands in the same change.** The pull request that completes a checkbox marks that
checkbox, appends its row to the [Metrics](#metrics) log, and updates any status line. A
discoverer of a hub defect files an issue first, adds a row to the
[Findings Register](#findings-register), and fixes it in a separate pull request. Label a new
issue `skills`, `agents`, or `docs` as fits, and link it back to the phase that surfaced it.

**Skill edits follow the [`skill-lifecycle`][skill-lifecycle-skill] Skill.** Edit only
`.agents/skills/`, and regenerate with `python3 scripts/build_dist.py` from a hub checkout.
Commit source plus generated tree in one commit, so the CI `--check` gate passes. Machines serve
the previous skill set until `scripts/skills_install.py` re-runs, so state that refresh in the
pull request.

**Carried-file edits propagate by fidelity class.** An intent-fidelity rewrite, like the Phase 3
runbook, drifts advisory until each repo's next resync, which is ordinary RESYNC work. A
verbatim rewrite, like a Phase 6 `AGENTS.md` change, fails audits fleet-wide until each repo
re-vendors, so it ships with its own per-repo sweep checklist.

**Behavior-affecting edits pass the benchmark.** Any edit that changes what a skill says at a
decision moment runs the relevant `docs/token-benchmark.md` cases before and after, and records
both runs there. A material regression blocks the change regardless of bytes saved.

**Verification is the gate, per [GOVERNANCE.md "Verification Discipline"][governance-verification].**
Markdown lints clean, `python3 scripts/prose_lint.py` passes, and
`python3 scripts/repo_gate.py --check eol` passes. Any added script carries its unittest under
`scripts/tests/`. Claim done only with the gate outputs in hand.

## Phased Delivery

The first step is Phase 1, the measuring sticks, paired with the already-proven reductions of
Phases 2 and 3 which need no measurement to start, only to accept. Phase 4 rides along normal
work in downstream repos rather than dedicated trips, per the second maintainer comment on
[issue #766][issue-766]. Phases 5, 6, and 7 are gated and start only on Phase 4 evidence.

### Phase 0: First Structural Reduction (complete)

[PR #773][pr-773] expanded `references/` in `python-codestyle` (17,312 to 8,696 B),
`dotnet-codestyle` (15,959 to 11,562 B), and `git-commit-conventions` (12,784 to 11,869 B), with
all rules moved rather than deleted. Recorded here so the later metrics rows have their starting
point.

### Phase 1: Baseline and Benchmark

Establishes G5 and G6, the two instruments every later phase reads.

- [ ] Add `scripts/token_inventory.py`: measures per-layer sizes, the `AGENTS.md` and
      `.github/copilot-instructions.md` section splits, and per-skill description, body, and
      reference bytes, emitting the tables this doc carries. Cover it with a unittest under
      `scripts/tests/` and document it in `scripts/README.md`. Verify it reproduces the baseline
      below.
- [ ] Author `docs/token-benchmark.md`: the structured prompt set from
      [issue #766][issue-766], positive and negative routing cases per skill, each recording
      prompt, expected skills, expected rules consulted, required action, forbidden action,
      required verification, and expected escalation. Seed it with at least the three example
      cases from the issue: `repo-worktree` on a trivial fix, `pr-review-conduct` on a green
      merge request, and `dotnet-codestyle` on a suppression request. Add two negative cases
      where sibling skills must stay inactive.
- [ ] Record the benchmark's baseline run, before any Phase 2 edit, in the benchmark doc, run by
      the maintainer or an agent over the seeded cases, repeated where nondeterminism shows.

Exit criteria: the inventory script runs clean from a hub checkout, the benchmark doc exists with
a recorded baseline, and this doc's Metrics table is reproducible by command rather than by hand.

### Phase 2: Skill Body Progressive Disclosure

Closes G2 and G4 by continuing the [PR #773][pr-773] pattern. The body keeps the common
execution path and the `WHEN / DO / UNLESS` gates. Rationale stays only where it changes how a
rule is interpreted, per finding 3 of [issue #766][issue-766]. Each item is one task: edit,
regenerate the dist tree, spot-check the benchmark cases that route to the skill, mark the box,
append the metrics row.

- [ ] `comment-and-doc-style` (14,726 B): keep the charset tiers, the semicolon and spaced-hyphen
      rules, the comment rules, and the PR-title rules in the body. Move the extended Markdown
      lint mechanics, the spelling-gate scoping, and the sentence-structure background into
      `references/`.
- [ ] `operational-vs-release-workflow` (12,340 B): keep the model-selection rule, the branching
      and promotion summary, and the two publish traps in the body. Move the complete operational
      delta and the release-model detail into a further `references/` file.
- [ ] `git-commit-conventions` (11,869 B): keep staging authorization, the signed-commit and
      identity checks, the force-push ban, and the destructive-command ban in the body. Move the
      shell-by-shell signing probe mechanics and the merge-conflict recipes into
      `references/`.
- [ ] `pr-review-conduct` (9,801 B): keep the merge gate, the expected loop, and the five
      outcomes in the body. Move the suppressed-finding answering detail and the escalation
      catalog into `references/`.
- [ ] `repo-worktree` (8,634 B): keep the mandate, the base-branch rule, the creation and cleanup
      commands in the body. Move the layout naming theory, forks, upstream joins, adopted repos,
      and collision behavior into `references/repository-layout.md`, per [issue #766][issue-766].
- [ ] `dotnet-codestyle` (11,562 B) and `python-codestyle` (8,758 B): second pass, one skill per
      change. Move the remaining extended examples and uncommon analyzer or profile cases into
      the existing `references/` trees.
- [ ] Narrative trim umbrella, one change per skill: `agent-conduct` (6,680 B), `skill-lifecycle`
      (8,105 B), `copilot-instructions-keeper` (6,140 B), `audit-a-repo` (5,907 B),
      `resync-a-repo` (5,151 B), `standup-a-repo` (6,150 B), `fleet-conformance-check`
      (4,367 B), `upstream-contribution-workflow` (5,672 B), `shell-codestyle` (3,584 B), and
      `workflow-ci-contract` (8,201 B). Shorten each `Why This Exists` to the rationale that
      changes interpretation, and move any mechanics a rare path needs into `references/`. Check
      this box only when every named skill is done in its own change.

Exit criteria: no skill body over 10 KB remains without either a `references/` split or a
recorded reason its content must stay whole, every moved rule is reachable from its body, and
the benchmark shows no routing or behavior regression.

### Phase 3: Copilot Runbook Reduction

Closes G1, the single largest tier 1 block in the fleet. The runbook is carried at intent
fidelity to every repo and loaded whole by every Copilot session, so each byte removed is
removed twenty-two times over.

The split: recognition and decision material stays, fallback mechanics moves. The carried file
keeps the review-loop contract summary, the helper-first rule, the recognition shapes, the
coverage reading, the reviewer-login tables, and the escalation triggers. These are what an
agent must recognize at runtime. The vetted shapes also stay in step with
[`scripts/pr_review.py`][pr-review-script], because a case reads them out of this file. The
hand-run cross-owner GraphQL fallback, the embedded query blocks, and the mutation walkthroughs
move to a hub-only doc, `docs/copilot-review-mechanics.md`. One paragraph in the runbook then
names when the fallback applies and where it lives. The
[`copilot-instructions-keeper`][keeper-skill] Skill governs the edit, and every repo-local
Disproved Claims ledger survives untouched.

- [ ] Author `docs/copilot-review-mechanics.md` carrying the moved mechanics, whole and
      unchanged.
- [ ] Rewrite the runbook section to the split above, keeping every recognition shape and
      decision gate byte-for-byte where feasible, target under 12 KB for the whole
      `.github/copilot-instructions.md`.
- [ ] Run the PR-review benchmark cases before and after, and record both in the benchmark doc.
      A Copilot agent's review loop on a cross-owner target is the case most at risk, so cover
      it explicitly.
- [ ] Sweep the prose that asserts the old runbook shape, including sibling docs and skills that
      quote the runbook length or structure.

Exit criteria: the carried file carries no embedded query that the hub-hosted helper already
implements, the keeper confirms ledger preservation, and the benchmark shows the review loop
still executes.

### Phase 4: Downstream and Provider Measurement

Closes G7, per the second maintainer comment on [issue #766][issue-766]. Measure during normal
work already happening in downstream repos, not on dedicated trips. For each representative
shape, capture the context composition: effective `AGENTS.md` content, repo-specific additions,
provider always-on instructions, skill discovery metadata, activated skill bodies, references
actually loaded, and the total attributable to fleet instructions.

- [ ] .NET shape: `PhotoCleaner` or `PlexCleaner`
- [ ] Python shape: `aiopurpleair` or `Financial-Modeling`
- [ ] Mixed-language shape: `PlexCleaner`
- [ ] Operational shape: `HomeAssistant-Config`
- [ ] Provider loading semantics, opencode: confirm `AGENTS.md` injection and whether all 18
      descriptions sit in the system prompt, and whether activated bodies load whole
- [ ] Provider loading semantics, Claude Code: how the generated plugin surfaces descriptions,
      and which instruction files auto-load
- [ ] Provider loading semantics, Codex: which instruction files and skill metadata auto-load
- [ ] Provider loading semantics, Copilot: confirm the whole runbook file loads per session and
      that no skill metadata participates
- [ ] Record the matrix and the findings in this doc's Metrics section, and state which tiers
      are recurring context per provider

Exit criteria: the matrix names, per provider, which layers are paid every session, and the
Phase 5 and Phase 6 gates have their input.

### Phase 5: Skill Description Compression

Closes G3. Gated: starts only after Phase 4 confirms description loading semantics and Phase 1's
benchmark can score routing. A description answers what the skill owns, when it activates, and
which adjacent skill must not activate, per finding 2 of [issue #766][issue-766]. Rationale,
history, and mechanics live in the body. The target is 50-75% off where routing holds, and the
benchmark's negative cases decide, run before and after each change.

- [ ] `operational-vs-release-workflow` (1,375 B)
- [ ] `pr-review-conduct` (1,324 B)
- [ ] `comment-and-doc-style` (1,271 B)
- [ ] `dotnet-codestyle` (1,252 B)
- [ ] `workflow-ci-contract` (1,139 B)
- [ ] `repo-worktree` (1,121 B)
- [ ] `git-commit-conventions` (1,105 B)
- [ ] `upstream-contribution-workflow` (1,099 B)
- [ ] `agent-conduct` (1,071 B)
- [ ] `fleet-conformance-check` (1,039 B)
- [ ] `shell-codestyle` (1,010 B)
- [ ] `python-codestyle` (999 B)
- [ ] `copilot-instructions-keeper` (984 B)
- [ ] `skill-lifecycle` (983 B)
- [ ] `audit-a-repo` (970 B)
- [ ] `standup-a-repo` (916 B)
- [ ] `resync-a-repo` (798 B)
- [ ] `carried-instruction-file-guard` (776 B)

Exit criteria: discovery metadata shrank materially where the provider matrix says it is
recurring, every description keeps its negative boundaries, and the benchmark routing record
shows no missed or false activation introduced.

### Phase 6: AGENTS.md Reduction Decision

Closes G8. Gated on Phase 4 data. The question is not whether the 17,508 B file can shrink. The
question is how many recurring tokens per downstream task a cut removes, and whether that saving
justifies a fleet-wide verbatim sweep, per the second maintainer comment on [issue #766][issue-766].

- [ ] Write the decision record from the Phase 4 matrix: projected per-task token saving of a
      40-60% cut against the sweep cost, appended to Metrics.
- [ ] On approval, rewrite the three verbatim sections in one hub pull request. State the
      bootstrap once, without the diagram-plus-prose duplication. State the discipline rules as
      rules. Slim the `Where the Rules Live` map, which Phase 5 already shortened the rows of.
      Update `spec/section-model.md` and sweep every cross-reference. The target from
      [issue #766][issue-766] is 40-60% overall, subject to the benchmark.
- [ ] Run the full benchmark before and after the rewrite, and record it.
- [ ] Execute the fleet re-vendor sweep, one resync per repo, tracked as a per-repo checklist in
      this phase in the style of [`docs/eol-lf-rollout.md`][eol-lf-rollout], added when the
      rewrite merges.
- [ ] On decline, record the decision and the measurement behind it, and close the phase.

Exit criteria: either the reduction landed with equivalent benchmark behavior and a completed
sweep, or a recorded decline with numbers.

### Phase 7: Skill Boundary Review

Closes G9. Gated on Phases 2 and 5, so decisions read post-compression sizes rather than today's.
Decisions route through the [`docs/fleet-map.md`][fleet-map] Gap Register and Decision Ledger,
and execution follows the `skill-lifecycle` Skill, whose retirement sweep deletes map rows and
sibling disambiguations in the same change.

- [ ] Re-measure and identify any skill still over 10 KB of body or activating on a surface its
      content cannot justify.
- [ ] Split decision, `comment-and-doc-style`: split only if it remains too expensive after
      Phase 2 and Phase 5, per finding 7 of [issue #766][issue-766]. Additional skills add
      routing metadata and overlap, so the default is no split.
- [ ] Fold decision, `carried-instruction-file-guard` and `copilot-instructions-keeper`: both
      guard carried files at resync moments with overlapping triggers. Decide one merged
      carried-file guard with the ledger rules as a `references/` file, or two skills with
      sharpened boundaries, on the measured activation overlap.
- [ ] Confirm the deliberate non-folds: `standup-a-repo`, `resync-a-repo`, `audit-a-repo`, and
      `fleet-conformance-check` stay separate because each names which session it is in, and
      `agent-conduct` stays the moment-based umbrella beside the task-based specialists.

Exit criteria: every boundary decision is recorded in the fleet-map Decision Ledger with its
measured basis, and executed or declined accordingly.

## Metrics

### Baseline at 5b8e008

| Artifact | Bytes | Load class |
| --- | ---: | --- |
| `AGENTS.md`, intro (intent) | 1,925 | Tier 1 |
| `AGENTS.md`, Fleet Bootstrap (verbatim) | 3,444 | Tier 1 |
| `AGENTS.md`, Context and Delegation Discipline (verbatim) | 5,597 | Tier 1 |
| `AGENTS.md`, Where the Rules Live (verbatim) | 6,542 | Tier 1 |
| `.github/copilot-instructions.md`, header and short sections | 5,564 | Tier 1, Copilot |
| `.github/copilot-instructions.md`, GitHub Copilot Review Runbook | 47,624 | Tier 1, Copilot |
| All 18 skill descriptions | 19,232 | Tier 2 |
| All 18 skill bodies | 142,377 | Tier 3 |
| All skill `references/`, 6 skills, 14 files | 64,322 | Tier 4 |

Bytes are `wc -c` on the working tree at commit `5b8e008`. The description bytes are the
normalized frontmatter text. Verify any figure against `scripts/token_inventory.py` once Phase 1
lands it, and prefer the script over this table on any disagreement.

### Re-measure Log

Append one row per completed phase or task batch, from the inventory script.

| Date | Commit | Phase | Tier 1 B | Tier 2 B | Tier 3 B | Tier 4 B | Notes |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| 2026-08-16 | `5b8e008` | 0 | 70,696 | 19,232 | 142,377 | 64,322 | Post-PR 773 baseline |

## Findings Register

Defects and surprises discovered during this campaign, one row per filed issue. Append freely,
never edit a closed row.

| Date | Phase | Finding | Issue | Disposition |
| --- | --- | --- | --- | --- |
|  |  |  |  |  |

## Non-Goals and Invariants

From [issue #766][issue-766], binding every phase.

- No rule is removed because a capable model would probably know it, when the rule encodes a
  fleet-specific decision.
- No safety, authorization, verification, or merge gate is weakened, and no deterministic
  instruction becomes model judgment where the fleet specifies behavior.
- Intentional decision-point duplication stays: the imperative repeats at the moment of
  violation, the complete policy exposition does not.
- `WHEN / DO / UNLESS` phrasing never softens into `consider`, `usually`, or `when appropriate`,
  unless the existing policy intends discretion.
- Descriptions never become generic enough to route unreliably, and negative boundaries stay.
- Canonical policy docs optimize for correctness and maintainability, not byte count, since they
  are read one section at a time.
- Behavioral evaluation is a human-reviewed benchmark over a structured prompt set, run before
  and after, never a CI-gated deterministic suite, and repeated runs are expected where routing
  is nondeterministic.

<!-- Internal -->

[eol-lf-rollout]: ./eol-lf-rollout.md
[files-json]: ../spec/files.json
[fleet-map]: ./fleet-map.md
[governance-verification]: ../GOVERNANCE.md#verification-discipline
[keeper-skill]: ../.agents/skills/copilot-instructions-keeper/SKILL.md
[pr-review-script]: ../scripts/pr_review.py
[repo-worktree-skill]: ../.agents/skills/repo-worktree/SKILL.md
[skill-lifecycle-skill]: ../.agents/skills/skill-lifecycle/SKILL.md
[token-cost]: ./token-cost.md

<!-- Fleet -->

[issue-766]: https://github.com/ptr727/ProjectTemplate/issues/766
[pr-773]: https://github.com/ptr727/ProjectTemplate/pull/773
