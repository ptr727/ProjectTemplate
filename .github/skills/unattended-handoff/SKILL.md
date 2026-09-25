---
name: unattended-handoff
description: >-
  Runs a ptr727/ProjectTemplate fleet repository's handoff chain with no maintainer present: a
  lean orchestrator loops, dispatching a picker subagent that returns one handoff issue whose work
  needs no maintainer decision, creating that handoff from the open backlog where none is waiting,
  then a worker subagent that resumes the handoff, fixes it, drives its pull request as far as the
  invocation's scope allows, and closes it, or parks it when a decision turns up, leaving the
  branch open, a state comment on the handoff, a `decision` issue for the maintainer, and the
  `blocked` label on the handoff. Use this whenever asked to run the handoff loop unattended, work
  the auto-resolvable issues while the maintainer is away, keep going until nothing is left that
  needs no decision, or run handoffs overnight. Triggers even when the backlog looks small, because
  the failure it guards against is an orchestrator that reads issues, diffs, and review threads
  itself and exhausts its context after a few rounds. Scope is named at invocation: develop by
  default, main to also merge each promotion pull request, release to also dispatch the release.
  Distinct from `backlog-burndown`, which runs parallel groups with the maintainer reachable, and
  from `session-handoff`, which owns the chain's shape and the attended "resume the handoff"
  session this loop's parked links return to.
---

# Unattended Handoff

## Why This Exists

The attended session, `session-handoff`'s "The Attended Session", needs the maintainer for every
decision it meets. Most open issues need none, so a loop can work those while the maintainer is
away and hand back only the ones that do. Two things make that loop hard. Its orchestrator runs for
hours, so any detail it reads is paid for again on every later round, and a loop that reads issues
itself dies of its own context long before the backlog is empty. And a worker that meets a decision
must stop without losing its work, in a state the maintainer's next attended session picks up with
no reminder.

## The Three Seats

- **The orchestrator** is the session this skill is invoked in. It dispatches, reads one line back,
  and dispatches again. It reads no file, issue, diff, or review, runs no command, writes no
  handoff, and saves no memory. Everything it would learn by looking belongs to a subagent that
  starts empty and is discarded after one round. Because it judges nothing, it runs on the cheapest
  model tier the host offers.
- **The picker** is a subagent dispatched once per round. It chooses one handoff whose work needs
  no maintainer decision, creating one where none is waiting, and returns its number.
- **The worker** is a subagent dispatched once per round on the handoff the picker returned. It
  does the work in its own worktree and ends the handoff as done or parked.

One worker runs at a time, so no two rounds claim the same file and no round needs the grouping
`backlog-burndown` does.

## Invocation and What It Authorizes

The maintainer names the scope when invoking the skill, and the scope is the whole grant.

| Scope | A worker may merge |
| --- | --- |
| `develop`, the default | its feature -> develop pull request |
| `main` | that, then a develop -> main promotion pull request carrying only its own change |
| `release` | both, then dispatch the release that promotion unblocks |

The default keeps every main merge the maintainer's. `main` promotes each fix alone, since many
develop merges queued behind one promotion make that promotion too large to review. `release`
exists because a merge to main with no release never exercises artifact creation.

- **The grant is bounded by the session it was named in**, as `backlog-burndown`'s "What Invoking
  This Skill Authorizes" bounds its own. A run resumed in a new session needs the scope named again.
- **The grant answers the explicit-permission item of the `pr-review-conduct` Merge Gate for the
  merges its scope names, and nothing else.** A pull request with an open finding still does not
  merge, and no scope authorizes closing an issue on judgment, changing repository settings, or
  touching another repository.
- **The orchestrator passes the scope into every worker brief.** A brief is not a grant, per
  `drive-pr`, and what makes the merge authorized is the maintainer having named the scope in this
  session.

A run may also be given a round cap. Where none is named, it is 20.

## The Loop

The orchestrator holds exactly four things: the scope, the round count, the handoff numbers
dispatched so far, and the one-line outcome of each round. Each round runs:

1. **Dispatch the picker** with the brief below, and wait on the dispatch mechanism's own
   completion signal rather than polling.
2. **Read its one line.** `NONE` ends the run. A handoff number already dispatched in this run
   ends it too, since a handoff a worker neither closed nor parked means the worker failed in a
   way this seat must not investigate.
3. **Dispatch the worker** on that handoff at the tier the picker named, and wait the same way.
4. **Record its one line** and start the next round. A reply that is not one of the lines below
   ends the run rather than being read further.

The run ends at `NONE`, at the round cap, or at either stop above. Its final message lists every
round's outcome line, then the count of parked handoffs, and names the attended session
(`session-handoff`, "resume the handoff") as where they get answered. It writes nothing else.

### The Briefs

Pass these verbatim, filling the angle brackets. They name this skill rather than restating it,
which keeps the orchestrator's own context to the brief's length.

```text
Load the `unattended-handoff` skill and act as its picker for <owner>/<repo>.
Reply with exactly one line, in the picker return form that skill states.
```

