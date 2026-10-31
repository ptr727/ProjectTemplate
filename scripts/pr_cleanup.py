#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["markdown-it-py>=4"]
# ///
"""Delete a merged pull request's head branch, and sweep what interrupted sessions left behind.

Usage: pr_cleanup.py NUMBER --repo OWNER/NAME [--checkout DIR]
       pr_cleanup.py --sweep --repo OWNER/NAME --checkout DIR [--apply] [--min-age-hours H]

`pr_review.py cleanup` forwards here, so a command line naming either script runs the same code.

The verify-then-delete `drive-pr` and `merge-and-release` prescribe. Call it through
`pr_review.py cleanup`, the spelling the host-safety installer's allow rule names. That
rule approves any arguments, so every check lives here. It takes the pull
request number, never a branch name. It refuses unless all of these hold:

- The pull request is merged into `develop` from a head in `--repo` itself.
- The head is not `develop`, `main`, or the default branch.
- The owner and repository pass `handoff.py`'s write-scope check.
- No ruleset or protection covers the branch.
- No open pull request has it as head or base.

The tip is checked against the pull
request's `headRefOid` before the delete and the delete is confirmed by reading the ref back.

With `--checkout <primary clone>`, a local half follows a remote half that exited 0. Every refusal
is decided before anything is deleted. A locked, dirty, or mid-operation worktree refuses, as does
a branch tip neither at the merged head nor behind it. The local half removes the worktrees
holding the branch, deletes the local branch, and fast-forwards the clone to `develop`, never a
reset, stash, or force. A clone left behind is reported and does not fail the run.

`cleanup --sweep --checkout <primary clone>` takes no pull request. It reports what is provably
finished, one `would-remove` or `keep <reason>` line per worktree or branch, and removes it only
with `--apply`. An item must sit untouched for `--min-age-hours` (default 24) and be clean. A
branch is finished when a pull request from `--repo` merged it at its exact tip or past it. It is
also finished when `origin/develop` or `origin/main` contains it. For a merged develop pull request the sweep also
deletes the remote head branch through the same checks as `cleanup <n>`. Permanent branches,
locked worktrees, and worktrees mid-operation are never touched.

Exit 0 = deleted and confirmed gone, or already gone, or a sweep that completed. Exit 1 = refused,
a moved tip naming both commits among them. Exit 2 = a read did not answer, a partial one included.
A delete or removal that could not be confirmed also exits 2, as does argparse's own usage error.
Exit 64 = the write scope could not be established or excludes the target, checked before the
registry and before any read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

import gh_graphql as shared
import handoff
import pr_review as core

Q_CLEANUP_TARGET = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){
    defaultBranchRef{ name }
    pullRequest(number:$n){
      state isDraft merged baseRefName headRefName headRefOid mergeCommit{ oid }
      headRepository{ nameWithOwner } } }}
"""
Q_CLEANUP_REF = """
query($o:String!,$r:String!,$q:String!){
  repository(owner:$o,name:$r){ ref(qualifiedName:$q){ id target{ oid } } }}
"""
Q_CLEANUP_HEADS = """
query($o:String!,$r:String!,$b:String!,$after:String){
  repository(owner:$o,name:$r){
    heads: pullRequests(states:OPEN,headRefName:$b,first:100,after:$after){
      pageInfo{ hasNextPage endCursor } nodes{ number headRepository{ nameWithOwner } } } }}
"""
Q_CLEANUP_BASES = """
query($o:String!,$r:String!,$b:String!){
  repository(owner:$o,name:$r){
    bases: pullRequests(states:OPEN,baseRefName:$b,first:20){ totalCount nodes{ number } } }}
"""
Q_SWEEP_DEFAULT = """
query($o:String!,$r:String!){ repository(owner:$o,name:$r){ defaultBranchRef{ name } }}
"""
Q_SWEEP_MERGED = """
query($o:String!,$r:String!,$b:String!){
  repository(owner:$o,name:$r){
    merged: pullRequests(states:MERGED,headRefName:$b,first:50){
      totalCount nodes{ number baseRefName headRefOid headRepository{ nameWithOwner } } } }}
"""
M_DELETE_REF = """
mutation($refId:ID!){
  deleteRef(input:{refId:$refId}){ clientMutationId }}
"""
DELETE_SETTLE_DELAYS = (1, 2, 4)


class CleanupUnread(Exception):
    """A read `cleanup` needed did not produce an answer, reported at exit 2."""


def cleanup_read(query: str, **variables: object) -> dict:
    """One GraphQL read for `cleanup`, raising rather than ending the run at exit 1.

    Exit 1 is `cleanup`'s refusal code, so a read that could not answer must not end on it.
    """
    try:
        return shared.request(query, variables).data
    except shared.GraphQLError as exc:
        sys.stderr.write(exc.detail)
        raise CleanupUnread(str(exc)) from exc


def cleanup_rest(path: str, what: str) -> object:
    """One REST read for `cleanup`, retried, raising with its stderr where nothing parsed."""
    proc = core.reread(path, ".")
    data = core.parsed_json(proc)
    if data is None:
        sys.stderr.write(proc.stderr)
        raise CleanupUnread(f"the {what} read failed rc={proc.returncode} or printed no JSON")
    return data


def cleanup_ref(owner: str, repo: str, branch: str) -> dict | None:
    """The branch's ref node, or None where the branch does not exist."""
    data = cleanup_read(Q_CLEANUP_REF, o=owner, r=repo, q=f"refs/heads/{branch}")
    repository = data.get("repository")
    if not isinstance(repository, dict):
        raise CleanupUnread(f"{owner}/{repo} returned no repository for the ref read")
    ref = repository.get("ref")
    if ref is None:
        return None
    oid = (ref.get("target") or {}).get("oid") if isinstance(ref, dict) else None
    if not isinstance(ref, dict) or not ref.get("id") or not oid:
        raise CleanupUnread(f"the ref read for {branch!r} returned no id and tip: {ref!r}")
    return ref


