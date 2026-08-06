# STANDUP.md

How an agent takes a repository from nothing (or a partial state) to **operational** against the fleet ground truth. This is the create-to-conformance procedure, and [`AUDIT.md`][audit] is its read-only verifier and owns the definition of done. Both read the same ground truth ([`registry/repos.json`][repos], the [`spec/`][spec] manifests, [`repo-config/`][repo-config], and the prose authorities [`GOVERNANCE.md`][governance], [`CODESTYLE.md`][codestyle] and [`WORKFLOW.md`][workflow]), so a repo stood up by this file passes the audit by construction.

Standing up a repo is **applying the manifests until the audit passes**, nothing more invented. If a repo needs a construct no manifest covers, that is a spec gap: raise it ([`AUDIT.md`][audit] section 9), never improvise a per-repo answer. This is the downward-audit model (standard-style repos the hub audits against their declared type), which the fleet uses because managing downstream divergence is too costly.

## 0. Verify Commit Identity and Signing, Before the First Commit

Do this before `git init` or any commit, because the window closes at the first one. A repo whose initial history is unsigned or committed under the wrong identity cannot be cleanly repaired: `Require signed commits` blocks the first `develop -> main` release, re-signing that history is a non-fast-forward the `Block force pushes` rule rejects, and completing it needs the ruleset temporarily disabled plus a maintainer force-push that [`docs/repo-config-carry.md`][repo-config-carry] forbids an agent to perform. Greenfield repos where signing is live before the first commit never hit this.

**Verify the inherited configuration. Never set it.** The host already carries the correct identity, so a repo-local `user.email` is redundant at best and a wrong identity at worst, and it silently shadows the global it overrides. Read the **`--global`** scope explicitly, and run these before there is a repo:

```shell
git config --global --get user.email        # the GitHub noreply address, per GOVERNANCE.md "Git and Commit Rules"
git config --global --get commit.gpgsign    # true
git config --global --get user.signingkey   # set
git config --global --get gpg.format        # ssh for an SSH key; unset or openpgp for GPG

# the agent holding the key, selected by the format above
if [ "$(git config --global --get gpg.format)" = ssh ]; then ssh-add -L; else gpg --list-secret-keys; fi
```

`--global` rather than the effective config, because the effective value depends on where the command runs: inside any existing repository a repo-local override wins, so a bare `git config --get user.email` there reports that repository's identity and hides the host setting this step exists to check. The two scopes together are what make the result sound, since this block proves the host is right and the block below proves nothing shadows it.

The agent check branches rather than listing both forms, because they are alternatives and running the wrong one fails on a correctly configured host: an SSH host need not have `gpg` installed at all. Signing is **SSH or GPG**, so judge the format and its agent together rather than requiring `ssh`: what matters is that the configured format has a matching agent holding the key, which is the check [GOVERNANCE.md "Git and Commit Rules"][governance-git-and-commit-rules] prescribes. Any of these wrong or absent is a **host** misconfiguration to surface to the maintainer ([`docs/host-setup.md`][host-setup] is the setup procedure), not something to patch per repo. Patching it locally hides a broken host that then produces wrong identities in every other repo on that machine.

After `git init` and before the first commit, confirm the repo added no override of its own. This one needs a repository, since `--local` fails outside one:

```shell
git config --local --get user.email || true    # expect no output
```

**The finding is a printed value, never the exit code.** An unset key prints nothing and exits `1`, so the passing case is a non-zero exit with empty output, and reading the exit status as failure inverts the check. The tolerant tail is in the snippet above so a copy into a `set -e` script does not abort on the expected case.

After the first commit, confirm it took with `git log -1 --format='%G? author=%an <%ae> committer=%cn <%ce>'`, so the passing result is `G` plus the expected `noreply` address in **both** identities. Read both rather than the author alone: the rule governs the `author` and the `committer` together, GitHub verifies the signature against the **committer**, and a rebase, amend, or cherry-pick rewrites the committer while leaving the author untouched, which is exactly the case an author-only check passes and should not. `git verify-commit HEAD` is the pass/fail form, exiting non-zero on a bad signature and writing its "Good signature" line to stderr rather than emitting a status letter.

