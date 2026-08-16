# TODO

Running backlog for this repo, kept in a committed file so the research survives across environments where agent memory does not. Entries are grouped by the change that ships them, so a `###` heading under "Work Clusters" is one pull request, and selecting work is reading the cluster headings rather than re-deriving the grouping from the entries. What each cluster touches and costs is a field on the cluster, since a cluster confined to one surface and a cluster spanning two are both legitimate and only the second needs saying.

An entry carries `Blocked by`, `Issue` and `Checked` exactly once each, in that order, and never omits one, because an omitted field reads as unknown rather than as none. `Open` states a decision the session doing the work makes, and `Settled` states a finding that is not re-derived, each carrying a number, a proper name, or a rejected alternative. `Checked` is the freshness anchor, naming the branch, the commit, and the date a claim was last read against the tree, so a claim older than the branch is a claim rather than a finding.

A cluster's `State` is one of four. `ready` means every open question is answerable by the session doing the work. `blocked` names the cluster it waits on. `decision` needs the maintainer. `measure` means the first action is a count rather than an edit.

Adoption gaps for the skill-based fleet system are registered in [`docs/fleet-map.md`][fleet-map] rather than here, so the two files do not fork. A new observation about such a gap lands as a register row there, and this file carries only the pointer.

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

### Default `.py` to LF Fleet-Wide, Retiring the Per-Path Pin List

One pull request changing [`GOVERNANCE.md`][governance] "Line Endings", [`.editorconfig`][editorconfig], and [`.gitattributes`][gitattributes] to pin `*.py` LF by extension, replacing the growing list of individually-pinned shebang scripts, plus the one-time renormalization it obliges fleet-wide.

**State** `decision`. **Touches** `GOVERNANCE.md` "Line Endings" (verbatim, so it re-vendors fleet-wide), `.editorconfig`, `.gitattributes`, and every downstream repo carrying a CRLF `.py` file. **Cost** one hub edit plus a renormalization pass per affected repo. The hub itself needs no renormalization, since every `.py` file it tracks is already LF.

- **Pin `*.py text eol=lf` by extension and drop the by-path list it replaces.** The by-path list exists because the fleet's default is CRLF for `.py` and only a shebang-executed script needs LF, so each new script has needed its own `.gitattributes` line and its own `.editorconfig` override. It reads 25 entries today, up from the roughly dozen it carried before this session added four more for two new scripts and their tests, which is the divergence outweighing the reason it was chosen.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - `develop` at `7f9caaa` on 2026-08-12, where `.gitattributes` carries 25 LF pins (3 forward-declared) over 19 tracked `.py` files, and every one of those 19 is already LF (none is a vanilla CRLF `.py`), so the hub side of this change is comment-and-pattern-only.
  - **Open** - Whether downstream repos with a vanilla (non-shebang) CRLF `.py` file need a coordinated renormalization pass or can pick it up on their own next resync. 5 repos in [`registry/repos.json`][repos] carry the `python` type (`aiopurpleair`, `homeassistant-purpleair`, `Financial-Modeling`, `PlexCleaner`, `ESPHome-Config`) and were not individually checked for CRLF `.py` content as part of writing this entry.
  - **Settled** - The default was set to CRLF in #229 for editor compatibility on Windows, not because LF broke anything measured. The reason it is being revisited is that non-VSCode Windows editors were the concern, and the per-path pin list's growth now outweighs that concern's practical weight, per the maintainer.
  - **Settled** - The by-path convention was reaffirmed in #503 ("Do not re-add a blanket `*.py text eol=lf`"), but that pull request only reshaped the two files' comment prose and did not re-examine the underlying policy, so it is not a second, independent rejection of this change.

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

### The Declared Repository Description

One pull request moving the canonical short description into declared data, so every check and every push reads a field rather than parsing a document, and the About panel gets something that writes it.

**State** `decision`. **Touches** [`registry/repos.json`][repos] and its schema, [`spec/audit.py`][audit], and `repo-config/configure.sh`. **Cost** one hub edit, and repos adopt the field one at a time. The tagline rule this cluster once carried shipped on 2026-08-08.

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

