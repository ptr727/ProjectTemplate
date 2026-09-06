---
name: workflow-ci-contract
description: >-
  Governs the WORKFLOW.md CI/CD behavioral contract for every ptr727/ProjectTemplate fleet repo: the D1-D9 guarantees, the output seam by destination (a file on the GitHub release, a package-registry push, an image-registry push, or a filesystem on a host the project owns), the artifact lifecycle, NBGV versioning and classification, validate-at-entry, and the 5A/5B/5C test methodology. Use this whenever writing or editing anything under .github/workflows/ or a composite action under .github/actions/, editing version.json, adding or dropping a release target, auditing a repo's workflows, or tracing which job, input, or condition made a publish run or skip. This is the YAML half of the pipeline, and the operational-vs-release-workflow skill keeps the git half (branching, promotion, publish policy), so which branch a change targets, and which events the fleet allows to publish at all, go there while the job graph implementing that policy is here. Triggers even when the edit looks mechanical, such as bumping an action, renaming a job, or adding one upload step, because SHA pinning, the ruleset-bound aggregator name, smoke gating on uploads, and retention-days are each easy to break in a one-line diff that no build fails on: an upload a smoke run should have skipped succeeds instead of erroring, retention-days sits on an upload step no smoke run reaches, the aggregator's name is bound by a branch ruleset no build reads, and a PR changing only .github/workflows/ is deliberately not smoke-built. WORKFLOW.md keeps authority, and GOVERNANCE.md's Workflow YAML Conventions and Release Model sections win where those two overlap.
---

# Workflow CI Contract

## Why This Exists

`WORKFLOW.md` is the fleet's CI/CD behavioral contract. This skill is that contract's surface, so an agent editing workflow YAML has the contract in view. It carries the summary, and `WORKFLOW.md` sections 3, 4, and 5 are each carried whole in `references/` as a generated include. `WORKFLOW.md`'s own canonical-scope note says which of it and `GOVERNANCE.md` is authoritative where the two overlap.

## How the Contract Is Read

- **Outcomes, not bytes.** A workflow is judged against `WORKFLOW.md` section 4's expected inputs and outputs, never against a snippet byte for byte, per `GOVERNANCE.md` "Foundational Principles".
- **Applicability.** A guarantee, or a 5A check or 5B scenario from `WORKFLOW.md` section 5, governing a construct the repo does not contain is N/A: recorded, excluded from the verdict, never a defect. A source-only pipeline is mostly N/A and that is fine.
- **Operational is binary.** Every applicable guarantee holds, or the workflow is not operational. A single applicable input-output mismatch is a defect regardless of how clean the YAML looks.
- **Reached, not carried.** A standard workflow whose job graph is identical across repos of a type is reached as a hub-hosted `workflow_call` task, per `GOVERNANCE.md` "Hub-Hosted Tooling". The repo's own surface is the caller stub, pinned to a hub release commit, and a composite-action hook at `.github/actions/<hook>` for what is its own. A hub task reaches its own actions and sibling tasks through `$/`, which resolves at that pinned commit. The merge-bot is the first, and `docs/reusable-workflows.md` in the hub carries the model, the hook contract, and the stage each workflow migrates in. Until a workflow's stage ships, its copy is graded against the same contract.
- **Two layers.** Orchestration (the PR entry workflow, publisher, version and release jobs) is generic and standard at the job level. Build leaves (the `build-<target>` tasks) are repo-owned in shape. Inputs like `github`/`dockerhub`/`expect_release_assets` live on the orchestrator, a leaf receives `ref`/`branch`/`smoke` and whatever else its target needs, a derived `push` among them where that leaf pushes, so assert each input in the layer that declares it. A package target declares no push input on either layer, its push living in a separate `publish-<target>` job in the repo's own publisher. What a repo curates is the list of targets, and D6.4 names every surface adding or dropping one edits.

## Style Rules

`GOVERNANCE.md` "Workflow YAML Conventions" keeps the style rules, and the `comment-and-doc-style` Skill keeps the line-ending policy, reached from `GOVERNANCE.md` "Documentation Style Conventions" under "Line Endings". Read both before editing a workflow or a composite action.

## The Contract Text

`references/architecture.md`, `references/d-guarantees.md`, and `references/test-methodology.md` carry `WORKFLOW.md` sections 3, 4, and 5 whole, each as a generated include, so the pipeline's architecture, a guarantee's exact wording, and the audit-trace-probe procedure are each one read away rather than restated in full here. A defect in an include region is fixed in `WORKFLOW.md` and regenerated, never edited in this skill, per the `skill-lifecycle` Skill. `WORKFLOW.md` keeps sections 1, 2, and 6 itself, the applicability rule, the style-rule pointer, and the per-project-type walkthroughs that say which items go N/A per type, so read those there.

## After Any Workflow Edit

A workflow-only change is not smoke-built, and actionlint still runs on it in CI. `GOVERNANCE.md` "Verification Discipline" requires the repository's whole lint gate before every push, rather than actionlint alone. A workflow change is still only fully exercised by CI, per the same "Verification Discipline" section.