## 0A. Hand Over What Only the Maintainer Can Supply

**Nothing in this procedure creates the GitHub repository.** Creating one is an outward-facing write that [GOVERNANCE.md "Repository Boundaries and Write Safety"][governance-repository-boundaries-and-write-safety] puts behind explicit per-session permission, so the agent asks for it rather than assuming it exists. Hand this list over before step 1, so it is a checklist at the start rather than a discovery at step 4:

- **The repository**, with its owner, name, and visibility.
- **The GitHub App installed on it.** An App that is created but not installed does not work, per [`repo-config/README.md`][repo-config-readme].
- **The App secret values**, in the Actions and Dependabot stores both.
- **Every publish credential and environment the repo's mechanisms declare** in [`spec/secrets.json`][secrets], including any environment a deploy gates on.

**A repo with no remote is not partially stood up. It is not started.** Steps 0 through 3 complete locally and report progress with no repository in existence, so local progress is not evidence of onboarding progress. [`AUDIT.md`][audit] is the check that would catch it, and it reads a live repo, so the one instrument that detects this condition is unavailable exactly while it holds.

**Escalate a blocking prerequisite the moment it is found, rather than carrying it.** In a task list a pending task and a blocking prerequisite look identical, and the second quietly becomes the first as work continues around it. Stop at the step that needs the missing input and say which input it is.

## 1. Classify and Catalog

Resolve the repo's type(s) with the [`AUDIT.md`][audit] section 2 detection rules, then write or repair its [`registry/repos.json`][repos] entry: `status`, `types[]`, `groundTruthBranch`, `hasDevelop`, `publish[]`, `requiredSecrets[]`, `consumerModel`, `releaseTrigger`, `workflowModel` (omit to take the `release` default), `configLayout`, and `driftNotes` that describe what the repo **actually is**. Run [`spec/validate.py`][validate] to confirm it classifies cleanly. The registry is ground truth about reality, not intent, and a `validate.py`-clean entry is still false if it disagrees with the live repo.

## 1A. Carry the Instruction Set, Before Authoring Anything

**Stop here until the instruction set is present and read.** The baseline in step 2 is one list, but it holds two kinds of file, and this kind is not a deliverable. `AGENTS.md`, `GOVERNANCE.md`, `CODESTYLE.md` and `WORKFLOW.md` are **the rules for producing every other file in the repo**, so carrying them late means everything authored beforehand was authored against unknown rules. The cost of that is rework rather than a warning, and it scales with how much got written first.

This is the same shape as step 0. Signing has to be live before the first commit rather than retrofitted, and governance has to be loaded before the first authored file for the same reason: the window closes quietly, and the repair is expensive out of proportion to the prevention.

Carry these before writing any repo content of your own:

- [`AGENTS.md`][agents], [`GOVERNANCE.md`][governance], [`CODESTYLE.md`][codestyle], [`WORKFLOW.md`][workflow] and [`AUDIT.md`][audit], adapted rather than cloned for the ones that describe a repo.
- **`.markdownlint-cli2.jsonc` and `cspell.json`**, which are the mechanical half. A rule nothing checks drifts silently, so a repo that carries the prose authorities without the linter configs has guidance and no gate. Scope a linter's **file set in the workflow** rather than relaxing either config, since `.markdownlint-cli2.jsonc` is carried `verbatim`.

Then **read** `CODESTYLE.md` and the `GOVERNANCE.md` documentation-style rules, rather than only placing the files. Comment shape, one sentence per line, US spelling and the character rules all govern the code and config you are about to write, and none of them are recoverable cheaply afterwards.

