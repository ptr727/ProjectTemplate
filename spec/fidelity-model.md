# Fidelity Model

How faithfully each carried unit must survive the carry, and how that is verified. This is a hub-only doc governing the carrying machinery ([`spec/files.json`][files], [`spec/files.schema.json`][files-schema], [`spec/audit.py`][audit]) and is not carried to the fleet. It is the companion to [`spec/scope-model.md`][scope-model]: scope decides *which* repos get a unit, fidelity decides *how faithfully* they must carry it.

## The Fixed and the Overridable

Carried content is a class with virtual functions. The **fixed** part is the interface: when a thing is invoked, what it is named, and where it is wired. The **overridable** part is the implementation body, which a repo replaces to fit its own targets. Validation must allow the override while detecting a change to the interface or to content meant to stay fixed. Integrity is by **content hash, never a version number**. A version stamp is a claim a repo can keep while editing the body, so it is never trusted for detection.

## The Fidelity Levels

Each [`spec/files.json`][files] entry declares one `fidelity`, defaulting to `presence`.

- **presence** - the unit exists (a file, or a Markdown section heading). The audit's baseline check.
- **intent** - carried faithfully but judged by meaning, not bytes. A downstream copy legitimately differs (a governed divergence or a paraphrase), and equivalence is a human call via `intentRef`. The audit asserts presence, plus a last-modified staleness advisory at drift: a hub canonical changing after the copy's own last commit marks the copy as possibly trailing, a hint rather than proof, and content is never judged. That "changing" walks past a revision whose diff is normalized-only (the same line-ending, action-pin, and job-needs normalization as verbatim below), so a Dependabot pin bump on a workflow file does not by itself mark every carrier as trailing (ptr727/ProjectTemplate#735).
- **verbatim** - byte-identical to the hub's canonical after line-ending, action-pin, and job-needs normalization. The audit content-hashes the downstream copy against canonical. It applies to a whole file, a workflow job region (a job selected by key), or a Markdown section region (a `## heading` block selected by name). The section granularity lets one file be **intent overall while a few of its sections are verbatim**. A universal rule block stays byte-identical fleet-wide even though the rest of the document is a repo-adapted paraphrase, so a stale section or a missing rule is caught while its heading still passes the presence check.
- **interface** - an overridable body that must honor a named contract. The audit checks the contract by name and wiring, never the body.

A declaration in `trees[]` uses **verbatim-tree**. Each included source file exists at the corresponding target path with identical bytes. A pruned target contains no extra path. The tree root exists even when the included source inventory is empty. Tree target roots never overlap because two declarations cannot own the same path safely, regardless of whether either declaration prunes extras.

Fidelity is a declared field defaulting to `presence`, never inferred from `whole`/`placeholders`. `.editorconfig` and `.markdownlint-cli2.jsonc` are both whole with no placeholders yet sit at opposite fidelity, because the discriminator is governance, not field shape.

## Why Each Unit Sits Where It Does

- **verbatim** - `.markdownlint-cli2.jsonc` (fleet-generic, no governed divergence), the `github-release` job region of the release task (the canonical orchestration a repo must not fork), and the universal rule sections of `AGENTS.md`, which are fleet-law with no repo-specific content (no SHAs, no `ptr727/<repo>` references) and where a paraphrase or a missing rule is a defect, not an adaptation. Which `AGENTS.md` sections are verbatim and which stay intent (the few that describe one repo) is defined in [section-model.md][section-model].
- **interface** - the release, PR, and bot workflows. Their fixed contract is the job and check names plus the artifact handoff, or for a caller stub the hub task it reaches and the secrets it maps, while the leaf build jobs are owned. See [`WORKFLOW.md`][workflow] "Two Layers: Orchestration vs Build" and [`docs/reusable-workflows.md`][reusable-workflows].
- **intent** - `.editorconfig` and `.gitattributes` (the `[*] end_of_line` default and path pins vary by platform), `cspell.json` (the words list and file scope vary), `CODESTYLE.md` / `WORKFLOW.md` / `AUDIT.md` / `.github/copilot-instructions.md` (carried docs judged by meaning), and the ruleset payloads (whose live state is diffed separately).
- **presence** - `README.md`, `HISTORY.md`, `.gitignore`, and the per-repo config that only needs to exist.

## The Workflow Override Seam Contract

The fixed interface of a workflow is stated in [`WORKFLOW.md`][workflow] ("Two Layers: Orchestration vs Build" and "The Seam Contract") and [`GOVERNANCE.md`][governance] ("Workflow YAML Conventions"), and the `interface` check enforces it by name and structure: the ruleset-bound required check `name: Check pull request workflow status job`, the `github-release` and `get-version` job keys, the `release-asset-<branch>-<target>` artifact-name handoff, and that `github-release` collects assets by `pattern:` / `merge-multiple:` and never by an `artifact-ids:` that names a build job's output. A repo owns the leaf `build-<target>-task` job list, its `needs` targets, and its paths-filter, and none of those are checked.

