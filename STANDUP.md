# STANDUP.md

How an agent takes a repository from nothing (or a partial state) to **operational** against the fleet ground truth. This is the create-to-conformance procedure, and [`AUDIT.md`][audit] is its read-only verifier and owns the definition of done. Both read the same ground truth ([`registry/repos.json`][repos], the [`spec/`][spec] manifests, [`repo-config/`][repo-config], and the prose authorities [`GOVERNANCE.md`][governance], [`CODESTYLE.md`][codestyle] and [`WORKFLOW.md`][workflow]), so a repo stood up by this file passes the audit by construction.

Standing up a repo is **applying the manifests until the audit passes**, nothing more invented. If a repo needs a construct no manifest covers, that is a spec gap: raise it ([`AUDIT.md`][audit] section 9), never improvise a per-repo answer. This is the downward-audit model (standard-style repos the hub audits against their declared type), which the fleet uses because managing downstream divergence is too costly.

## 1. Classify and Catalog

Resolve the repo's type(s) with the [`AUDIT.md`][audit] section 2 detection rules, then write or repair its [`registry/repos.json`][repos] entry: `status`, `types[]`, `groundTruthBranch`, `hasDevelop`, `publish[]`, `requiredSecrets[]`, `consumerModel`, `releaseTrigger`, `workflowModel` (omit to take the `release` default), `configLayout`, and `driftNotes` that describe what the repo **actually is**. Run [`spec/validate.py`][validate] to confirm it classifies cleanly. The registry is ground truth about reality, not intent, and a `validate.py`-clean entry is still false if it disagrees with the live repo.

## 2. Carry the Baseline Files

