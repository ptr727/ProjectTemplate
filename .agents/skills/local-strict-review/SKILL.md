---
name: local-strict-review
description: >-
  Runs a read-only, adversarial review pass against this branch's current diff against its
  target branch, full file context included, on the strongest model tier the session can reach,
  before a unit of work is pushed toward a pull request or claimed done. Use this whenever staged,
  committed, or untracked work is about to be pushed on a PR-bound branch, and whenever
  `agent-conduct`'s "about to claim work is done, verified, green, or fixed" trigger fires for
  PR-bound work. Triggers even when the change looks small or the same session already judged its
  own diff ready, because a self-review pass judging its own diff inherits its own blind spots,
  the exact gap this skill exists to close before a PR-hosted reviewer closes it instead. Reuses
  `code-review`'s "Review the Change" criteria rather than restating them, and owns only this
  local, pre-PR moment. Once a pull request exists, `pr-review-conduct` and `drive-pr` own
  triaging and disposing of what a PR-hosted reviewer finds. Also triggers whenever a change
  edits rule text, a Skill, or any other canonical content this repository authors and other
  repositories carry, because that content reaches a reviewer whole only when a repository
  carries it for the first time, and a second pass reading each changed unit's whole text is
  what moves that read into the repository that can act on what it finds.
---

# Local Strict Review

## Why This Exists

A coding agent that finishes a unit of work, judges it ready, and opens the pull request is judging its own diff with the model, and often the blind spots, that wrote it. CodeRabbit, Qodo, and Copilot routinely find real defects that a local pass missed, and each round costs review latency and, for a rate-limited reviewer, shared account-wide quota. A local, full-file-context adversarial pass before the pull request exists catches the same class of defect for a fixed, smaller cost, the same reasoning that already runs local lint before a push instead of waiting for CI.

## What It Does

Dispatches one read-only subagent against this branch's full diff since it forked from its target branch. Resolve `<target>` once, `develop` unless `repo-worktree`'s base-branch rule put this branch on `main` instead, then fetch it, `git fetch origin <target>`, and diff against the merge-base, `git diff "$(git merge-base origin/<target> HEAD)"`. Stop and report a failed fetch rather than running the merge-base or diff commands anyway: an existing local `origin/<target>` ref can still resolve after a failed fetch, and reviewing against it silently trades the current target for a stale one. Use the same resolved `<target>` in every command below, never a literal `develop` alongside it. Naming the target branch explicitly matters: the branch's own `@{u}` tracking ref points at the branch's own remote once it has been pushed, not at the branch it targets, so anchoring there silently narrows a later run to only the diff since the last push instead of the full accumulated diff. That merge-base diff covers every commit already on the branch plus whatever is currently staged or unstaged, so it never reviews only the latest increment, at any of the moments this skill is invoked from. An empty diff is not the same as nothing to review, and it is never the signal to stop: it reports no untracked file at all, and it reports nothing for content a commit carries that the working tree has since put back. The untracked-file list below covers the first of those. The second is why this skill commits before reviewing, since a removal or a restore that is committed leaves no net content to miss, and why the engine reads HEAD rather than this diff, its change set coming from the merge base against HEAD, the index and the working tree, so the two answer different questions. A fresh review of the full accumulated diff is what catches what per-push review misses, the exact evidence this skill exists to act on.

`git diff` never reports a path `git add` has not touched, so a newly created file sitting untracked would otherwise go unread. List it explicitly, `git ls-files --others --exclude-standard`, and read each result in full alongside the diff, the same as any other file the diff touches.

The subagent reads the full content of every file the diff and the untracked-file list touch, not just the hunks, since cross-file and whole-file context is exactly what incremental review misses. It reports findings only. It never fixes, stages, or commits anything.

Review criteria are `code-review`'s "Review the Change" section, reused rather than restated here, plus three traps worth calling out explicitly for a pass that runs before a human or a PR-hosted reviewer ever sees the diff: unguarded type coercions, TOCTOU/race conditions, and platform-specific behavior differences. `code-review`'s separate "Publish Every Finding" section does not apply here: this skill has no PR to post a comment on and no coverage marker to close a review with, so its own report contract below replaces that section rather than extending it.

