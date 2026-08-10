# RESYNC.md

How an agent brings a repository that is **already stood up** back into line with the current hub. This is the procedure a request to sync a repository with the hub resolves to, and it is the third of three entry points: [`STANDUP.md`][standup] takes a repository from nothing to operational, [`AUDIT.md`][audit] measures one and changes nothing, and this file applies what the measurement found, in an order that matters.

Resyncing invents nothing. It is [`AUDIT.md`][audit] run for the findings, then each finding applied by the remedy its class already carries. What this file adds is the **order**, the **remedies that remove content rather than update it**, and an honest account of **what a resync cannot detect**, none of which a findings list states on its own.

## 0. Route Here Only If the Repository Is Stood Up

Three states look similar from inside a repository and take different procedures, so establish which one before doing anything. The `AGENTS.md` "Fleet Bootstrap" section carries this same routing in the repository itself, byte-locked, so an agent finds it without knowing this file exists.

- **No repository, or a local tree with no remote.** [`STANDUP.md`][standup] from section 0. Nothing here applies, because there is nothing to bring into line.
- **A partial instruction set.** [`STANDUP.md`][standup] sections 1A and 2. An absent carried file is a baseline that never arrived, not drift, and the two are fixed differently: a baseline is carried, and drift is converged. Resyncing a repository whose `AGENTS.md` never carried the rule sections would re-vendor sections into a document that was never given them, which reads as drift repair and is really a late standup.
- **A full instruction set, current or stale.** This file. Stale is the normal case and is not a defect, since the hub moves and a repository is a snapshot of the revision it last converged with.

**The distinction is measured, not assumed.** `spec/audit.py <Repo>` reports an absent carried file as a `LETTER` and a present-but-stale one as a `DRIFT`, so the finding kinds themselves say which procedure the repository is owed. A run that is mostly letters is a repository that needs [`STANDUP.md`][standup].

## 1. Reach the Hub, and Verify the Host

**Fetch a hub checkout of your own immediately before reading it.** A clone is whatever it last fetched rather than the branch it names, so a stale one answers confidently instead of failing, and a resync driven from a stale hub converges a repository onto a revision that is already history. Read `main`, the promoted and gated state, per [GOVERNANCE.md "Hub-Hosted Tooling"][governance-hub-hosted-tooling]. Work only in that checkout rather than in one another task is using.

**Verify the host before running any hub tool.** The tools carry version floors, and a host below one does not fail cleanly: it answers `--version`, looks healthy, and produces a wrong answer. Both host defects this fleet has hit were version facts on a tool that was installed.

```shell
python3 scripts/host_gate.py --repo <path-to-target-checkout>   # run from the hub, floors from spec/host-tools.json
```

A finding here is a **host** misconfiguration to fix on the machine, or to surface to the maintainer, and never something to patch per repository. [`docs/host-setup.md`][host-setup] is the contract it checks.

**Pass `--repo`, because the flag is what makes the target's own floors count.** A repository may declare a root `host-tools.json` layering over the hub's, tighten-only, per [`scripts/README.md`][scripts], and the gate reads that file relative to `--repo`, which defaults to the working directory. So a bare run from a hub checkout layers the hub's own declaration, silently passes on the floors the target adds, and prints the same healthy digest either way. A target carrying no local file is the common case and is silent by design, so the absence of a layering note is not evidence the flag was unnecessary.

The identity and signing checks in [`STANDUP.md`][standup] section 0 apply to a resync too, since it ends in commits like any other change. Read the `--global` scope explicitly, because inside a repository a local override wins and hides the host setting.

## 2. Measure, Before Changing Anything

Run [`AUDIT.md`][audit] end to end. The mechanized subset is one command, and the rest of that file is the half no tool evaluates:

```shell
python3 spec/audit.py <Repo>                    # the deterministic findings, read at the registry groundTruthBranch
python3 spec/audit.py --issue <Repo>            # the same findings as a ready-to-file convergence issue
python3 spec/fidelity_honesty.py --report       # regenerate reports/divergences.md before using it as a work list
```

**Regenerate the divergence report rather than reading the committed copy.** It is a live pass over each repository's ground-truth branch, and the checked-in file is only as current as its last run, so a stale one hands out a work list measured against a tree that no longer exists.

**A finding is a snapshot.** Quote the run stamp (`audit run <UTC> | hub <sha>`) in anything derived from the run, and re-run before acting on a finding written earlier, because a repository moves between filing and pickup and a stale list leads to re-fixing what is already fixed.

## 3. Apply, in This Order

The order is load-bearing. Each step below either changes the rules the later steps are judged against, or removes something a later step would otherwise refresh.

