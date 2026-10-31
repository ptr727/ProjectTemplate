"""Squash-merge a feature pull request into `develop` once the Merge Gate's readable items are met.

Usage: pr_review.py merge NUMBER --repo OWNER/NAME [--cleanup [--checkout DIR]]

`pr_review.py merge` forwards here. Call it through that spelling, since the allow rule the
host-safety installer registers names `pr_review.py`. That rule approves any arguments, so every
check lives here. Whether the merge is authorized stays with the skill that runs it. What this
checks in code is the Merge Gate's mechanically readable items, and that the merge is current, in
the owner, and into `develop` only. A finding the pull request's body lists is judgment no code
here reads, so the driver disposes of it per `pr-review-conduct` before calling this.

It refuses unless all of these hold:

- The owner and repository pass `cleanup`'s write-scope check, before any read.
- The pull request is open, not a draft, and based on `develop`.
- Its head lives in the base repository rather than in a fork.
- Its head branch is not a permanent branch, and no ruleset or branch protection covers it.
- `wait`'s own gate, run once on a short bound and requesting no review, exits 0.
- A fresh read after the gate meets each item that 0 leaves undecided. Coverage reads full, a file
  table, or an attested local pass. No thread is unresolved. Every count the digest gives of a
  finding no thread carries reads zero. The merge state reads CLEAN.
- The head read after the gate is the head read before it.

The merge is pinned to that head with `--match-head-commit`. It is confirmed by reading the pull
request back rather than by the merge call's own answer. `--cleanup` then runs `cleanup` on the
same pull request, and its exit is this command's.

Exit 0 = merged and confirmed, or with `--cleanup` the exit `cleanup` returned. Exit 1 = refused,
nothing merged, a merge `gh` refused that reads back open among them. Exit 2 = a read did not
answer. It is also a merge call that timed out, never ran, or reads back unsettled, which may have
happened on the server. Exit 64 = the owner check. Any other code is the gate's, nothing merged.
"""

from __future__ import annotations

import argparse
import re

import pr_cleanup
import pr_review as core

Q_MERGE_TARGET = """
query($o:String!,$r:String!,$n:Int!){
  repository(owner:$o,name:$r){
    defaultBranchRef{ name }
    pullRequest(number:$n){
      state isDraft baseRefName headRefName headRefOid mergeCommit{ oid }
      headRepository{ nameWithOwner } } }}
"""

GATE_TIMEOUT = 60
MERGE_TIMEOUT = 120
UNRUN = "gh could not be run or did not finish"
MET_COVERAGE = ("full", "table", "carried:table", "local")
UNTHREADED = ("suppressed", "previously_missed", "cr_outside_diff", "qodo_open")


def merge_target(owner: str, repo: str, num: int) -> dict:
    """The pull request's live merge fields, raising `CleanupUnread` where nothing answered."""
    data = pr_cleanup.cleanup_read(Q_MERGE_TARGET, o=owner, r=repo, n=num)
    repository = data.get("repository") or {}
    pr = repository.get("pullRequest")
    pr = pr if isinstance(pr, dict) else {}
    default = (repository.get("defaultBranchRef") or {}).get("name")
    if not (pr.get("headRefOid") and pr.get("headRefName") and default):
        raise pr_cleanup.CleanupUnread(f"{owner}/{repo} #{num} returned no head or default branch")
    return {**pr, "defaultBranch": default}


def ready(owner: str, repo: str, num: int, rules: bool) -> dict | None:
    """The pull request where this merges it, else None once the refusal is printed.

    The rule read is REST, and the head branch cannot change, so the read after the gate skips it.
    """
    pr = merge_target(owner, repo, num)
    branch, base, refusals = pr["headRefName"], pr.get("baseRefName"), []
    if pr.get("state") != "OPEN":
        refusals.append(f"it reads {pr.get('state')}, not OPEN")
    if pr.get("isDraft") is not False:
        refusals.append("it is a draft")
    if base == "main":
        refusals.append(
            "its base is 'main', and a promotion merge belongs to the `merge-and-release` "
            "skill with the maintainer's go-ahead"
        )
    elif base != "develop":
        refusals.append(f"its base is {base!r}, not 'develop'")
    if not pr_cleanup.in_repository(pr, owner, repo):
        head_repo = (pr.get("headRepository") or {}).get("nameWithOwner")
        refusals.append(f"its head lives in {head_repo or 'no repository'}, not {owner}/{repo}")
    if pr_cleanup.permanent_branch(branch, pr["defaultBranch"]):
        refusals.append(f"its head branch {branch!r} is a permanent branch")
    if found := pr_cleanup.branch_rules(owner, repo, branch) if rules and not refusals else []:
        refusals.append(f"its head branch {branch!r} is covered by {', '.join(found)}")
    if why := ", and ".join(refusals):
        print(f"status=REFUSED nothing was merged into {owner}/{repo} #{num}: {why}")
        return None
    return pr


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


