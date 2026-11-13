"""The REST reads `pr_cleanup.py` makes, and the promotion shape it reads with them.

This is a library module and has no command line. Every read here is a GET through `pr_review`'s
REST helpers. A read that does not answer raises `CleanupUnread`, which `cleanup` reports at
exit 2 rather than at its refusal code.
"""

from __future__ import annotations

import re
import sys

import pr_review as core

COMMIT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class CleanupUnread(Exception):
    """A read `cleanup` needed did not produce an answer, reported at exit 2."""


def cleanup_rest(path: str, what: str, jq: str = ".") -> object:
    """One REST read for `cleanup`, retried, raising with its stderr where nothing parsed."""
    proc = core.reread(path, jq)
    data = core.parsed_json(proc)
    if data is None:
        sys.stderr.write(proc.stderr)
        raise CleanupUnread(f"the {what} read failed rc={proc.returncode} or printed no JSON")
    return data


def in_history(owner: str, repo: str, branch: str, oid: str) -> bool:
    """Whether commit `oid` is in `branch`'s history, by `pr_review.commit_state`'s comparison."""
    state = core.commit_state(owner, repo, oid, branch)
    if state == core.UNREAD:
        raise CleanupUnread(f"the comparison of {oid} with {branch} did not answer")
    return not state


def promotion_refusal(owner: str, repo: str, head: str, default: str) -> str | None:
    """Why a pull request merged into the default branch is no promotion, or None where it is one.

    The documented promotion that `develop`'s rules cannot resolve is a branch cut from the
    default branch that merges `develop`, landed by a merge commit. So its merged head is in the
    default branch's history, and that head is a merge whose second parent is in `develop`'s.
    Each fact is read from GitHub, never inferred from the head's name, which any branch can take.
    """
    if not in_history(owner, repo, default, head):
        return (
            f"its merged head {head} is not in {default!r}'s history, so no merge commit landed it"
        )
    parents = cleanup_rest(
        f"repos/{owner}/{repo}/commits/{head}", "head commit", "[.parents[].sha]"
    )
    if not isinstance(parents, list) or not all(
        isinstance(p, str) and COMMIT_ID.fullmatch(p) for p in parents
    ):
        raise CleanupUnread(f"the head commit read returned no parent list: {parents!r}"[:400])
    if len(parents) != 2:
        return f"its merged head {head} has {len(parents)} parents, so it merged no develop"
    if not in_history(owner, repo, "develop", parents[1]):
        return f"its merged head's second parent {parents[1]} is not in develop's history"
    return None