**A caution about learning house style from the carried files.** Some carried configuration still holds comment blocks that predate the current rules, so read the rule text as the authority and do not infer style from a file's existing formatting. Where a carried file and the rules disagree, the rules win and the file is a backlog item for the hub.

## 1B. Capture the Source, Before It Changes

**This step applies only when the repo's content comes from a live external system the repo replaces.** The capture is independent of every other step here and runs as early as the source is reachable, ahead of scaffolding where the source is paid for, rented, or scheduled for shutdown. It is the same window-closes shape as steps 0 and 1A, with a harder edge: a source system is not under version control, so nothing about it can be re-derived once it stops serving.

Capture the source, verify the capture **against the source**, and hold the verification artifacts (a golden URL list, an export manifest of content hashes) as the before-snapshot, then convert from that rather than from the live system. [`docs/content-import.md`][content-import] holds the three failures that make a capture look complete when it is not: an export that omits externally hosted media, a sitemap that is not the URL contract, and an HTTP fetch that returns a derivative rather than the original. Each reconciles cleanly against the artifact the source hands you, which is why the verification has to read the rendered pages, a live crawl, and content hashes instead.

## 2. Carry the Baseline Files

Copy every [`spec/files.json`][files] entry whose `appliesTo` matches the repo's **selector set**, **adapted, not cloned**. The selector set is the repo's `types` plus its `workflowModel`, `releaseTrigger`, and `consumerModel`, so filtering on type alone silently drops the entries a non-type selector carries ([`spec/scope-model.md`][scope-model] defines the four namespaces and how they resolve). The prose files (`CODESTYLE.md`, `README.md`, and the like) describe the repo's own toolchain, so adapt them to reality rather than propagating template specifics verbatim (see the "Adapt before propagating" callout in [`CODESTYLE.md`][codestyle], since a verbatim copy that misdescribes the repo is rejected in review). The baseline covers `WORKFLOW.md`, `version.json`, the two rulesets, `.github/dependabot.yml`, `.editorconfig`, `.gitattributes`, the linter configs, and the per-type files (`.vscode/tasks.json` from the language's snippet, `codecov.yml`, `.dockerignore`, `Docker/README.md`). **Every repo carries `repo-config/main.json`**, and only the `develop` payload varies by workflow model: `repo-config/develop.json` for a release repo, `repo-config/operational/develop.json` for an operational one.

**`version.json` is a file to carry and a floor to choose.** [`WORKFLOW.md`][workflow] D3.3 makes its `version` field the repo's own major.minor floor, with NBGV appending the git height as the patch, so the number carried in with the file is a claim about a release history the new repo does not have. Set it deliberately, at standup, before the first release:

- **A new project starts at `1.0`**, or at `0.1` while it is deliberately pre-release and its consumers are told so.
- **A project with releases behind it keeps its established scheme**, adapted to NBGV rather than restarted. The field carries a major.minor floor and NBGV counts the patch from the git height rather than from where the published sequence stopped, so a floor matching the published major.minor emits a patch counted from that floor's first commit, which lands under an existing tag whenever the published patch ran ahead of the height. Raise the minor above the highest published one, which clears the collision and leaves nothing to maintain. `versionHeightOffset` shifts the height instead, at the cost of an offset the repo carries from then on. Either way `nbgv get-version` prints the computed version, and it has to sort above the latest tag before the first release.
- **A repo that ships no package still chooses.** An operational or source-only repo releases a tag and a source archive, which is a published version like any other, so "nothing consumes it" is not a reason to leave the carried number in place.
- **Carry only the fields the repo uses.** `nugetPackageVersion` is packaging configuration for a NuGet publisher, so a repo that publishes no package drops the block rather than carrying a setting nothing reads. `publicReleaseRefSpec` names the repo's own default branch, which D3.2 requires it to agree with.

