---
name: merge-and-release
description: >-
  Merges a ready develop -> main promotion PR for any ptr727/ProjectTemplate fleet repo and, when
  asked, dispatches the release, in this hub always refreshing this machine's installed Skills
  from the newly promoted content as part of that release step, never as a separate ask. Use this
  whenever asked to merge main, ship a release, cut a release, or finish a promotion once its PR
  is already green and fully resolved (produced by drive-pr or by hand). When the request does
  not say how far ("merge main", "ship it"), ask once whether to merge only or merge and release,
  rather than guessing which the maintainer wants this time. Triggers even when the phrasing is
  as short as "merge main and release", because that already states the scope and is itself the
  explicit, current go-ahead this skill acts on without asking again, though it never substitutes
  for the pr-review-conduct Merge Gate, a promotion PR that is not actually green and fully
  resolved gets reported and stopped on, not merged.
---

# Merge and Release

## Why This Exists

Once drive-pr (or a maintainer by hand) leaves a promotion PR ready, the same two steps follow
every time: merge it, and usually dispatch the release it unblocks. In this hub a promotion can
also change `.agents/skills` content this very session depends on, so the release step always
carries a Skills refresh with it there, never a separate branch to ask about, an ambiguous "merge
and release" on the hub must not leave the maintainer unsure whether Skills got refreshed. One
skill covers all of it, scoped down by what the maintainer actually asks for.

## How Far to Go

- Read the invocation for an explicit scope first. "Just merge" or "merge only" means stop after
  the merge. "Merge and release", "ship it", or "cut a release" means also dispatch, and in this
  hub also refresh Skills as part of that same step. Act on either without asking.
- When the request names no scope ("merge main"), ask once, before merging: merge only, or merge
  and release. Recommend "merge and release" as the default on a release-model repo, a promotion
  merged without its release is the more common regret there. Recommend "merge only" as the
  default on an operational repo (registry `workflowModel: operational`), where a release is a
  separate, deliberate dispatch rather than an automatic follow-on to a promotion, per
  operational-vs-release-workflow's "Operational repositories" delta.
- Detect the hub automatically, `git remote get-url origin` or `gh repo view --json
  nameWithOwner` naming `ptr727/ProjectTemplate`. There the release scope silently includes the
  Skills refresh, a downstream repo never sees it, it has no `.agents/skills` of its own to
  refresh.

## What Invoking This Skill Authorizes

- Naming this skill, and answering its how-far question, is the maintainer's explicit, current
  go-ahead to merge the promotion PR and to perform the scope chosen, for the one repo and PR in
  front of the agent. It is never a standing mode carried to the next PR.
- It is never permission to merge a PR that fails the Merge Gate. Re-verify the gate at
  invocation time, a check from earlier in the session can be stale.

## The Procedure

1. Identify the open develop -> main promotion PR for this repo, stop and report if none is open.
2. From a hub checkout, `scripts/` is not carried into downstream repos, run `scripts/pr_review.py
   status [number] --repo owner/repo` on it and confirm the pr-review-conduct Merge Gate. Stop
   and report exactly what is missing rather than merging on a partial gate.
3. `gh pr merge [number] --merge --repo owner/repo`. Never `--delete-branch`, the promotion PR's
   head is `develop`.
4. Confirm the merge landed, `mergedAt` set, `main`'s tip matching the merge commit.
5. In the hub, when the chosen scope includes a release, first bring this checkout to the merged
   content: `git fetch origin main`, then `git checkout -B main origin/main` to force the local
   `main` to the fetched tip regardless of what it pointed to before. `skills_install.py` stamps
   and installs from whatever this checkout's HEAD already is, so a plain `git checkout main`
   would leave a local `main` that already existed pointing at its old, pre-fetch commit, and
   skip the refresh silently. Only then run `python3 scripts/skills_install.py
   --report`, then `python3 scripts/skills_install.py` to install, and confirm `--report` now
   reads current, always, not only when separately asked. This refreshes only the machine running
   this session, per skill-lifecycle, every other machine still refreshes on its own next run or
   `docs/host-setup.md` "Fleet Skills Install" cadence.
6. When the chosen scope includes a release, first check the registry's `releaseTrigger` for this
   repo in `registry/repos.json`, three shapes. Report that no release is configured and go to
   step 8 without dispatching or watching anything when it reads `none`. For `publish-on-merge`,
   the merge in step 3 is itself the trigger, no dispatch is needed, note that and go to step 7 to
   watch the run it produced. Otherwise (`two-phase` or `dispatch-only`), dispatch, `gh workflow
   run publish-release.yml --ref main --repo owner/repo`, or `--ref develop` only when the
   maintainer explicitly asked for a prerelease dispatch instead, then go to step 7.
7. Correlate the specific run this step's trigger produced rather than assuming the newest one is
   it. After an explicit dispatch, `gh run list --repo owner/repo --workflow
   publish-release.yml --branch main --event workflow_dispatch --json databaseId,createdAt` (or
   `--branch develop` for a prerelease dispatch), matched by `createdAt` against the dispatch
   time. For a `publish-on-merge` repo, the same query with `--event push` instead, matched by
   `createdAt` against the step 3 merge time. Poll only when exactly one candidate matches, report
   and stop rather than guessing when zero or more than one do, a concurrent run of a different
   event on the same branch must never be mistaken for this one. Poll that one run id to
   completion in one
   bounded background wait with an explicit timeout, `timeout <seconds> gh run watch <run-id>
   --repo owner/repo --exit-status` on a host with GNU `timeout`, or the equivalent bounded-wait
   mechanism on a host without it (macOS without coreutils, native Windows), and report a timeout
   separately from a completed run's own conclusion, the tag or version it produced. A run that
   fails, times out, or never starts is reported, never silently retried.
8. Run the repo-worktree post-merge cleanup regardless of how steps 5 through 7 ended, no release
   configured, a merge-triggered release, a dispatch failure, an ambiguous run match, a timeout,
   or a failed run all still reach this step, the merge in step 3 already landed by then and
   cleanup is never conditioned on the release outcome. Fetch and prune, fast-forward the base
   clone to
   `develop`, remove any worktree the completed task leaves behind.

## Mechanics Live Elsewhere

- The Merge Gate itself: pr-review-conduct.
- Never delete develop, no-op republish, the operational repos' dispatch-only model:
  operational-vs-release-workflow.
- What the dispatch actually builds and publishes: workflow-ci-contract.
- Skills install and report semantics: skill-lifecycle.
- Cleanup mechanics: repo-worktree.

## Stop and Report, Never Guess

- A merge conflict, a newly failing check, or a gate item that regressed since drive-pr finished
  are each a stop, report the exact state, never force or retry blindly.
- `gh pr merge` or `gh workflow run` failing is reported with its actual output, never
  suppressed, never assumed harmless on the agent's side alone.
