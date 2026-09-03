---
name: backlog-burndown
description: >-
  Burns a ptr727/ProjectTemplate fleet repository's open-issue backlog down by rounds: rank the
  open issues, group them so no two groups touch the same file, dispatch one subagent per group to
  drive its own feature -> develop pull request to merge, open at most one develop -> main
  promotion pull request per round for the maintainer to merge, then re-rank and go again,
  because every review round files new issues that change what the next round should pick. Use
  this whenever asked to work the backlog, burn the backlog down, clear the open issues, resolve
  or cull the backlog, or run issues in parallel until they are gone, and whenever the ask is a
  standing one rather than a single named issue. Triggers even when the backlog looks small enough
  to work by hand, because the failure it exists to prevent is two agents editing the same
  prose-heavy Markdown file in the same round, which surfaces as a merge conflict long after both
  branches are already deep in review. Drives one repository, the one the session is in, never a
  fleet-wide sweep. Ends when a re-rank finds nothing left it can act on, and never merges main,
  which stays the maintainer's own step through merge-and-release.
---

# Backlog Burndown

## Why This Exists

Asking for one issue to be fixed is `drive-pr`'s job and needs no skill above it. Asking for a
whole backlog to be worked down is a different problem, and three things about it are not obvious.
Parallelism is bounded by file overlap rather than by agent count, so the grouping decides the
throughput. The backlog is not a fixed list, since every review round files deferral issues that
belong in the next round's ranking, so a plan made once is stale by its second round. And a
prose-heavy repository conflicts on content rather than on syntax, so two agents rewording the
same section produce a conflict no tool resolves and no reviewer catches early.

## The Two Seats

Everything below turns on which seat is acting, so both are named once here.

- **The orchestrator** is the session this skill runs in. It ranks, groups, dispatches, and drives
  the promotion pull request. It opens no feature branch and fixes no issue itself, which is what
  keeps it out of every worker's files. It does write: it comments on issues, it drives and
  amends the promotion pull request, and it owns worktree and branch cleanup, which "Dispatching a
  Worker" states in full.
- **A worker** is one dispatched subagent holding one group, one worktree, and one feature branch,
  which is `AGENTS.md` "Session Scope"'s one-branch-one-deliverable rule applied as written. It
  drives its own pull request into develop and ends there.

## Scope

One repository, the one the session is in, resolved from its own `origin`. Reads are unrestricted
per `GOVERNANCE.md` "Repository Boundaries and Write Safety", so reading another repository's
issues breaks no rule. Working them is out of this skill's scope, and a fleet-wide backlog
sweep is a different request. That section bounds writes to the owner of
this repository rather than to this repository alone, and a run staying inside the one repository
it was invoked for is narrower than the rule requires, deliberately.

## What Invoking This Skill Authorizes

- Naming this skill is the maintainer's explicit go-ahead for the feature -> develop squash merges
  this run performs, in every round of it. A per-round merge question would idle every agent at
  every boundary, which is the thing this skill exists to avoid.
- **The grant is bounded by the session it was named in.** A run interrupted and resumed in a new
  session needs the skill named again, which costs one sentence and is the difference between a
  grant and a mode. A grant read back from a note is one nobody gave.
- The grant does not weaken the `pr-review-conduct` Merge Gate. It answers that gate's item 5 for
  this run's feature -> develop merges and nothing else, so a pull request with one open finding
  still does not merge.
- It is never authorization to merge a develop -> main promotion pull request, to dispatch a
  release, to close an issue on judgment, or to touch another repository. Each stays the
  maintainer's, and merging a promotion pull request is `merge-and-release`, invoked on its own.

## The Round

A round is the unit. Each one runs these steps in order.

1. **Rank** every open issue, per "Ranking".
2. **Group** the top of that ranking, per "Grouping and File Claims".
3. **Verify** each group's predicted file set against everything in flight before dispatching
   anything. A group whose files are already claimed waits for the next round.
4. **Dispatch** at most four workers, one per group, per "Dispatching a Worker".
5. **Collect** each worker's outcome: merged to develop, parked on a question, or stopped with a
   reason. Bound this wait per "Bounding the Wait on a Worker".
6. **Clean up** the worktrees, local branches, and merged remote branches of every group that has
   finished or been abandoned, per "Dispatching a Worker".