**This decision is effectively one-way, which is why it belongs here.** Once a repo publishes against a floor, lowering it regresses the released version order, so a floor that was never chosen is kept rather than corrected. Inherited floors are the observed failure, not a hypothetical one: four operational config repos run on a floor none of them picked and have released against it.

**Repo-specific content has a declared destination, not a judgment call.** The baseline is what a repo *carries*. Anything the repo knows that the fleet does not needs somewhere to live, and improvising a location per repo is what the destinations in [`spec/section-model.md`][section-model] exist to prevent. Four topical docs take it, chosen by what the content **is**:

- [`CODESTYLE.md`][codestyle]: the repo's language and formatting conventions beyond the carried rules.
- `ARCHITECTURE.md`: how a code repo is built, its module layout, data flow, and design decisions.
- `OPERATIONS.md`: how the repo is run, under the headings `Runbooks`, `Backup and Recovery`, `Logs and Debugging`, `Tool Usage`, and `Configuration Layout`.
- `TODO.md`: the repo's running backlog, per [`spec/readme-structure.md`][readme-structure]. It keeps open work out of the README's section order, where it does not belong and changes on a different cadence from everything around it.

**`OPERATIONS.md` is required on every repo**, not optional, so it appears in the baseline above with `appliesTo: "*"`. It is presence-checked only, the same footing as `README.md` and `HISTORY.md`, so its content is entirely the repo's own and a repo with little to say still carries the file as a stub, meaning those five headings with no content under them, for which this repo's own `OPERATIONS.md` is the worked example. Do not read the `operational` workflow model into the requirement, because that selector describes where config lives rather than whether the repo has runbooks, and a repo that publishes to a package registry or deploys a site has operational surface under either model. It is the operational analogue of `ARCHITECTURE.md`, and it is where an `AGENTS.md` split puts the repo-specific half, so real runbooks (a deploy procedure, a rollback, a retention policy, a credential rotation) go there rather than into a carried file. It is agent-instruction content, so it takes the inline-link exception the Markdown rules name rather than the reference-style default. `ARCHITECTURE.md` and `TODO.md` stay advisory and are required by no selector, so a repo with nothing to say in one carries no file rather than an empty one.

Choose the destination while scaffolding rather than after. Repo-specific content left in a carried file is drift, which the audit lists as an undeclared section to reconcile, and reconciling it later means moving prose that downstream readers have already started trusting in the wrong place.

## 3. Stand Up the Workflows

Implement the Actions that satisfy [`WORKFLOW.md`][workflow] for the repo's type (its section 6 per-type walkthrough): the source-only subset for a source-only repo, the file-target leaf(s) for a publishing repo, the two-workflow shape for an operational config repo. Reuse [`catalog/snippets/workflows/`][workflows] as the reference implementation, satisfying the contract by outcome rather than byte for byte.

## 4. Apply Settings, Rulesets, and Secrets

**Read the remote and the repository before running anything else here**, since this is the first step needing either and every step before it passes without both:

```shell
git remote get-url origin                                 # expect a URL, not an error
gh repo view "<owner>/<repo>" --json nameWithOwner,visibility
```

The placeholder is quoted because an unquoted `<` is input redirection, so the line fails on paste against a file rather than against the repository.

Three conditions fail here, and the two commands together are what separate them:

- **No `origin`.** The checkout has nowhere to push even where the repository exists, and it is the state a local-only standup reaches with every step reporting success.
- **No repository.** It surfaces as a resolution error against whatever `configure.sh` calls first, which reads as a permissions or naming problem rather than as the missing prerequisite it is.
- **The two disagree.** Neither command checks this, so compare the `origin` URL against `nameWithOwner` and confirm they name the same repository.

Each is step 0A's escalation rather than something to work around.

