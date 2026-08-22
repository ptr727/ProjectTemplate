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
2. Run `scripts/pr_review.py status [number] --repo owner/repo` on it and confirm the
   pr-review-conduct Merge Gate. Stop and report exactly what is missing rather than merging on a
   partial gate.
3. `gh pr merge [number] --merge --repo owner/repo`. Never `--delete-branch`, the promotion PR's
   head is `develop`.
4. Confirm the merge landed, `mergedAt` set, `main`'s tip matching the merge commit.
5. In the hub, when the chosen scope includes a release, first bring this checkout to the merged
   content: `git fetch origin main`, then `git checkout main`. `skills_install.py` stamps and
   installs from whatever this checkout's HEAD already is, so skipping the fetch and checkout
   refreshes Skills from stale pre-merge content instead, and a fast-forward attempted from
   whatever branch happens to be checked out risks failing or updating the wrong branch, checking
   out `main` directly has neither risk. Only then run `python3 scripts/skills_install.py
   --report`, then `python3 scripts/skills_install.py` to install, and confirm `--report` now
   reads current, always, not only when separately asked. This refreshes only the machine running
   this session, per skill-lifecycle, every other machine still refreshes on its own next run or
   `docs/host-setup.md` "Fleet Skills Install" cadence.
6. When the chosen scope includes a release, `gh workflow run publish-release.yml --ref main
   --repo owner/repo`, or `--ref develop` only when the maintainer explicitly asked for a
   prerelease dispatch instead.
7. Poll the dispatched run to completion (`gh run list`, `gh run view`), report its conclusion
   and the tag or version it produced. A run that fails, or never starts, is reported, never
   silently retried.
8. Run the repo-worktree post-merge cleanup, fetch and prune, fast-forward the base clone to
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