def branch_rules(owner: str, repo: str, branch: str) -> list[str]:
    """What protects the branch, rulesets and classic protection alike, empty where nothing does."""
    if not core.safe_ref(branch):
        raise CleanupUnread(
            f"the head branch {branch!r} holds a character that changes a REST path, so the "
            "rules applying to it cannot be read"
        )
    rules = cleanup_rest(f"repos/{owner}/{repo}/rules/branches/{branch}", "branch rules")
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise CleanupUnread(f"the branch rules read returned no list of rules: {rules!r}"[:400])
    found = [f"ruleset rule {r.get('type') or 'unnamed'}" for r in rules]
    detail = cleanup_rest(f"repos/{owner}/{repo}/branches/{branch}", "branch protection")
    protected = detail.get("protected") if isinstance(detail, dict) else None
    if not isinstance(protected, bool):
        raise CleanupUnread(f"the branch read returned no protected flag: {detail!r}"[:400])
    if protected:
        found.append("branch protection")
    return found


def permanent_branch(branch: str, default: str) -> bool:
    """Whether `branch` is one a feature pull request's merge or cleanup never takes as its head."""
    return branch in ("develop", "main", default)


def in_repository(node: dict, owner: str, repo: str) -> bool:
    """Whether a pull request's head lives in `owner/repo`, a fork's same-named branch being none."""
    name = (node.get("headRepository") or {}).get("nameWithOwner") or ""
    return name.lower() == f"{owner}/{repo}".lower()


def local_heads(owner: str, repo: str, branch: str) -> list[object]:
    """The number of each open pull request whose head is the branch in `owner/repo`."""

    def fetch(cursor: str | None) -> dict:
        extra = {"after": cursor} if cursor else {}
        return cleanup_read(Q_CLEANUP_HEADS, o=owner, r=repo, b=branch, **extra)

    numbers: list[object] = []
    try:
        for parent in shared.walk(fetch, ("repository", "heads"), strict_page=True):
            nodes = parent["heads"].get("nodes")
            if not isinstance(nodes, list) or not all(isinstance(n, dict) for n in nodes):
                raise CleanupUnread(
                    f"the open pull request read returned no heads list: {nodes!r}"[:400]
                )
            numbers += [n.get("number") for n in nodes if in_repository(n, owner, repo)]
    except shared.GraphQLError as exc:
        raise CleanupUnread(str(exc)) from exc
    return numbers


def branch_users(owner: str, repo: str, branch: str) -> list[str]:
    """The open pull requests in this repository whose head or base is the branch."""
    users = [f"#{number} (head)" for number in local_heads(owner, repo, branch)]
    data = cleanup_read(Q_CLEANUP_BASES, o=owner, r=repo, b=branch)
    repository = data.get("repository")
    if not isinstance(repository, dict):
        raise CleanupUnread(f"{owner}/{repo} returned no repository for the open pull request read")
    found = repository.get("bases")
    nodes = found.get("nodes") if isinstance(found, dict) else None
    total = found.get("totalCount") if isinstance(found, dict) else None
    if not isinstance(nodes, list) or not isinstance(total, int):
        raise CleanupUnread(f"the open pull request read returned no bases list: {found!r}"[:400])
    users += [f"#{n.get('number')} (base)" for n in nodes if isinstance(n, dict)]
    if total > len(nodes):
        users.append(f"{total - len(nodes)} more with the branch as base")
    return users


def pr_target(owner: str, repo: str, num: int) -> dict:
    """The pull request's live fields beside its repository's default branch, for both writes.

    `merge` reads it before and after its merge, and `cleanup` before its delete. One read keeps
    the two agreeing on the head and branch they act on. Raises `CleanupUnread` where nothing
    answered.
    """
    data = cleanup_read(Q_CLEANUP_TARGET, o=owner, r=repo, n=num)
    repository = data.get("repository") or {}
    pr = repository.get("pullRequest")
    default = (repository.get("defaultBranchRef") or {}).get("name")
    if not isinstance(pr, dict) or not default:
        raise CleanupUnread(f"{owner}/{repo} #{num} returned no pull request or no default branch")
    if not pr.get("headRefName") or not pr.get("headRefOid"):
        raise CleanupUnread(f"{owner}/{repo} #{num} returned no head branch or head commit")
    return {**pr, "defaultBranch": default}


class CleanupPlan(NamedTuple):
    branch: str
    head: str
    ref: dict | None


def cleanup_check(owner: str, repo: str, num: int) -> CleanupPlan:
    """Every read-only check on pull request `num` and its remote branch, with no write.

    Raises `CleanupRefused` carrying the whole refusal text, and `CleanupUnread` on any
    unanswered read. A branch anything protects is refused whatever that protection would allow.
    The write guard's per-branch rule check never sees a delete made from inside this script.
    """
    target = f"{owner}/{repo}"
    pr = pr_target(owner, repo, num)
    branch, head = pr["headRefName"], pr["headRefOid"]
    head_repo = (pr.get("headRepository") or {}).get("nameWithOwner") or ""
    refusals = []
    if pr.get("merged") is not True:
        refusals.append("the pull request is not merged")
    if pr.get("baseRefName") != "develop":
        refusals.append(f"its base is {pr.get('baseRefName')!r}, not 'develop'")
    if not in_repository(pr, owner, repo):
        refusals.append(f"its head lives in {head_repo or 'no repository'}, not {target}")
    if permanent_branch(branch, pr["defaultBranch"]):
        refusals.append(f"its head branch {branch!r} is a permanent branch")
    if refusals:
        raise CleanupRefused(
            "REFUSED", f"nothing was deleted from {target} #{num}: {', and '.join(refusals)}"
        )

    ref = cleanup_ref(owner, repo, branch)
    if ref is None:
        return CleanupPlan(branch, head, None)
    rules = branch_rules(owner, repo, branch)
    if rules:
        raise CleanupRefused(
            "REFUSED",
            f"nothing was deleted: {branch!r} on {target} is covered by "
            f"{', '.join(rules)}, so its delete is the maintainer's call",
        )
    users = branch_users(owner, repo, branch)
    if users:
        raise CleanupRefused(
            "REFUSED",
            f"nothing was deleted: open pull requests use {branch!r} on {target}, "
            f"{', '.join(users)}, and deleting it would close or retarget them",
        )
    tip = ref["target"]["oid"]
    if tip != head:
        raise CleanupRefused(
            "TIP_MOVED",
            f"nothing was deleted: {branch!r} on {target} is at {tip}, and #{num} merged "
            f"{head}, so something reached the branch after the merge or the name was reused",
        )
    return CleanupPlan(branch, head, ref)