1. **The instruction set first.** `AGENTS.md` and `GOVERNANCE.md` verbatim sections, then `CODESTYLE.md` and `WORKFLOW.md`. These are the rules for producing every other file, so carrying them last means everything touched beforehand was judged against the previous revision. This is the same closing-window shape as [`STANDUP.md`][standup] section 1A, and the cost of getting it wrong is rework proportional to how much was changed first.
2. **Deletions second, before any re-vendor.** A `hub-only:` finding names a file the hub hosts rather than carries, and its remedy removes the file. Doing it after the re-vendors means refreshing a copy that is about to be deleted, which is wasted work that also reads as a deliberate update in the diff. See section 4, which is the whole of what deletion means here.
3. **Verbatim re-vendors.** Copy the current hub canonical down, whole file or the one named `## heading` region. A finding classified **stale** matches a past hub revision and needs no judgment. One classified **modified** matches no revision, so the repository changed fixed content and the change is read before it is overwritten, since it may be an improvement the hub should adopt instead.
4. **Interface workflows.** Honor the named contract (required jobs, the ruleset-bound check name, the artifact-name handoff) rather than copying bytes. The body is the repository's own.
5. **Settings, rulesets, and secrets.** Run the hub's script against the repository by name, never a carried copy: `repo-config/configure.sh check <owner>/<repo> release|operational`, then `apply` for what it reports. Pass the model explicitly rather than relying on the registry lookup.
6. **Intent files last, and by hand.** See section 5, which states why these carry no mechanical signal at all.

**Reconcile the registry entry in the same pass.** `status`, `types`, `releaseTrigger`, `workflowModel` and `driftNotes` record reality rather than intent, and a `driftNote` describing work that is now finished is deleted rather than left standing. A note asserting outstanding work in prose ("pending", "not yet", "behind") contradicts a clean audit outright.

## 4. Deleting Is a Remedy, and It Is the One That Can Destroy Work

Every other finding in this procedure is satisfied by adding or replacing content. This one is satisfied by removing it, which makes it the only class where acting on a wrong finding loses something.

The detector is derived rather than listed: the hub's git-tracked paths minus the [`spec/files.json`][files] baseline is what the hub hosts and no repository carries. That means a file dropped from the manifest starts being reported on the next run with no retirement list to maintain, and it also means **the match is on path alone**, so a hit is a candidate and not a verdict.

- **Only a `retire` disposition in [`spec/divergences.json`][divergences] authorizes a deletion.** It records that the file is the hub's content with nothing per-repository in it, and what to reach instead.
- **An untriaged hit is read before it is touched.** A repository's own content at a path the hub also uses matches this check while carrying nothing of the hub's. The first fleet-wide run found two: a KiCad tooling document at `scripts/README.md`, and per-repository formatting hooks at `.husky/pre-commit`, each of which shares the path and none of the content. Deleting either would have destroyed work the hub never owned.
- **An `accepted` disposition closes the hit permanently**, whether it is a path collision or a file every repository legitimately owns, such as `LICENSE` and `TODO.md`.
- **Where a carried document named the deleted path, re-point it at the hub's**, since a pointer that resolves nowhere teaches a reader that a pointer in carried text is decorative.

## 5. What a Resync Cannot Detect

State this rather than letting a clean run imply more than it earned. A carried file at `intent` fidelity is checked for **presence and nothing else**, per [`spec/fidelity-model.md`][fidelity-model], so a hub revision inside one produces no finding anywhere. That covers `CODESTYLE.md`, `WORKFLOW.md`, the carried `AUDIT.md`, `.github/copilot-instructions.md`, `.editorconfig`, `.gitattributes`, `cspell.json` and `version.json`.

So a repository can be clean on every mechanized check and still carry an `intent` file many hub revisions old. Two things follow. Read the hub's own history for those files when a resync is meant to be thorough, rather than trusting the finding list to raise them. And treat `spec/fidelity_honesty.py`'s promotion candidates as the structural fix: an `intent` unit that is content-identical fleet-wide can become `verbatim` and gain drift detection for free, which is the class that hid the `configure.sh` drift for as long as it did.

The other half is section 4 of [`AUDIT.md`][audit]: no check belonging to a project type in [`spec/project-types.json`][project-types] is mechanized at all. A clean tool run is evidence for the deterministic subset, no evidence for a type's checks, and partial evidence across the cross-cutting dimensions.

## 6. Ship It

- **One focused pull request per drift class**, branched from the target's `develop`, cross-referencing the finding it closes. A sprawling all-drifts pull request draws many review rounds and never feels done.
- **Never push a fix directly to a protected branch**, and never hand-edit a target outside a pull request. An operational repository commits to `develop` directly by design, and a conformance change is still a reviewable change.
- **Close the review loop.** Request a review on every push, confirm it covered the head commit, and answer and resolve every thread, per [GOVERNANCE.md "PR Review Etiquette"][governance-pr-review-etiquette] and the [Copilot review runbook][copilot-runbook].
- **The maintainer merges.** The agent drives to green and stops.
- **Fix systemic drift in the hub instead.** Where many repositories share a drift, fix the rule or add a check here and let a re-audit re-flag it, rather than hand-patching each repository for a shared cause.

**Done means measured, not applied.** Re-run the audit after the merge and commit the report, because a convergence asserted without a report is a convergence nobody can check.

<!-- Repo -->

[audit]: ./AUDIT.md
[copilot-runbook]: ./.github/copilot-instructions.md
[divergences]: ./spec/divergences.json
[fidelity-model]: ./spec/fidelity-model.md
[files]: ./spec/files.json
[governance-hub-hosted-tooling]: ./GOVERNANCE.md#hub-hosted-tooling
[governance-pr-review-etiquette]: ./GOVERNANCE.md#pr-review-etiquette
[host-setup]: ./docs/host-setup.md
[project-types]: ./spec/project-types.json
[scripts]: ./scripts/README.md
[standup]: ./STANDUP.md
