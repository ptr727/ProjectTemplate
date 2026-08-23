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
5. When the chosen scope includes a release, first bring the hub checkout used for this procedure
   current, `git fetch origin main`, and read this repo's `releaseTrigger` from that fetched tip,
   `git show origin/main:registry/repos.json`, rather than a possibly-stale working tree copy,
   relevant when the target repo is the hub itself and this exact promotion changed its own
   registry entry. Two shapes, not three: when it reads `none`, report that no release is
   configured, dispatch and run-correlation (step 6) do not apply. Otherwise (`two-phase`,
   `dispatch-only`, or `publish-on-merge` alike), dispatch explicitly, `gh workflow run
   publish-release.yml --ref main --repo owner/repo`, or `--ref develop` only when the maintainer
   explicitly asked for a prerelease dispatch instead. `publish-on-merge`'s automatic publish is
   gated on the actor being the codegen App merging a Dependabot or codegen PR
   (operational-vs-release-workflow's publishing rules), so an ordinary human promotion merge,
   exactly what step 3 just did, never triggers it, this step's explicit dispatch is what actually
   ships the release here, not a side effect of the merge.
6. Correlate the specific run this dispatch produced rather than assuming the newest one is it.
   `gh run list --repo owner/repo --workflow publish-release.yml --branch main --event
   workflow_dispatch --json databaseId,createdAt,headSha` (or `--branch develop` for a prerelease
   dispatch), matched by `headSha` against the dispatched ref's tip (`main`'s tip confirmed in
   step 4, or `develop`'s current tip for a prerelease) and by `createdAt` against the dispatch
   time. `gh run list` can momentarily omit a just-created run, so poll this query itself for a
   bounded interval before concluding none exists, a single query reporting zero candidates is not
   yet "never started". Poll only when exactly one candidate matches, report and stop rather than
   guessing when zero remain after the bounded interval or more than one do, a concurrent run of a
   different event on the same branch must never be mistaken for this one. Poll that one run id to
   completion in one bounded background wait with an explicit timeout, `timeout <seconds> gh run
   watch <run-id> --repo owner/repo --exit-status` on a host with GNU `timeout`, or the equivalent
   bounded-wait mechanism on a host without it (macOS without coreutils, native Windows), and
   report a timeout separately from a completed run's own conclusion, the tag or version it
   produced. A run that fails, times out, or never starts is reported, never silently retried.
7. In the hub, when the chosen scope includes a release, bring this checkout to the merged
   content: `git fetch origin main`, then `git checkout -B main origin/main` to force the local
   `main` to the fetched tip regardless of what it pointed to before. `skills_install.py` stamps
   and installs from whatever this checkout's HEAD already is, so a plain `git checkout main`
   would leave a local `main` that already existed pointing at its old, pre-fetch commit, and
   skip the refresh silently. Only then run `python3 scripts/skills_install.py --report`, then
   `python3 scripts/skills_install.py` to install, and confirm `--report` now reads current,
   regardless of whether step 5 or 6 dispatched, skipped, or failed a release, this step is gated
   only on the chosen scope, never on the release outcome. This refreshes only the machine running
   this session, per skill-lifecycle, every other machine still refreshes on its own next run or
   `docs/host-setup.md` "Fleet Skills Install" cadence.
8. Run cleanup regardless of how steps 5 through 7 ended, no release configured, a dispatch
   failure, an ambiguous run match, a timeout, a failed run, or a hub Skills refresh all still
   reach this step, the merge in step 3 already landed by then. Two parts, both required, neither
   optional:
   - The promotion PR's own worktree: fetch and prune, fast-forward the base clone to `develop`,
     remove the worktree. Never delete `develop`, it is the promotion PR's own head, and the
     repo's auto-delete-head-branches setting is kept off fleet-wide for exactly this reason, so
     nothing does this automatically.
   - A defensive sweep for anything drive-pr's own cleanup should already have removed but might
     not have, an interrupted loop, a fix landed by hand outside that skill, or a maintainer
     merge in the GitHub UI. `git worktree list` for any worktree still registered under this
     task's feature branches, `git branch -vv` for any local feature branch, `git ls-remote
     --heads origin` for any matching remote feature branch. For each, verify it finished by
     reading GitHub's own state, `gh pr list --head <branch> --state merged --repo owner/repo`
     (or `gh pr view <branch> --repo owner/repo`), confirming `mergedAt` is set and the branch's
     current remote tip matches that pull request's head SHA, proving nothing landed on it since.
     `git merge-base --is-ancestor <branch> develop` must never be used for this, a squash merge
     (drive-pr's own merge method) never makes the feature tip a literal ancestor of `develop`, so
     the check reports every already-finished branch as unmerged. Only once GitHub confirms it,
     and the worktree is clean (a dirty worktree stops cleanup rather than discarding uncommitted
     work), remove the worktree, `git worktree remove`, then delete the branch. `git branch -d`
     has the identical squash blindness as `git merge-base --is-ancestor` and refuses too, so use
     `git branch -D <exact-branch>` here, safe only because the GitHub-state check just proved
     that exact branch finished, the narrow post-squash exception git-commit-conventions
     describes, never applied to an unverified branch. Then `git push origin --delete <branch>`
     for the remote side. Never apply this sweep to `develop` or `main` themselves, only to
     feature branches a drive-pr loop created.

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
