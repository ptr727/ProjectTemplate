---
name: local-strict-review
description: >-
  Runs one read-only, adversarial review pass against this branch's current diff against its
  target, full file context included, on the strongest model tier the session can reach, before a
  unit of work is committed toward a pull request or claimed done. Use this whenever staged or
  unstaged work is about to become a commit on a PR-bound branch, and whenever `agent-conduct`'s
  "about to claim work is done, verified, green, or fixed" trigger fires for PR-bound work.
  Triggers even when the change looks small or the same session already judged its own diff ready,
  because a self-review pass judging its own diff inherits its own blind spots, the exact gap this
  skill exists to close before a PR-hosted reviewer closes it instead. Reuses `code-review`'s
  "Review the Change" criteria rather than restating them, and owns only this local, pre-PR moment.
  Once a pull request exists, `pr-review-conduct` and `drive-pr` own triaging and disposing of what
  a PR-hosted reviewer finds.
---

# Local Strict Review

## Why This Exists

A coding agent that finishes a unit of work, judges it ready, and opens the pull request is judging its own diff with the model, and often the blind spots, that wrote it. CodeRabbit, Qodo, and Copilot routinely find real defects that pass missed, and each round costs review latency and, for a rate-limited reviewer, shared account-wide quota. A local, full-file-context adversarial pass before the pull request exists catches the same class of defect for a fixed, smaller cost, the same reasoning that already runs local lint before a push instead of waiting for CI.

## What It Does

Dispatches one read-only subagent against this branch's full diff against its merge-base with the branch it targets: `git diff "$(git merge-base @{u} HEAD)"`. That single diff covers every commit already on the branch plus whatever is currently staged or unstaged, so it never reviews only the latest increment, and it is never empty at any of the moments this skill is invoked from, before the first commit or before a later fix commit alike. Reviewing only the most recent staged hunk would recreate the exact incremental blind spot this skill exists to close, since a fresh review of the full accumulated diff is what catches what per-push review misses.

The subagent also reads the full content of every file the diff touches, not just the hunks, since cross-file and whole-file context is exactly what incremental review misses. It reports findings only. It never fixes, stages, or commits anything.

Review criteria are `code-review`'s "Review the Change" section, reused rather than restated here, plus three traps worth calling out explicitly for a pass that runs before a human or a PR-hosted reviewer ever sees the diff: unguarded type coercions, TOCTOU/race conditions, and platform-specific behavior differences. `code-review`'s separate "Publish Every Finding" section does not apply here: this skill has no PR to post a comment on and no coverage marker to close a review with, so its own report contract below replaces that section rather than extending it.

## Running It

Follow `AGENTS.md` "Context and Delegation Discipline"'s subagent briefing shape:

```text
Task: adversarial review of this branch's diff against its merge-base with the branch it
  targets, read full surrounding files where the diff hunks alone do not give enough context.
Paths: exactly the files `git diff --name-only "$(git merge-base @{u} HEAD)"` lists, nothing
  wider.
Rules that bind this task: quote `code-review`'s "Review the Change" section into the prompt,
  plus flag unguarded type coercions, TOCTOU/race conditions, and platform-specific behavior
  differences explicitly. Do not quote "Publish Every Finding", this task's report contract is
  the Return line below, not a PR comment or a coverage marker.
Return: one finding per line, file:line, the concrete failure scenario, no severity theater.
Bounds: read-only. No edit, no stage, no commit, no push, no PR-hosted write of any kind.
If a rule you were given does not cover what you find, stop and report it. Do not guess, and do
  not read a governance file to resolve it.
```

**Model tier:** the strongest tier this session can reach, the same convention `AGENTS.md` states for delegation generally. Reaching for a weaker tier to satisfy some notion of "a different reviewer" is the tier-down mistake `AGENTS.md` "Never tier down the seat holding the judgment" forbids, extended here to reviewing. Run the pass on the same tier that authored the change when only one tier is reachable: a second, adversarially-prompted look still catches what the authoring pass's own "looks ready" judgment did not, even unchanged.

## Disposing of Findings

Every finding maps to one of `pr-review-conduct`'s five outcomes before the pull request opens: fixed, evidence-disproven, filed as a deferred issue, escalated to the maintainer for an explicit call, or, if it keeps recurring, taken as a signal to fix the class. A finding this pass raised and not fixed is never the agent's own call to just leave. Per outcome 3, that decision needs the maintainer's explicit answer, the same way a PR-hosted finding would. Running this pass is expected before every commit toward a pull request, per `agent-conduct`. Its findings stay advisory: a finding it raises does not by itself block `git commit` or `gh pr create`, the disposition above is what closes it, the same posture local lint holds today. It posts nothing to GitHub, it only reports to the session driving the work. A finding raised here and not fixed is not thereby resolved: the same finding shape reaching a PR-hosted reviewer later still gets its own fresh disposition, per `pr-review-conduct`'s "a disposition decided on one PR does not carry to the next."

## When to Run It

- After staging or committing a unit of work, before the first push toward a pull request (`drive-pr`'s Drive Loop step 2, `pr-review-conduct`'s Expected review loop step 1).
- Before pushing a fix for a reviewer finding, the same self-review blind spot applies to a fix as to the original diff (`drive-pr`'s "Disposing of Every Finding", `pr-review-conduct`'s outcome 1).
- Whenever `agent-conduct`'s "about to claim work is done, verified, green, or fixed" trigger fires for work that will become, or already is, a pull request.

## Mechanics Live Elsewhere

- Review criteria: `code-review`.
- Delegation shape and model-tier discipline: `AGENTS.md` "Context and Delegation Discipline".
- Finding disposition once a pull request exists, the Merge Gate, `scripts/pr_review.py`: `pr-review-conduct`, `drive-pr`.