def cleanup_fresh(owner: str, repo: str, plan: CleanupPlan) -> CleanupPlan:
    """Read the remote branch again right before its delete, so the delete uses a fresh node id."""
    if plan.ref is None:
        return plan
    ref = cleanup_ref(owner, repo, plan.branch)
    if ref is None:
        return CleanupPlan(plan.branch, plan.head, None)
    tip = ref["target"]["oid"]
    if tip != plan.head:
        raise CleanupRefused(
            "TIP_MOVED",
            f"nothing was deleted: {plan.branch!r} on {owner}/{repo} is at {tip} just before "
            f"its delete, and the pull request merged {plan.head}",
        )
    return CleanupPlan(plan.branch, plan.head, ref)


def cleanup_remote_delete(owner: str, repo: str, plan: CleanupPlan) -> int:
    """Delete the remote branch `plan` verified and read it back.

    A delete that reports a failure may still have landed, so the read back decides either way.
    A ref still at the deleted tip is read again on `DELETE_SETTLE_DELAYS` before it is reported.
    """
    target = f"{owner}/{repo}"
    branch = plan.branch
    if plan.ref is None:
        print(f"status=ALREADY_GONE branch={branch} is not on {target}, so nothing was deleted")
        return 0
    tip = plan.ref["target"]["oid"]
    print(f"deleting: {branch} on {target} at {tip}")
    try:
        response = shared.request(M_DELETE_REF, {"refId": plan.ref["id"]}).data
        print(f"  response: {json.dumps(response)[:400]}")
    except shared.GraphQLError as exc:
        print(f"  the delete reported a failure: {exc}")
        sys.stderr.write(exc.detail)
    try:
        after = core.backoff(
            cleanup_ref(owner, repo, branch),
            lambda: cleanup_ref(owner, repo, branch),
            lambda ref: ref is not None and ref["target"]["oid"] == tip,
            delays=DELETE_SETTLE_DELAYS,
        )
    except CleanupUnread as exc:
        print(
            f"status=DELETE_NOT_CONFIRMED the delete of {branch!r} on {target} was sent, and "
            f"reading the ref back did not answer: {exc}"
        )
        return 2
    if after is not None and after["target"]["oid"] != tip:
        print(
            f"status=DELETE_NOT_CONFIRMED {branch!r} on {target} reads at "
            f"{after['target']['oid']}, a tip other than the one deleted, so something pushed to "
            "the branch"
        )
        return 2
    if after is not None:
        print(
            f"status=DELETE_NOT_CONFIRMED {branch!r} on {target} still reads at {tip} "
            "after the delete"
        )
        return 2
    print(f"status=DELETED branch={branch} oid={tip}, confirmed by reading the ref back")
    return 0


def _cleanup(owner: str, repo: str, num: int) -> int:
    """The checks, the delete, and the read back, raising `CleanupUnread` on any unanswered read."""
    try:
        plan = cleanup_check(owner, repo, num)
    except CleanupRefused as exc:
        print(f"status={exc.status} {exc}")
        return 1
    return cleanup_remote_delete(owner, repo, plan)


def cleanup_scope(owner: str, repo: str, done: str = "deleted") -> int | None:
    """The exit code where the write scope refuses or cannot be established, else None.

    `done` names the write in the refusal text, since `merge` checks the same scope.
    """
    ok, why = core.in_scope(owner)
    if not ok:
        print(f"status=OUT_OF_SCOPE nothing was {done}: {why}")
        return 64
    try:
        handoff.require_write_scope(argparse.Namespace(cmd="cleanup", repo=f"{owner}/{repo}"))
    except handoff.Refusal as exc:
        print(f"status=REFUSED nothing was {done}, refused by handoff.py's write scope: {exc}")
        return 1
    except handoff.Execution as exc:
        print(f"status=UNREAD nothing was {done}: {exc}")
        return 2
    return None