```text
Load the `unattended-handoff` skill and act as its worker on handoff #<n> in <owner>/<repo>,
scope <develop|main|release>. The maintainer named that scope when invoking the run.
Reply with exactly one line, in the worker return form that skill states.
```

### Return Lines

| Seat | Line | Means |
| --- | --- | --- |
| picker | `PICK #<n> tier=<tier>` | work handoff `#<n>` on that model tier |
| picker | `NONE <reason>` | nothing left that needs no decision |
| worker | `DONE #<n> <pull request numbers>` | merged as far as the scope allows, handoff closed |
| worker | `PARKED #<n> decision #<d>` | parked on decision issue `#<d>` |

## Auto-Resolvable

An issue needs no maintainer decision when every one of these holds. Where any is unclear, it
does not qualify, since skipping one costs nothing and a guess costs a revert and a review round.

- **The right outcome is determined** by the issue together with the committed rules, and the issue
  leaves no choice open between alternatives it names.
- **Nothing on it waits on the maintainer.** It carries none of `decision`, `blocked`, `handoff`, or
  `question`, and no comment asks the maintainer something still unanswered.
- **The fix stays inside this repository's tree.** It changes no repository setting, ruleset,
  visibility, secret, or release condition, and needs no credential, account, or host the session
  lacks.
- **It reverses no settled decision** recorded in an issue, a handoff, or the rule text.
- **Nothing else is working it.** No open pull request names it, and no open handoff names it in
  its next steps.

## The Picker

1. **Resolve the repository** from the checkout's `origin`, and read the chain with
   `scripts/handoff.py tracks` from a hub checkout, per `session-handoff` "Running the Chain".
2. **Prefer an open handoff not carrying `blocked`** whose next steps are all auto-resolvable,
   oldest first, since a live link is work already framed. A handoff whose steps are partly
   auto-resolvable does not qualify, because the worker would park it at the first step that is not.
3. **Otherwise pick from the backlog.** Rank the open issues by `backlog-burndown`'s "Ranking"
   criteria, keep the auto-resolvable ones, and take the top one. Read the list with an explicit
   page size, since `gh issue list` returns 30 rows unless told otherwise.
4. **Create its handoff** with `handoff.py new --track auto-<issue>`, `--dry-run` first. The body
   carries the sections `session-handoff` "What Goes in the Body" names, with the next steps naming
   the issue and what done looks like. That skill's rules on the body bind it.
5. **Choose the worker's tier** by `backlog-burndown`'s "Choosing the Worker's Model Tier".
6. **Reply with one line.** A picker writes nothing but the handoff it creates.

## The Worker

1. **Resume the handoff** with `handoff.py resume` on its track, and re-derive live state rather
   than trusting the body, per `session-handoff` "Resuming".
2. **Isolate** in a worktree of its own, per `repo-worktree`, on the branch the handoff names or on
   `feature/auto-<issue>`.
3. **Fix and drive.** Run `local-strict-review` before every push, and drive the pull request with
   `drive-pr` to develop. Under `main` or `release`, continue to a promotion pull request carrying
   only this change and hand it to `merge-and-release`, merging only under `main` and merging and
   releasing under `release`. Every Merge Gate item other than the permission still has to hold.
4. **Wait in the foreground.** Each wait is one bounded command such as `pr_review.py wait`, run in
   the worker's own turn. A subagent receives no completion notification, so a wait handed to a
   monitor or a background task never wakes it.
5. **Park at the first decision**, per "Parking" below. That includes a merge the harness refuses
   after one retry, which is parked as ready to merge rather than routed around.
6. **Close on done.** Comment on the handoff what merged, which issues it fixed, and what it filed
   along the way, then close the handoff. The lane ends there and no successor link is written.
7. **Save no memory**, since state lives in the chain where any session on any machine reads it,
   and a lesson goes in the closing comment for the attended session to judge. Reply with one line.

## Parking

A worker parks rather than asks, since no one is present to answer. Park in this order, so that an
interruption part way leaves the work findable rather than lost.

1. **Keep the work.** Commit it and push the branch under the ordinary push rules, and leave the
   branch and any pull request open. Where the push cannot run, leave the worktree exactly as it
   stands and name it in the comment below.
2. **File the question** as an issue carrying `decision`, per `GOVERNANCE.md` "Communicating with
   the User". It states the question, the choices as the options, the recommended one first with
   its reason, and what each choice would do to the parked work.
3. **Comment the state on the handoff**: what is done, the branch and pull request, whether the
   worktree was left standing, what remains, and the decision issue it now waits on. This comment
   is what the attended session resumes from, so it is complete enough to continue with no other
   context.
4. **Label the handoff `blocked`**, per `GOVERNANCE.md` "Durable Knowledge and Self-Improvement".
   The picker skips it from then on, and the attended session takes it first.

Each of these is an outward-facing write bound by `GOVERNANCE.md` "Repository Boundaries and Write
Safety": this repository only, every identifier read live in the same run, and no output
suppressed.
