"""Squash-merge a feature pull request into `develop`, only once the Merge Gate reads met in code.

Usage: pr_review.py merge NUMBER --repo OWNER/NAME [--cleanup [--checkout DIR]]

`pr_review.py merge` forwards here. Call it through that spelling, since the allow rule the
host-safety installer registers names `pr_review.py`. That rule approves any arguments, so every
check lives here. Whether the merge is authorized stays with the skill that runs it. What this
guarantees is that the merge is reviewed, current, in the owner, and into `develop` only.

It refuses unless all of these hold:

- The owner passes `pr_review.py`'s write-scope check, before any read.
- The pull request is open, not a draft, and based on `develop`.
- Its head lives in the base repository rather than in a fork.
- `wait`'s own gate, run once on a short bound and requesting no review, exits 0.
- The head read back after the gate is the head read before it.

The merge is pinned to that head with `--match-head-commit`. It is confirmed by reading the pull
request back rather than by the merge call's own answer. `--cleanup` then runs `cleanup` on the
same pull request, and its exit is this command's.

Exit 0 = merged and confirmed, or with `--cleanup` the exit `cleanup` returned. Exit 1 = refused,
nothing merged. Exit 2 = a read did not answer. It is also a merge call the read back could not
confirm, so the merge may have happened on the server. Exit 64 = the write scope could not be
established or excludes the target. Any other code is the gate's, returned unchanged with its
status line, and nothing was merged.
"""

from __future__ import annotations

import argparse
import sys

import gh_graphql as shared
import pr_cleanup
import pr_review as core

Q_MERGE_TARGET = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){ pullRequest(number:$n){
    state isDraft baseRefName headRefOid mergeCommit{ oid }
    headRepository{ nameWithOwner } baseRepository{ nameWithOwner } } }}
"""

GATE_TIMEOUT = 60
MERGE_TIMEOUT = 120
MERGE_SETTLE_DELAYS = (1, 2, 4)


class MergeUnread(Exception):
    """A read `merge` needed did not produce an answer, reported at exit 2."""


def merge_target(owner: str, repo: str, num: int) -> dict:
    """The pull request's live merge fields, raising `MergeUnread` where nothing answered."""
    try:
        data = shared.request(Q_MERGE_TARGET, {"o": owner, "r": repo, "n": num}).data
    except shared.GraphQLError as exc:
        sys.stderr.write(exc.detail)
        raise MergeUnread(str(exc)) from exc
    pr = (data.get("repository") or {}).get("pullRequest")
    if not isinstance(pr, dict) or not pr.get("headRefOid"):
        raise MergeUnread(f"{owner}/{repo} #{num} returned no pull request or no head commit")
    return pr


def merge_refusals(pr: dict) -> list[str]:
    """Why the pull request is not one this merges, empty where it is."""
    refusals = []
    if pr.get("state") != "OPEN":
        refusals.append(f"it reads {pr.get('state')}, not OPEN")
    if pr.get("isDraft") is not False:
        refusals.append("it is a draft")
    base = pr.get("baseRefName")
    if base == "main":
        refusals.append(
            "its base is 'main', and a promotion merge belongs to the `merge-and-release` "
            "skill with the maintainer's go-ahead"
        )
    elif base != "develop":
        refusals.append(f"its base is {base!r}, not 'develop'")
    head_repo = (pr.get("headRepository") or {}).get("nameWithOwner") or ""
    base_repo = (pr.get("baseRepository") or {}).get("nameWithOwner") or ""
    if not head_repo or head_repo.lower() != base_repo.lower():
        refusals.append(f"its head lives in {head_repo or 'no repository'}, not {base_repo}")
    return refusals


def gate(owner: str, repo: str, num: int) -> int:
    """`wait`'s exit on the pull request, reading the gate as it stands and requesting nothing."""
    a = argparse.Namespace(
        number=num,
        timeout=GATE_TIMEOUT,
        min_rounds=0,
        check_grace=core.CHECK_GRACE,
        check_stall=core.CHECK_STALL,
        ignore_quota_signal=False,
        request=False,
    )
    return core.run_wait(owner, repo, a, may_request=False)


