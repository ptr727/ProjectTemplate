"""CodeRabbit's summary comment, read off one payload: whether it is in view and its clean pass.

This is a library module and has no command line. `pr_review_side.py` and `pr_review_digest.py`
import it as a sibling. CodeRabbit keeps one summary comment and edits it in place, so a summary
in view is the current one. A clean pass leaves no review object, and this summary is the only
record of it.
"""

from __future__ import annotations

import re

from pr_review_rounds import CODERABBIT_LOGIN, head_nodes, page_flag, reading, reviewer_nodes

CLEAN_PASS = re.compile(r"^No actionable comments were generated", re.MULTILINE)
REVIEWED_UP_TO = re.compile(r"\bbetween [0-9a-f]{40,64} and ([0-9a-f]{40,64})\b")
SUMMARY_MARKER = re.compile(
    r"<!--\s*This is an auto-generated comment: summarize by coderabbit\.ai"
)


def summaries(pr: dict) -> list[dict]:
    """CodeRabbit's comments in view carrying its summary marker or a reviewed range, oldest first.

    The marker stands from the summary's first state, a pass still running or skipped included, and
    a finished pass prints its range. So an empty list means no summary is in view.
    """
    nodes = reviewer_nodes(pr, "comments", CODERABBIT_LOGIN)
    return [
        n
        for n in nodes
        if SUMMARY_MARKER.search(b := n.get("body") or "") or REVIEWED_UP_TO.search(b)
    ]


def summary_blind(pr: dict) -> bool:
    """True where CodeRabbit's summary can sit behind the comments window unseen.

    One summary edited in place is all CodeRabbit keeps, so one in view decides.
    """
    return page_flag(pr, "comments", "hasPreviousPage") and not summaries(pr)


def clean_pass(pr: dict) -> str:
    """The commit CodeRabbit's summary records a clean pass on, empty where it records none.

    That pass leaves no review, only its summary saying no actionable comments were generated, each
    range it prints ending at the commit read. A rate-limit notice prints one too and never counts.
    """
    for n in reversed(summaries(pr)):
        ends = set(REVIEWED_UP_TO.findall(text := n.get("body") or ""))
        limited = reading(n, CODERABBIT_LOGIN).rate_limited
        if CLEAN_PASS.search(text) and len(ends) == 1 and not limited:
            return ends.pop()
    return ""


def rabbit_on_head(pr: dict) -> bool:
    """Whether CodeRabbit reviewed the current head, by a review on its commit or a clean pass on it."""
    reviewed = head_nodes(pr, reviewer_nodes(pr, "reviews", CODERABBIT_LOGIN))
    return bool(reviewed) or clean_pass(pr) == pr["headRefOid"]