- **Close the README-to-About hop, which is the only one nothing writes.** The audit reports a drifted About panel, and no tool sets it.
  - **Blocked by** - The entry above, since the field is what `repo-config/configure.sh` would set the panel from.
  - **Issue** - [#639][issue-639], filed on 2026-08-09 because this entry had been carrying [#577][issue-577], whose body covers only the README tagline and never mentions the About panel, and whose tagline half shipped on 2026-08-08.
  - **Checked** - `develop` on 2026-08-08, where `repo-config/configure.sh` sets every other repository setting and carries no `description` handling, and [`catalog/snippets/workflows/publish-docker-readme-task.yml`][workflows] pushes `github.event.repository.description` to Docker Hub.
  - **Open** - Nothing beyond sequencing.
  - **Settled** - The chain is README, then the About panel by hand, then Docker Hub by CI, so the unautomated hop is the first one and it is the one that drifts. PhotoCleaner is the worked case, where the About panel still matched the README and only the Docker Hub short description had diverged.
  - **Settled** - CI keeps reading `repository.description` rather than the README. Pointing it at the README puts a Markdown parser in a publish job, which PhotoCleaner#32 measured at nine guards, every one of which fails the release rather than the tagline.
  - **Settled** - The tagline rule itself shipped on 2026-08-08 and is no longer owed here. The extraction rule this entry was once blocked on already existed: [`spec/audit.py`][audit] measured the first line for the About and Docker Hub mirrors all along, and narrowing the `HISTORY.md` mirror to match it was one line, so the sequencing that held the rule behind the registry field was stated more strongly than the code warranted.

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

**State** `decision`. **Touches** [`AUDIT.md`][audit-doc], [`spec/secrets.json`][secrets], and [`spec/files.json`][files]. **Cost** one hub edit plus a retirement per repo on its next visit. The workflow half of this cluster, replacing copy-pasted workflow content with cross-repo reuse, is measured and answered under "Hub-Hosted Reusable Workflows" below.

- **Measure carried [`AUDIT.md`][audit-doc] and [`spec/secrets.json`][secrets] against the test.** Each is adapted per repo today and the question is how much of each is genuinely per-repo.
  - **Blocked by** - Nothing.
  - **Issue** - None filed, and [#305][issue-305] covers the propagation half from the other direction.
  - **Checked** - `develop` at `3d1a0b1` on 2026-08-06, where [`spec/files.json`][files] declares both at `intent` and no longer declares `repo-config/configure.sh` at all.
  - **Open** - Which of the two moves, if either.
  - **Settled** - The test is stated: a repository carries the content it is audited against and the configuration that describes it, and it reaches machinery whose content is identical in every repository.
  - **Settled** - `repo-config/configure.sh` is the first file moved across, carrying the ledger's only `retire` disposition and naming six repos, NxWitness, aiopurpleair, homeassistant-purpleair, ESPHome-NonRoot, VSCode-Server-DotNetCore and LanguageTags.
  - **Settled** - An unreachable hub means the tool did not run, reported as not run rather than worked around, since a hand-rolled substitute is the duplicated effort the model exists to end.

### The README Structure Rework

The spec rework and its audit check shipped. What remains is the per-repo conformance the check now reports, and one section the fleet carries that the model does not name.

**State** `decision`, on where `## Build Artifacts` belongs, which is the only thing here a hub pull request settles. The four conformance entries above it are not selectable as hub work at all: each lands on a repo's own next visit, in the sense "Fleet Sweeps" below gives that phrase, and they sit here rather than there because the finding counts are what the shipped check measures. **Touches** each repo's `README.md` on its next visit, plus [`spec/readme-structure.md`][readme-structure] and [`spec/readme-sections.json`][readme-sections] if `Build Artifacts` is adopted. **Cost** one edit per repo, driven by the finding rather than by a sweep.

- **Work off the conformance backlog the `readme-structure` dimension now reports.** Measured across all 22 cataloged repos on 2026-08-08, against the shipped checks: 73 findings, 71 on sections and 2 on shields, plus the 3 retired-badge findings the entry below carries.
  - **Blocked by** - Nothing, and no repo is edited by the hub. Each lands on its own next visit.
  - **Issue** - None filed.
  - **Checked** - Every repo's default branch on 2026-08-08, with the hub read at its own `develop`.
  - **Settled** - The shape of the work: 17 repos owe `3rd Party Tools`, 10 owe the `Overview` rename, 9 owe a Table of Contents, 7 owe a License section, and 5 public repos owe `Questions or Issues`.
  - **Settled** - Three order findings are genuine and each is one move: LanguageTags places Installation after Usage, aiopurpleair places Getting Started after Installation, and PlexCleaner places Questions or Issues immediately after the Table of Contents where the order now puts it ninth.
  - **Settled** - Two placement findings are genuine: KiCadLibrary carries a `## TODO` after `## License`, which the "TODO.md" rule already forbids, and HomeAutomation-Config renders the license shield twice, once outside the License section.
  - **Settled** - MediaTools carries a `NuGet Pre-Release` shield that renders the same version as its `NuGet Release` shield, and it is dropped on that repo's next visit. The check does not report it, because a shield class is a floor and an extra shield is never a finding.
  - **Settled** - Blog is the only repo carrying a `3rd Party Tools` table today, and it needs both fixes the rule now states: drop the License column, which the audit reports since 2026-08-09, and rewrite all three roles, since "theme, vendored under `themes/`" and "web server, serving the built site and the redirects" describe this repo's wiring where "static site generator" describes the tool correctly and differs only by its opening capital and its full stop.

- **Bring each repo's `3rd Party Tools` entries onto the shared catalog.** Measured on 2026-08-09: 57 findings across four repos, a link, a description, or an ordering that disagrees with [`spec/third-party-tools.json`][third-party-tools], plus the one License column [`spec/readme-structure.md`][readme-structure] forbids.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - Every repo's default branch on 2026-08-09, with the hub read at its own `develop`, which now conforms.
  - **Settled** - The bulk is absent descriptions rather than wrong ones: 48 of the 57 are a tool listed with no description at all, across LanguageTags, MediaTools and PlexCleaner, and PlexCleaner alone accounts for 25 of those and 27 findings overall. Of the remaining nine, three describe a tool differently from the catalog, four link it differently, one is Blog listing Hugo, PaperMod, Caddy out of alphabetical order, and one is Blog's License column.
  - **Settled** - Twelve tools already appear in more than one repo, which is what makes the catalog worth having before the 17 repos owing the section write their own wording for each.
  - **Settled** - Four tools are already linked by two different URLs across the fleet, and the catalog picks one each: GitHub Actions takes `github.com/actions`, Dependabot takes `github.com/dependabot`, Nerdbank.GitVersioning takes the project repo rather than its marketplace action, and uv takes `docs.astral.sh/uv/` to match ruff. The hub was the outlier on the first two and is fixed.
  - **Settled** - PlexCleaner lists Bring Your Own Badge as a tool, so the retired badge service has a fourth touchpoint beyond the three rendering it, and that entry goes with the same deletion.
  - **Settled** - The catalog is a standard set and not a complete one, so a tool only one repo uses is unaudited. Of the 36 tools the fleet lists today, 24 are used by exactly one repo and are declared only so the second adopter copies rather than invents.

- **Work off the reference-link naming and grouping backlog.** Measured across all 22 repos on 2026-08-08: 55 letter findings on naming and 27 drift findings on grouping.
  - **Blocked by** - Nothing, and each repo's block is one edit.
  - **Issue** - None filed.
  - **Checked** - Every repo's default branch on 2026-08-08, with the hub read at its own `develop`, which now conforms.
  - **Settled** - The naming half was already the fleet's practice before it was written down: 119 of 122 shield references end `-shield` and 514 of 532 URI references end `-link`, and `actions-link`, `releases-link`, `issues-link` and `discussions-link` are unanimous across every repo carrying them.
  - **Settled** - The two real naming inconsistencies are the repository root, which 10 of 20 call `github-link` and the rest name for the project, and `./LICENSE`, which 9 repos call `license-link` where a repo-local path is a bare reference.
  - **Settled** - The grouping half is drift rather than letter because it is not met: the fleet carries seventeen distinct group-header names, and two repos, NxWitness with 116 definitions and ESPHome-NonRoot with 45, carry no group headers at all.
  - **Settled** - KiCadLibrary is the largest single block at 22 naming findings, almost all of them repo-local paths named `-link`.

- **Delete the retired `byob.yarr.is` last-build badge from the three repos still carrying it.** The service is deprecated and the badge is not required by any shield class, so the fix is a deletion rather than a replacement.
  - **Blocked by** - Nothing, and each repo's fix is deleting one shield line and one reference definition.
  - **Issue** - None filed.
  - **Checked** - Each repo's default branch on 2026-08-08, with the endpoints requested the same day: MediaTools and KiCadLibrary both return **HTTP 404**, so they already render a broken badge, and ESPHome-NonRoot still returns 200.
  - **Settled** - The audit reports it, so this does not rely on anyone remembering: `deprecatedShields` in [`spec/readme-sections.json`][readme-sections] carries the retired service and the check fires on exactly those three repos.
  - **Settled** - A dead badge is worse than an absent one, because it renders broken rather than missing and a visitor cannot tell a retired service from a failing build.
  - **Settled** - All three repos are already non-conformant on other grounds, so this rides their next visit rather than earning a pass of its own.

- **Decide where `## Build Artifacts` belongs.** LanguageTags and aiopurpleair both carry it, opening with the same `**Build process and artifacts**:` line and covering package, versioning, and publishing.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - Both repos' default branches on 2026-08-08, where the section is the only one recurring across repos that [`spec/readme-sections.json`][readme-sections] does not name.
  - **Open** - Whether it becomes a named optional section, folds into `Build and Distribution`, or moves to [`WORKFLOW.md`][workflow], since its content overlaps both.
  - **Settled** - It is not a finding today. An unnamed heading is dropped before the order comparison, so the two repos carrying it pass, which is why this is a decision rather than a defect.

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

### Hub-Hosted Reusable Workflows

One pull request per stage moving a standard workflow out of every repo and into the hub as a `workflow_call` task, with a downstream caller stub and a composite-action hook for what is genuinely repo-specific. The design, the hook contract, the pin policy and the staged rollout are in [`docs/reusable-workflows.md`][reusable-workflows-doc], and the burn-down is [`reports/workflow-reuse.md`][workflow-reuse-report], regenerated by `python3 spec/workflow_reuse.py --report`. The merge-bot task shipped with the design and its adoption is a sweep, so this cluster starts at the gates. The stage-by-stage completion state, one checkbox per hub change and per adopting repo with the evidence that closed it, is that doc's "Rollout" section, and a session resumes from there rather than from here.

**State** `ready` for the gates, `blocked` on the gates for everything after. **Touches** the hub's `.github/workflows/`, [`spec/files.json`][files], [`catalog/snippets/workflows/`][workflows], and [`WORKFLOW.md`][workflow] where a guarantee names a copied job. **Cost** one hub edit per stage plus an adoption per repo on its next visit, and no re-vendor beyond the stub each stage introduces.

- **Host the gates: `validate-task.yml` with a `validate` hook, and `test-pull-request-task.yml` with the fixed aggregator.** The hub owns the per-type doc-lint block once, the hook carries a repo's own tests, and the stub carries the trigger shape, operational or release. This stage is where the hook fallback is first proven live, on the hub for the default and on a pilot for the override.
  - **Blocked by** - Nothing.
  - **Issue** - None filed. [#585][issue-585] and [#729][issue-729] are settled inside this stage, the first by the operational stub's trigger and the second by the one place the hub validate task pins or floats its `uvx` tools.
  - **Checked** - `develop` at `7c67328` on 2026-08-15, where the report counts 20 copies of `test-pull-request.yml` in 13 variants and 13 copies of `validate-task.yml` in 11, and the doc-lint block (markdownlint, cspell, actionlint, editorconfig-checker) repeats in every one.
  - **Open** - Whether the per-type lint steps are selected by an input the stub sets or read from the repo's registry entry through a hub checkout at `github.job_workflow_sha`, since the second needs no per-repo input and the first needs no network read.
  - **Open** - Whether a `validate` hook that runs a domain compile (an ESPHome build, a KiCad ERC) is one hook or several, given the two repos carrying such a step run it as a separate job today.
  - **Settled** - Pilots are PhotoCleaner, which piloted the merge-bot stub in ptr727/PhotoCleaner#53 on 2026-08-15 as a release-model repo with Dependabot, C#, executable and Docker targets, then HomeAutomation-Config for the operational trigger shape, so both shapes are exercised before the sweep.
  - **Settled** - The step gated on `hashFiles('.github/actions/validate/action.yml') != ''` runs the caller's hook from its own checkout, else the default from a hub checkout under `.hub/`, and a local composite action resolves at step time from the workspace, which is what makes the fallback expressible at all.

- **Host the pure functions: `get-version-task.yml` and `publish-plan-task.yml`.** Neither has a repo-specific line, and the plan job is missing where D4.1 needs it.
  - **Blocked by** - The gates, only for sequencing, since a repo adopts one stub per visit and the gates come first.
  - **Issue** - None filed.
  - **Checked** - `develop` at `7c67328` on 2026-08-15, where 5 of 8 `get-version-task.yml` copies are identical and all 3 `publish-plan-task.yml` copies are.
  - **Open** - Nothing.
  - **Settled** - PlexCleaner carries no `plan` job, so its next scheduled run ships a Dependabot bump, and the hub-hosted plan job is the fix rather than a per-repo copy.

- **Host the release chain: `build-release-task.yml` with `build-<target>` hooks, `publish-release-task.yml`, and the Docker core.** The orchestration is generic and the target list is per repo, which the hooks express without a per-repo copy of the orchestrator.
  - **Blocked by** - The pure functions, since the release task calls both.
  - **Issue** - None filed.
  - **Checked** - `develop` at `7c67328` on 2026-08-15, where the report counts 10 copies of `build-release-task.yml` in 6 variants, 17 of `publish-release.yml` in 12, and 5 of `build-docker-task.yml` in 4, with the Docker core identical in every copy.
  - **Open** - The `matrix` input shape for a multi-image Docker repo, and whether a base-image build is a hook or a second task the stub calls first.
  - **Settled** - The three no-asset release shapes the fleet runs today collapse into `expect_release_assets`, and PhotoCleaner and PlexCleaner pilot, then the NuGet, PyPI and Docker-only repos.
  - **Settled** - Vanilla Docker repos need only `image` and build-args, ESPHome-NonRoot adds a `docker-prepare` hook for its upstream pin, and NxWitness adds the matrix hook and `build-base`, in that order.

- **Host the type-specific tasks: Docker Hub readme, upstream-version tracking, deploy-site, codegen, and the date badge.** Each with its hook, and the two string-command inputs the catalog carries today (`transform-run`, `resolver-command`) become hooks.
  - **Blocked by** - The release chain, since the readme task replaces the in-job description push.
  - **Issue** - None filed.
  - **Checked** - `develop` at `7c67328` on 2026-08-15, where each of these has one or two carriers.
  - **Open** - Whether ESPHome-NonRoot's second tracker, whose bump waits for a human, is the same task with `auto-merge: false` or stays repo-local.
  - **Settled** - The `operational-vs-release-workflow` skill's note that a target-agnostic target list is "intentionally not done" is retired by this stage rather than before it, since it is true until then.

- **Decide the three merge-bot inputs the design leaves open.** The `delete-branch` default, the Dependabot semver-major filter two repos carry, and a `requiredHubUses` audit contract.
  - **Blocked by** - Nothing, and each is a maintainer call rather than a finding.
  - **Issue** - None filed.
  - **Checked** - `develop` at `7c67328` on 2026-08-15, against the 16 downstream copies read for the design.
  - **Open** - All three, stated in [`docs/reusable-workflows.md`][reusable-workflows-doc] "Open Decisions".
  - **Settled** - Neither blocks adoption: `delete-branch: false` is the hub's behavior and seven repos opt in, and the semver filter is a D8.1 conformance question for the two repos that carry it.

- **Bring the Docker repos onto one multi-stage Dockerfile shape.** The build stages are inconsistent across the five Docker repos, and that is Dockerfile content rather than workflow content, so it rides beside the workflow migration rather than inside it.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - Not measured. Raised by the maintainer on 2026-08-15 while reviewing the Docker family design, and the first action is a read of the five Dockerfiles.
  - **Open** - Whether the shape is a `CODESTYLE.md` section, a `docker` type check, or both.

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
  - **Issue** - [#623][issue-623], filed from a downstream repository against the `PARTIAL` caveat's claim that the reviewer names no file list. The reading that surfaces the state shipped under [#607][issue-607].
  - **Checked** - `develop` at `674a27a` on 2026-08-08, measured over 348 Copilot review bodies on the newest 120 pull requests here and 121 on the fleet's Blog repository, each read against the pull request's own changed-file list rather than against its counts alone.
  - **Settled** - The reviewer does name a file list, and the caveat saying otherwise was wrong. It is a `| File | Description |` table carried by 91 of the 348 bodies, and every table row in the corpus belongs to one of those tables.
  - **Settled** - The table names the unread file on exactly one round of the seven, which states 16 of 17 and names 16, omitting `GOVERNANCE.md`. That round is also the only evidence on record that the unread file is a real file rather than an artifact of counting.
  - **Settled** - It cannot be read as coverage anywhere else. It names the whole changed set on partial and fully covered rounds alike, including all seven partials on Blog, while one round here states 61 of 62 and names 50, another states 33 of 33 and names 32, and a third names `GOVENANCE.md`, the reviewer's own spelling and a path no diff carries. A reading identical under both outcomes discriminates neither.
  - **Settled** - Three of the four partials here carry their table on the round before a push, describing the diff that push replaced, so the comparison is head-scoped like the counts and reports no table rather than a stale list of unreviewed files.
  - **Open** - Whether a partial round is worth escalating to GitHub at all. One named file on one round is a starting point rather than the pattern an escalation needs.
  - **Settled** - It is durable rather than flaky. Four pull requests and seven rounds (#476, #479, #592, and the #609 promotion), and **every later round repeated the identical ratio**. A re-request has never cleared one, so the remedy the digest first stated was wrong and now says so.
  - **Settled** - Size does not predict it. The partials changed 502, 629 and 961 lines, while fully covered pull requests here reach 33 files and 2,219 lines.
  - **Settled** - The reviewer counts the file and does not read it, rather than losing it earlier. The stated denominator equals the API's own `changedFiles` on **103 of 104** pull requests, the exception being one whose branch shrank between rounds.
  - **Settled** - Splitting remains a real remedy for a feature branch and is unavailable for a promotion, whose head is `develop`, so a promotion carrying a partial round is a maintainer decision by construction.

### A Resolve the Loop Cannot Perform and a Thread Nobody Can Find

The review loop ends by replying on a thread and resolving it, and both halves failed on one pull request in ways the runbook describes nowhere. The resolve mutation was refused by the agent harness's own permission layer before any request left the machine, seconds after the reply mutation carrying the identical thread id had succeeded, so the refusal was neither GitHub's nor the id's. Handing the resolve to the maintainer then failed a second time, because the digest names a thread by its `PRRT_` node id, that id appears nowhere in the GitHub interface, and the person asked to resolve it could not find what to click.

**State** `ready`. **Touches** `scripts/pr_review.py`, the runbook section in [`.github/copilot-instructions.md`][copilot-instructions], and [`OPERATIONS.md`][operations]. **Cost** one pull request, since the query change is one field and the runbook change is one paragraph.

- **Carry a thread's own web address beside its node id, so a resolve can be handed to a person.** `Q_THREADS` selects `id`, `isResolved`, `path`, `line` and the first comment's `author` and `body`, and not its `url`, so the digest can name a thread and cannot point at it. Selecting `url` and printing it beside the id makes the hand-off one click.
  - **Blocked by** - Nothing.
  - **Checked** - `develop` at `0e4a1c2` on 2026-08-08, reading `Q_THREADS` in `scripts/pr_review.py` against the digest line that consumes it.
  - **Detail** - The two identifiers are not interchangeable and neither is derivable from the other without a query. A `PRRT_` node id is what a mutation takes, and a `#discussion_r` fragment is what the web page anchors on.
  - **Detail** - The evidence is [#620][pr-620], where a thread was handed over by node id and the reply was that it could not be found.

- **Give the runbook a shape for a write the harness refuses, which it currently has none for.** Its list of dead paths is entirely GitHub's own refusals, a silent no-op, a 422, and the wrong bot login for the API in use, so a local refusal matches none of them and reads as a bad identifier, which invites the retry a blocked write must never get.
  - **Blocked by** - Nothing.
  - **Checked** - `develop` at `0e4a1c2` on 2026-08-08, against the known-non-working-paths list in the runbook.
  - **Detail** - The distinguishing evidence is that a reply on the same thread id, in the same session, had already succeeded and returned a comment url, so the identifier was demonstrably good.
  - **Detail** - What cleared it was a permalink and a human click, and the durable remedy is a permission rule in host settings. That is host state rather than repo content, so it belongs in the runbook as a note rather than in a committed configuration file.

- **Confirm a resolve by re-reading the thread rather than by the mutation returning.** `reply` already exits 63 where the resolve did not report the thread resolved, which is the right shape, and a loop driving `gh api` by hand gets no exit code at all and so cannot notice. The rule worth writing down is that the state is the evidence.
  - **Blocked by** - Nothing.
  - **Checked** - `develop` at `0e4a1c2` on 2026-08-08, reading the exit-code table in the `scripts/pr_review.py` module docstring.
  - **Detail** - This is the failure the suppressed-findings count already exists for, where a step that stopped running reads exactly like a step that passed.

### Watching a Downstream Pull Request Touch Hub-Owned Content

One pull request adding the observer the fleet has no equivalent of, reading merged pull requests across the fleet and resolving every changed path against what the hub declares it owns. The tools today read standing state, so a divergence is visible only once it is already there, and a repo-local file the manifest never names is invisible at every stage.

**State** `ready`. **Touches** a new `spec/carry_watch.py` with its self-test, [`reports/`][reports], [`AUDIT.md`][audit-doc], and [`.github/workflows/validate-task.yml`][validate-task]. **Cost** one hub script, hub-only, plus a first run whose output is a triage backlog rather than a change.

- **Read merged fleet pull requests and classify each changed path against the manifest.** The gap is a whole reading rather than a missing field, since nothing anywhere enumerates pull requests.
  - **Blocked by** - Nothing.
  - **Issue** - None filed. [#633][issue-633] is the instance that prompted it, raised by a downstream agent after the maintainer noticed it editing hub-managed CI files, and nothing mechanical had reported that.
  - **Checked** - `develop` at `c2ce145` on 2026-08-08, reading [`spec/files.json`][files], [`spec/divergences.json`][divergences] and [`registry/repos.json`][repos], and running the enumeration query live against the owner.
  - **Open** - How a window wider than a thousand results is split, since the GitHub search API caps there and a silent truncation is the false clean this whole class of tool exists against. The split has to be visible in the output rather than inferred.
  - **Open** - Whether a `SECTION` or `CONTRACT` classification reads content in the same pass or defers to a human, since the path alone says a carried file moved and not which region of it.
  - **Settled** - The enumeration is one query rather than a per-repo loop, measured live: `search(query: "org:ptr727 is:pr is:merged base:main merged:>=<DATE>", type: ISSUE)` returned 112 pull requests over a fortnight with per-pull-request `files` and `repository` inline. The `base:` term comes from each repo's registry `groundTruthBranch` rather than a hardcoded `main`.
  - **Settled** - It belongs in `spec/` beside [`spec/fidelity_honesty.py`][fidelity-honesty], which is its sibling in every respect that decides placement, being owner-initiated, absent from CI, an importer of `audit` as a library, and a writer of a generated report. [`scripts/`][scripts] holds gates run against one repo named by `--root`.
  - **Settled** - The classes are `OVERSTEP` for a `verbatim` whole unit, `SECTION` for a path declaring verbatim sections, `CONTRACT` for an `interface` unit, `GAP` for a path the hub tracks that the manifest never names, and `CANDIDATE` for a path absent from the hub changed in a pull request that also touched one of the others.
  - **Settled** - `intent` and `presence` units are deliberately not watched, being downstream-owned by design, and that exclusion is what makes suppression keyed on the ledger correct rather than over-broad.
  - **Settled** - `GAP` plus `CANDIDATE` is the pair that means a downstream wired local tooling into a workflow the hub authored, which is exactly `ptr727/Blog#69`: it changed `.gitattributes`, `.github/workflows/validate-task.yml` which is a ledger `gaps` entry dispositioned `investigate`, and a `checks/check-eol-pins.py` the hub has never heard of. A sibling pull request shows the same shape over a whole `checks/` tree.
  - **Settled** - Triage needs no new store. [`spec/divergences.json`][divergences] already carries `upstream-candidate` in its disposition vocabulary, meaning the downstream carries an improvement the hub should adopt, and nothing in the ledger uses it today. A dispositioned pair prints with its disposition and everything else renders `UNTRIAGED`, which is what [`reports/divergences.md`][divergences-report] already does.
  - **Settled** - The rule goes in [`AUDIT.md`][audit-doc] section 9 rather than [`GOVERNANCE.md`][governance], whose sections are verbatim fleet law, so an edit there puts every downstream repo into verbatim drift until re-vendored for a sentence that is procedure rather than law.
  - **Settled** - Two floors are not optional. A run reading zero pull requests reports that rather than a clean zero, and a repo the search surfaces with no registry entry is reported separately, which feeds "Registry Membership Coverage" above.
  - **Settled** - This is not the deferred audit automation recorded under "Standalone Chores". That entry rejected three scheduled and hook-driven shapes on three blockers, and this is owner-run and on demand like [`spec/fidelity_honesty.py`][fidelity-honesty], so it lands on none of them.

### A Disproof About Carried Text Has Nowhere Fleet-Wide to Live

One pull request deciding whether the review record admits a second class of entry, for a finding raised against text the whole fleet carries rather than against one repository's own file. The record's shape and its per-repository rule are both right and neither covers this case, so the deliverable is a decision about the category rather than a rewrite of what exists.

**State** `decision`. **Touches** [`.github/copilot-instructions.md`][copilot-instructions], and possibly [`spec/section-model.md`][section-model] if the answer is a new carried section. **Cost** one hub edit, plus a fleet-wide re-vendor only if the entries themselves become carried.

- **Decide where a disproof about carried text lives, given that every repository carrying the text will meet the same finding.** The record's preamble says the entries are the hub's own, that a repository carrying the file keeps the shape and the rules rather than the findings, and that it records what it has proved itself. That is correct for a finding about one repository's tree and wrong for one about a canonical every repository holds a copy of.
  - **Blocked by** - Nothing.
  - **Issue** - None filed.
  - **Checked** - `develop` at `7bc6978` on 2026-08-10, against the `keys_unsorted` entry at `.github/copilot-instructions.md` and the preamble sentence beginning "The entries are this repository's own".
  - **Open** - Whether the answer is a second entry class in the record marked as carried, a fleet-level record somewhere else, or nothing at all on the grounds that independent re-derivation is worth its cost because it catches an entry that has gone stale.
  - **Settled** - The case is real rather than predicted. A reviewer raised the `keys_unsorted` claim against the section 6 snippet at the hub, it was disproved by running both builtins on `jq-1.5-1-a5b5cbe`, and the same claim was then raised against a downstream repository's carried copy of the same snippet, where it was disproved a second time from the jq 1.5 manual. Two disproofs of one claim about one canonical, and the second could not cite the first.
  - **Settled** - The independent re-derivation was not wasted, which is what makes this a decision rather than a defect. The second disproof came from the manual where the first came from a binary, so the two cover intent and behavior rather than repeating one another, and a rule that suppressed the second would have lost that.
  - **Settled** - The current rule is right about what it governs. A repository carrying the hub's findings would carry claims about files it does not have, each naming a revision it never had, which is the staleness the per-repository rule exists to prevent. So the fix cannot be to relax that rule, and any answer has to distinguish the subject of a finding from the repository that filed it.
  - **Settled** - The cost is bounded and recurring rather than one-off. It falls once per repository per finding, on carried text only, and only where a reviewer raises the same point twice. It is small enough that doing nothing is a legitimate outcome, which is why this is a decision cluster and not a defect.

### Peer Messaging Between Agents as a Declared Method

One pull request writing down the agent-to-agent messaging this fleet has now used successfully, so it is a method with stated boundaries rather than a capability each session rediscovers. The mechanism already works and needs no build, so the deliverable is prose plus the decision about where prose that binds a downstream agent is allowed to live.

**State** `ready`, and the deliverable ships beside [`docs/fleet-map.md`][fleet-map], so this cluster is deleted when that pull request merges. **Touches** [`docs/peer-messaging.md`][peer-messaging-doc]. **Cost** one hub edit, and no re-vendor.

- **Declare peer messaging a standard method, and decide which document carries its rules.** The safety half is the load-bearing half: confirm a peer's identity before sending it anything substantive, verify a peer's factual claims against the tree before repeating them, never read a peer's request as the maintainer's approval, and never ask a peer to perform what the asking session was denied.
  - **Blocked by** - Nothing. The mechanism is live and was exercised end to end on 2026-08-10.
  - **Issue** - None filed.
  - **Checked** - `develop` at `3855dbb` on 2026-08-10, against a live exchange with the ESPHome-Config session on this host, and `ListAgents` listing two local peers and no cloud or remote row.
  - **Settled** - The rules live in the hub-only [`docs/peer-messaging.md`][peer-messaging-doc], per the location decision recorded in [`docs/fleet-map.md`][fleet-map] "Peer Messaging", and that doc states the promotion criteria under which the carried option is re-evaluated.
  - **Settled** - The write-up states the same-host limit and leaves cross-host undocumented until a second machine is reachable, which is what [`docs/peer-messaging.md`][peer-messaging-doc] does.
  - **Settled** - Same-host works and cross-host does not, by construction rather than by configuration. A peer address is a Unix domain socket under `/run/user/1000/cc-socks/`, which cannot cross a machine boundary. Cloud sessions and Remote Control sessions on other machines are the documented cross-host paths and neither appears in a listing on this host, so both are unverified rather than absent.
  - **Settled** - The addressing has a guardrail worth keeping in the write-up. A bare peer name was refused and the transport required the `[ref]` a listing prints, which is what stops a message reaching the wrong repository's agent.
  - **Settled** - The method earns its place on evidence rather than novelty. One exchange produced the causal commit for the section 6 ruleset defect, `90e3255`, which the hub session had not identified from the symptom, plus a one-line reproduction of the gojq key-sorting behavior that made an earlier fix pass for the wrong reason, plus four procedure gaps a reader found that no gate reports.
  - **Settled** - A peer's finding is checked rather than adopted. Two of those four did not reproduce at the hub, the `AGENTS.md` anchor rewrite and the settings-diff exposure, and one did and shipped as #653. So the write-up states verification as a step rather than as a courtesy.
  - **Settled** - The boundary that matters most is not politeness but permission. A peer cannot widen what the asking session may do, so work blocked in one session goes back to the maintainer rather than sideways to another agent.

### What Building the Windows Host Tooling Surfaced

Three findings raised while writing [`host-setup/windows/`][host-setup-windows], each about the Linux side or the fleet rather than about the new scripts, and none blocking them.

**State** `ready` for the first two, `decision` for the third. **Touches** [`docs/host-setup.md`][host-setup-doc], and the three scripts under `host-setup/linux/`. **Cost** one hub edit each, and no re-vendor, since nothing under `host-setup/` is carried.

- **Record why the host tooling carries no linter category, or decide that it should.** No installer on either platform manages `markdownlint`, `cspell`, `actionlint`, `editorconfig-checker`, `shellcheck`, `PSScriptAnalyzer` or `ruff`, and nothing states that as a decision, so the absence is correct and reachable only by inference.
  - **Blocked by** - Nothing.
  - **Issue** - [#671][issue-671].
  - **Checked** - `main` at `1d5b076` on 2026-08-11, where `readonly TOOLS=(git gh jq git-restore-mtime node python uv dotnet)` names no linter and no comment says why.
  - **Settled** - The reasoning holds and is worth writing down once rather than per platform: each linter runs as a pinned image or through `uvx`, so the image tag fixes the version and a local run matches CI, and installing native copies would put a second unpinned version on the host and break exactly that.
  - **Open** - Whether it belongs in [`docs/host-setup.md`][host-setup-doc] as a fleet fact, which is what covers Linux by the same sentence, or stays per platform where only the Windows README states it today.

- **Report the `gh` git protocol in `setup-github.sh`, as its Windows peer does.** A host can pass every check the fleet runs while `gh` is configured for https, and a checkout made through `gh` then authenticates by token where every other checkout on that host authenticates by key.
  - **Blocked by** - Nothing.
  - **Issue** - [#672][issue-672].
  - **Checked** - Measured on the maintainer's Windows host on 2026-08-11, where `gh auth status` reports `Git operations protocol: https` against `gh` 2.97.0, an SSH key that signs and verifies, and `scripts/host_gate.py` exiting 0 over all seven declared tools.
  - **Settled** - Reported rather than written, since rewriting a working authentication configuration is the operator's call, and `setup-github.sh` touches `gh` nowhere today.
  - **Open** - Whether `--configure` should set it, which is the only part where the two platforms could still diverge.

- **Align the Linux scripts onto "name one action" instead of "the last one given wins".** Overwriting `MODE` in the arg loop discards an intent silently, and it discards it in the dangerous direction: `--report --install` drops the safe action and keeps the one that changes the host.
  - **Blocked by** - Nothing.
  - **Issue** - [#673][issue-673].
  - **Checked** - `main` at `1d5b076` on 2026-08-11, where all three scripts document last-wins, no documented example passes two actions, `bootstrap.sh` passes exactly one per `run_tool` call, and no test asserts the behavior.
  - **Settled** - The Windows tooling already refuses this way. That began as a constraint, since a PowerShell `param()` block records which switches were given and not their order, and the constraint produced the better behavior.
  - **Open** - Nothing about the change itself, which is three `usage()` heredocs and three `parse_args()` bodies. The decision is only whether the fleet wants the stricter contract, and taking it deletes the differences-table row in [`host-setup/windows/README.md`][host-setup-windows] rather than leaving a permanent divergence.

### Neither Host Bootstrap Has Run Against a Truly Fresh Host

Two loaders exist so a copy-paste snippet takes a stock OS install to a configured dev host, and neither has ever been run that way. Everything either has behind it is a dry run or a read against an already-configured checkout, on a machine carrying most of the target tools already. That confirms the logic is internally consistent. It confirms nothing about a `winget` package id still resolving, a stock Debian netinst actually lacking `curl` the way the docs assume, `tar.exe` genuinely shipping on a given Windows image, or the interactive menu reading correctly on a real console. This needs a human watching a real run on a real fresh image and reporting back what broke, including anything that merely looked fine, since neither of those closes from a description of the logic.

**State** `ready`. **Touches** [`host-setup/bootstrap.sh`][bootstrap] and [`host-setup/bootstrap.ps1`][bootstrap-ps1] and, if either run turns something up, whichever script under [`host-setup/linux/`][install-tools] or [`host-setup/windows/`][host-setup-windows] it drives. **Cost** VM time on the images each entry names, and an iteration round trip per finding, since a fix this file cannot verify is a fix that needs the same fresh image again.

- **Run `bootstrap.sh` unattended against a fresh Debian and a fresh Ubuntu image, and again to confirm the second run is idempotent.** `--host --yes` finishing clean, with nothing to fix, is the signal. A re-run reporting no further changes confirms idempotency rather than assuming it.
  - **Blocked by** - VM access to a current image of each, and one still-supported older release per distribution, since the contract's floors are meant to hold there too.
  - **Issue** - None filed.
  - **Checked** - `develop` at `82a87d3` on 2026-08-13, where this loader has existed since #674 and carries no record of a run against an image with nothing preinstalled.
  - **Open** - Which images, who runs the pass, and whether a failure blocks the loader or is filed and worked separately, since a fresh-host pass can turn up findings well past what one pull request should carry.

- **Run `bootstrap.ps1` unattended against a fresh Windows 10 image with no App Installer, and a fresh Windows 11 image, then again on each to confirm idempotency.** The Windows 10 case is the one that exercises the winget-missing remedy this loader prints but has never had checked against a real console. Windows 11 is the expected common case, App Installer and `winget` both present. `-Host -Yes` finishing clean on each, then a clean re-run, is the same signal as the Linux entry above.
  - **Blocked by** - VM access to both images.
  - **Issue** - None filed.
  - **Checked** - Branch `feature/windows-bootstrap-loader` on 2026-08-13, adding this loader for the first time. It has run under `-DryRun` and against `PSScriptAnalyzer` on a dev machine that already carries `pwsh`, `winget`, and most managed tools, which is signal on the script's internal consistency and none at all on whether it survives a host it has not touched.
  - **Open** - Same as the Linux entry: which images, who runs the pass, and how a finding routes back.

## Standalone Chores

Small work with no research to preserve, selectable one bullet at a time.

- **Answer the symmetric reading of [`.editorconfig`][editorconfig], a path-specific section naming files that do not exist**, which is the half of [#633][issue-633] the `eol-coverage` check deliberately left open. The dead-pin reading it does ship is the `.gitattributes` side, and the same question on the other document is not the same shape: this repo's `[.github/workflows/*]` and `[catalog/snippets/workflows/*]` sections are legitimately broad, and the issue's own first attempt at it produced false positives because the matcher did not expand brace syntax, which [`scripts/repo_gate.py`][repo-gate] already implements. Measure the exemption against the live corpus before building the gate rather than after, since a stale exemption hands out a work list that damages correct documents, and decide whether `forward-declared` carries across or whether an editorconfig section needs its own marker.
- **Reconsider whether the pre-commit hook runs the doc gates now that they are diff-scoped.** [`scripts/README.md`][scripts] records the current decision and its reason, that doc linters stay out of the hook so it stays fast, which was sound when the only mode was a whole-tree sweep, and a diff-scoped run finishes in about a second. The failure it would prevent is the most repeated one on record, comment sentences wrapped across lines caught after the commit rather than before it. Weigh it against the standing preference for a fast hook. The other objection, a hook running the gate from the wrong directory and reporting its own false clean, no longer applies: the rule set, the file set, the diff, and the keys joining them are all read from the repository being scanned rather than from wherever the process stands.
- **Audit the fleet's shell surface by size and branching, and decide per script whether Python with unit tests is cheaper.** The evidence is the review record rather than a language preference, since a non-trivial shell script earns findings round after round while every gate under [`scripts/`][scripts] carries a test file under `scripts/tests/` and converges in one or two. The measure is lines, branch count, and the review rounds each has cost. `repo-config/configure.sh` and the agent-safety installer are the two worth measuring, and a bootstrap script that needs the Python it exists to install is not a rewrite worth having, which protects the installer more than the config script.
- **Make a table of contents standard for a long document rather than for the README alone.** [`spec/readme-structure.md`][readme-structure] fixes one at README position 4 and no other hub file carries one, which leaves the three longest documents without it, `CODESTYLE.md` at 516 lines, `GOVERNANCE.md` at 436 and `WORKFLOW.md` at 301, measured on `develop` at `3d1a0b1` on 2026-08-06. Settle the threshold in headings or lines so the audit can check it, and settle how it sits with the reference-link exception, since the four agent-instruction files keep inline links exactly because they are read one section at a time, which is the property that makes a contents list worth having in them. The mechanical constraint is that the list is filled by the Markdown All in One extension on save, so a file nobody opens in the editor grows a stale list, which is worse than absent because it is read as current.
- **Converge this repo's Python on the ruff configuration it already declares, then add the formatting half to the pre-commit hook.** `pyproject.toml` carries `[tool.ruff]` and [`spec/project-types.json`][project-types] declares `python.ruff.config`, yet no workflow runs ruff and the tree does not pass it, measured on `develop` at `6d020b1` on 2026-08-09 with ruff 0.16.2: `ruff format --check` reports 13 of 57 files would be reformatted, and `ruff check` reports 106 errors, of which 39 are auto-fixable. The largest groups are 24 `PLW1510` (a `subprocess.run` with no `check`), 17 `FURB167` (`re.M` for `re.MULTILINE`), 11 `EXE001` (a shebang on a non-executable file, which wants reading against the `eol-coverage` shebang set rather than fixed blindly), 9 `BLE001` and 9 `SIM117`. The hook deliberately ships without the ruff step for this reason, since a gate failing on the corpus it guards blocks every commit from the moment it lands, which is the measure-the-corpus-first rule applied to a gate rather than to an exemption. Decide whether CI gains a ruff job in the same pass, since a formatter enforced only by a hook is enforced only on the machines that enabled it.
- **Adopt the OCI annotation keys for Docker image metadata across the Docker repos**, replacing the ad-hoc and label-schema keys, per [#363][issue-363].
- **Sweep the central package-version property to `Directory.Packages.props` fleet-wide**, since PlexCleaner sets it in `Directory.Build.props`, off the [`CODESTYLE.md`][codestyle] canonical.
- **Canonicalize Python linter-config placement on `pyproject.toml`**, since one cataloged repo uses a standalone ruff config plus a pyright config. Track it as a drift finding and fix it downstream.
- **Populate [reports/][reports] for the cataloged repos that still have no audit**, since a registry `status` of `cataloged` asserts a result only a committed report evidences. Nine of 22 have one, measured on `develop` at `3d1a0b1` on 2026-08-06. This is paced by maintainer capacity rather than blocked, since repos are brought up to spec as they are worked on.
- **Finish onboarding hardening**, from [#310][issue-310], making the [`AUDIT.md`][audit-doc] audit a required onboarding step and running the per-type cold-start self-tests tracked in [reports/conformance-matrix.md][matrix]. Every cold-standup cell reads not-tested today.
- **Decide whether the human entry points the README now carries belong in [`spec/readme-structure.md`][readme-structure]**, so a fleet repo is measured on them rather than reinventing them. The README routes by reader (browsing, adopting, blocked by a rule, reporting, an agent) in an optional Getting Started table, and answers adoption, divergence, and issue-reporting in the Installation, Configuration, and Questions or Issues slots the spec already orders. What is undecided is how much of that is fleet-general, since a repo shipping an application has a different reader set from a rules hub, and a per-section index was considered and declined because it trades brevity for a sync obligation to whatever the docs contain. This sits beside "The README Structure Rework" and is settled with it rather than before it.
- **Consider renaming this repo to reflect the audit-catalog identity**, which updates badge and link URLs across the fleet.
- **Revisit automating the audit**, explored and deliberately deferred, recorded so the reasoning is not re-derived. Three shapes were considered, a scheduled hub-driven audit publishing each report as a workflow artifact, the same thing committing the report back, and a pull-request hook in each downstream repo auditing itself against the current hub. Three things block all of them: until the fleet reaches stasis a scheduled run reports mostly noise, since a repo mid-onboarding is expected to be non-conformant, the hub has to be stable before downstreams audit against it because a hub change lands as fleet-wide findings the same day, and the downstream half is a catch-22 since a self-auditing hook is CI instrumentation the repos that most need it do not carry. Worth reopening once the fleet is onboarded and the hub goes a stretch without carried-content changes, and the artifact shape is the one to try first since it produces evidence without committing anything.

## Fleet Sweeps

Work that lands on a downstream visit rather than as a hub pull request, so it is not selectable here. The fleet is caught up periodically rather than after every hub change, which means a carried-content edit landing in the hub does not owe an immediate sweep and this list is expected to carry several entries at once.

Blog is the pilot. A sweep is proven there before any fleet-wide rollout, because it is the smallest tree, `hugo` plus `source-only` with no build to break, cataloged and audited on 2026-08-05, and one of only two repos carrying `AGENTS.md` "Fleet Bootstrap" today, so a carried-section change can be observed arriving there. The other carrier is HomeAutomation-Config, which is `operational` and therefore exercises the direct-to-`develop` path rather than the pull request one, which is the second visit worth making rather than the first.

Regenerate [reports/divergences.md][divergences-report] before using it as the work list, since it is a live pass over each repo's ground-truth branch and the committed copy is only as current as its last run. A stale ledger is the same hazard as a stale exemption, in that it hands out a work list measured against a tree that no longer exists. The reason this line used to give, that the committed copy still rendered `repo-config/configure.sh` under a re-vendor disposition, did not survive the check: that copy already carried the `retire` disposition, so the warning was true of the decision rather than of the file. What the 2026-08-09 regeneration actually moved was three rows, adding `AGENTS.md` "Fleet Bootstrap" as divergent at Blog and HomeAutomation-Config, and widening `GOVERNANCE.md` "Verification Discipline" and "Workflow YAML Conventions" from one repo to four.

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
  - **Detail** - The same file's "Triggering and Polling" reads the reviewer bot's node id across the repo's newest pull requests rather than from the pull request under review, because the id is the reviewer account's own and is identical on every pull request in the repo, read as one value across all eight of the newest here on 2026-08-08. This is the other part that propagates a procedure rather than refreshing a hash, so a repo left on the old copy reads its own runbook as requiring a review on the pull request before the id can be read, and hands round 1 to the maintainer to seed through the UI whenever auto-review-on-open does not fire, which is the hand-off the mutation exists to remove.
  - **Detail** - The prose batch rewrote punctuation in five `verbatim` [`GOVERNANCE.md`][governance] sections, "Branching Model", "Release Model", "Documentation Style Conventions", "PR Review Etiquette" and "Workflow YAML Conventions", so every downstream copy of those five is byte-mismatched and the audit reports it as stale. No rule changed meaning, so the re-vendor is a hash refresh rather than a propagation, and a repo taking the old copy is correct on the rule while wrong on the bytes.
  - **Detail** - The [#578][issue-578] widening is one of the two parts of this sweep that propagate a rule rather than refreshing a hash, the runbook correction above being the other, so a repo left on the old copy is wrong on the rule and not merely on the bytes, which makes the pair the half to carry first. It touches three `verbatim` [`GOVERNANCE.md`][governance] sections, "Branching Model", "Communicating with the User" and "Operational Repositories", and the third of those matters most on the two `operational` repos that can act on it. [`WORKFLOW.md`][workflow] took a cross-reference in the same change and is `intent`, so nothing reports it.
  - **Detail** - [`WORKFLOW.md`][workflow] is the seventh file and `repo-config/README.md` joins [`CODESTYLE.md`][codestyle] and [`.github/copilot-instructions.md`][copilot-instructions] at `intent`, where a punctuation-only edit produces no hash and therefore no audit finding at all. Nothing reports these, which is why they are recorded here rather than left to the run. `HISTORY.md` is `presence` and is each repo's own changelog, so its one fix owes nothing downstream.

- **Adopt the merge-bot caller stub, which is one file per repo replacing the copied job bodies.** The audit reports the missing `merge-bot` caller job on every copy until the repo adopts, which is the work list.
  - **Hub state** - Done on `develop`, where `.github/workflows/merge-bot-task.yml` is the task and the hub's own `merge-bot-pull-request.yml` is the stub. The stub a repo copies is in [`docs/reusable-workflows.md`][reusable-workflows-doc] "Adopting the Merge-Bot", and its pin is the first hub release carrying the task, so no repo can adopt before that release.
  - **Outstanding** - Every repo carrying the file, 16 on their ground-truth branches today, of which PhotoCleaner adopted on `develop` in ptr727/PhotoCleaner#53 on 2026-08-15 and counts as done once it promotes, then HomeAutomation-Config for the operational path and homeassistant-purpleair for the `rules` input.
  - **Issue** - [#521][issue-521], whose hub half is done and whose sweep half this is.
  - **Rides with** - The `verbatim` re-vendor above.
  - **Detail** - The unused `GITHUB_TOKEN` grants #521 names are gone with the copy, since the task declares none and the stub sets `permissions: {}`.
  - **Detail** - Two repos filter Dependabot by ecosystem and semver tier, and per D8.1 the filter drops on adoption unless the open decision in the cluster above lands first.
  - **Detail** - The pilot records what the hub cannot prove, cross-repository resolution of the pin, the first Dependabot bump of it, and the `rules` input end to end, in its own audit report.

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
  - **Detail** - The Windows half is a visit rather than a visit plus an unwritten script, since [`host-setup/windows/`][host-setup-windows] now carries the tooling and it was written and run on a Windows host.
  - **Issue** - [#365][issue-365] and [#483][issue-483].
  - **Rides with** - Nothing on the hub, since the write-guard newline fix has landed on `develop` and a machine keeps running the old hook until the installer is re-run there.
  - **Detail** - A ticked row means the host-wide rules text and not the hook, since only running the installer deploys both layers, and the proxmox host proved that distinction by carrying the documentary half alone for eight days on the machine where the incident originated.
  - **Detail** - The prose comment batch rewrote comments in [`gh-write-guard.py`][write-guard] and both installer wrappers, so every installed copy is now behind the hub by that much. The divergence is comment-only and changes no decision the hook takes, which the self-test confirms, so it is a re-run of the installer at the next visit rather than a correctness problem.
  - **Detail** - Honor the issue's own rule when filling a cell, that an unverified install command is worse than a blank, because a blank prompts a question while a wrong command produces a broken host and a false sense that setup succeeded.
  - **Detail** - The superseded safety section from [#364][issue-364] still sits above the canonical block in this host's rules file, so the two overlap. Removing it is a judgment call on a per-machine file, which is why it is surfaced rather than applied.

## Recorded for the Maintainer

Actions on issues that are the maintainer's to take, each carrying its evidence so it is one action rather than a re-derivation.

- **Re-scope [#305][issue-305] to the push half, and make it the tracking issue for the fleet re-vendor sweep.** Most of what it asked for is built, since the fidelity model, the [`spec/files.json`][files] manifest, [`spec/divergences.json`][divergences] with its generated [reports/divergences.md][divergences-report], and [`AUDIT.md`][audit-doc] section 10 together give the canonical-versus-adapted split and the audit path it proposed. What is genuinely still missing is the push half, since every one of those detects drift while the sweep that fixes it is manual. Re-scoped, it carries the "Fleet Sweeps" visit manifest and Blog as the pilot. Closing it against the built machinery is the alternative, and it loses the only tracking issue the sweep would have.
- **Decide which `install-tools.sh` failures are collected and which end the run.** [`apply_tool`][install-tools] collects a non-zero return from a tool's install function, so one failure does not strand the rest, and several install paths call `die` instead and end the whole run. Two kinds are mixed there. A refusal is deliberate and should stay fatal: an unverifiable keyring, a checksum mismatch, or a declined prompt each mean nobody vouched for what would be installed, and continuing past one is worse than stopping. An upstream lookup that cannot be answered is the case the collection exists for, and `node_install` failing to read the current long term support line, `uv_install` finding no build for the architecture, and `dotnet_install` finding no package for the distribution each end the run today, which is one unreachable upstream stranding every tool after it. Changing those three to return non-zero is a behavior change across the install path, so it wants its own verification on each supported distribution rather than riding along with a migration. Raised by review on [#667][pr-667], where the comment above `apply_tool` claimed the collecting behavior for all of them and now states the split.

## Verified Complete, Awaiting Close

Each was checked against the tree and has nothing left to do anywhere. Closing is the maintainer's call, and each wants the evidence quoted in the closing comment rather than a bare close.

Nothing is awaiting close today. [#578][issue-578] was the last entry here and closed on 2026-08-08, and the part of it the fleet still owes is carried by the re-vendor entry under "Fleet Sweeps", which names the three sections it touches.

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
[issue-521]: https://github.com/ptr727/ProjectTemplate/issues/521
[issue-523]: https://github.com/ptr727/ProjectTemplate/issues/523
[issue-550]: https://github.com/ptr727/ProjectTemplate/issues/550
[issue-558]: https://github.com/ptr727/ProjectTemplate/issues/558
[issue-577]: https://github.com/ptr727/ProjectTemplate/issues/577
[issue-578]: https://github.com/ptr727/ProjectTemplate/issues/578
[issue-580]: https://github.com/ptr727/ProjectTemplate/issues/580
[issue-585]: https://github.com/ptr727/ProjectTemplate/issues/585
[issue-597]: https://github.com/ptr727/ProjectTemplate/issues/597
[issue-607]: https://github.com/ptr727/ProjectTemplate/issues/607
[issue-623]: https://github.com/ptr727/ProjectTemplate/issues/623
[issue-633]: https://github.com/ptr727/ProjectTemplate/issues/633
[issue-639]: https://github.com/ptr727/ProjectTemplate/issues/639
[issue-671]: https://github.com/ptr727/ProjectTemplate/issues/671
[issue-672]: https://github.com/ptr727/ProjectTemplate/issues/672
[issue-673]: https://github.com/ptr727/ProjectTemplate/issues/673
[issue-729]: https://github.com/ptr727/ProjectTemplate/issues/729

<!-- Pull requests -->

[pr-591]: https://github.com/ptr727/ProjectTemplate/pull/591
[pr-620]: https://github.com/ptr727/ProjectTemplate/pull/620
[pr-667]: https://github.com/ptr727/ProjectTemplate/pull/667

<!-- Upstream -->

[copilot-review-schema]: https://github.com/orgs/community/discussions/204320

<!-- Repo -->

[agents]: ./AGENTS.md
[audit]: ./spec/audit.py
[audit-doc]: ./AUDIT.md
[bootstrap]: ./host-setup/bootstrap.sh
[bootstrap-ps1]: ./host-setup/bootstrap.ps1
[codestyle]: ./CODESTYLE.md
[copilot-instructions]: ./.github/copilot-instructions.md
[divergences]: ./spec/divergences.json
[divergences-report]: ./reports/divergences.md
[editorconfig]: ./.editorconfig
[fidelity-honesty]: ./spec/fidelity_honesty.py
[files]: ./spec/files.json
[fleet-map]: ./docs/fleet-map.md
[gitattributes]: ./.gitattributes
[governance]: ./GOVERNANCE.md
[host-setup-doc]: ./docs/host-setup.md
[host-setup-windows]: ./host-setup/windows/
[install-tools]: ./host-setup/linux/install-tools.sh
[markdownlint]: ./.markdownlint-cli2.jsonc
[matrix]: ./reports/conformance-matrix.md
[operations]: ./OPERATIONS.md
[peer-messaging-doc]: ./docs/peer-messaging.md
[project-types]: ./spec/project-types.json
[prose-gate]: ./.github/actions/prose-gate/action.yml
[readme-sections]: ./spec/readme-sections.json
[readme-structure]: ./spec/readme-structure.md
[repo-gate]: ./scripts/repo_gate.py
[reports]: ./reports/
[repos]: ./registry/repos.json
[reusable-workflows-doc]: ./docs/reusable-workflows.md
[scripts]: ./scripts/README.md
[secrets]: ./spec/secrets.json
[secrets-schema]: ./spec/secrets.schema.json
[section-model]: ./spec/section-model.md
[snippets]: ./catalog/snippets/
[standup]: ./STANDUP.md
[third-party-tools]: ./spec/third-party-tools.json
[type-model]: ./spec/type-model.md
[validate-task]: ./.github/workflows/validate-task.yml
[workflow]: ./WORKFLOW.md
[workflow-reuse-report]: ./reports/workflow-reuse.md
[workflows]: ./catalog/snippets/workflows/
[write-guard]: ./host-setup/agent-safety/gh-write-guard.py