## Running It

Follow `AGENTS.md` "Context and Delegation Discipline"'s subagent briefing shape:

```text
Task: adversarial review of this branch's diff against its merge-base with its target branch,
  read full surrounding files where the diff hunks alone do not give enough context.
Paths: the files `git diff --name-only "$(git merge-base origin/<target> HEAD)"` and
  `git ls-files --others --exclude-standard` list, mandatory floor. Reading a specific
  unchanged caller or consumer beyond that list is in bounds only where a candidate finding's
  proof actually depends on it, per code-review's own "follow data and control flow beyond the
  edited lines" instruction below, never as an open-ended exploration.
Rules that bind this task: quote `code-review`'s "Review the Change" section into the prompt,
  plus flag unguarded type coercions, TOCTOU/race conditions, and platform-specific behavior
  differences explicitly. Do not quote "Publish Every Finding", this task's report contract is
  the Return line below, not a PR comment or a coverage marker.
Return: one finding per line, file:line, the concrete failure scenario, no severity theater.
Bounds: read-only. No edit, no stage, no commit, no push, no PR-hosted write of any kind.
<AGENTS.md's own unresolved-rule closing line, quoted verbatim from "Context and Delegation Discipline", not restated here>
```

**Model tier:** the strongest tier this session can reach, per `AGENTS.md` "Match the model tier to the judgment" and "Never tier down the seat holding the judgment", applied here to the reviewer rather than the author. Run the pass on the same tier that authored the change when only one tier is reachable, a second, adversarially-prompted look still catches what the authoring pass's own "looks ready" judgment did not.

## Recording the Pass

`scripts/local_review.py` is what makes this rule checkable rather than something each session has to remember. It holds no review logic: the pass above is the review, and the engine records that it happened, keyed on the content the reviewer actually saw. That receipt is what a capture point reads, this repository's `.husky/pre-push` hook being the first of them.

Commit first, then read the digest, then dispatch the subagent, then hand that same value back. Nothing may change the tree between the read and the record. Staging a modified tracked file is such a change, moving the digest although the content did not, and a commit can move it too, since HEAD decides which paths are in the change set at all. Reading after the commit is what leaves neither of them between the read and the record.

```sh
engine="<hub-checkout>/scripts/local_review.py"   # in the hub itself, scripts/local_review.py
python3 "$engine" status --target <target>        # JSON, take contentDigest
# run the pass above, then:
python3 "$engine" record --reviewer agent-skill --target <target> --expect-digest <digest> [--findings N]
```

Both commands run with the repository under review as the working directory, whichever repository that is. The engine takes no `--repo` and reads whichever repository it is run in, so the path names where the script lives and the working directory names what it measures.

`<target>` is the same branch "What It Does" resolved for the review, passed to both commands. Leaving it off defaults them to `develop`, and on a `main`-based branch that computes the digest against a merge base the reviewer never read, so the receipt would attest to a change set nobody looked at. A receipt is only valid against the target it names, so the two have to agree.

`--expect-digest` is required rather than optional, and binding it to the earlier read is the whole point. A format-on-save or a hook autofix between the review and the record would otherwise be stamped as reviewed by a pass that never saw it. A refusal there is the content having moved, so the answer is another pass over the current content rather than another read of the digest.

Record the pass whatever it found, including nothing. The key covers the net content the branch introduces against its target rather than the commit series, so an interactive rebase that leaves the tree alone keeps the receipt valid, and changing one byte invalidates it.

**Why the commit comes first**, rather than being an ordering that could equally run the other way. A push delivers the commit, so a receipt recorded over uncommitted work describes something else, and a capture point gating a push refuses on exactly that. Two smaller reasons point the same way: staging a modified tracked file moves the key even though its content did not change, and a commit made after the record can carry content the pass never read. Reviewing earlier than this is still worth doing as ordinary diligence, and it does not substitute for the recorded pass: the digest read and the record bracket a window in which the tree holds still, and a commit inside that window ends it.