Run `repo-config/configure.sh apply owner/repo release|operational` from a hub checkout, naming the repo being stood up and its model, to apply the fleet settings, the Dependabot security features, and the two rulesets idempotently (import the JSON, never hand-build it, per [`docs/repo-config-carry.md`][repo-config-carry]), then `repo-config/configure.sh check owner/repo [release|operational]` to validate the repo and exit non-zero on any drift. The script is hub-hosted rather than carried, so the repo being stood up holds no copy of it and never needs one, and naming the target is what keeps the write off the checkout the command runs in. Pass the model explicitly here rather than relying on the lookup. Run from a hub checkout the registry is present, so a repo not yet registered resolves through `defaults.workflowModel` to `release` and applies the wrong `develop` ruleset to an operational repo, and a repo being stood up is exactly the one the registry has not got yet. Reconcile its registry entry in step 6 either way. Configure every required secret per [`spec/secrets.json`][secrets] (the registry `requiredSecrets[]` list plus the implicit baseline) in the right store(s), meaning Actions plus Dependabot where the mechanism needs it, and confirm no forbidden secret is present. The required check binds by name (`Check pull request workflow status job`) and turns green only after the PR workflow has run once, which is why this step follows step 3 rather than preceding it. A ruleset requiring a name no run has ever reported leaves the first pull request waiting on a status nothing produces, and on an operational repo the `develop -> main` promotion is a pull request too, so the same wait applies there.

## 5. Verify: Run the Audit

Run [`AUDIT.md`][audit] end to end. The repo is stood up only when it is **operational** (every applicable check passes) or its residual deltas are tracked in `reports/<repo>/audit.md` plus an issue. Converge any drift through a Copilot-reviewed target PR ([`AUDIT.md`][audit] section 10), and the maintainer merges. A repo left partially set up and unrecorded is the exact failure this procedure exists to prevent.

## Onboarding a New Repo Type

When a repo matches no existing type, the work is onboarding a **type**, not just a repo:

1. Add the type to [`spec/project-types.json`][project-types] (`detect[]`, plus `checks` with verdict tiers and intent refs) and any per-type files to [`spec/files.json`][files], then add its publish mechanism to [`spec/secrets.json`][secrets] if new. Add the type's token to [`spec/scope-model.md`][scope-model] and the type itself to [`spec/type-model.md`][type-model] in the same change, which that file's own rule requires. A type publishing to a **new destination** also needs the target added to the closed `target` enum in [`registry/repos.schema.json`][repos-schema] and mapped in `targetMechanisms`, or the first repo declaring it fails `spec/validate.py` with an unknown-target error.
2. Add the reference workflow leaf to [`catalog/snippets/workflows/`][workflows] and document the type's [`WORKFLOW.md`][workflow] walkthrough. A leaf must not be named `build-*-task.yml` unless the type really is a build target, since `source-only.detect` is literally "no `build-*-task.yml`" and the name alone would make that declaration false for any repo carrying both.
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

[agents]: ./AGENTS.md
[audit]: ./AUDIT.md
[codestyle]: ./CODESTYLE.md
[content-import]: ./docs/content-import.md
[files]: ./spec/files.json
[governance]: ./GOVERNANCE.md
[governance-git-and-commit-rules]: ./GOVERNANCE.md#git-and-commit-rules
[governance-repository-boundaries-and-write-safety]: ./GOVERNANCE.md#repository-boundaries-and-write-safety
[host-setup]: ./docs/host-setup.md
[matrix]: ./reports/conformance-matrix.md
[project-types]: ./spec/project-types.json
[readme-structure]: ./spec/readme-structure.md
[repo-config]: ./repo-config/
[repo-config-carry]: ./docs/repo-config-carry.md
[repo-config-readme]: ./repo-config/README.md
[repos]: ./registry/repos.json
[repos-schema]: ./registry/repos.schema.json
[scope-model]: ./spec/scope-model.md
[secrets]: ./spec/secrets.json
[section-model]: ./spec/section-model.md
[spec]: ./spec/
[type-model]: ./spec/type-model.md
[validate]: ./spec/validate.py
[workflow]: ./WORKFLOW.md