def unmet_items(owner: str, repo: str, num: int) -> tuple[str, list[tuple[str, str]]]:
    """The head one fresh read shows, and each gate item `wait`'s 0 leaves undecided that it fails.

    The coverage and the counts are read off the digest's own line, so a refusal names what the
    driver was shown. The threads are walked to the end, since that line reads one page.
    """
    pr = core.full_read(owner, repo, num)
    text = core.digest(owner, repo, num, pr=pr)[0]
    fields = dict(re.findall(r"(?:^| )([a-z_]+)=(\S+)", text.splitlines()[0]))
    unmet = []
    if (cover := fields.get("coverage")) not in MET_COVERAGE:
        unmet.append(
            ("COVERAGE_NOT_SHOWN", f"the digest reads coverage={cover}, so item 2 is unmet")
        )
    if opened := core.open_on_head(owner, repo, num, pr):
        unmet.append(("THREADS_UNRESOLVED", f"{len(opened)} review thread(s) are unresolved"))
    counts = [f"{k}={fields[k]}" for k in UNTHREADED if fields.get(k, "0") != "0"]
    if fields.get("overview", "").startswith("?"):
        counts.append(f"overview={fields['overview']}")
    if "  FINDINGS WITH NO THREAD (" in text:
        counts.append("FINDINGS WITH NO THREAD")
    if counts:
        unmet.append(("UNTHREADED_FINDINGS", f"the digest reads {', '.join(counts)}"))
    if (state := pr.get("mergeStateStatus")) != "CLEAN":
        unmet.append(("MERGE_NOT_CLEAN", f"the merge state reads {state}, not CLEAN"))
    return pr["headRefOid"], unmet


def merge_call(target: str, num: int, head: str) -> int | None:
    """Send the squash merge pinned to `head`, print what `gh` answered, and return its exit.

    None is a call that timed out or never ran, since only a `gh` that answered can refuse.
    """
    argv = ["gh", "pr", "merge", str(num), "--squash", "--match-head-commit", head]
    argv += ["--repo", target]
    print(f"merging: #{num} into develop on {target} at {head}")
    proc = core._run(argv, MERGE_TIMEOUT, UNRUN, text=True, encoding="utf-8")
    print(f"  gh pr merge exited {proc.returncode}")
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or "").splitlines():
            print(f"  {line}")
    return None if proc.stderr == UNRUN and not proc.stdout else proc.returncode


def merge(owner: str, repo: str, num: int, cleanup: bool, checkout: str | None) -> int:
    """Gate, merge, and confirm pull request `num`, then clean it up where asked.

    The gate's short `GATE_TIMEOUT` only absorbs a merge state still settling. A failed read inside
    the gate ends `wait` with a message, which reads as unread here rather than refused. A merge
    that landed can read OPEN for a moment, so the read back is retried on `cleanup`'s delays.
    """
    if (refused := pr_cleanup.cleanup_scope(owner, repo, "merged")) is not None:
        return refused
    target = f"{owner}/{repo}"
    try:
        pr = ready(owner, repo, num, rules=True)
    except pr_cleanup.CleanupUnread as exc:
        print(f"status=UNREAD nothing was merged: {exc}")
        return 2
    if pr is None:
        return 1
    head = pr["headRefOid"]
    try:
        code = gate(owner, repo, num)
        read_head, unmet = unmet_items(owner, repo, num) if code == 0 else (head, [])
        after = ready(owner, repo, num, rules=False) if code == 0 and not unmet else None
    except (SystemExit, pr_cleanup.CleanupUnread) as exc:
        print(f"status=UNREAD nothing was merged: a read for the gate did not answer: {exc}")
        return 2
    if code != 0:
        print(f"note: nothing was merged, since the Merge Gate read above exited {code}")
        return code
    for status, why in unmet:
        print(f"status={status} nothing was merged into {target} #{num}: {why}")
    if after is None:
        return 1
    if moved := sorted({read_head, after["headRefOid"]} - {head}):
        print(
            f"status=HEAD_MOVED nothing was merged: #{num} on {target} is at "
            f"{', '.join(moved)}, and the gate read began at {head}"
        )
        return 1
    answered = merge_call(target, num, head)
    try:
        landed = core.backoff(
            merge_target(owner, repo, num),
            lambda: merge_target(owner, repo, num),
            lambda polled: polled.get("state") != "MERGED",
            delays=pr_cleanup.DELETE_SETTLE_DELAYS,
        )
    except pr_cleanup.CleanupUnread as exc:
        print(
            f"status=MERGE_NOT_CONFIRMED the merge of #{num} on {target} was sent, and reading "
            f"it back did not answer, so the merge may have happened on the server: {exc}"
        )
        return 2
    state = landed.get("state")
    if state == "MERGED":
        commit = (landed.get("mergeCommit") or {}).get("oid") or "unknown"
        print(f"status=MERGED pr={num} head={head} merge_commit={commit}, confirmed on read back")
        return pr_cleanup.cleanup(owner, repo, num, checkout) if cleanup else 0
    if answered not in (None, 0) and state == "OPEN":
        print(
            f"status=MERGE_REFUSED gh refused the merge of #{num} on {target} with exit "
            f"{answered}, and it reads OPEN, so nothing was merged"
        )
        return 1
    print(
        f"status=MERGE_NOT_CONFIRMED #{num} on {target} reads {state} after the merge call, so "
        "this read cannot confirm a merge that may still land on the server"
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    core.utf8_console()
    ap = argparse.ArgumentParser(
        prog="pr_review.py merge",
        description="Squash-merge into develop once the Merge Gate's readable items read met.",
    )
    ap.add_argument("number", type=int)
    ap.add_argument("--repo", required=True, metavar="OWNER/NAME")
    ap.add_argument("--cleanup", action="store_true", help="run `cleanup` once merge confirms")
    ap.add_argument("--checkout", metavar="DIR", help="--cleanup: the primary clone it cleans")
    a = ap.parse_args(argv)
    if a.checkout is not None and not a.cleanup:
        ap.error("--checkout belongs to --cleanup, which this command line does not pass")
    owner, _, repo = a.repo.partition("/")
    if not owner or not repo or "/" in repo:
        ap.error(f"--repo takes OWNER/NAME, not {a.repo!r}")
    return merge(owner, repo, a.number, a.cleanup, a.checkout)