The engine is hub-hosted per `GOVERNANCE.md` "Hub-Hosted Tooling", so a downstream repository reaches a hub checkout's copy rather than carrying one, which is what the path above is for.

## The Carried-Content Pass

A second pass under the same rule, run in the repository that authors canonical content other repositories carry, which in this fleet is the hub. It answers an ordering defect rather than a quality one. Such content is written and merged against a diff of a few lines, and reaches a reviewer as a new file, in full, only when a repository carries it for the first time, so the first real read of a rule happens where nothing can be done about the result: the tree is manifest-owned, the copy is digest-compared, and a local edit there is drift on the next fidelity check. Every carrier after that re-discovers the same defect, and the finding arrives in a session holding no checkout of the authoring repository and no standing to test the claim.

**The unit is what a reviewer reads whole**, one level-two section of a Markdown canonical and one file otherwise. The pass reads that unit's whole current text rather than the diff that moved it, because reproducing the carrier's read is the entire point, and a diff with surrounding context is a different read that has already been done by the pass above.

Run it at the same model tier and in the same delegation shape, changing only the brief:

```text
Task: adversarial review of one canonical unit, read as a repository carrying it for the first
  time reads it, whole, knowing nothing about what this branch changed in it.
Paths: <the unit key, substituted here>, read in full out of the file that key names.
  Read the whole unit, never a diff of it.
Rules that bind this task: <quote code-review's "Review the Change" section>, and judge the text
  as a reader who has only this unit: a claim it makes about a tool, a path, a command, or
  another rule is a defect wherever that claim is false, stale, or unverifiable from the unit
  itself, and an instruction it gives is a defect wherever following it literally fails.
Return: one finding per line, the sentence quoted, and what is wrong with it. No severity theater.
Bounds: read-only. Report a rule that looks incomplete rather than guessing at what it meant.
<AGENTS.md's own unresolved-rule closing line, quoted verbatim from "Context and Delegation Discipline", not restated here>
```

```sh
engine="<hub-checkout>/scripts/canonical_review.py"   # in the hub itself, scripts/canonical_review.py
python3 "$engine" check      # each uncovered unit, with the digest to hand back
# run the pass above over each unit it named, then, per unit:
python3 "$engine" record --reviewer agent-skill --unit '<key>=<digest>' [--findings N]
```

Both commands run with the authoring repository as the working directory, which is the hub checkout the path above already points into, since the engine takes no `--repo` and reads whichever repository it is run in. `check` names each uncovered unit with the digest to hand back, so nothing has to be looked up separately, and `list` is there for reading the whole set rather than for this loop.

The digest is bound to the read for the same reason `--expect-digest` is above: recording a unit by name alone would stamp whatever the file holds at record time, so an edit between the review and the record would be attested to by a reviewer who never saw it. Record each unit whatever the pass found, including nothing.

**A unit nothing has read here yet is not this branch's debt.** `check` refuses only the units this branch's own diff moved, and the hub's `reports/canonical-review.md` carries the rest as a burn-down. Working one of those off is worthwhile, and it is its own change rather than a tax on an unrelated one.

## Disposing of Findings

Every finding maps to one of `pr-review-conduct`'s five outcomes before the pull request opens: fixed, evidence-disproven, filed as a deferred issue, escalated to the maintainer for an explicit call, or, if it keeps recurring, taken as a signal to fix the class. A finding this pass raised and not fixed is never the agent's own call to just leave. Per outcome 3, that decision needs the maintainer's explicit answer, the same way a PR-hosted finding would. Running this pass is required before every push toward a pull request, per `agent-conduct`. Two claims sit next to each other here and they point opposite ways, so they are stated apart rather than in one sentence. **The pass is mandatory**, and where a capture point enforces it, a push carrying content no recorded pass covers is refused. That refusal is the gate working rather than a fault to route around. **The findings stay advisory**, and the count a pass raises gates nothing at all, since a pass records that a review ran and never that the content is clean. The disposition above is what closes each finding, the same posture local lint holds today. It posts nothing to GitHub, it only reports to the session driving the work. A finding raised here and not fixed is not thereby resolved: the same finding shape reaching a PR-hosted reviewer later still gets its own fresh disposition, per `pr-review-conduct`'s "a disposition decided on one PR does not carry to the next."

