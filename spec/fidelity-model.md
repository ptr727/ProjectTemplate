# Fidelity Model

How faithfully each carried unit must survive the carry, and how that is verified. This is a hub-only doc governing the carrying machinery ([`spec/files.json`][files], [`spec/files.schema.json`][files-schema], [`spec/audit.py`][audit]) and is not carried to the fleet. It is the companion to [`spec/scope-model.md`][scope-model]: scope decides *which* repos get a unit, fidelity decides *how faithfully* they must carry it.

## The Fixed and the Overridable

Carried content is a class with virtual functions. The **fixed** part is the interface - when a thing is invoked, what it is named, and where it is wired. The **overridable** part is the implementation body, which a repo replaces to fit its own targets. Validation must allow the override while detecting a change to the interface or to content meant to stay fixed. Integrity is by **content hash, never a version number** - a version stamp is a claim a repo can keep while editing the body, so it is never trusted for detection.

## The Four Fidelity Levels

Each [`spec/files.json`][files] entry declares one `fidelity`, defaulting to `presence`.

- **presence** - the unit exists (a file, or a markdown section heading). The audit's baseline check.
- **intent** - carried faithfully but judged by meaning, not bytes. A downstream copy legitimately differs (a governed divergence or a paraphrase), and equivalence is a human call via `intentRef`. The audit asserts nothing beyond presence.
- **verbatim** - byte-identical to the hub's canonical after line-ending, action-pin, and job-needs normalization. The audit content-hashes the downstream copy against canonical. It applies to a whole file, a workflow job region (a job selected by key), or a markdown section region (a `## heading` block selected by name). The section granularity lets one file be **intent overall while a few of its sections are verbatim** - a universal rule block stays byte-identical fleet-wide even though the rest of the document is a repo-adapted paraphrase, so a stale section or a missing rule is caught while its heading still passes the presence check.
- **interface** - an overridable body that must honor a named contract. The audit checks the contract by name and wiring, never the body.

Fidelity is a declared field defaulting to `presence`, never inferred from `whole`/`placeholders`. `.editorconfig` and `.markdownlint-cli2.jsonc` are both whole with no placeholders yet sit at opposite fidelity, because the discriminator is governance, not field shape.

## Why Each Unit Sits Where It Does

- **verbatim** - `.markdownlint-cli2.jsonc` (fleet-generic, no governed divergence), the `github-release` job region of the release task (the canonical orchestration a repo must not fork), and the universal rule sections of `AGENTS.md` - fleet-law with no repo-specific content (no SHAs, no `ptr727/<repo>` references), where a paraphrase or a missing rule is a defect, not an adaptation. Which `AGENTS.md` sections are verbatim and which stay intent (the few that describe one repo) is defined in [section-model.md][section-model].
- **interface** - the release and PR workflows. Their fixed contract is the job and check names plus the artifact handoff, while the leaf build jobs are owned. See the override seam in [`AGENTS.md`][agents].
- **intent** - `.editorconfig` and `.gitattributes` (the `[*] end_of_line` default and path pins vary by platform), `cspell.json` (the words list and file scope vary), `CODESTYLE.md` / `WORKFLOW.md` / `AUDIT.md` / `.github/copilot-instructions.md` (carried docs judged by meaning), and the ruleset payloads (whose live state is diffed separately).
- **presence** - `README.md`, `HISTORY.md`, `.gitignore`, and the per-repo config that only needs to exist.

## The Workflow Override Seam Contract

The fixed interface of a workflow is stated in [`AGENTS.md`][agents] ("Orchestration vs. build - the override seam" and "Workflow YAML Conventions"), and the `interface` check enforces it by name and structure: the ruleset-bound required check `name: Check pull request workflow status job`, the `github-release` and `get-version` job keys, the `release-asset-<branch>-<target>` artifact-name handoff, and that `github-release` collects assets by `pattern:` / `merge-multiple:` and never by an `artifact-ids:` that names a build job's output. A repo owns the leaf `build-<target>-task` job list, its `needs` targets, and its paths-filter, and none of those are checked.

## Normalization

A verbatim check compares content by hash after **line-ending, action-pin, and job-needs normalization** - EOL variance is governed by the line-ending rules, a `uses: <action>@<sha>` pin (with its trailing `# vN` comment) is Dependabot-owned and bumped per repo, and a job's `needs:` list is pruned per repo to its vendored targets (an unvendored name fails the workflow to load), so all three are governed drift rather than a fidelity deviation. This keeps a verbatim workflow job region (the `github-release` job) from flagging on a routine action bump or a legitimate needs-prune while still catching a real structural fork. It does **not** mask placeholders: a verbatim unit carries none. The files that declare a `placeholders` list (for example `.github/copilot-instructions.md` with `<owner>`, `<repo>`, `<N>`) are fidelity `intent`, judged by hand and never hashed. Masking could not serve a hash anyway - a downstream copy holds the substituted value (`ptr727`), not the token (`<owner>`), so masking the token in the canonical alone would guarantee a mismatch. A verbatim unit that ever needed a per-repo substitution would require template-matching (the canonical as a pattern, the copy as an instance), not this content hash. None does today.

## Stale Versus Modified

A verbatim mismatch is one of two things, told apart **by hash, not by a version**. The audit hashes each past revision of the hub's canonical from its own git history. If the downstream copy matches a **past** canonical revision, the base advanced and the copy is **stale** - re-vendor it. If it matches **no** revision the base ever produced, the repo **modified fixed content** - review it. A version stamp could claim to be current while being neither, so it is demoted to a human-facing label and never consulted for integrity.

<!-- Repo -->
[agents]: ../AGENTS.md
[audit]: ./audit.py
[files]: ./files.json
[files-schema]: ./files.schema.json
[section-model]: ./section-model.md
[scope-model]: ./scope-model.md