def cleanup(owner: str, repo: str, num: int, checkout: str | None = None) -> int:
    """Delete the head branch of merged pull request `num` once every check passes.

    Returns 0 where the branch is deleted and confirmed gone, or is already gone, and 1 on a
    refusal. Returns 2 where a read did not answer or the delete could not be confirmed. Returns
    64 where the write scope `comment` and `reply` check excludes the target. Past that check,
    the owner is bound to the fleet registry. The repository must be registered or a fork, by
    `handoff.py`'s own write-scope check, reused rather than copied so the two cannot drift
    apart. Its refusal text is printed as `handoff.py` words it.

    With `checkout`, the local half follows a remote half that exited 0. The checkout and every
    local refusal are judged before the remote delete. A local refusal then means nothing at all
    was deleted, the remote branch included. A re-check after the delete can still refuse when a
    concurrent session changed the branch or a worktree. The output then names what happened.
    """
    refused = cleanup_scope(owner, repo)
    if refused is not None:
        return refused
    target = f"{owner}/{repo}"
    primary = None
    try:
        if checkout is not None:
            primary = cleanup_primary(checkout, target)
        plan = cleanup_check(owner, repo, num)
        if primary is not None:
            cleanup_preflight(primary, plan.branch, plan.head)
            cleanup_leave_branch(primary, plan.branch)
            plan = cleanup_fresh(owner, repo, plan)
    except CleanupRefused as exc:
        if exc.status in ("REFUSED", "TIP_MOVED", "BAD_CHECKOUT"):
            print(f"status={exc.status} {exc}")
        else:
            print(f"status={exc.status} nothing was deleted, remote branch included: {exc}")
        return 1
    except CleanupUnread as exc:
        print(f"status=UNREAD nothing was deleted: {exc}")
        return 2
    code = cleanup_remote_delete(owner, repo, plan)
    if code != 0 or primary is None:
        return code
    try:
        return cleanup_local(primary, plan.branch, plan.head)
    except CleanupRefused as exc:
        print(f"status={exc.status} {exc}")
        return 1
    except CleanupUnread as exc:
        print(f"status=UNREAD the local half stopped: {exc}")
        return 2


ON_WINDOWS = os.name == "nt"