## When to Run It

- Before the first push toward a pull request (`drive-pr`'s Drive Loop step 2, `pr-review-conduct`'s Expected review loop step 1).
- Before pushing a fix for a reviewer finding, the same self-review blind spot applies to a fix as to the original diff (`drive-pr`'s "Disposing of Every Finding", `pr-review-conduct`'s outcome 1).
- Whenever `agent-conduct`'s "about to claim work is done, verified, green, or fixed" trigger fires for work that will become, or already is, a pull request.
- Before pushing a change that edits canonical content other repositories carry, over each unit that change moved, per "The Carried-Content Pass" above.

This repository's `.husky/pre-push` hook checks the receipt, and the canonical-unit coverage beside it, at the push itself, so the moments above are where each pass is run rather than the only places it is noticed. The hub's own `.github/actions/validate` hook runs the canonical-unit half again on every pull request, which is the one capture point a push cannot bypass. A blocked push usually means one of them was skipped. The pre-push hook is a backstop under this skill and not a replacement for it, the pull request one being the layer that does not depend on the skill having been read: it fires only in a clone that enabled `core.hooksPath`, it says nothing about a repository that carries no such hook, and it is bypassable by design, `--no-verify` being the documented route for a genuine pickle rather than for a diff nobody read.

**Not every refusal is a missing pass, and re-running the pass at one of these does nothing.** Read the refusal itself, which names its own case. Some of these the hook decides before the engine runs, so there is no engine message under them, and the rows below say where each one's detail comes from.

| The refusal says | What it means | What clears it |
| --- | --- | --- |
| Tracked content differs from HEAD | A push delivers HEAD while a receipt covers the index and working tree, so the receipt does not describe this push | Commit what is being pushed, then the pass, then the record |
| The commit is not this worktree's HEAD | `git push origin some-other-branch` from a checkout sitting elsewhere, and the engine reads the checkout it runs in | Check the pushed branch out in its own worktree, per `repo-worktree` |
| Any wording saying the gate did not or could not run | An execution boundary rather than a verdict, which blocks because a gate that waves a push through when it could not run has stopped gating. The cause is named in that same message or in the engine error printed above it, and it is a missing Python interpreter, an unresolvable target, an unreadable receipt, a git command that failed, or any unexpected failure | Whatever the message names, most often installing an interpreter per `docs/host-setup.md` or fetching the target branch. Never another pass |
| This branch changes N carried canonical unit(s) that no recorded pass covers | The carried-content pass was skipped for a unit this change moved, and the refusal names each one with the digest to hand back | One carried-content pass per named unit, then `canonical_review.py record` for each |
| A recorded pass names a branch the check did not measure | The hook reads `develop` and nothing else, so a branch based elsewhere is measured against `develop` whatever the pass targeted, and the engine deliberately prints no record command, since the one it would print records a pass over a diff nobody read | One more pass against the branch this work actually targets, where it does target the measured one. Where it does not, the gate cannot judge the branch at all and the bypass is its answer |

This table is the fleet's one enumeration of these, and every other surface states the principle and routes here rather than listing shapes or counting them. That is deliberate: through this skill's own review the count went from two to four, and it has grown again since, with every round leaving at least one restatement behind.

## Mechanics Live Elsewhere

- Review criteria: `code-review`.
- Delegation shape and model-tier discipline: `AGENTS.md` "Context and Delegation Discipline".
- Branch base rule (`develop` unless the task is explicitly `main`-only): `repo-worktree`.
- Finding disposition once a pull request exists, the Merge Gate, `scripts/pr_review.py`: `pr-review-conduct`, `drive-pr`.
- The receipt's key, its backends, and the three-valued exit contract a capture point folds: `scripts/README.md` "`local_review.py`".
- The unit model, the coverage ledger, and the burn-down report: `scripts/README.md` "`canonical_review.py`".