7. **Promote**, per "The Promotion Boundary".
8. **Re-rank from scratch**, and note that the next round prepares under the freeze "The Promotion
   Boundary" describes whenever a promotion pull request is still waiting on the maintainer, so it
   ranks, groups, and works locally without pushing until that merge lands. Do not carry the
   previous round's ranking forward. The deferral
   issues this round's reviews filed are now open issues with a claim on the next round's
   attention, and an issue that ranked low last round can rank high once a sibling fix lands.

## Ranking

Where the repository carries no priority label, and the hub does not, the ranking is the
orchestrator's judgment against stated criteria rather than a field read off the issue. Where a
repository does carry one, that label is the first input and these criteria order what it leaves
tied. Write the ranking, and the reason for the top of
it, into the report this skill makes at each round boundary, per "Ending the Run".

Rank on these, highest first where they conflict:

- **It blocks other work.** An issue whose fix changes a rule, a gate, or a shared contract that
  other issues' fixes must then obey is worth doing before them, not after.
- **It is a correctness or safety defect** in something that runs, over an improvement to
  something that reads.
- **It is a root cause rather than a leaf.** A parent issue grouping several filed symptoms is
  worth more than any one of its children, and fixing it may close them.
- **It is new.** An issue filed by a recent review round is evidence of something the current
  content actually got wrong, and it is the freshest context anyone has on it.
- **It is small and self-contained**, as a tie-break only. Size breaks a tie between two issues of
  equal value, and it never promotes a trivial issue over a real defect.

An issue that asks a question rather than states a defect is not ranked. It goes to the maintainer
per "Raising a Blocked Question" rather than being guessed at.

## Grouping and File Claims

Group so that **no file is claimed by two live groups at once.** This is the rule the skill exists
for, and it binds harder than any throughput target.

- **Group by the files a fix will touch**, not by the issues' subject matter. Two issues that read
  as unrelated but both edit a shared governance file are one group. Two issues that read as near
  duplicates but touch different files are two groups.
- **Genuine duplicates are one group.** Never close an issue during triage on the orchestrator's
  own judgment. Comment to cross-link the pair, and let the fix close both.
- **Closing keywords go on the promotion pull request**, not the feature pull request, per
  `operational-vs-release-workflow`. A feature pull request merging into develop fires no
  auto-close, so a `Fixes #N` line there closes nothing. Reference the issue in the feature pull
  request body, and carry the keyword to the promotion pull request, which "The Promotion
  Boundary" states in full.
- **Predict each group's file set** by reading the issues, not by guessing from their titles.
- **Record every claim on the issue itself**, as a comment naming the predicted file set **and the
  branch that holds it**, before dispatching. The branch name is what lets a later session walk
  from a worktree it found back to the claim explaining it, which is the direction the next bullet
  actually travels. Working notes do not survive the session, so a claim living only in them is
  invisible to the round that has to respect it, and recording it on the issue is what makes the
  next bullet a read of durable state rather than of the orchestrator's memory.
- **Verify the prediction before dispatching**, against everything in flight, which is wider than
  this round: the files changed by every open **feature** pull request on this repository (`gh pr
  diff <number> --name-only` per open pull request), and the claim comments of every group still
  holding a branch, parked groups from earlier rounds included. `git worktree list` reports the
  registered worktrees and the branch checked out in each, which is not the same as every branch
  that exists, so pair it with `git branch -r` for one that was pushed and whose worktree is
  already gone. Those two enumerate the branches, and the claim comments are what say which files
  each one holds, since a branch name says nothing about a file set and an unpushed branch has no
  diff to read. **Exclude the open promotion pull request from that enumeration.**
  Its diff is all of `origin/main..origin/develop`, so counting it claims nearly every file any
  earlier round touched, and a round run during the freeze would defer every group it formed. A
  predicted claim that collides with a real one is a group deferred to the next round, never one
  dispatched hoping the overlap stays small.
- **Re-verify when a worker reports that its real file set grew** beyond its claim. A worker
  needing a file another group holds stops and reports rather than editing it, and the
  orchestrator decides which group keeps the file, then tells the loser which of three things to
  do rather than leaving it to choose: narrow its change to drop that file, park until the holder
  merges, or abandon its branch and return the issue to the next round's ranking. Where the loser
  has already opened a pull request, say whether it closes or waits, since one left open on an
  abandoned branch reads to every later round as a live claim. **Update the claim comment whenever
  the adjudication changes what a group holds**, in either direction, or the durable record drifts
  from the real claim it exists to report.