def merge_call(target: str, num: int, head: str) -> None:
    """Send the squash merge pinned to `head`, printing whatever `gh` answered."""
    argv = ["gh", "pr", "merge", str(num), "--squash", "--match-head-commit", head]
    argv += ["--repo", target]
    print(f"merging: #{num} into develop on {target} at {head}")
    proc = core._run(argv, MERGE_TIMEOUT, "gh could not be run", text=True, encoding="utf-8")
    print(f"  gh pr merge exited {proc.returncode}")
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or "").splitlines():
            print(f"  {line}")


def merge(owner: str, repo: str, num: int, cleanup: bool, checkout: str | None) -> int:
    """Gate, merge, and confirm pull request `num`, then clean it up where asked.

    The gate's bound is `GATE_TIMEOUT`, since `wait` is the step that waits. The bound only absorbs
    a merge state still settling. A failed read inside the gate ends `wait` with a message, which
    reads as unread here rather than refused. A merge that landed can read OPEN for a moment, so
    the read back is retried on `MERGE_SETTLE_DELAYS` before it reports.
    """
    if refused := core.scope_refusal(owner):
        return refused
    target = f"{owner}/{repo}"
    try:
        pr = merge_target(owner, repo, num)
    except MergeUnread as exc:
        print(f"status=UNREAD nothing was merged: {exc}")
        return 2
    if refusals := merge_refusals(pr):
        print(f"status=REFUSED nothing was merged into {target} #{num}: {', and '.join(refusals)}")
        return 1
    head = pr["headRefOid"]
    try:
        code = gate(owner, repo, num)
    except SystemExit as exc:
        print(f"status=UNREAD nothing was merged: the gate read did not answer: {exc}")
        return 2
    if code != 0:
        print(f"note: nothing was merged, since the Merge Gate read above exited {code}")
        return code
    try:
        after = merge_target(owner, repo, num)
    except MergeUnread as exc:
        print(f"status=UNREAD nothing was merged: {exc}")
        return 2
    if refusals := merge_refusals(after):
        print(f"status=REFUSED nothing was merged into {target} #{num}: {', and '.join(refusals)}")
        return 1
    if after["headRefOid"] != head:
        print(
            f"status=HEAD_MOVED nothing was merged: #{num} on {target} is at "
            f"{after['headRefOid']}, and the gate read began at {head}"
        )
        return 1
    merge_call(target, num, head)
    try:
        landed = core.backoff(
            merge_target(owner, repo, num),
            lambda: merge_target(owner, repo, num),
            lambda polled: polled.get("state") != "MERGED",
            delays=MERGE_SETTLE_DELAYS,
        )
    except MergeUnread as exc:
        print(
            f"status=MERGE_NOT_CONFIRMED the merge of #{num} on {target} was sent, and reading "
            f"it back did not answer, so the merge may have happened on the server: {exc}"
        )
        return 2
    if landed.get("state") != "MERGED":
        print(
            f"status=MERGE_NOT_CONFIRMED #{num} on {target} reads {landed.get('state')} after "
            "the merge call, so this read cannot confirm a merge that may still land on the server"
        )
        return 2
    commit = (landed.get("mergeCommit") or {}).get("oid") or "unknown"
    print(f"status=MERGED pr={num} head={head} merge_commit={commit}, confirmed by reading it back")
    return pr_cleanup.cleanup(owner, repo, num, checkout) if cleanup else 0


def main(argv: list[str] | None = None) -> int:
    core.utf8_console()
    ap = argparse.ArgumentParser(
        prog="pr_review.py merge",
        description="Squash-merge a pull request into develop once the Merge Gate reads met.",
    )
    ap.add_argument("number", type=int)
    ap.add_argument("--repo", required=True, metavar="OWNER/NAME")
    ap.add_argument(
        "--cleanup",
        action="store_true",
        help="run `cleanup` on the pull request once its merge is confirmed",
    )
    ap.add_argument(
        "--checkout",
        metavar="DIR",
        help="--cleanup: the primary clone whose worktree, local branch, and base are cleaned",
    )
    a = ap.parse_args(argv)
    if a.checkout is not None and not a.cleanup:
        ap.error("--checkout belongs to --cleanup, which this command line does not pass")
    owner, _, repo = a.repo.partition("/")
    if not owner or not repo or "/" in repo:
        ap.error(f"--repo takes OWNER/NAME, not {a.repo!r}")
    return merge(owner, repo, a.number, a.cleanup, a.checkout)
