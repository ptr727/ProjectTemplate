---
name: repo-worktree
description: >-
  Mandates and mechanizes task isolation in ptr727/ProjectTemplate fleet repos: every task,
  including a continuation of a prior session's task, creates its own git worktree on its own
  feature branch before its first file edit, based on the branch work starts on (develop on both
  fleet workflow models, never whichever branch a tool defaulted to). Also wraps the mechanics:
  creating a worktree with git worktree add, the fleet layout convention, listing what is in
  flight, and removing a worktree and its branch after merge. Use this whenever about to create
  or edit files in a fleet repo, whenever starting or resuming a task, whenever the task's
  branch is already checked out in a shared checkout, and whenever creating, listing, or
  removing a worktree. Triggers even when the session was launched in the primary checkout or
  the change looks like a one-line fix, because the primary checkout is the maintainer's own
  surface and the incident this guards against was two sessions sharing one checkout, each
  session's blanket add committing the other's uncommitted files.
---

# Repo Worktree

## Why This Exists

Two agent sessions once ran concurrently in the same primary checkout, on the same feature
branch, neither knowing the other was in the tree. One session's commits swept in the other
session's uncommitted files, so two commits landed carrying work their subjects never mention,
committed by a task that never saw it. No rule fired at the moment it was violated, which is the
first file edit: the commit-time and review-time skills all run after a sweep has already
happened. This skill is that missing task-start surface. `GOVERNANCE.md` "Repository Boundaries
and Write Safety" keeps the isolation law and wins on any disagreement, and the mechanics below
are this skill's own content.

## The Mandate

- **Every task isolates into its own worktree before its first file edit.** All new work begins
  by creating a unique worktree (or clone) on its own feature branch. The primary checkout is
  the maintainer's own surface, so a session launched there isolates before writing rather than
  after noticing contention.
- **A continuation re-isolates.** A session resuming prior work finds its branch already checked
  out somewhere and naturally resumes there, and that instinct is the hazard: a branch sitting
  checked out in a shared tree is exactly how two sessions end up in one checkout. Create a
  fresh worktree for the continuation and check the branch out there.
- **The moment is the first file edit, not the commit.** By commit time another task's
  uncommitted work can already be swept into the staging area, so isolating late protects
  nothing. Reading anywhere is fine, and the worktree exists before the first write.
- **Someone else's tree stays theirs.** A branch that changes when nothing you did changed it,
  or an edit of yours reverted with no conflict, means another task is live in that tree, and
  the response is to stop rather than to re-apply the edit, per `GOVERNANCE.md` "Repository
  Boundaries and Write Safety".

## The Base Branch

Base the worktree on the branch work starts on for the repository's model, not on whichever
branch a tool defaulted to. GitHub's own "default branch" setting reads `main`, but on both
fleet workflow models work starts on `develop`, so a worktree defaulted to "the default branch"
lands on `main` and silently misses everything merged to `develop` but not yet promoted. Branch
from `develop` unless the task is explicitly about `main`-only content, per `GOVERNANCE.md`
"Branching Model". Fetch immediately before creating and base on the remote ref, because a clone
is whatever it last fetched rather than the branch it names.

## Creating a Worktree

The fleet layout convention keeps every base clone and every in-flight task visible in one
place, with no owner segment since every repo here is under one owner:

```text
~/repos/<Repo>                          base clone, on its default/working branch
~/repos/worktrees/<Repo>-<task-slug>    one worktree per in-flight task, own branch
```

```sh
git -C ~/repos/<Repo> fetch origin develop
git -C ~/repos/<Repo> worktree add ~/repos/worktrees/<Repo>-<task-slug> -b <task-branch> origin/develop
```

A continuation attaches the task's existing branch rather than forking a fresh one:

```sh
git -C ~/repos/<Repo> fetch origin <task-branch>
git -C ~/repos/<Repo> worktree add ~/repos/worktrees/<Repo>-<task-slug> <task-branch>
```

When the base clone holds only the remote-tracking ref, the same command creates the local
branch tracking `origin/<task-branch>` through git's ordinary checkout guessing, so a fresh
clone needs no separate branch setup. Git refuses to attach a branch that is already checked
out somewhere else, and that refusal is the mandate working, since the branch sitting checked
out in a shared tree is the hazard the continuation rule exists for. Return that checkout to
its own working branch first when its tree is clean, and stop when it is not, because a dirty
tree there may be another task's uncommitted work.

A machine not yet migrated to this layout still isolates exactly the same way, since the mandate
is the isolation rather than the path: create the worktree beside whatever layout the machine
has, and note that the base clone may live elsewhere than `~/repos/<Repo>`.

Claude Code's own `EnterWorktree` tool acts only on an explicit instruction from the user or the
project instructions, which is why the carried rules state this mandate in so many words. Given
a `name`, it creates the worktree under `.claude/worktrees/` inside the repo and bases it on the
GitHub default branch, which is the wrong path and the wrong base here. Create the worktree with
`git worktree add` as above, then attach with `EnterWorktree` `path:`, not `name:`.

## Listing and Cleanup

- `git worktree list`, run in any checkout of a repo, names that repo's base clone and every
  worktree with its branch. On the convention layout, one `ls ~/repos/worktrees/` reads what is
  in flight across the whole fleet.
- After the task's pull request merges, remove the worktree and its branch from the base clone:
  `git worktree remove ~/repos/worktrees/<Repo>-<task-slug>`, then `git branch -d <task-branch>`.
- A worktree that refuses removal is dirty, and force is not the fix: look at what is
  uncommitted in it first, since discarding uncommitted work runs only on explicit instruction,
  per the `git-commit-conventions` skill.