- **Cap the round at four workers**, whatever the grouping allows.

## Bounding a Prose Group

A group whose files are prose-heavy Markdown bloats in a way a code group does not, and it needs
its own bound stated in the worker's brief.

- **The change stays inside the units the issue names.** Rewording an adjacent section because it
  now reads inconsistently is the next issue, filed, not this one's diff.
- **Deleting a claim beats qualifying it.** Where a review disproves something a rule leaned on,
  remove the rule that leaned on it. A narrowed qualifier is where a new false claim gets
  introduced, and it is the most common way a prose round produces the finding the following round
  then fixes.
- **Set a review-round budget before the first push.** A whole-unit prose review can run many
  rounds where a finding was introduced by the previous round's fix, so state a number in the
  brief, and when it is reached, land what is correct and file the remainder rather than churning.

## Dispatching a Worker

Brief on `AGENTS.md` "Context and Delegation Discipline"'s subagent shape.

- **The worker drives its group to a develop merge**, by invoking `drive-pr` with the target
  stated as develop only. That skill owns the review loop, the finding disposition, and the merge,
  so brief the group and the bounds rather than restating the loop.
- **The worker creates its own worktree**, as `drive-pr` step 1 and `repo-worktree`'s task-start
  mandate already require of the task itself. The one exception is a worker replacing a dead or
  stopped one, which takes over that worktree instead, per "Bounding the Wait on a Worker".
- **The worker does no cleanup**, which is this skill's one stated override of `drive-pr` step 4
  and of `repo-worktree`'s post-merge procedure. Say so in the brief, because a worker following
  either alone will clean up. The worker still performs step 4's merge itself, and what the override
  moves is that step's two cleanup halves, the worktree procedure and the verify-then-delete of the
  merged remote branch, **both** rather than only the first. "Cleanup Is the Orchestrator's" below, in this
  same section, says why and what it covers.
- **The worker runs `local-strict-review` before every push**, including one that only fixes a
  review finding.
- **The worker never merges to main**, never touches another group's files, and never resolves a
  thread it did not actually dispose of.

### Cleanup Is the Orchestrator's

`repo-worktree`'s post-merge procedure returns the base clone to current develop before proving
the cleanup, and `operational-vs-release-workflow` states that requirement independently. Four workers doing
that concurrently mutate one shared checkout, which `GOVERNANCE.md` "Repository Boundaries and
Write Safety" forbids outright by giving each task its own checkout. A worker also cannot
finish the procedure from inside its own worktree, since removing that worktree leaves it with no
working directory in which to delete its branch.

So the whole procedure moves to the orchestrator, which runs it from the base clone at the round's
cleanup step, while no worker is live in a tree it touches. It carries `drive-pr` step 4's remote
half too, verifying the merged branch's tip against the pull request's `headRefOid` before
`git push origin --delete`, since taking that step from the worker without naming a new owner
would leave a live remote branch behind every group. It covers every group that is done with its tree,
which is the finished ones **and the abandoned ones**: a group told to abandon its branch keeps a
registered worktree until something removes it, and that worktree holds a live claim that would
collide with the very group the next round re-forms for the same issue. The procedure is deferred
rather than dropped: `repo-worktree`'s verify-before-removing and prove-the-cleanup steps run
unchanged, just later and in one seat. A worker still live, a promotion fix included, keeps its
worktree until the next round's cleanup step.

### Choosing the Worker's Model Tier

`AGENTS.md` "Delegation" owns the model-tier rule. Read it there. This is only what it leaves to
judgment here: the tier is chosen per group rather than defaulted, because a stronger tier
produces better work up front and takes fewer review rounds to land it, which often costs less
than a cheaper worker looping. Three kinds of group are never tiered down:

- One touching **carried canonical content**: rule text, a Skill, or anything else this repository
  authors and other repositories carry, since a wrong rule propagates to every carrier.
- One touching **a gate, a ruleset, a release condition, or a carried governance section**, which
  is `AGENTS.md`'s own list of what counts as a design change however small the diff looks.
- One whose issues are **complex or entangled**, where the fix depends on reasoning across several
  files or on a contract not stated in the file being edited.

