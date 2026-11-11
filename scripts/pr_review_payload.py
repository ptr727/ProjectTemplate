"""The review policy's reads off a pull request payload, and the paging that fills one in.

This is a library module and has no command line. `gql` attaches the parsed policy to each payload
it reads, under `POLICY_KEY`. A reader may not import `pr_review_policy`, which reads files and runs
`gh`. So the key and the two reads every consumer of it repeats live here instead. The writer
and the readers name this one constant, so they cannot disagree on it. The two pagers take the
walk as a callable for the same reason, so nothing here makes a call.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

POLICY_KEY = "_reviewPolicy"
FILES_UNREAD = "unread"
FILES_CAP = 3000


def default_branch(pr: dict) -> str:
    """The base repository's default branch off `pr`, empty where it was not read."""
    return ((pr.get("baseRepository") or {}).get("defaultBranchRef") or {}).get("name") or ""


def bot_entry(pr: dict | None, name: str) -> Any:
    """The policy's settings for bot `name` off `pr`, None where no policy rides the payload."""
    policy = (pr or {}).get(POLICY_KEY)
    return policy.bot(name) if policy is not None else None


def page_files(pr: dict, pages: Callable[[str], Iterable[dict]]) -> None:
    """Read the rest of `pr`'s changed-file list into it, past the full read's first page.

    `pages` walks the pages after a cursor. A list reaching `FILES_CAP` stays marked as cut short,
    and a page read at another head leaves it unread under `FILES_UNREAD`. A list cut short or
    unread is compared against nothing. A failed page read ends the run.
    """
    files = pr.get("files")
    if not isinstance(files, dict):
        return
    page = files.get("pageInfo")
    if not isinstance(page, dict) or page.get("hasNextPage") is not True:
        return
    nodes = files["nodes"]
    for more in pages(page["endCursor"]):
        if more["headRefOid"] != pr.get("headRefOid"):
            files[FILES_UNREAD] = (
                "the head moved while the changed-file list was paged, so the list describes "
                "no one commit"
            )
            return
        nodes.extend(more["files"]["nodes"])
        if len(nodes) >= FILES_CAP:
            return
    files["pageInfo"] = {"hasNextPage": False}


def page_reviews(pr: Any, pages: Callable[[], Iterable[dict]]) -> None:
    """Replace a windowed `reviews` read with every review, oldest first, where older ones exist.

    The window reads the newest reviews, so `pages` walks the whole connection forward instead. It
    starts at the first page and replaces the windowed nodes, so it holds all they held. A failed
    page read ends the run, so a list is whole or never returned, and a read holding no connection
    is left alone.
    """
    reviews = pr.get("reviews") if isinstance(pr, dict) else None
    page = reviews.get("pageInfo") if isinstance(reviews, dict) else None
    if not isinstance(page, dict) or page.get("hasPreviousPage") is not True:
        return
    pr["reviews"] = {
        "nodes": [node for more in pages() for node in more["reviews"]["nodes"]],
        "pageInfo": {"hasPreviousPage": False},
    }