class CleanupRefused(Exception):
    """A local check refused, carrying the `status=` name to print, reported at exit 1."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class Worktree(NamedTuple):
    path: str
    head: str
    branch: str | None
    locked: bool
    prunable: bool


GITHUB_REMOTE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|ssh://[^@/]+@github\.com/|[^@/]+@github\.com:)"
    r"([^/]+)/([^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def cleanup_git(
    root: str, *args: str, ok: tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess[str]:
    """One git call in `root` as an argv list, raising with its stderr on an unexpected code."""
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CleanupUnread(f"`git {' '.join(args)}` did not run: {exc}") from exc
    if proc.returncode not in ok:
        sys.stderr.write(proc.stderr)
        raise CleanupUnread(f"`git {' '.join(args)}` failed rc={proc.returncode}")
    return proc


def cleanup_primary(checkout: str, target: str) -> str:
    """The real path of the primary checkout at `checkout`, refusing anything else."""
    probe = cleanup_git(
        checkout,
        "rev-parse",
        "--absolute-git-dir",
        "--git-common-dir",
        "--show-toplevel",
        ok=(0, 128),
    )
    lines = probe.stdout.splitlines()
    if probe.returncode != 0 or len(lines) != 3:
        raise CleanupRefused("BAD_CHECKOUT", f"{checkout!r} is not a git checkout with a work tree")
    git_dir, common, top = lines
    if os.path.realpath(git_dir) != os.path.realpath(os.path.join(checkout, common)):
        raise CleanupRefused(
            "BAD_CHECKOUT", f"{checkout!r} is a linked worktree, not a primary one"
        )
    url = cleanup_git(top, "config", "--get", "remote.origin.url", ok=(0, 1)).stdout.strip()
    found = GITHUB_REMOTE.match(url)
    if found is None or f"{found[1]}/{found[2]}".lower() != target.lower():
        shown = re.sub(r"//[^/@]*@", "//", url) or "unset"
        raise CleanupRefused("BAD_CHECKOUT", f"origin of {top} is {shown}, not {target}")
    return os.path.realpath(top)


def cleanup_worktrees(primary: str) -> list[Worktree]:
    """Every worktree git lists for the clone, the primary first."""
    out = cleanup_git(primary, "worktree", "list", "--porcelain").stdout
    trees = []
    for block in out.split("\n\n"):
        fields = dict(line.partition(" ")[::2] for line in block.splitlines())
        if "worktree" not in fields:
            continue
        branch = fields.get("branch")
        trees.append(
            Worktree(
                path=os.path.realpath(fields["worktree"]),
                head=fields.get("HEAD", ""),
                branch=branch.removeprefix("refs/heads/") if branch else None,
                locked="locked" in fields,
                prunable="prunable" in fields,
            )
        )
    return trees


def cleanup_tip(primary: str, ref: str) -> str | None:
    """The commit `ref` names, or None where it does not exist."""
    proc = cleanup_git(primary, "rev-parse", "--verify", "--quiet", ref, ok=(0, 1))
    return proc.stdout.strip() or None


def cleanup_dirty(path: str, untracked: bool) -> bool:
    """Whether the work tree at `path` has changes, without refreshing its index.

    A refreshed index would make an untouched tree look touched to the age rule of `--sweep`.
    """
    flag = "--untracked-files=all" if untracked else "--untracked-files=no"
    out = cleanup_git(path, "--no-optional-locks", "status", "--porcelain", flag).stdout
    return bool(out.strip())


IN_PROGRESS = (
    "rebase-merge",
    "rebase-apply",
    "BISECT_LOG",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
)


def cleanup_admin(path: str) -> str | None:
    """The git directory holding the worktree at `path`'s state, or None where unreadable."""
    if os.path.isdir(os.path.join(path, ".git")):
        return os.path.join(path, ".git")
    try:
        text = Path(path, ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    return os.path.join(path, text.removeprefix("gitdir:").strip())


def cleanup_in_progress(path: str) -> bool:
    """Whether a rebase, bisect, merge, cherry-pick, or revert is paused in the worktree."""
    admin = cleanup_admin(path)
    return admin is not None and any(os.path.exists(os.path.join(admin, n)) for n in IN_PROGRESS)


def cleanup_operation_names(path: str) -> set[str]:
    """The branch names a rebase or bisect paused in the worktree at `path` is working on."""
    admin = cleanup_admin(path)
    names: set[str] = set()
    if admin is None:
        return names
    for name in ("rebase-merge/head-name", "rebase-apply/head-name", "BISECT_START"):
        try:
            text = Path(admin, name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        names.add(text.removeprefix("refs/heads/"))
    return names


def cleanup_cwd_inside(path: str) -> bool:
    """Whether this process's working directory is the worktree at `path` or inside it."""
    try:
        here = os.path.realpath(os.getcwd())
    except OSError:
        return False
    return here == path or here.startswith(path + os.sep)


def cleanup_remove_worktree(primary: str, path: str) -> bool:
    """Remove the worktree at `path` without force and read the list back for it.

    Returns whether this process was standing inside it. The process moves to the primary
    checkout first. Windows cannot free a directory the parent shell holds, so callers refuse
    there before the first removal.
    """
    inside = cleanup_cwd_inside(path)
    if inside:
        os.chdir(primary)
    try:
        cleanup_git(primary, "worktree", "remove", path)
    except CleanupUnread:
        if any(w.path == path for w in cleanup_worktrees(primary)):
            raise
        if os.path.exists(path):
            raise CleanupUnread(f"git failed to remove {path}, which is unlisted but still on disk")
        print(f"  git reported an error, yet {path} is no longer listed, so it is already gone")
        return inside
    if any(w.path == path for w in cleanup_worktrees(primary)):
        raise CleanupUnread(f"the worktree {path} is still listed after its removal")
    return inside


def cleanup_moved_note() -> None:
    print("  note: the shell's directory was that worktree and is gone, so change directory")


def cleanup_delete_branch(primary: str, branch: str, tip: str) -> bool:
    """Delete the local branch while it still names `tip`, and read it back.

    Returns whether this call deleted it, False where it was already gone. The tip is read again
    just before the delete. Git's own refusal then covers a branch that is checked out, being
    rebased, or being bisected in any worktree. It is raised as a refusal.
    """
    ref = f"refs/heads/{branch}"
    now = cleanup_tip(primary, ref)
    if now is None:
        return False
    if now != tip:
        raise CleanupRefused(
            "LOCAL_TIP_MOVED", f"{ref} reads {now} just before its delete, and {tip} was expected"
        )
    proc = cleanup_git(primary, "branch", "-D", "--", branch, ok=(0, 1))
    if proc.returncode != 0:
        if cleanup_tip(primary, ref) is None:
            return False
        raise CleanupRefused("WORKTREE_KEPT", f"git kept {ref}: {proc.stderr.strip()}")
    if cleanup_tip(primary, ref) is not None:
        raise CleanupUnread(f"{ref} still exists after its delete")
    return True


def cleanup_base(primary: str, apply: bool = True) -> bool:
    """Fast-forward the primary checkout to current `develop`, never reset, stash, or force.

    Returns whether the clone is current, printing a status line for each way it is not. A clone
    on another branch, with tracked changes, or that cannot fast-forward is left alone. No
    outcome here is an error, so callers report it and never fail a run on it. Without `apply`
    it only reports the fast-forward it would make.
    """
    try:
        current = cleanup_git(primary, "symbolic-ref", "--quiet", "--short", "HEAD", ok=(0, 1))
        on = current.stdout.strip()
        if on != "develop":
            print(f"keep base-not-on-develop {primary} is on {on or 'a detached head'}")
            return False
        if cleanup_dirty(primary, False):
            print(f"status=BASE_DIRTY {primary} has tracked changes, so it was left where it is")
            return False
        behind = cleanup_git(
            primary, "merge-base", "--is-ancestor", "HEAD", "origin/develop", ok=(0, 1)
        )
        if behind.returncode == 1:
            print(f"status=BASE_NOT_FF develop in {primary} cannot fast-forward to origin/develop")
            return False
        if not apply and cleanup_tip(primary, "HEAD") != cleanup_tip(
            primary, "refs/remotes/origin/develop"
        ):
            print(f"would-fast-forward develop in {primary}")
            return True
        if apply:
            try:
                cleanup_git(primary, "merge", "--ff-only", "origin/develop")
            except CleanupUnread:
                if cleanup_tip(primary, "HEAD") != cleanup_tip(
                    primary, "refs/remotes/origin/develop"
                ):
                    raise
    except CleanupUnread as exc:
        print(f"status=BASE_UNREAD develop in {primary} could not be brought current: {exc}")
        return False
    print(f"  develop in {primary} is at origin/develop")
    return True


def cleanup_behind(primary: str, tip: str, head: str) -> bool:
    """Whether commit `tip` is an ancestor of `head`, so it holds nothing `head` lacks."""
    proc = cleanup_git(primary, "merge-base", "--is-ancestor", tip, head, ok=(0, 1, 128))
    return proc.returncode == 0


def cleanup_preflight(primary: str, branch: str, head: str) -> tuple[str | None, list[Worktree]]:
    """Fetch, then decide every local refusal, returning the branch tip and the worktrees to remove."""
    cleanup_git(primary, "fetch", "--prune", "origin")
    ref = f"refs/heads/{branch}"
    tip = cleanup_tip(primary, ref)
    if tip is not None and tip != head and not cleanup_behind(primary, tip, head):
        raise CleanupRefused(
            "LOCAL_TIP_MOVED",
            f"{ref} is at {tip}, which is neither the merged head {head} nor behind it, "
            "so it may carry commits that were never pushed",
        )
    mirror = cleanup_tip(primary, f"refs/remotes/origin/{branch}")
    if mirror is not None and mirror != head:
        raise CleanupRefused(
            "TIP_MOVED",
            f"nothing was deleted: origin/{branch} is at {mirror}, and the pull request merged "
            f"{head}, so something reached the branch after the merge",
        )
    linked = []
    trees = cleanup_worktrees(primary)
    others = [w for w in trees if w.path != primary]
    for w in trees:
        if not w.prunable and branch in cleanup_operation_names(w.path):
            raise CleanupRefused(
                "WORKTREE_KEPT", f"{w.path} has a rebase or bisect in progress on {branch!r}"
            )
    holds = any(w.path == primary and w.branch == branch for w in cleanup_worktrees(primary))
    if holds and cleanup_in_progress(primary):
        raise CleanupRefused(
            "WORKTREE_KEPT", f"{primary} has a merge, cherry-pick, revert, or bisect in progress"
        )
    if holds and cleanup_dirty(primary, False):
        raise CleanupRefused(
            "BASE_DIRTY", f"{primary} has {branch!r} checked out with tracked changes"
        )
    for w in (w for w in others if w.branch == branch):
        why = (
            "it is locked"
            if w.locked
            else "a rebase, bisect, merge, cherry-pick, or revert is in progress in it"
            if cleanup_in_progress(w.path)
            else "it holds submodules, which only git's own check may remove"
            if os.path.exists(os.path.join(w.path, ".gitmodules"))
            else "run from a directory outside it, such as the base clone"
            if ON_WINDOWS and cleanup_cwd_inside(w.path)
            else None
        )
        if why is not None:
            raise CleanupRefused("WORKTREE_KEPT", f"{w.path}, {why}")
        if not w.prunable and cleanup_dirty(w.path, True):
            raise CleanupRefused("WORKTREE_DIRTY", f"{w.path} has uncommitted changes")
        linked.append(w)
    return tip, linked


def cleanup_leave_branch(primary: str, branch: str) -> None:
    """Move the primary checkout to `develop` where it holds `branch`, refusing if git will not.

    This runs before the remote delete, so a checkout that fails still deletes nothing.
    """
    if not any(w.path == primary and w.branch == branch for w in cleanup_worktrees(primary)):
        return
    try:
        cleanup_git(primary, "checkout", "develop")
    except CleanupUnread as exc:
        raise CleanupRefused(
            "WORKTREE_KEPT", f"{primary} could not leave {branch}, so the branch was kept"
        ) from exc
    print(f"  switched {primary} off {branch} to develop")


def cleanup_local(primary: str, branch: str, head: str) -> int:
    """Remove the finished branch's worktrees and local branch, then return the base clone."""
    try:
        tip, linked = cleanup_preflight(primary, branch, head)
    except CleanupRefused as exc:
        raise CleanupRefused(exc.status, f"nothing local was removed: {exc}") from exc
    for w in linked:
        moved = cleanup_remove_worktree(primary, w.path)
        print(f"  removed worktree {w.path}")
        if moved:
            cleanup_moved_note()
    if tip is None:
        print(f"  local branch {branch} is already gone")
    else:
        try:
            deleted = cleanup_delete_branch(primary, branch, tip)
        except CleanupUnread as exc:
            print(f"status=LOCAL_DELETE_NOT_CONFIRMED {exc}")
            return 2
        if deleted:
            print(f"  deleted local branch {branch} at {tip}, confirmed by reading the ref back")
        else:
            print(f"  local branch {branch} is already gone")
    current = cleanup_base(primary)
    try:
        first = cleanup_git(primary, "status", "--short", "--branch").stdout.splitlines()[:1]
        print(f"  {first[0] if first else 'status unavailable'}")
    except CleanupUnread:
        print("  status unavailable")
    try:
        print(f"  worktrees listed: {len(cleanup_worktrees(primary))}")
    except CleanupUnread:
        print("  worktrees listed: unavailable")
    base = "current" if current else "kept"
    print(f"status=CLEANED branch={branch} oid={head} locally, base={base}")
    return 0


class CleanupSweep:
    """One pass over a clone's worktrees and local branches, removing what is provably finished.

    Nothing here is a failure unless a read did not answer or a removal could not be confirmed.
    `problems` counts those, so the run continues with the other items and still exits 2.
    """

    def __init__(self, owner: str, repo: str, primary: str, apply: bool, min_age: float) -> None:
        self.owner = owner
        self.repo = repo
        self.target = f"{owner}/{repo}"
        self.primary = primary
        self.apply = apply
        self.min_age = min_age
        self.problems = 0
        self.permanent = {"develop", "main"}
        self.common = ""
        self.operating: set[str] = set()

    def keep(self, reason: str, what: str) -> None:
        print(f"keep {reason} {what}")

    def failed(self, what: str, exc: CleanupUnread) -> None:
        self.problems += 1
        print(f"unread {what}: {exc}")

    def newest_hours(self, paths: list[str]) -> float:
        """Hours since the newest mtime among `paths`, infinite where none can be read."""
        stamps = []
        for path in paths:
            try:
                stamps.append(os.stat(path).st_mtime)
            except OSError:
                continue
        return float("inf") if not stamps else (time.time() - max(stamps)) / 3600

    def worktree_hours(self, w: Worktree) -> float | None:
        """The idle hours of a linked worktree, or None where its admin directory is unreadable."""
        admin = cleanup_admin(w.path)
        if admin is None:
            return None
        files = [os.path.join(admin, n) for n in ("HEAD", "index", os.path.join("logs", "HEAD"))]
        return self.newest_hours([w.path, *files])

    def finished(self, branch: str, tip: str) -> tuple[str, int | None] | None:
        """How `branch` at `tip` is known finished, with the develop pull request's number if any.

        A merged pull request counts only from a head in this repository whose last commit is
        `tip`. A reused branch name or a later commit then never rides on an old merge.
        """
        data = cleanup_read(Q_SWEEP_MERGED, o=self.owner, r=self.repo, b=branch)
        found = ((data.get("repository") or {}).get("merged")) or {}
        nodes = found.get("nodes")
        total = found.get("totalCount")
        if not isinstance(nodes, list) or not isinstance(total, int) or total > len(nodes):
            raise CleanupUnread(f"the merged pull request read for {branch!r} was incomplete")
        matches = [
            n
            for n in nodes
            if isinstance(n, dict)
            and in_repository(n, self.owner, self.repo)
            and (
                n.get("headRefOid") == tip
                or cleanup_behind(self.primary, tip, n.get("headRefOid") or "")
            )
        ]
        matches.sort(key=lambda n: n.get("baseRefName") != "develop")
        if matches:
            num = matches[0].get("number")
            return f"merged PR #{num}", num if matches[0].get("baseRefName") == "develop" else None
        for upstream in ("origin/develop", "origin/main"):
            if cleanup_behind(self.primary, tip, f"refs/remotes/{upstream}"):
                return f"contained in {upstream}", None
        return None

    def remote(self, num: int | None, branch: str) -> None:
        """Report, or with `apply` run, the remote half for a merged develop pull request."""
        if num is None:
            return
        if not self.apply:
            try:
                plan = cleanup_check(self.owner, self.repo, num)
            except CleanupRefused as exc:
                print(f"keep-remote via #{num}: {exc}")
            except CleanupUnread as exc:
                self.failed(f"remote branch of #{num}", exc)
            else:
                if plan.ref is None:
                    print(f"remote already gone {branch} via #{num}")
                else:
                    print(f"would-remove remote branch {branch} via #{num}")
            return
        print(f"  remote half of #{num}:")
        try:
            code = _cleanup(self.owner, self.repo, num)
        except CleanupUnread as exc:
            self.failed(f"remote branch of #{num}", exc)
            return
        if code == 2:
            self.problems += 1
        elif code != 0:
            print(f"kept-remote #{num}, the local removal stands")

    def run(self) -> int:
        cleanup_git(self.primary, "fetch", "--prune", "origin")
        found = cleanup_read(Q_SWEEP_DEFAULT, o=self.owner, r=self.repo)
        default = ((found.get("repository") or {}).get("defaultBranchRef") or {}).get("name")
        if not default:
            raise CleanupUnread(f"{self.target} returned no default branch")
        self.permanent.add(default)
        self.common = os.path.realpath(
            os.path.join(
                self.primary,
                cleanup_git(self.primary, "rev-parse", "--git-common-dir").stdout.strip(),
            )
        )
        trees = cleanup_worktrees(self.primary)
        linked = [w for w in trees if w.path != self.primary]
        for w in trees:
            if w.path == self.primary and w.branch:
                self.permanent.add(w.branch)
        for w in trees:
            if not w.prunable:
                self.operating |= cleanup_operation_names(w.path)
        pruned = self.prune([w for w in linked if w.prunable and not w.locked])
        held = {w.branch for w in trees if w.branch and w.path not in pruned}
        for w in linked:
            if not w.prunable or w.locked:
                self.linked(w)
        listed = cleanup_git(
            self.primary, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads/"
        ).stdout.splitlines()
        for line in listed:
            name, _, tip = line.partition("\0")
            branch = name.removeprefix("refs/heads/")
            if branch not in held and branch not in self.permanent:
                self.loose(branch, tip)
        self.base()
        return 2 if self.problems else 0

    def prune(self, stale: list[Worktree]) -> set[str]:
        """Prune the entries whose directory is gone and whose record is old enough."""
        old = []
        for w in stale:
            hours = self.entry_hours(w)
            if hours < self.min_age:
                self.keep("too-recent", f"prunable worktree entry {w.path} idle {hours:.1f}h")
            else:
                old.append(w)
        if not old:
            return set()
        if not self.apply:
            for w in old:
                print(f"would-remove prunable worktree entry {w.path}")
            return {w.path for w in old}
        expire = f"{int(self.min_age * 3600)}.seconds.ago"
        try:
            cleanup_git(self.primary, "worktree", "prune", "--expire", expire)
            left = {w.path for w in cleanup_worktrees(self.primary)}
        except CleanupUnread as exc:
            self.failed("prunable worktree entries", exc)
            return set()
        for w in old:
            if w.path in left:
                self.problems += 1
                print(f"unread prunable worktree entry {w.path} is still listed after the prune")
            else:
                print(f"removed prunable worktree entry {w.path}")
        return {w.path for w in old if w.path not in left}

    def entry_hours(self, w: Worktree) -> float:
        """The idle hours of a missing worktree's record, from the `index` file prune reads."""
        base = os.path.join(self.common, "worktrees")
        try:
            names = os.listdir(base)
        except OSError:
            return 0.0
        for name in names:
            try:
                text = Path(base, name, "gitdir").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if os.path.realpath(os.path.dirname(os.path.join(base, name, text))) == w.path:
                return self.newest_hours([os.path.join(base, name, "index")])
        return 0.0

    def linked(self, w: Worktree) -> None:
        what = f"worktree {w.path}" + (f" branch={w.branch}" if w.branch else " detached")
        try:
            if w.locked:
                self.keep("locked", what)
                return
            if cleanup_in_progress(w.path):
                self.keep("in-progress-operation", what)
                return
            if os.path.exists(os.path.join(w.path, ".gitmodules")):
                self.keep("submodules", what)
                return
            if ON_WINDOWS and cleanup_cwd_inside(w.path):
                self.keep("cwd-inside", what)
                return
            if w.branch in self.permanent:
                self.keep("permanent-branch", what)
                return
            if w.branch in self.operating:
                self.keep("in-progress-operation", what)
                return
            hours = self.worktree_hours(w)
            if hours is None:
                self.keep("age-unreadable", what)
                return
            if hours < self.min_age:
                self.keep("too-recent", f"{what} idle {hours:.1f}h of {self.min_age:g}h")
                return
            if cleanup_dirty(w.path, True):
                self.keep("dirty", what)
                return
            if w.branch is None:
                return self.detached(w, what)
            tip = cleanup_tip(self.primary, f"refs/heads/{w.branch}")
            if tip is None:
                self.keep("branch-unreadable", what)
                return
            done = self.finished(w.branch, tip)
            if done is None:
                self.keep("unmerged-commits", what)
                return
            evidence, num = done
            line = f"{what} tip={tip} evidence={evidence}"
            if not self.apply:
                print(f"would-remove {line}")
                self.remote(num, w.branch)
                return
            if cleanup_remove_worktree(self.primary, w.path):
                cleanup_moved_note()
            print(f"removed worktree {w.path}")
            try:
                deleted = cleanup_delete_branch(self.primary, w.branch, tip)
            except CleanupRefused as exc:
                print(f"kept branch {w.branch} after removing its worktree: {exc}")
                return
            if deleted:
                print(f"removed branch {w.branch} tip={tip} evidence={evidence}")
            else:
                print(f"branch {w.branch} was already gone")
            self.remote(num, w.branch)
        except CleanupUnread as exc:
            self.failed(what, exc)

    def detached(self, w: Worktree, what: str) -> None:
        contained = cleanup_git(
            self.primary,
            "for-each-ref",
            "--contains",
            w.head,
            "refs/remotes/origin",
            "--count=1",
            "--format=%(refname)",
        ).stdout.strip()
        if not contained:
            self.keep("unpushed-commits", what)
            return
        if not self.apply:
            print(f"would-remove {what} head={w.head} evidence=contained in {contained}")
            return
        if cleanup_remove_worktree(self.primary, w.path):
            cleanup_moved_note()
        print(f"removed worktree {w.path} head={w.head} evidence=contained in {contained}")

    def loose(self, branch: str, tip: str) -> None:
        """A local branch no worktree holds, judged by the age of its reflog."""
        what = f"branch {branch}"
        try:
            if branch in self.operating:
                self.keep("in-progress-operation", what)
                return
            log = os.path.join(self.common, "logs", "refs", "heads", branch)
            if not os.path.exists(log):
                self.keep("age-unreadable", what)
                return
            hours = self.newest_hours([log])
            if hours < self.min_age:
                self.keep("too-recent", f"{what} idle {hours:.1f}h of {self.min_age:g}h")
                return
            done = self.finished(branch, tip)
            if done is None:
                self.keep("unmerged-commits", what)
                return
            evidence, num = done
            if not self.apply:
                print(f"would-remove {what} tip={tip} evidence={evidence}")
                self.remote(num, branch)
                return
            if cleanup_delete_branch(self.primary, branch, tip):
                print(f"removed {what} tip={tip} evidence={evidence}")
            else:
                print(f"{what} was already gone")
            self.remote(num, branch)
        except CleanupRefused as exc:
            reason = "tip-moved" if exc.status == "LOCAL_TIP_MOVED" else "checked-out-elsewhere"
            self.keep(reason, f"{what}: {exc}")
        except CleanupUnread as exc:
            self.failed(what, exc)

    def base(self) -> None:
        """Return the primary checkout to current `develop`, reporting what it could not do."""
        cleanup_base(self.primary, self.apply)


def cleanup_sweep(owner: str, repo: str, checkout: str, apply: bool, min_age: float) -> int:
    """Report, or with `apply` remove, the clone's worktrees and branches that are provably done.

    Exits 0 where the pass completed. Exits 2 where a read did not answer or a removal could
    not be confirmed, the other items having been tried either way.
    """
    refused = cleanup_scope(owner, repo)
    if refused is not None:
        return refused
    try:
        primary = cleanup_primary(checkout, f"{owner}/{repo}")
        sweep = CleanupSweep(owner, repo, primary, apply, min_age)
        code = sweep.run()
    except CleanupRefused as exc:
        print(f"status={exc.status} nothing was removed: {exc}")
        return 1
    except CleanupUnread as exc:
        print(f"status=UNREAD the sweep stopped: {exc}")
        return 2
    mode = "removed" if apply else "reported only, pass --apply to remove"
    print(f"status=SWEPT {mode}, problems={sweep.problems}")
    return code


def main(argv: list[str] | None = None) -> int:
    core.utf8_console()
    ap = argparse.ArgumentParser(prog="pr_cleanup.py")
    ap.add_argument("number", type=int, nargs="?")
    ap.add_argument("--repo", required=True, metavar="OWNER/NAME")
    ap.add_argument(
        "--checkout",
        metavar="DIR",
        help="the primary clone whose worktree, local branch, and base are cleaned after the "
        "remote branch, and the one `--sweep` cleans",
    )
    ap.add_argument(
        "--sweep",
        action="store_true",
        help="take no pull request, and report the finished worktrees and branches of --checkout",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="--sweep: remove what the report lists, including the remote head branch of a "
        "merged develop pull request, through the same checks as a numbered cleanup",
    )
    ap.add_argument(
        "--min-age-hours",
        type=float,
        default=None,
        help="--sweep: hours an item must sit untouched before it is eligible (default 24)",
    )
    a = ap.parse_args(argv)
    if a.number is None and not a.sweep:
        ap.error("the following arguments are required: number")
    if a.sweep and a.number is not None:
        ap.error("--sweep takes no pull request number")
    if a.sweep and a.checkout is None:
        ap.error("--sweep requires --checkout, the primary clone it sweeps")
    if not a.sweep and (a.apply or a.min_age_hours is not None):
        ap.error("--apply and --min-age-hours belong to `cleanup --sweep`")
    if a.min_age_hours is not None and a.min_age_hours < 0:
        ap.error("--min-age-hours cannot be negative")
    owner, _, repo = a.repo.partition("/")
    if not owner or not repo or "/" in repo:
        ap.error(f"--repo takes OWNER/NAME, not {a.repo!r}")
    if a.sweep:
        hours = 24.0 if a.min_age_hours is None else a.min_age_hours
        return cleanup_sweep(owner, repo, a.checkout, a.apply, hours)
    return cleanup(owner, repo, a.number, a.checkout)


if __name__ == "__main__":
    sys.exit(main())