Copy every [`spec/files.json`][files] entry that applies to the repo's types (the `appliesTo: "*"` baseline plus the per-type additions), **adapted, not cloned**. The prose files (`CODESTYLE.md`, `README.md`, and the like) describe the repo's own toolchain, so adapt them to reality rather than propagating template specifics verbatim (see the "Adapt before propagating" callout in [`CODESTYLE.md`][codestyle], since a verbatim copy that misdescribes the repo is rejected in review). The baseline covers `WORKFLOW.md`, `version.json`, the two rulesets, `.github/dependabot.yml`, `.editorconfig`, `.gitattributes`, the linter configs, and the per-type files (`.vscode/tasks.json` from the language's snippet, `codecov.yml`, `.dockerignore`, `Docker/README.md`). **Every repo carries `repo-config/main.json`**, and only the `develop` payload varies by workflow model: `repo-config/develop.json` for a release repo, `repo-config/operational/develop.json` for an operational one.

**Repo-specific content has a declared destination, not a judgment call.** The baseline is what a repo *carries*. Anything the repo knows that the fleet does not needs somewhere to live, and improvising a location per repo is what the destinations in [`spec/section-model.md`][section-model] exist to prevent. Three topical docs take it, chosen by what the content **is**:

- [`CODESTYLE.md`][codestyle]: the repo's language and formatting conventions beyond the carried rules.
- `ARCHITECTURE.md`: how a code repo is built, its module layout, data flow, and design decisions.
- `OPERATIONS.md`: how an operational repo is run, covering runbooks, backup, log and debug procedures, tool-usage notes, and config layout.

`OPERATIONS.md` is the operational-repo analogue of `ARCHITECTURE.md`, and it is where an `AGENTS.md` split puts the repo-specific half, so an `operational` repo with real runbooks (a deploy procedure, a rollback, a retention policy, a credential rotation) writes them there rather than into a carried file. It is agent-instruction content, so it takes the inline-link exception the markdown rules name rather than the reference-style default.

Choose the destination while scaffolding rather than after. Repo-specific content left in a carried file is drift, which the audit lists as an undeclared section to reconcile, and reconciling it later means moving prose that downstream readers have already started trusting in the wrong place.

## 3. Stand Up the Workflows

Implement the Actions that satisfy [`WORKFLOW.md`][workflow] for the repo's type (its section 6 per-type walkthrough): the source-only subset for a source-only repo, the file-target leaf(s) for a publishing repo, the two-workflow shape for an operational config repo. Reuse [`catalog/snippets/workflows/`][workflows] as the reference implementation, satisfying the contract by outcome rather than byte for byte.

## 4. Apply Settings, Rulesets, and Secrets

Run `repo-config/configure.sh apply [owner/repo] [release|operational]` (the repo defaults to the current one, the model to the registry lookup or, absent a registry, to the carried payload) to apply the fleet settings, the Dependabot security features, and the two rulesets idempotently (import the JSON, never hand-build it, per [`docs/repo-config-carry.md`][repo-config-carry]), then `repo-config/configure.sh check [owner/repo] [release|operational]` to validate the repo and exit non-zero on any drift. Configure every required secret per [`spec/secrets.json`][secrets] (the registry `requiredSecrets[]` list plus the implicit baseline) in the right store(s), meaning Actions plus Dependabot where the mechanism needs it, and confirm no forbidden secret is present. The required check binds by name (`Check pull request workflow status job`) and turns green only after the PR workflow has run once.

## 5. Verify: Run the Audit

Run [`AUDIT.md`][audit] end to end. The repo is stood up only when it is **operational** (every applicable check passes) or its residual deltas are tracked in `reports/<repo>/audit.md` plus an issue. Converge any drift through a Copilot-reviewed target PR ([`AUDIT.md`][audit] section 10), and the maintainer merges. A repo left partially set up and unrecorded is the exact failure this procedure exists to prevent.

## Onboarding a New Repo Type

When a repo matches no existing type, the work is onboarding a **type**, not just a repo:

1. Add the type to [`spec/project-types.json`][project-types] (`detect[]`, plus `checks` with verdict tiers and intent refs) and any per-type files to [`spec/files.json`][files], then add its publish mechanism to [`spec/secrets.json`][secrets] if new.
2. Add the reference workflow leaf to [`catalog/snippets/workflows/`][workflows] and document the type's [`WORKFLOW.md`][workflow] walkthrough.
3. Add the type to the [conformance matrix][matrix] and run the cold-start self-test until a context-free agent stands it up to operational.

## Self-Test: Cold-Start Conformance

The onboarding docs are sufficient only if a **context-free agent stands up each supported repo shape from them alone**, a shape being the project type(s) plus the workflow model (`operational` is a `workflowModel` overlay, not a `spec/project-types.json` type). Run this whenever the onboarding docs or manifests change, and periodically as a fleet health check:

- For each shape in the [conformance matrix][matrix], task a fresh agent (no prior context) with "Using only this repo's docs, stand up a `<shape>` repo," pointing it at this file.
- Run [`AUDIT.md`][audit] against the result. Record pass or fail, and the first doc gap that tripped the agent, in the [conformance matrix][matrix].
- Iterate the **docs and tooling** (not the agent's memory) until every supported shape stands up cold to operational. A shape that cannot be stood up cold is a documentation defect, tracked like any other.

The same [`AUDIT.md`][audit] run is the on-demand audit for any known repo, and its report lists deviations and repo-specific deltas. The self-test and the fleet audit are one procedure, pointed at a new repo or an existing one.

<!-- Workflow -->

[workflows]: ./catalog/snippets/workflows/

<!-- Repo -->

[audit]: ./AUDIT.md
[codestyle]: ./CODESTYLE.md
[files]: ./spec/files.json
[governance]: ./GOVERNANCE.md
[matrix]: ./reports/conformance-matrix.md
[project-types]: ./spec/project-types.json
[repo-config]: ./repo-config/
[repo-config-carry]: ./docs/repo-config-carry.md
[repos]: ./registry/repos.json
[secrets]: ./spec/secrets.json
[section-model]: ./spec/section-model.md
[spec]: ./spec/
[validate]: ./spec/validate.py
[workflow]: ./WORKFLOW.md