Once a workflow's job graph moves into the hub, the carried unit is a caller stub rather than the job graph itself, and the contract moves with it: `publish-release.yml`'s `interface` check asserts the `plan`, `validate`, and `publish` job keys and that `plan` and `publish` each name their hub task by token (`publish-plan-task.yml`, `build-release-task.yml`), never the build job list a carried `build-release-task.yml` used to expose.

## What a Local Edit Looks Like

A downstream repo editing carried content is a different question from a copy trailing the hub, and the answer differs by tier. [`GOVERNANCE.md`][governance] "Durable Knowledge and Self-Improvement" sends a change to carried rule text upstream rather than into the local copy. What follows is why a local patch there is so easy to miss.

The unit decides, never the file. `GOVERNANCE.md` is `intent` as a file while 18 of its 20 declared sections are `verbatim`. An edit inside one of those is caught, and an edit to the prose around them is not.

- **verbatim and verbatim-tree** - detected. The content hash is compared against the canonical and every past revision, and "Stale Versus Modified" below reads the result. An edit matching no revision reports as modified fixed content, and one restoring an earlier revision reports as stale, since the hash cannot say how the bytes got there. A `verbatim-tree` declaration also fails structurally, on a missing included path or an extra one under a pruned root, before any hash is read.
- **interface** - detected only where the edit moves the contract. A changed job key, check name, or hub task token is a finding. An edit to the owned body is the override this tier exists for.
- **intent** - not detected. Content is never judged, and the staleness advisory never identifies a local edit. It compares last-change dates, so the edit first suppresses the advisory by making the copy newer. After the hub's next substantive change to that file, meaning one the line-ending, action-pin, and job-needs normalization does not explain away, the advisory fires and reports the copy as trailing, which routes to a re-vendor that overwrites the edit rather than to the edit itself.
- **presence** - a content edit is not detected. The unit is asserted to exist and is never read, so deleting it is still a finding and editing it is not.

So a local rule edit to `CODESTYLE.md` or `WORKFLOW.md`, both `intent` and whole, is reported nowhere. That silence is intended for a repo's own additions, which `CODESTYLE.md` is a declared destination for, and it is the hazard for a patch of carried rule text, since the two read alike to every check. Two checks reach some non-verbatim prose, and neither reaches those two files. [`section-model.md`][section-model]'s undeclared-section advisory names a heading the hub does not declare, and it runs only against `AGENTS.md`, `GOVERNANCE.md` and `.github/copilot-instructions.md`. It reads headings rather than rule text, so a rule rewritten under a declared heading stays invisible. [`audit.py`][audit]'s `template_ref_outside_verbatim` reads the prose itself, and it catches only a coordination reference rather than a changed rule.

## Normalization

A verbatim check compares content by hash after **line-ending, action-pin, and job-needs normalization**. EOL variance is governed by the line-ending rules, a `uses: <action>@<sha>` pin (with its trailing `# vN` comment) is Dependabot-owned and bumped per repo, and a job's `needs:` list is pruned per repo to its vendored targets (an unvendored name fails the workflow to load), so all three are governed drift rather than a fidelity deviation. This keeps a verbatim workflow job region (the `github-release` job) from flagging on a routine action bump or a legitimate needs-prune while still catching a real structural fork. It does **not** mask placeholders: a verbatim unit carries none. The files that declare a `placeholders` list (for example `.github/copilot-instructions.md` with `<owner>`, `<repo>`, `<N>`) are fidelity `intent`, judged by hand and never hashed. Masking could not serve a hash anyway. A downstream copy holds the substituted value (`ptr727`), not the token (`<owner>`), so masking the token in the canonical alone would guarantee a mismatch. A verbatim unit that ever needed a per-repo substitution would require template-matching (the canonical as a pattern, the copy as an instance), not this content hash. None does today.

`verbatim-tree` applies no normalization. It compares file bytes because a fully owned generated tree has no per-repository substitutions or Dependabot-owned regions.

## Stale Versus Modified

A verbatim mismatch is one of two things, told apart **by hash, not by a version**. The audit hashes each past revision of the hub's canonical from its own git history. If the downstream copy matches a **past** canonical revision, the base advanced and the copy is **stale**, so re-vendor it. If it matches **no** revision the base ever produced, the repo **modified fixed content**, so review it. A version stamp could claim to be current while being neither, so it is demoted to a human-facing label and never consulted for integrity.

<!-- Repo -->
[audit]: ./audit.py
[files]: ./files.json
[files-schema]: ./files.schema.json
[governance]: ../GOVERNANCE.md
[reusable-workflows]: ../docs/reusable-workflows.md
[scope-model]: ./scope-model.md
[section-model]: ./section-model.md
[workflow]: ../WORKFLOW.md