State the chosen tier and its reason in the round's report.

## Bounding the Wait on a Worker

`AGENTS.md` requires a wait to separate its outcomes and to be bounded, so this one is. A worker
reports merged, parked, or stopped. A worker that reports nothing at all is the case needing a
bound, since it is indistinguishable from a slow one and dying mid-drive is ordinary here.

The bound is a state read rather than a clock: when the other workers in the round have reported,
read the silent worker's branch and pull request directly, `git log` on that branch and
`gh pr view <branch>`, passing the branch as the positional argument that command takes, since a
bare `gh pr view` resolves the pull request of whatever branch the caller is standing on and never
the worker's. Let what they show decide. A pull request that is merged, or a branch whose work is
complete, means the worker died after doing the work and the group is finished.

Anything else means the group is re-dispatched, and **the replacement takes over the dead worker's
existing worktree rather than creating one**, which is the single exception to a worker creating
its own. Three things force it. `git worktree add` refuses a branch already checked out in another
worktree, that worktree is ordinarily dirty so `git worktree remove` refuses it too, and
`repo-worktree` forbids forcing either. Taking it over is not sharing a live tree, since the task
that held it is gone, and it is what lets the replacement commit whatever it finds uncommitted onto
the branch it now owns instead of that work being discarded. The orchestrator never reaches into
the tree itself to clear it, because no read tells a dead worker from a slow one well enough to
make writing there safe. Where no other worker remains to bound the wait, one direct read of that
branch and pull request after a reasonable interval answers the same question.

## Raising a Blocked Question

A group reaching a question only the maintainer can answer stops that group and nothing else.
`pr-review-conduct`'s "Escalate to the maintainer when" list is what makes a finding a question
rather than a decision.

- **The group stops, and nothing about it is disposed of.** No thread is resolved, no finding is
  answered on the orchestrator's own judgment, and no pull request merges.
- **The other groups keep driving.** One stopped group never idles the round.
- **The question travels worker to orchestrator to maintainer, and reaches the maintainer at the
  point the work stops.** A worker escalates to whoever dispatched it, per `pr-review-conduct`,
  since a dispatched subagent is not the seat that can prompt anyone. The orchestrator is that
  seat, and it asks then and there through the interface's own prompt mechanism, per
  `GOVERNANCE.md` "Communicating with the User". Holding the question for a round boundary is the
  handoff-buried-in-a-paragraph that section forbids, and a boundary can be a long way off or,
  for a group blocking the promotion pull request, never arrive at all. Where several groups stop
  close together, their questions go in one prompt, which is batching without deferral.
- **The question is also written on its issue**, so it survives the session that asked it.
- **A stopped group keeps its worktree, its branch, and its claim.** A worker resuming it takes
  that worktree over rather than creating one, for the reasons "Bounding the Wait on a Worker"
  gives: the branch is already checked out there, the tree is likely dirty, and the task that held
  it has ended, so this is a handover rather than a shared tree.

## The Promotion Boundary

Each round ends with at most one develop -> main promotion pull request, driven to green and left
for the maintainer, so that one carries a single round rather than accumulating several.

1. **Open it whenever develop is ahead of main**, which `git fetch origin` and then
   `git rev-list --count origin/main..origin/develop` answers, and this round's own outcome does
   not. Fetch first every time: a stale remote-tracking ref reports zero and the round would report
   nothing to promote while develop carries work. A round in which every group deferred or parked
   can still owe a promotion pull request, for work an earlier round landed and no promotion has
   yet carried. A count of zero is the only case with nothing to promote, and the round reports
   that instead of attempting one.
2. Drive its review loop per `drive-pr` steps 5 through 8, **with a review-round budget set before
   the first one**, the same discipline "Bounding a Prose Group" applies to a feature branch. That
   loop repeats until the promotion pull request carries no open finding, and nothing in it
   terminates on its own, so when the budget is reached, stop and put the state to the maintainer
   rather than continuing to spend the run's only forward gear on one pull request.
3. Put the ready pull request to the maintainer through the interface's own prompt mechanism,
   naming the merge as the action that unblocks the run. The maintainer's merge is the run's clock, so one
   reported in a closing paragraph and never actually asked about stalls every round behind it.
   Do not merge it.
