"""CodeRabbit's summary comment, read off one payload: whether it is in view and its clean pass.

This is a library module and has no command line. `pr_review_side.py` and `pr_review_digest.py`
import it as a sibling. CodeRabbit keeps one summary comment and edits it in place, so a summary
in view is the current one. A clean pass leaves no review object, and this summary is the only
record of it.
"""

from __future__ import annotations

import re

from pr_review_rounds import CODERABBIT_LOGIN, reading, reviewer_nodes

CLEAN_PASS = re.compile(r"^No actionable comments were generated", re.MULTILINE)
REVIEWED_UP_TO = re.compile(r"\bbetween [0-9a-f]{40,64} and ([0-9a-f]{40,64})\b")


def summaries(pr: dict) -> list[dict]:
    """CodeRabbit's comments in view that print a reviewed commit range, oldest first.

    Its summary prints one, so an empty list means no summary is in view.
    """
    nodes = reviewer_nodes(pr, "comments", CODERABBIT_LOGIN)
    return [n for n in nodes if REVIEWED_UP_TO.search(n.get("body") or "")]


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
