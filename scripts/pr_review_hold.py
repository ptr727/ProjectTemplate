"""What holds a head's merge beyond its checks, for `wait` and `merge` in `pr_review.py`.

This is a library module and has no command line, and nothing here makes a call. It reads a
payload already fetched. Its importers are `pr_review.py`, `pr_review_answers.py`, and
`pr_review_side.py`, which asks the reviewers whose asks and checks are read here.

A hosted reviewer asked by comment registers its check only after the ask. Until then the rollup
carries no check of its review, so a head whose other checks have all passed reads ready to
merge. The registration grace closes that window for a bounded time. It also builds the
`CHECKS_PENDING` line a held `wait` prints, since that line names the grace beside the checks.

`HOSTED_GRACE` is measured rather than chosen. Across 89 asked CodeRabbit runs on this repository,
the check registered 10 to 33 seconds after its ask. So 90 seconds leaves a margin over the most.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pr_review_checks import (
    HOSTED_REVIEWERS,
    check_nodes,
    checks_unread,
    head_commit,
    hosted_reviews_running,
    required_unregistered,
    reviewer_check,
    runs_open,
    utc_instant,
)
from pr_review_rounds import READS_AS_REVIEWER, TRUSTED_ASSOCIATIONS


def side_command(name: str, command: str = "review") -> str:
    """The exact comment body that asks `name` for a round, `command` naming CodeRabbit's kind."""
    return f"@coderabbitai {command}" if name == "coderabbit" else "/review"


def side_asks(pr: dict, body: str) -> list[dict]:
    """Each trusted, shown comment whose whole body is the request `body`, oldest first."""
    nodes = (pr.get("comments") or {}).get("nodes") or []
    asks = [
        n
        for n in nodes
        if (n.get("authorAssociation") or "") in TRUSTED_ASSOCIATIONS
        and not n.get("isMinimized")
        and (n.get("body") or "").strip() == body
    ]
    return sorted(asks, key=lambda n: n.get("createdAt") or "")


def head_arrived(pr: dict) -> str:
    """When the head reached the pull request, read as its earliest check suite's creation.

    GitHub creates a head's suites at the push, so a commit's own older timestamp never stands in.
    Empty while no suite exists yet, which every reader here takes as a head that just arrived.
    """
    suites = (head_commit(pr).get("checkSuites") or {}).get("nodes") or []
    stamps = [n["createdAt"] for n in suites if n.get("createdAt")]
    stamps += [n["since"] for n in check_nodes(pr) if n.get("since")]
    return min(stamps) if stamps else ""


def hosted_check(node: dict) -> bool:
    """Whether a check is a hosted reviewer's own, Copilot's review run included."""
    name = (node.get("name") or "").casefold()
    return bool(READS_AS_REVIEWER.search(name)) or any(
        reviewer_check(node, vocab, login) for vocab, login in HOSTED_REVIEWERS.items()
    )


HOSTED_GRACE = 90
ASK_BODIES = (side_command("coderabbit"), side_command("coderabbit", "full review"), "/review")


def stamp(at: datetime) -> str:
    """An instant as GitHub prints one, to the second in UTC."""
    return f"{at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"


def hosted_grace(pr: dict, nodes: list[dict]) -> tuple[datetime, datetime] | None:
    """The (anchor, end) of the window a hosted review's check may still register in, or None.

    The anchor is the latest of the head's push, its last check settling, and the newest trusted
    review ask. An asked reviewer registers only after the ask, and an ask can follow the checks
    by minutes. A hosted reviewer's own check, Copilot's included, is left out of the settling, so
    its conclusion never restarts the window. None where nothing anchors it. None also where a
    CodeRabbit or Qodo check that did not conclude SKIPPED is on the head, since its own hold then
    decides. A SKIPPED one is the reviewer declining an automatic review, which an ask can follow.
    """
    if any(
        reviewer_check(n, vocab, login) and (n.get("conclusion") or "") != "SKIPPED"
        for n in nodes
        if not n.get("unreadable")
        for vocab, login in HOSTED_REVIEWERS.items()
    ):
        return None
    settled = [n.get("done") or "" for n in nodes if not hosted_check(n)]
    asked = [n.get("createdAt") or "" for body in ASK_BODIES for n in side_asks(pr, body)]
    stamps = [utc_instant(s) for s in (head_arrived(pr), *settled, *asked)]
    anchor = max((s for s in stamps if s is not None), default=None)
    return None if anchor is None else (anchor, anchor + timedelta(seconds=HOSTED_GRACE))


def grace_open(pr: dict, nodes: list[dict], now: datetime) -> datetime | None:
    """When the registration grace ends, None where it does not hold the head at `now`."""
    window = hosted_grace(pr, nodes)
    return window[1] if window is not None and now < window[1] else None


def grace_note(pr: dict, nodes: list[dict], now: datetime) -> str:
    """A note on the grace for a `wait` that returns 0, empty where no grace applies.

    An open one is reported because `merge` refuses it. An ended one is reported because it
    released the merge with no hosted reviewer's check on the head.
    """
    if (window := hosted_grace(pr, nodes)) is None:
        return ""
    if now < window[1]:
        return (
            "note: no hosted reviewer's check is on this head yet, and the registration grace "
            f"holds the merge until {stamp(window[1])}, so `merge` refuses until then: wait again"
        )
    return (
        f"note: no hosted reviewer's check registered within {HOSTED_GRACE}s of "
        f"{stamp(window[0])}, the latest of the head's push, its last check settling, and its "
        "newest review ask, so the grace released the merge without one"
    )


def _unmet_note(pr: dict, required: frozenset[tuple[str, int | None]], nodes: list[dict]) -> str:
    """Names the required contexts still unmet, so a reader sees what the hold waits on."""
    unmet = required_unregistered(pr, nodes, required)
    where = ", which may sit in the rollup's unread part" if checks_unread(nodes) else ""
    return f" (required, unmet: {', '.join(unmet)}{where})" if unmet else ""


def pending_line(
    pr: dict,
    nodes: list[dict],
    now: datetime,
    grace: float,
    stall: float,
    required: frozenset[tuple[str, int | None]],
) -> str:
    """The `CHECKS_PENDING` line for a covered head still held at the timeout."""
    runs = runs_open(pr)
    hosted = hosted_reviews_running(pr, nodes, now, grace, stall)
    until = grace_open(pr, nodes, now)
    return (
        "status=CHECKS_PENDING an attested local pass covers this head, and by the timeout a "
        "check had not concluded, none this can read had posted, a workflow run on the head had "
        "not completed, a hosted reviewer was still reviewing it or had not registered inside "
        "its grace, a check the base branch requires had not registered or came from another "
        "app, or the merge was still UNKNOWN, so the merge is not ready yet: wait again, or read "
        "the checks above"
        + _unmet_note(pr, required, nodes)
        + (f" (not completed: {', '.join(runs)})" if runs else "")
        + (f" (reviewing: {', '.join(hosted)})" if hosted else "")
        + (f" (registration grace until: {stamp(until)})" if until is not None else "")
    )