4. **While it waits, develop takes only what that pull request itself needs.** A finding against
   it lands as its own feature -> develop pass, and that landing moving its head is expected, since
   its head **is** develop. **That pass is dispatched as a worker like any other**, which is the
   one push the freeze permits and the reason the orchestrator still opens no branch of its own.
   `drive-pr` step 6 sends the seat driving a promotion pull request back through its own steps 1
   to 4 for such a fix, and here that seat dispatches rather than drives it.
5. **A promotion fix outranks any file claim.** A group holding a file it needs yields, because the
   promotion pull request is what the whole run is queued behind. A holder that is merely parked
   yields by handing the file over. A holder that already pushed and has an open pull request
   yields by having that pull request wait, its branch untouched, and by the promotion fix taking
   the file, since the two must not be in flight on one file at once. Once the fix lands, that
   pull request narrows to drop the file, or merges develop in to pick the fix up. Never rebase it:
   its branch is already pushed, so a rebase needs the force-push `git-commit-conventions` forbids
   outright.
6. **Nothing else pushes.** The next round may rank, group, verify claims, create worktrees, and
   work locally, and a stopped group may resume locally on its answer, but neither pushes, opens a
   pull request, nor merges to develop until the promotion pull request merges.
7. The merge unfreezes the run, and the prepared round and any resumed group push then.

The run advances no faster than the maintainer merges promotion pull requests. That is the human
gate, stated plainly rather than left for a stalled round to reveal.

### Assembling the Promotion Body

The body carries one `Fixes #N` per issue whose fix is on develop and not yet on main. Derive that
set in two hops rather than one, because the commits in `origin/main..origin/develop` are squash
merges whose subjects carry the **pull request** number and not the issue number, and this skill
deliberately keeps the closing keyword off the feature pull request, so nothing in the range names
an issue directly. Read the pull request numbers out of that range, freshly fetched, then read each
of those pull requests for the issues its body references. The set is derived this way rather than
from what this round dispatched, since a group that deferred or parked contributes none and an
earlier round's work may still be uncarried. A fix landing during the freeze at step 4 adds its issue to a body already written, so
amend the body when it lands rather than leaving the issue to be closed by hand later.

## Run State

- **Working notes outside the repository hold the round**: the ranking, the working groups, the
  tier choices, and the worker assignments. A scratch file the harness gives a session serves
  where there is one, and any note kept out of the tree serves where there is not. It is working
  state, and nothing about it is committed.
- **GitHub holds what outlives the session.** A claim comment records a group's file set, a pull
  request body records what a round carried, a `Fixes #N` line records what the promotion closes, a
  deferral issue records what was put off and why, a thread reply records how a finding was
  disposed of, and a stopped group's question is a comment on its issue.
- **No tracker file is committed for the run.** A committed tracker is a file every round rewrites,
  which is the contention the file-claim rule exists to prevent.

## Ending the Run

The run ends at either of two points, and they are different endings.

- **The backlog is worked out**, meaning a full re-rank finds no open issue this skill can act on.
  That is not the same as zero open issues, since a backlog of nothing but maintainer questions is
  a finished run. Report it as finished, with the questions put to the maintainer.
- **The session ends**, for a context limit or because the maintainer stops it. The run ends with
  it, since the merge authorization was bounded to that session. What the rounds already landed
  stands on its own in GitHub, and the branches, claim comments, and questions left behind are
  what a later run reads to pick the work up. That later run is a new run, named again, not this
  one continuing.

Report at every round boundary and at either ending: what merged to develop, what the promotion
pull request carries, what was newly filed, what is stopped and on which question, and what the
next round would pick.

## Mechanics Live Elsewhere

- The review loop, the Merge Gate, the five finding outcomes, and when a finding is a question:
  `pr-review-conduct`.
- Driving one pull request, and the promotion-pull-request wrinkle: `drive-pr`.
- Worktree isolation, the base branch, and the cleanup procedure this skill re-seats:
  `repo-worktree`.
- Closing keywords, branch protection, and the promotion trap: `operational-vs-release-workflow`.
- The pre-push adversarial pass and its recorded receipt: `local-strict-review`.
- Merging the promotion pull request and dispatching a release: `merge-and-release`, invoked
  separately.
- Delegation briefing shape, model-tier rules, wait discipline, and session scope: `AGENTS.md`
  "Context and Delegation Discipline".
