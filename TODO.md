# TODO

Running backlog for this repo, kept in a committed file so the research survives across environments where agent memory does not. Entries are grouped by the change that ships them, so a `###` heading under "Work Clusters" is one pull request, and selecting work is reading the cluster headings rather than re-deriving the grouping from the entries. What each cluster touches and costs is a field on the cluster, since a cluster confined to one surface and a cluster spanning two are both legitimate and only the second needs saying.

An entry carries `Blocked by`, `Issue` and `Checked` exactly once each, in that order, and never omits one, because an omitted field reads as unknown rather than as none. `Open` states a decision the session doing the work makes, and `Settled` states a finding that is not re-derived, each carrying a number, a proper name, or a rejected alternative. `Checked` is the freshness anchor, naming the branch, the commit, and the date a claim was last read against the tree, so a claim older than the branch is a claim rather than a finding.

A cluster's `State` is one of four. `ready` means every open question is answerable by the session doing the work. `blocked` names the cluster it waits on. `decision` needs the maintainer. `measure` means the first action is a count rather than an edit.

## How to Select the Next Item

The steps below are followed in order rather than sampled.

1. Run `gh issue list --state open` and confirm every number it returns appears somewhere in this file. A number appearing nowhere is an entry that does not exist yet, so write it before selecting anything, because an invisible issue cannot be selected. Nothing mechanical enforces this, which is the honest limit of a hand-maintained file and the reason the step is first.
2. Run `gh pr list --state open` and confirm every pull request it returns carries a **stated active blocker**, written where the pull request itself carries it rather than held in a session that has ended. A blocker is active only while the thing it names is still true, so a review round that has landed, a dependency that has merged, and an outage that has passed each stop being one, and what they leave behind is a forgotten pull request rather than a parked one. The remedy is to finish it, close it, or write the current blocker down, and it happens before selecting new work rather than after, because the cost is not the waiting. A bot pull request is read rather than excluded, since nobody is there to write a blocker on one, so its blocker is whichever gate holds it open and is read off the pull request itself: an unfinished or failing check, a merge state of `BEHIND` because a sibling bot pull request merged first, or auto-merge disabled by a maintainer push, the last two of which [`GOVERNANCE.md`][governance] "Branching Model" documents as expected rather than as faults. One sitting open under none of them is the merge-bot having missed it, which is the finding rather than the exemption. [#591][pr-591] was parked correctly during a GitHub Actions outage and came back three days later twenty commits behind `develop`, conflicting in six regions, and carrying an exit code that had come to mean something else in the meantime.
3. Read the cluster headings and their `State` lines. A cluster is the unit of selection, so pick a cluster rather than an entry, and never carry two clusters in one pull request.
4. Prefer a cluster whose state is `ready`. Select a `decision` cluster only when the maintainer is present to answer its open questions, select a `blocked` cluster only after the cluster it names has shipped, and select a `measure` cluster knowing its deliverable is a number rather than a behavior change.
5. Re-verify every `Checked` line in the chosen cluster against current `develop` before writing anything, by reading the surface the anchor names rather than by re-reading the issue. An issue records the tree as it was on the day it was filed, so a claim in one is a starting point for a check rather than a finding to act on.
6. Rewrite the `Checked` line with the branch, the short commit, and the date whenever a claim is confirmed, whether or not the work ships in the same session. A re-verification that leaves no anchor is a check the next session repeats.
7. Move a claim the tree contradicts out of `Settled` and state what the tree carries instead. Where the tree answers a whole entry, move the entry to "Verified Complete, Awaiting Close" with the commit that answered it, and never delete it silently, since a deleted entry reads as work nobody recorded.
8. Fold a new observation in under one of four dispositions, named on the pull request carrying it: `New entry`, `Amends "<entry title>"`, `Already covered`, or `Already shipped as #N`. A second observation of a surface an entry already reasons about strengthens that entry rather than opening a second one.
9. An amendment adds a `Settled` bullet, shortens `Open`, and refreshes `Checked`. An observation that answers an open question deletes that question rather than annotating it.
10. Delete a cluster heading when its pull request merges, and move anything the pull request did not carry into a new cluster with its own state.

## Work Clusters

### Giving the Fleet's Own Pins Something to Resolve Against

One pull request pointing a hub `uses:` at a hub-owned action, so that the resolvability pass added beside it has a reference under this owner to read. It is separated from that pass because it changes what a workflow runs, where the pass only changes what a gate reports.

**State** `decision`. **Touches** the hub's own workflows. **Cost** one hub edit, hub-only, and it changes a running workflow so it is not a paper change.

- **Decide whether the hub consumes its own [`prose-gate`][prose-gate] action the way the fleet does.** Today it calls `prose_lint.py` directly, so every `uses:` in the tree is under another owner.
  - **Blocked by** - Nothing, though it is only worth doing on its own merits rather than to give a gate something to read.
  - **Issue** - None filed.
  - **Checked** - `develop` at `dbd1cdc` on 2026-08-07, where the tree carries 45 pinned `uses:` refs and not one of them names a `ptr727` repository.
  - **Open** - Whether the hub gating itself through its own pinned action is desirable at all, given the action reads the rules from hub `develop` on a non-`main` target and the hub already has the script in its own checkout.
  - **Settled** - The resolvability pass reports what it covered on every run, so the hub's zero is visible rather than silent, which is why this is a separate decision rather than a defect in that pass.
  - **Settled** - The fleet's `ptr727` pins are live in the downstream repos that consume the action, and `repo_gate.py --root <repo>` from a hub checkout reads them there, so the pass is not idle fleet-wide.

### Three Rules That Leave the Recurring Case Unstated

One pull request widening three carried [`GOVERNANCE.md`][governance] rules that each state their common case and go quiet on the case that recurs, filed together because they share that shape and land in one re-vendor.

**State** `ready`. **Touches** [`GOVERNANCE.md`][governance] "Git and Commit Rules", "Communicating with the User", and "Operational Repositories". **Cost** one hub edit plus a carried-section re-vendor, which rides the visit in "Fleet Sweeps".

- **Say when an issue is closed by hand, not only that the closing keyword belongs on the promotion.** The uncovered case is work complete on `develop` with no promotion imminent.
  - **Blocked by** - Nothing.
  - **Issue** - [#578][issue-578] item 1.
  - **Checked** - `develop` at `b82c1a3` on 2026-08-05, where the rule licenses a hand-close only once a promotion has merged without the keyword.
  - **Open** - Nothing.
  - **Settled** - Downstream agents keep re-deriving the rule and reporting it as a discovery, which says it is being missed rather than that it is missing, so the discoverability half is not a wording fix.
  - **Settled** - The widening says an issue is closed when the work is verifiably complete, citing the squash commit that completed it, and that the promotion keyword is the automation for the common case rather than the only permitted route.

- **Say that the message carrying the clickable link comes before the prompt it accompanies.** A message emitted after the prompt is not read before the question is answered.
  - **Blocked by** - Nothing.
  - **Issue** - [#578][issue-578] item 2.
  - **Checked** - `develop` at `b82c1a3` on 2026-08-05, where the rule says accompanying rather than preceding.
  - **Open** - Nothing.
  - **Settled** - The rule already gets the hard part right, that an interactive prompt renders neither a Markdown link nor a bare URL, so the reference inside it is a bare number and the link goes in the message.
  - **Settled** - The recurrence is evidence that the wording does not reach the case rather than that the rule is ignored, which is the same diagnosis the entry above reaches.

- **State that an operational repository still opens a pull request for a large or risky change.** The grant to commit direct to `develop` says nothing about when to decline it.
  - **Blocked by** - Nothing.
  - **Issue** - [#578][issue-578] item 3.
  - **Checked** - `develop` at `3d1a0b1` on 2026-08-06, where `repo-config/operational/develop.json` carries exactly three rules, `deletion`, `non_fast_forward` and `required_signatures`.
  - **Open** - What counts as large, stated as a shape rather than a line count, since the property that matters is whether the change can be read at a glance and reverted cleanly.
  - **Settled** - The reason the grant exists is the one-line config edit that a review round costs more than it protects, and that reason stops applying well before a change gets large.
  - **Settled** - This stays guidance by construction, because adding a `pull_request` rule to the operational ruleset would withdraw the direct-commit grant the model exists to give.

### The Declared Repository Description

One pull request moving the canonical short description into declared data, which settles the second-paragraph ambiguity by construction rather than by writing an extraction rule the same change then deletes.

**State** `decision`. **Touches** [`registry/repos.json`][repos] and its schema, [`spec/audit.py`][audit], [`spec/readme-structure.md`][readme-structure], and [`CODESTYLE.md`][codestyle]. **Cost** one hub edit plus a carried re-vendor of the `CODESTYLE.md` item, and repos adopt the field one at a time.

- **Declare the description in [`registry/repos.json`][repos] instead of deriving it by parsing the README.** Every check and every push then reads a field.
  - **Blocked by** - Nothing.
  - **Issue** - None filed, and the disposition is recorded on [#509][issue-509].
  - **Checked** - `develop` at `3d1a0b1` on 2026-08-06, where neither `registry/repos.json` nor `registry/repos.schema.json` carries a `description` key.
  - **Open** - Nothing beyond sequencing, which is that this leads and the README shape follows.
  - **Settled** - PhotoCleaner#32 measures the cost of parsing, since a workflow step reading the intro at publish time needs nine guards against headings, block quotes, all four list markers, ordered lists, HTML, tables, code, links and the length cap, and every one of them fails the release rather than the tagline.
  - **Settled** - The field makes the README intro a third mirror rather than the source, so the audit compares all three against one declared value and `repo-config/configure.sh` sets the About panel from the same field it already sets every other setting from.
  - **Settled** - The 100-character cap stays, since Docker Hub's short description is the tightest surface.
  - **Settled** - The field is optional at first so the audit falls back to the README intro while repos adopt it, and it needs a schema entry because `registry/repos.schema.json` sets `additionalProperties: false`.
  - **Settled** - The ask on the Docker repos meanwhile is only that the parsing step is not propagated further.

- **Let the README intro carry more than the tagline, and say which line the mirrors take.** The current pair of rules forbids a README from saying anything further about itself above the fold.
  - **Blocked by** - The entry above, since taking this first means writing an extraction rule the registry change deletes.
  - **Issue** - [#577][issue-577], which carries the three surfaces that change together.
  - **Checked** - `develop` at `b82c1a3` on 2026-08-05, where [`spec/readme-structure.md`][readme-structure] item 1 reads as though the canonical description is the paragraph after the H1 and [`CODESTYLE.md`][codestyle] has `HISTORY.md` copy the same intro paragraph verbatim.
  - **Open** - Nothing.
  - **Settled** - The shape is that the first line after the H1 is the tagline, it alone carries the 100-character link-free rule, it alone mirrors to the GitHub About panel, the Docker Hub short description and the `HISTORY.md` opening, and any further paragraph is free prose no mirror reads.
  - **Settled** - The audit changes with the rule, since it measures the first non-empty line and would otherwise report a legitimate second paragraph.
  - **Settled** - The cap belongs to a mirror rather than to the reader, which is why declaring the field removes the parser that motivates it.

### Content in the Wrong File

One pull request teaching the audit to see content sitting in a file the section model assigns elsewhere, which is invisible today and reported as a missing file instead.

**State** `decision`. **Touches** [`spec/audit.py`][audit] and possibly [`spec/files.json`][files]. **Cost** one hub edit, hub-only, and it changes what every repo's next audit reports.

- **Compare an `intent` file's headings against the destinations the section model assigns.** Collect the level-two headings, subtract the ones the manifest declares for that file, and compare the remainder against the headings other destinations declare.
  - **Blocked by** - Nothing.
  - **Issue** - [#523][issue-523], which carries the four things to settle.
  - **Checked** - `develop` at `1ed0cc8` on 2026-08-03, where the audit checks file presence, declared-section presence, verbatim hashes, and workflow interface conformance, and nothing that reads a heading against a destination.
  - **Open** - Whether an undeclared heading is a finding at all, given a repo may legitimately add locally.
  - **Open** - Whether the destination mapping becomes declared data rather than prose, and whether it reaches the advisory `ARCHITECTURE.md`.
  - **Open** - How many repos are affected, measured before the check is designed rather than after it starts reporting.
  - **Settled** - The case that found it is a repo whose `.github/copilot-instructions.md` carried 311 lines under nine headings assigned to `ARCHITECTURE.md` and `OPERATIONS.md`, reported as a missing-file letter while the misplacement that caused it was invisible.
  - **Settled** - The similarity-based version is rejected by [`spec/section-model.md`][section-model], and a detector built on it produces findings whose remedy is to delete content.

### Registry Membership Coverage

One pull request asking the inverse question the fleet tools never ask, whether a repository that exists has a registry entry, since every tool iterates the registry and an omission at standup is permanent and silent.

**State** `decision`. **Touches** [`spec/audit.py`][audit], [`registry/repos.json`][repos] and its schema, and [`STANDUP.md`][standup]. **Cost** one hub edit, hub-only.

- **Report a non-fork repository under the owner that has no registry entry.** The reports read as complete while under-counting today.
  - **Blocked by** - Nothing.
  - **Issue** - [#550][issue-550], which carries the four repos the comparison found.
  - **Checked** - `develop` at `362aec8`, per the issue, and unverified since.
  - **Open** - How a deliberate exclusion is recorded, since without one the check becomes a permanent four-line complaint people learn to scroll past, and the candidates are a third `status` value or a separate list carrying a reason per entry.
  - **Open** - Where the check runs, since neither `validate.py` in CI nor an owner-initiated audit catches an omission at the moment it is made, which is the standup itself and the moment the fix costs one line.
  - **Settled** - The consequence is worse than a gap, because the reports are confidently wrong rather than silent: [reports/divergences.md][divergences-report] counted 19 repos owing `AGENTS.md` "Fleet Bootstrap" when the real number was 20.
  - **Settled** - The procedure is not the gap, since [`STANDUP.md`][standup] section 1A already says to write the entry and names every field, and nothing verifies it happened.
  - **Settled** - The reason matters more than the mechanism, since an unexplained exclusion is the same silent omission in a different file.
  - **Settled** - Private repositories are outside the public listing the issue used, so the true count is a floor rather than a total.

### Reducing the Carried Surface Further

One pull request measuring the remaining carried surface against the carry-versus-reach test and moving whatever qualifies, now that the model is settled rather than open.

**State** `decision`. **Touches** [`AUDIT.md`][audit-doc], [`spec/secrets.json`][secrets], [`spec/files.json`][files], and [`catalog/snippets/workflows/`][workflows]. **Cost** one hub edit plus a retirement per repo on its next visit.

- **Measure carried [`AUDIT.md`][audit-doc] and [`spec/secrets.json`][secrets] against the test.** Each is adapted per repo today and the question is how much of each is genuinely per-repo.
  - **Blocked by** - Nothing.
  - **Issue** - None filed, and [#305][issue-305] covers the propagation half from the other direction.
  - **Checked** - `develop` at `3d1a0b1` on 2026-08-06, where [`spec/files.json`][files] declares both at `intent` and no longer declares `repo-config/configure.sh` at all.
  - **Open** - Which of the two moves, if either.
  - **Settled** - The test is stated: a repository carries the content it is audited against and the configuration that describes it, and it reaches machinery whose content is identical in every repository.
  - **Settled** - `repo-config/configure.sh` is the first file moved across, carrying the ledger's only `retire` disposition and naming six repos, NxWitness, aiopurpleair, homeassistant-purpleair, ESPHome-NonRoot, VSCode-Server-DotNetCore and LanguageTags.
  - **Settled** - An unreachable hub means the tool did not run, reported as not run rather than worked around, since a hand-rolled substitute is the duplicated effort the model exists to end.

- **Investigate replacing copy-pasted workflow content with cross-repo reuse.** A public repository's composite actions and reusable workflows are consumable by any other repository regardless of owner type, so the organization account this pattern was assumed to need is not needed.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - `develop` at `3d1a0b1` on 2026-08-06, where [`.github/actions/prose-gate/action.yml`][prose-gate] is the worked example and has zero callers, in this repo or the fleet.
  - **Open** - Which jobs are genuinely identical across repos against which only look similar, since a reusable workflow needing a long input list to cover per-repo variation is worse than the copy it replaces.
  - **Settled** - The catalog under [`catalog/snippets/workflows/`][workflows] is copied into each repo, so a fix to a shared job is a fleet sweep rather than one edit, and it is the mechanism by which a defect in a snippet seeds itself into every repo that adopted it.
  - **Settled** - The ref policy is settled rather than open, since CI reaches hub code as an action pinned to a commit SHA, which is the action-pinning rule applied unchanged.
  - **Settled** - `uses:` accepts no expressions, so a per-branch ref cannot be selected in the workflow file and any branch-dependent behavior belongs inside the consumed action, which is what the prose gate action does.

### The README Structure Rework

One pull request reworking the README spec to the hand-crafted PlexCleaner shape the maintainer wants, and making the result auditable rather than advisory.

**State** `decision`. **Touches** [`spec/readme-structure.md`][readme-structure] and the `readme-structure` dimension in [`spec/audit.py`][audit]. **Cost** one hub edit, and it re-grades every repo's README.

- **Encode the distribution channel by deliverable rather than as one fixed label.** Four divergences are already identified against PlexCleaner and this repo.
  - **Blocked by** - "The Declared Repository Description", since the mirrors settle first.
  - **Issue** - None filed.
  - **Checked** - `develop` at `1ed0cc8` on 2026-08-03.
  - **Open** - Nothing.
  - **Settled** - PlexCleaner ships executables and calls the channel Binary Releases, while the spec fixes the label as Versioned Releases for every repo, so the label belongs in a per-channel table.
  - **Settled** - The license shield sits in the top Build Status block here and at the very bottom of PlexCleaner, inside a closing License section reading that the project is licensed under the MIT License, followed by the shield, immediately before the link definitions.
  - **Settled** - The Release Notes section closes by pointing at the release history for complete release notes and older versions, which is the wanted form, and PlexCleaner writes that link inline, which the reference-style rule forbids, so the wording is adopted and the reference form kept.
  - **Settled** - Channel bullets and shields vary by deliverable, meaning GitHub binaries, Docker Hub, NuGet and PyPI each carry a different bullet label and shield set, which is what a per-type table has to encode.

- **Decide whether the canonical section order follows PlexCleaner.** This affects every repo plus the audit dimension.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - `develop` at `1ed0cc8` on 2026-08-03.
  - **Open** - The position of the sections the spec already names, since PlexCleaner places Questions or Issues immediately after the Table of Contents where the spec orders it ninth.
  - **Settled** - PlexCleaner's Performance Considerations, Runtime Metrics, Custom Plugins, Testing, Development Tooling, Feature Ideas and Sample Media Files are correctly repo-specific under the recurrence rule in [`spec/section-model.md`][section-model] and stay undeclared.

### Two Project Types and a Shared C++ Style

One pull request extending the type model with the two types the fleet already needs, plus the shared style the `cpp` type has no canonical for.

**State** `ready`. **Touches** [`spec/project-types.json`][project-types], [`catalog/snippets/`][snippets], [`CODESTYLE.md`][codestyle]. **Cost** one hub edit plus a carried `CODESTYLE.md` re-vendor.

- **Add a linter-only Python type for codegen and boilerplate Python.** Code that runs during another tool's build to emit generated source ships no unit tests and no coverage and needs only the linter.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - `develop` at `1ed0cc8` on 2026-08-03.
  - **Open** - Nothing.
  - **Settled** - It stays distinct from the existing `python` type, which is utility code that can and should carry unit tests and coverage, as in PlexCleaner.
  - **Settled** - ESPHome-Config stays `source-only` until it exists and its reclassification is deferred, so its one outstanding validation finding is accepted meanwhile.

- **Add a fleet-standard clang-format config for the `cpp` type.** A catalog snippet plus a `CODESTYLE.md` C++ section, the analogue of the shared ruff config.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - `develop` at `1ed0cc8` on 2026-08-03.
  - **Open** - Nothing.
  - **Settled** - It exists so the `cpp` clang-format check references one canonical style rather than each repo inventing its own, and the ESPHome-Config agent's proposed file is the base.

### How a Hugo Repository Carries Its Theme

One pull request deciding what the `hugo` type says about a theme, which it declares nothing about today.

**State** `decision`. **Touches** [`spec/project-types.json`][project-types] and [`spec/type-model.md`][type-model]. **Cost** one hub edit, and it becomes the type's contract that a second generator inherits.

- **Decide the theme carry mechanism as a question about the type rather than about Blog.** The candidates differ along the same axis the carried-content clusters are about.
  - **Blocked by** - Nothing.
  - **Issue** - None filed, and [#456][issue-456] and [#558][issue-558] carry the type's intake.
  - **Checked** - `develop` at `b82c1a3` on 2026-08-05.
  - **Open** - Which of three the type requires, the vendored copy Blog ships, a submodule pinned to an upstream ref, or a separate fleet-owned repository the site consumes.
  - **Settled** - A vendored theme is a copy that goes stale with nothing detecting it, and a submodule is a pin Dependabot can see, which is the whole difference.
  - **Settled** - Three details the intake predicted are wrong against what Blog runs, so planning from the prediction encodes requirements the repo does not meet: the theme is vendored with no recorded upstream ref rather than a Dependabot-tracked submodule, the generator is pinned by version and hash rather than run at latest, and the deploy is a separate dispatch rather than a tag cut last after the live check.
  - **Settled** - What held is that the deploy is a publish, the type is named for the generator with the generic checks phrased so they do not name it, and the URL parity gate asserting a floor on the golden list length before comparing is the check of record.
  - **Settled** - Promoting the generator-agnostic `hugo` checks to a shared type when a second generator arrives is a registry edit by construction, per [`spec/type-model.md`][type-model] "Generators".
  - **Settled** - The `copilot_code_review` rule in both ruleset payloads gates no merge today, because gated Copilot review is an invite-only beta, which deserves a sentence near the merge gate so no repo reads the rule as the enforcement and relaxes the manual discipline holding the line.

### Locally Required Secrets

One pull request giving a repo a declared way to say what it needs at runtime, the way GitHub-stored secrets are already declared.

**State** `decision`. **Touches** [`spec/secrets.json`][secrets] and its schema, [`spec/audit.py`][audit], and the hub's own `.gitignore`. **Cost** one hub edit plus adoption per repo that deploys.

- **Make a gitignored secrets directory the fleet standard and declare its contents.** The required set is discoverable only by reading the deploy today.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - `develop` at `1ed0cc8` on 2026-08-03, where [`spec/secrets.json`][secrets] covers only the Actions and Dependabot stores and the hub carries neither the directory nor a `.gitignore` entry for one.
  - **Open** - Nothing on the local half, and the GitHub half below is the same axis rather than a separate problem.
  - **Settled** - The pattern already runs in the fleet in two shapes, HomeAutomation-Config keeping a gitignored secrets directory of env files and Docker secret files, and ESPHome-Config keeping a gitignored `secrets.yaml` beside a committed `_secrets.yaml`.
  - **Settled** - The committed file carries the required names with dummy values, so the shape of the requirement is in git while the values never are, which is the split the GitHub side already gets from `requiredSecrets`.
  - **Settled** - Blog needs it immediately, since it deploys on the proxmox host through HomeAutomation-Config's Docker Compose stack and carries the copy destinations and the internal URI.
  - **Settled** - Adopting it in the hub comes first, since the hub carries neither piece.
  - **Settled** - The GitHub side has the same missing axis, surfaced by the `hugo` type, since a deploy's credentials are per-environment secrets and variables while `stores` is a closed enum of `actions` and `dependabot`, and [`spec/audit.py`][audit] seeds its map with those two keys and indexes it unguarded, so adding an `environments` value raises a key error for every repo whose publish maps to that mechanism.
  - **Settled** - An optional `environments` block is legal in [`spec/secrets.schema.json`][secrets-schema] so a repo may declare its per-environment names, and no tool reads one where it exists, which is honest and is not a gate, so a clean audit says nothing about whether an environment is configured.

### The Docker Image Freshness Rule

One pull request stating that an agent never assumes a Docker image is present locally, however recently it pulled one.

**State** `ready`. **Touches** [`GOVERNANCE.md`][governance], and [`OPERATIONS.md`][operations] if the mirrored one-liners move with it. **Cost** one hub edit, plus a carried re-vendor if the rule lands in a carried section.

- **State the always-pull default and the explicit pull where the flag does not apply.** A background prune can remove an image between two commands of the same session.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - `develop` at `1ed0cc8` on 2026-08-03, where the four documented lint invocations already carry the always-pull flag and no rule states why.
  - **Open** - Where it lives, since "Running the Linters Locally" is scoped to the four lint tools while the rule covers any container an agent starts, and whether it is carried, since every repo runs the same images from the same instructions.
  - **Settled** - What is missing is the rule rather than the one-liners, since an agent composing an ad-hoc `docker run` drops the flag precisely because it believes the image is cached.
  - **Settled** - The honest limit stops the flag reading as the whole answer, since `docker run` against a registry tag re-pulls an absent image on its own, so the cases that break are a locally built tag with no registry to pull from, and any command that branches on the image being present such as `docker image inspect` or `docker images`.

### The Merge-Bot Token Grants

One pull request dropping the unused token grants from the highest-blast-radius workflow shape in the fleet.

**State** `ready`. **Touches** [`.github/workflows/merge-bot-pull-request.yml`][merge-bot]. **Cost** one hub edit plus a re-vendor, since every repo carries the file.

- **Drop the grants no step consumes.** Every write in the file authenticates with the App token.
  - **Blocked by** - Nothing.
  - **Issue** - [#521][issue-521].
  - **Checked** - `develop` at `3d1a0b1` on 2026-08-06, where three jobs carry both `contents: write` and `pull-requests: write` and the fourth carries `pull-requests: write` alone, so the issue's claim that all four carry both is one job wide.
  - **Open** - Whether to drop the job-level blocks or set an empty workflow-level permissions map.
  - **Open** - Whether the audit compares permissions at all, given the file is `interface` fidelity with only a required-job-keys contract.
  - **Settled** - The finding is least privilege on a `pull_request_target` workflow holding an App private key, where the grant is not exploitable today only because no step consumes it.
  - **Settled** - [`spec/files.json`][files] declares this workflow at `appliesTo: "*"`, which closes the separate gap [#456][issue-456] raised, that the audit graded a file the file spec never required.

### Review Cost and the Local Review Pass

One pull request, after a measurement, stating what change size licenses and whether a local adversarial pass earns its place, which are one question because both are about where review cost goes.

**State** `measure`. **Touches** [`GOVERNANCE.md`][governance] branching or review guidance, once the numbers exist. **Cost** a measurement first, then one hub edit plus a carried re-vendor.

- **Measure review rounds against pull request size, and decide what the number licenses.** The data needs no new instrumentation, since the review history carries it.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - `develop` at `1ed0cc8` on 2026-08-03.
  - **Open** - The threshold, expressed as the size at which a change is split rather than as advice to keep changes small.
  - **Settled** - For each recent pull request the record carries the diff size in files and lines, the number of rounds, and the findings per round, counting suppressed findings alongside threaded ones because they are the majority of what these loops produce.
  - **Settled** - Two confounds bound any line drawn from the numbers, that a large change is usually also a novel one so size and unfamiliarity move together, and that a round finding something new is the reviewer working rather than evidence of a problem, so the metric is findings a smaller first cut would have surfaced earlier.

- **Try local defensive-review subagents as a first pass, and measure what the pass is worth.** One agent per lens rather than one general reviewer.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - `develop` at `b82c1a3` on 2026-08-05.
  - **Open** - Whether the overlap is large enough to shorten the remote loop rather than to add a step in front of it, which is what running both for a stretch measures.
  - **Settled** - The remote loop is where most of a session's tokens and wall-clock go, and it delivers findings one round at a time, which is the slowest available way to learn that a change had five problems.
  - **Settled** - The trap is that a local pass finding nothing reads exactly like a clean change, and the next inference is that the remote review can be skipped, which is the one outcome the review contract exists to prevent, so the local pass is an input to the loop and never a substitute for the round the merge gate requires.

### Where a Disproof Goes When the Reviewer Is Not Copilot

One pull request routing the disproof record from the provider-agnostic contract, so an agent that never opens the provider runbook still knows where a proof lives after the thread closes.

**State** `ready`. **Touches** [`GOVERNANCE.md`][governance] "PR Review Etiquette". **Cost** one hub edit plus a carried re-vendor of a byte-locked section, which is why it is not folded into the change that built the record.

- **State that a disproof is recorded where it survives the pull request, not only in the thread.** The record exists in [`.github/copilot-instructions.md`][copilot-instructions] "Disproved Claims" and nothing agent-agnostic points at it.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - `develop` at `756a53e` on 2026-08-07, where outcome 2 of "Every Finding Ends in an Action" ends at the thread, "Responding and Resolution Expectations" requires the proof and says nothing about where it then lives, and the only pointer to the runbook is scoped to provider mechanics.
  - **Open** - Whether the destination is named in the byte-locked text at all, since a repository is free to keep its record elsewhere and a rule naming one file is a rule that has to be true in every copy.
  - **Settled** - The write side is where the gap bites rather than the read side, because an agent following the loop is already routed to the runbook for mechanics and an agent posting a decline is routed nowhere.
  - **Settled** - "Durable Knowledge and Self-Improvement" already requires durable knowledge to reach a committed file, so this states where one class of it goes rather than adding an obligation.

### A Programmatic Reading of a Copilot Review

One pull request, after a survey, deciding whether anything stands between this fleet's review loop and the raw prose of a Copilot review. Today `scripts/pr_review.py` reads the review body as text and holds a vetted inventory of the headings, collapsed sections, metadata labels and coverage wordings it recognizes, blocking on anything it does not. That design is correct for a prose surface and it carries a cost the maintainer has accepted deliberately: a wording change at GitHub blocks every open pull request in the fleet at once, until the inventory is updated. The cost is worth paying against a reviewer silently missing a raised finding, which is the failure it replaces, but it is worth paying only for as long as prose is the only surface on offer.

**State** `measure`. **Touches** `scripts/pr_review.py` and the runbook section in [`.github/copilot-instructions.md`][copilot-instructions], once the survey says whether there is anything to move to. **Cost** a survey first, then either nothing or a rewrite of the reading layer, which is the larger of the two outcomes and the reason the survey comes first.

- **Find out whether GitHub publishes a structured form of a Copilot review, and decide whether to read that instead of the prose.** A schema, an API surface, a published payload, or a maintained library, anything that would make a wording change a non-event rather than a fleet-wide block.
  - **Blocked by** - Nothing. The prose reader ships either way, so this decides what replaces it rather than whether the loop has a gate.
  - **Issue** - None filed here, and the ask is filed upstream as [GitHub community discussion 204320][copilot-review-schema], which asks for a versioned machine-readable schema carrying severity, category, suggestion and resolution state, rather than the human-facing prose an integration has to infer those from. It is unanswered, so it is a place to watch rather than a dependency to wait on. The prose reader and its vetted inventory shipped under [#607][issue-607], which is the change this would supersede.
  - **Checked** - `develop` at `20916ad` on 2026-08-07, reading the live GraphQL schema by introspection and one review over REST, against the reader in `scripts/pr_review.py`.
  - **Open** - Whether `bodyHTML` is a better surface than the Markdown body, since it arrives as a rendered tree whose structure survives a change in Markdown syntax, while leaving the wording drift the inventory exists for exactly where it is.
  - **Open** - Whether any third-party library tracks this output, and whether depending on one is acceptable at all, given that [`scripts/README.md`][scripts] holds these scripts to the standard library with no third-party packages.
  - **Open** - Whether the review's own inline threads and their metadata carry enough to derive coverage and suppression without reading the body, which would narrow the prose surface rather than replace it.
  - **Settled** - The public API carries no structured Copilot review as of the date above. GraphQL `PullRequestReview` exposes `body`, `bodyText` and `bodyHTML` and no field naming a finding, a file count, or a withheld section, and REST returns the same prose body beside its ids and its state.
  - **Settled** - The only Copilot-named types in the GraphQL schema are `CopilotCodeReviewParameters` and its input form, which configure review-on-push inside a branch ruleset and describe nothing about a review that has run, so the schema search that looks promising by name answers a different question.
  - **Settled** - A negative finding is the deliverable as much as a positive one, and it is recorded here rather than re-derived, since the reading layer's design rests on prose being the only surface and that premise is worth re-checking rather than assuming.

- **Find out which file a partial round skips, and why re-requesting never clears it.** The coverage reading shipped in #608 blocks on a partial round, and the record says the state is durable rather than transient.
  - **Blocked by** - Nothing, though it is research rather than a change, and the reader already reports the state correctly.
  - **Issue** - None filed. The reading that surfaces it shipped under [#607][issue-607].
  - **Checked** - `develop` at `fa1ebf1` on 2026-08-08, measured over the 332 Copilot review bodies on the newest 120 pull requests, read with `gh pr list --json number,changedFiles,additions,deletions` beside them.
  - **Open** - Which file is skipped. The reviewer names no file list in these rounds, so it cannot be recovered from the API, and the GitHub pull request page is the only place it may appear.
  - **Open** - Whether a partial round is worth escalating to GitHub at all, which needs the file first.
  - **Settled** - It is durable rather than flaky. Four pull requests and seven rounds (#476, #479, #592, and the #609 promotion), and **every later round repeated the identical ratio**. A re-request has never cleared one, so the remedy the digest first stated was wrong and now says so.
  - **Settled** - Size does not predict it. The partials changed 502, 629 and 961 lines, while fully covered pull requests here reach 33 files and 2,219 lines.
  - **Settled** - The reviewer counts the file and does not read it, rather than losing it earlier. The stated denominator equals the API's own `changedFiles` on **103 of 104** pull requests, the exception being one whose branch shrank between rounds.
  - **Settled** - Splitting remains a real remedy for a feature branch and is unavailable for a promotion, whose head is `develop`, so a promotion carrying a partial round is a maintainer decision by construction.

## Standalone Chores

Small work with no research to preserve, selectable one bullet at a time.

- **Reconsider whether the pre-commit hook runs the doc gates now that they are diff-scoped.** [`scripts/README.md`][scripts] records the current decision and its reason, that doc linters stay out of the hook so it stays fast, which was sound when the only mode was a whole-tree sweep, and a diff-scoped run finishes in about a second. The failure it would prevent is the most repeated one on record, comment sentences wrapped across lines caught after the commit rather than before it. Weigh it against the standing preference for a fast hook and against a hook that runs the gate from the wrong directory, which is its own false clean.
- **Audit the fleet's shell surface by size and branching, and decide per script whether Python with unit tests is cheaper.** The evidence is the review record rather than a language preference, since a non-trivial shell script earns findings round after round while every gate under [`scripts/`][scripts] carries a test file beside it and converges in one or two. The measure is lines, branch count, and the review rounds each has cost. `repo-config/configure.sh` and the agent-safety installer are the two worth measuring, and a bootstrap script that needs the Python it exists to install is not a rewrite worth having, which protects the installer more than the config script.
- **Make a table of contents standard for a long document rather than for the README alone.** [`spec/readme-structure.md`][readme-structure] fixes one at README position 4 and no other hub file carries one, which leaves the three longest documents without it, `CODESTYLE.md` at 516 lines, `GOVERNANCE.md` at 436 and `WORKFLOW.md` at 301, measured on `develop` at `3d1a0b1` on 2026-08-06. Settle the threshold in headings or lines so the audit can check it, and settle how it sits with the reference-link exception, since the four agent-instruction files keep inline links exactly because they are read one section at a time, which is the property that makes a contents list worth having in them. The mechanical constraint is that the list is filled by the Markdown All in One extension on save, so a file nobody opens in the editor grows a stale list, which is worse than absent because it is read as current.
- **Adopt the OCI annotation keys for Docker image metadata across the Docker repos**, replacing the ad-hoc and label-schema keys, per [#363][issue-363].
- **Sweep the central package-version property to `Directory.Packages.props` fleet-wide**, since PlexCleaner sets it in `Directory.Build.props`, off the [`CODESTYLE.md`][codestyle] canonical.
- **Canonicalize Python linter-config placement on `pyproject.toml`**, since one cataloged repo uses a standalone ruff config plus a pyright config. Track it as a drift finding and fix it downstream.
- **Populate [reports/][reports] for the cataloged repos that still have no audit**, since a registry `status` of `cataloged` asserts a result only a committed report evidences. Nine of 22 have one, measured on `develop` at `3d1a0b1` on 2026-08-06. This is paced by maintainer capacity rather than blocked, since repos are brought up to spec as they are worked on.
- **Finish onboarding hardening**, from [#310][issue-310], making the [`AUDIT.md`][audit-doc] audit a required onboarding step and running the per-type cold-start self-tests tracked in [reports/conformance-matrix.md][matrix]. Every cold-standup cell reads not-tested today.
- **Refresh the README, which has gone stale, and evaluate a lower-maintenance structure**, for example a per-section index pointing into each doc with a one-line description, keeping the README as the adoption and audit-instruction entry point. A per-section index trades brevity for a sync obligation, since it must track what the docs contain.
- **Consider renaming this repo to reflect the audit-catalog identity**, which updates badge and link URLs across the fleet.
- **Revisit automating the audit**, explored and deliberately deferred, recorded so the reasoning is not re-derived. Three shapes were considered, a scheduled hub-driven audit publishing each report as a workflow artifact, the same thing committing the report back, and a pull-request hook in each downstream repo auditing itself against the current hub. Three things block all of them: until the fleet reaches stasis a scheduled run reports mostly noise, since a repo mid-onboarding is expected to be non-conformant, the hub has to be stable before downstreams audit against it because a hub change lands as fleet-wide findings the same day, and the downstream half is a catch-22 since a self-auditing hook is CI instrumentation the repos that most need it do not carry. Worth reopening once the fleet is onboarded and the hub goes a stretch without carried-content changes, and the artifact shape is the one to try first since it produces evidence without committing anything.

## Fleet Sweeps

Work that lands on a downstream visit rather than as a hub pull request, so it is not selectable here. The fleet is caught up periodically rather than after every hub change, which means a carried-content edit landing in the hub does not owe an immediate sweep and this list is expected to carry several entries at once.

Blog is the pilot. A sweep is proven there before any fleet-wide rollout, because it is the smallest tree, `hugo` plus `source-only` with no build to break, cataloged and audited on 2026-08-05, and one of only two repos carrying `AGENTS.md` "Fleet Bootstrap" today, so a carried-section change can be observed arriving there. The other carrier is HomeAutomation-Config, which is `operational` and therefore exercises the direct-to-`develop` path rather than the pull request one, which is the second visit worth making rather than the first.

Regenerate [reports/divergences.md][divergences-report] before using it as the work list, since the committed copy predates the retirement decision and renders `repo-config/configure.sh` under a re-vendor disposition that no longer applies to it. A stale ledger is the same hazard as a stale exemption, in that it hands out a work list measured against a tree that no longer exists.

- **Re-vendor the changed `verbatim` content, which is one sweep covering seven files.** Every repo holding a copy of a changed section is byte-mismatched against the hub until it takes the new one, which the audit reports as stale rather than modified.
  - **Hub state** - Done, verified `develop` at `3d1a0b1` on 2026-08-06 for the sections below, with the prose batch adding five more [`GOVERNANCE.md`][governance] sections, verified `develop` at `d791930` on 2026-08-07.
  - **Outstanding** - The whole fleet, pilot on Blog first.
  - **Issue** - None filed, and it is the follow-through [#489][issue-489] and [#379][issue-379] wait on.
  - **Rides with** - The `configure.sh` retirement and the `.editorconfig` line from [#353][issue-353], since all three are the same visit.
  - **Detail** - In [`AGENTS.md`][agents], "Context and Delegation Discipline" carries the wait rule's failure clause and "Where the Rules Live" carries a row for "Hub-Hosted Tooling".
  - **Detail** - In [`GOVERNANCE.md`][governance], "Verification Discipline" carries the rule that a launched process is not a result and the rule that a change's checks are located before any is run, with CI's coverage not being that list, "PR Review Etiquette" carries the five outcomes that close a finding, "Repository Boundaries and Write Safety" carries the rule that a refused write is reported rather than re-shaped, and both "Representative Data in Agent-Authored Text" and "Hub-Hosted Tooling" are entirely new carried sections no downstream repo holds, which the audit reports as sections that never arrived rather than as drift.
  - **Detail** - Three further [`GOVERNANCE.md`][governance] sections differ by a single word each, "Documentation Style Conventions", "Communicating with the User" and "Repository Details", where a format name took the capitalization [`CODESTYLE.md`][codestyle] "Markdown and Spelling" states, so they are byte-mismatched for a reason a reader of the diff would otherwise call cosmetic.
  - **Detail** - Two comment lines in [`.markdownlint-cli2.jsonc`][markdownlint] took the same capitalization, and that file is `verbatim` and `whole`, so every downstream copy is byte-mismatched on a config nothing else changed about.
  - **Detail** - [`CODESTYLE.md`][codestyle] is the fifth file, at `intent` rather than `verbatim`, so it reaches the fleet as a rule each repo adopts in its own copy, and the same mixed spelling waits in every downstream tree.
  - **Detail** - [`.github/copilot-instructions.md`][copilot-instructions] is the sixth, also at `intent`, where "Reply and Thread Resolution Workflow" now leads with the hub's reply helper and keeps the hand-run mutations as the cross-owner and unreachable-hub path. A repo taking the old copy is not broken by it, since the mutations it documents still work, so this rides the visit rather than gating it.
  - **Detail** - The prose batch rewrote punctuation in five `verbatim` [`GOVERNANCE.md`][governance] sections, "Branching Model", "Release Model", "Documentation Style Conventions", "PR Review Etiquette" and "Workflow YAML Conventions", so every downstream copy of those five is byte-mismatched and the audit reports it as stale. No rule changed meaning, so the re-vendor is a hash refresh rather than a propagation, and a repo taking the old copy is correct on the rule while wrong on the bytes.
  - **Detail** - [`WORKFLOW.md`][workflow] is the seventh file and `repo-config/README.md` joins [`CODESTYLE.md`][codestyle] and [`.github/copilot-instructions.md`][copilot-instructions] at `intent`, where a punctuation-only edit produces no hash and therefore no audit finding at all. Nothing reports these, which is why they are recorded here rather than left to the run. `HISTORY.md` is `presence` and is each repo's own changelog, so its one fix owes nothing downstream.

- **Carry the `Local Verification` heading into every repository's `OPERATIONS.md`.** The heading leads the file and states what verifying a change there requires, naming the part of the repo's contract CI structurally cannot exercise, and a repo whose gates are entirely in CI says that under it rather than omitting it.
  - **Hub state** - Done, verified `develop` at `8e10a2c` on 2026-08-06, where [`spec/section-model.md`][section-model] and [`STANDUP.md`][standup] declare six headings and this repo's own [`OPERATIONS.md`][operations] leads with the section.
  - **Outstanding** - Every repo carrying an `OPERATIONS.md`, which is every repo, since none holds the heading yet.
  - **Issue** - [#597][issue-597], filed from a downstream repo whose pre-merge gate sat under a heading of its own invention and was skipped by an agent following every carried rule correctly.
  - **Rides with** - The `verbatim` re-vendor above, since the carried [`GOVERNANCE.md`][governance] rule that points at the heading lands in the same visit and neither half works alone.
  - **Detail** - The audit reports nothing here today, because `OPERATIONS.md` is presence-checked only, so a repo using none of the declared headings passes. The heading check is [#523][issue-523]'s cluster, "Content in the Wrong File", and until it ships this sweep is verified by reading each file rather than by a run.
  - **Detail** - A repo that already documents a local gate has the content and not the location, so the visit is usually a re-heading rather than new prose, and the prose it does need is the sentence naming what CI cannot reach.

- **Retire the downstream `repo-config/configure.sh` copies.** Delete the copy as each repo is next worked on and run the hub's script against it by name.
  - **Hub state** - Done, verified `develop` at `3d1a0b1` on 2026-08-06, where [`spec/files.json`][files] no longer declares the file and [`spec/divergences.json`][divergences] carries it under the `retire` disposition.
  - **Outstanding** - Six repos, NxWitness, aiopurpleair, homeassistant-purpleair, ESPHome-NonRoot, VSCode-Server-DotNetCore and LanguageTags.
  - **Issue** - None filed, and [#580][issue-580] carries the decision.
  - **Rides with** - The `verbatim` re-vendor above.
  - **Detail** - Nothing asks a repo for the file and nothing reports its absence, which makes this a visit-ordered chore rather than a gate.
  - **Detail** - The six carried a fork predating the payload-driven check mode, which is the drift this removes rather than converges.

- **Drop the `.editorconfig` analyzer relaxation across six C# repos.** The hub side is done and the tree confirms it.
  - **Hub state** - Done, verified `develop` at `3d1a0b1` on 2026-08-06, where the analyzer severity property appears nowhere in `.editorconfig`.
  - **Outstanding** - Six C# repos, sequenced in the issue so PhotoCleaner's 362 sites do not gate the other five.
  - **Issue** - [#353][issue-353], which stays open on the downstream half alone.
  - **Rides with** - The `verbatim` re-vendor above.

- **Close out the two downstream acknowledgements that hold their issues open.** Neither is hub work.
  - **Hub state** - Done, verified `develop` at `1ed0cc8` on 2026-08-03, where the manifest gap [#379][issue-379] raised is closed by `repo-config/settings.json` reaching [`spec/files.json`][files], and the `configure.sh` half has since been retired outright.
  - **Outstanding** - Financial-Modeling's acknowledgement and re-vendor for [#379][issue-379], and the re-vendor [#489][issue-489] leaves.
  - **Issue** - [#379][issue-379] and [#489][issue-489].
  - **Rides with** - The `verbatim` re-vendor above.

- **Widen the operational lint trigger to `develop` on four repos.** Each triggers on a pull request to `main` only and therefore runs nothing at all on a pull request into `develop`.
  - **Hub state** - Done, verified `develop` at `b82c1a3` on 2026-08-05, where the change is prose and spec, so it fixes no downstream repo by itself.
  - **Outstanding** - Four repos, HomeAutomation-Config, ESPHome-Config, HomeAssistant-Config and Vantage-Config, one line each.
  - **Issue** - [#585][issue-585].
  - **Rides with** - Nothing, since an operational repo takes its changes direct to `develop`.
  - **Detail** - Confirm the workflow really does trigger on `main` alone before editing, because a repo already naming both is conformant and needs no change.
  - **Detail** - Leave the ruleset alone, since the required check stays on `main` and nothing is added to `repo-config/operational/develop.json`.
  - **Detail** - The evidence this is not hypothetical is HomeAutomation-Config PR 34, which merged into `develop` with an empty check list and a clean mergeable state.

- **Finish the host rollout and fill the tooling matrix, which are one visit each.** The rollout needs the matrix to be repeatable and the matrix is only worth filling if the rollout uses it.
  - **Hub state** - Done for the documentary half, verified `develop` at `1ed0cc8` on 2026-08-03.
  - **Outstanding** - Four machines, WSL2 Ubuntu, the MacBook Air and both ThinkPads, plus any headless or cron environment running with the token. macOS needs someone on that platform, the Proxmox question is whether that host also runs containers which decides whether Docker is required there, and the engine-inside-the-distro variant of the WSL2 Docker cell is unverified.
  - **Issue** - [#365][issue-365] and [#483][issue-483].
  - **Rides with** - Nothing on the hub, since the write-guard newline fix has landed on `develop` and a machine keeps running the old hook until the installer is re-run there.
  - **Detail** - A ticked row means the host-wide rules text and not the hook, since only running the installer deploys both layers, and the proxmox host proved that distinction by carrying the documentary half alone for eight days on the machine where the incident originated.
  - **Detail** - The prose comment batch rewrote comments in [`gh-write-guard.py`][write-guard] and both installer wrappers, so every installed copy is now behind the hub by that much. The divergence is comment-only and changes no decision the hook takes, which the self-test confirms, so it is a re-run of the installer at the next visit rather than a correctness problem.
  - **Detail** - Honor the issue's own rule when filling a cell, that an unverified install command is worse than a blank, because a blank prompts a question while a wrong command produces a broken host and a false sense that setup succeeded.
  - **Detail** - The superseded safety section from [#364][issue-364] still sits above the canonical block in this host's rules file, so the two overlap. Removing it is a judgment call on a per-machine file, which is why it is surfaced rather than applied.

## Recorded for the Maintainer

Actions on issues that are the maintainer's to take, each carrying its evidence so it is one action rather than a re-derivation.

- **Re-scope [#305][issue-305] to the push half, and make it the tracking issue for the fleet re-vendor sweep.** Most of what it asked for is built, since the fidelity model, the [`spec/files.json`][files] manifest, [`spec/divergences.json`][divergences] with its generated [reports/divergences.md][divergences-report], and [`AUDIT.md`][audit-doc] section 10 together give the canonical-versus-adapted split and the audit path it proposed. What is genuinely still missing is the push half, since every one of those detects drift while the sweep that fixes it is manual. Re-scoped, it carries the "Fleet Sweeps" visit manifest and Blog as the pilot. Closing it against the built machinery is the alternative, and it loses the only tracking issue the sweep would have.
- **Comment on [#577][issue-577] that it is decided together with the declared description.** Declaring the field in [`registry/repos.json`][repos] makes every mirror read a field rather than parse a paragraph, so taking [#577][issue-577] first means writing an extraction rule the registry change then deletes.

## Verified Complete, Awaiting Close

Each was checked against the tree and has nothing left to do anywhere. Closing is the maintainer's call, and each wants the evidence quoted in the closing comment rather than a bare close.

- **[#519][issue-519], the hub's own tree does not pass the prose gate it ships.** Complete on the prose and on both questions.
  - **Fixed by** - `f7a6a13` (snippets), `c9c92dd` (comments), `d791930` (hub-only Markdown), and the carried batch on `prose/carried-semicolons`.
  - **Checked** - `develop` at `d791930` on 2026-08-07, where `python3 scripts/prose_lint.py --summary` reported 41 across 6 files, and 0 across 0 with the carried batch applied.
  - **Closing evidence** - The whole-tree figure went 557 across 45 to zero, in four batches split by surface, being 184 in `catalog/snippets/`, 241 in non-Markdown comments, 90 in hub-only Markdown and 41 in the six carried files. Question 1 is answered by `reports/` being exempt as a generated tree, and question 2 by the snippets leading, since a non-conformant snippet seeds its violations into every repo that adopts it.
  - **Closing evidence** - The issue's claim that the governance files were clean, and that this was therefore not a carry problem, was true of the checker of the day and false of the tree. Today's checker reports 38 findings against the same six files as they stood at `69688ec`, the commit the issue measured, while that commit's own checker reports zero. Scoping the list exemption to a sentence rather than a whole bullet accounts for 37 of the 38, because a colon anywhere ahead of the first semicolon had exempted every semicolon after it. The carry problem was real throughout and invisible, which is the stale-exemption hazard running in the loose direction.

- **[#557][issue-557], the agent-isolation rule and its two open questions.** Complete on the rule and on both questions.
  - **Fixed by** - `9d85941`.
  - **Checked** - `develop` at `9d85941` on 2026-08-06.
  - **Closing evidence** - [`GOVERNANCE.md`][governance] "Repository Boundaries and Write Safety" carries the rule that each task runs in its own checkout, in its own directory, on its own feature branch, scoped per task rather than per agent, which adopts the first open question. It names the three commands that cross the boundary while being correct in isolation, and it names the two signals that another task is live in a tree, whose stated response is to stop, which adopts the second. The rule reaches every machine through the same text in [`host-setup/agent-safety/`][agent-safety].

- **[#579][issue-579], a repository with no instruction set cannot resolve its own vocabulary.** Complete on the term definitions and on the routing.
  - **Fixed by** - `9d85941`.
  - **Checked** - `develop` at `9d85941` on 2026-08-06.
  - **Closing evidence** - [`AGENTS.md`][agents] "Fleet Bootstrap" names the hub as a defined term and carries the reach rule, and [`README.md`][readme] defines the six words a request is phrased in, the hub, the fleet, standing a repository up, auditing one, closing the review loop, and carried against reached, each naming the file that answers it. The host-wide block under [`host-setup/agent-safety/`][agent-safety] carries the same definition, which is what reaches a repository holding no instruction set at all.

<!-- Issues -->

[issue-305]: https://github.com/ptr727/ProjectTemplate/issues/305
[issue-310]: https://github.com/ptr727/ProjectTemplate/issues/310
[issue-353]: https://github.com/ptr727/ProjectTemplate/issues/353
[issue-363]: https://github.com/ptr727/ProjectTemplate/issues/363
[issue-364]: https://github.com/ptr727/ProjectTemplate/issues/364
[issue-365]: https://github.com/ptr727/ProjectTemplate/issues/365
[issue-379]: https://github.com/ptr727/ProjectTemplate/issues/379
[issue-456]: https://github.com/ptr727/ProjectTemplate/issues/456
[issue-483]: https://github.com/ptr727/ProjectTemplate/issues/483
[issue-489]: https://github.com/ptr727/ProjectTemplate/issues/489
[issue-509]: https://github.com/ptr727/ProjectTemplate/issues/509
[issue-519]: https://github.com/ptr727/ProjectTemplate/issues/519
[issue-521]: https://github.com/ptr727/ProjectTemplate/issues/521
[issue-523]: https://github.com/ptr727/ProjectTemplate/issues/523
[issue-550]: https://github.com/ptr727/ProjectTemplate/issues/550
[issue-557]: https://github.com/ptr727/ProjectTemplate/issues/557
[issue-558]: https://github.com/ptr727/ProjectTemplate/issues/558
[issue-577]: https://github.com/ptr727/ProjectTemplate/issues/577
[issue-578]: https://github.com/ptr727/ProjectTemplate/issues/578
[issue-579]: https://github.com/ptr727/ProjectTemplate/issues/579
[issue-580]: https://github.com/ptr727/ProjectTemplate/issues/580
[issue-585]: https://github.com/ptr727/ProjectTemplate/issues/585
[issue-597]: https://github.com/ptr727/ProjectTemplate/issues/597
[issue-607]: https://github.com/ptr727/ProjectTemplate/issues/607

<!-- Pull requests -->

[pr-591]: https://github.com/ptr727/ProjectTemplate/pull/591

<!-- Upstream -->

[copilot-review-schema]: https://github.com/orgs/community/discussions/204320

<!-- Repo -->

[agent-safety]: ./host-setup/agent-safety/
[agents]: ./AGENTS.md
[audit]: ./spec/audit.py
[audit-doc]: ./AUDIT.md
[codestyle]: ./CODESTYLE.md
[copilot-instructions]: ./.github/copilot-instructions.md
[divergences]: ./spec/divergences.json
[divergences-report]: ./reports/divergences.md
[files]: ./spec/files.json
[governance]: ./GOVERNANCE.md
[markdownlint]: ./.markdownlint-cli2.jsonc
[matrix]: ./reports/conformance-matrix.md
[merge-bot]: ./.github/workflows/merge-bot-pull-request.yml
[operations]: ./OPERATIONS.md
[project-types]: ./spec/project-types.json
[prose-gate]: ./.github/actions/prose-gate/action.yml
[readme]: ./README.md
[readme-structure]: ./spec/readme-structure.md
[reports]: ./reports/
[repos]: ./registry/repos.json
[scripts]: ./scripts/README.md
[secrets]: ./spec/secrets.json
[secrets-schema]: ./spec/secrets.schema.json
[section-model]: ./spec/section-model.md
[snippets]: ./catalog/snippets/
[standup]: ./STANDUP.md
[type-model]: ./spec/type-model.md
[workflow]: ./WORKFLOW.md
[workflows]: ./catalog/snippets/workflows/
[write-guard]: ./host-setup/agent-safety/gh-write-guard.py
