"""The review policy's side of the gate: CodeRabbit and Qodo requests, and the checks they wait on.

This is a library module and has no command line. It is apart from `pr_review.py` for a reason.
Copilot is requested through a review-request mutation. CodeRabbit and Qodo are asked by a
comment and answer in their own shapes. Keeping their reading and asking here keeps the gate's
Copilot loop the size it was. `pr_review.py` hands in the reads and the one write. So nothing here
reaches GitHub on its own, and every case can drive it with constructed payloads.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from datetime import UTC, datetime

import pr_review_policy as review_policy
from pr_review_checks import (
    HOSTED_REVIEWERS,
    age,
    check_nodes,
    check_shape,
    checks_settling,
    head_commit,
    reviewer_check,
    runs_open,
)
from pr_review_payload import bot_entry, default_branch
from pr_review_rounds import (
    CODERABBIT_LOGIN,
    QODO_LOGIN,
    READS_AS_REVIEWER,
    TRUSTED_ASSOCIATIONS,
    head_nodes,
    page_flag,
    qodo_comment,
    reviewed_heads,
    reviewer_nodes,
)

COMMENT_PATH = ("addComment", "commentEdge", "node")
SIDE_LOGINS = {"coderabbit": CODERABBIT_LOGIN, "qodo": QODO_LOGIN}


def side_command(name: str, command: str = "review") -> str:
    """The exact comment body that asks `name` for a round, `command` naming CodeRabbit's kind."""
    return f"@coderabbitai {command}" if name == "coderabbit" else "/review"


def head_arrived(pr: dict) -> str:
    """When the head reached the pull request, read as its earliest check suite's creation.

    GitHub creates a head's suites at the push, so a commit's own older timestamp never stands in.
    Empty while no suite exists yet, which every reader here takes as a head that just arrived.
    """
    suites = (head_commit(pr).get("checkSuites") or {}).get("nodes") or []
    stamps = [n["createdAt"] for n in suites if n.get("createdAt")]
    stamps += [n["since"] for n in check_nodes(pr) if n.get("since")]
    return min(stamps) if stamps else ""


def checks_hold(pr: dict, now: datetime, grace: float, stall: float) -> str:
    """What holds a review back on the head's checks, empty where nothing does.

    FAILURE where a check finished without passing, and a stuck shape's own name where one sits
    in a shape no wait clears. PENDING where one is settling, a run on the head is open, or none
    posted inside the grace after the head arrived. The open run counts as `held_checks_open`
    counts it, since a run's later jobs enter the rollup only as their `needs:` finish. A hosted
    reviewer's own check is left out, since the review it stands for is the one being held. A
    workflow run it posts from still counts while open, since a run names no reviewer it belongs to.
    DIRTY where no check has posted on a conflicted head, which runs no `pull_request` workflow.
    """
    nodes = [n for n in check_nodes(pr) if not hosted_check(n)]
    if any(n.get("unreadable") for n in nodes):
        return "UNREAD"
    if not nodes and pr.get("mergeStateStatus") == "DIRTY":
        return "DIRTY"
    shapes = {check_shape(n, now, grace, stall) for n in nodes}
    if "FAILED" in shapes:
        return "FAILURE"
    if stuck := sorted(shapes - {"", "RUNNING_LONG"}):
        return stuck[0]
    if runs_open(pr) or checks_settling(nodes, now, grace, stall):
        return "PENDING"
    since = age(head_arrived(pr), now) if head_arrived(pr) and not nodes else None
    return "PENDING" if not nodes and (since is None or since <= grace) else ""


def hosted_check(node: dict) -> bool:
    """Whether a check is a hosted reviewer's own, Copilot's review run included."""
    name = (node.get("name") or "").casefold()
    return bool(READS_AS_REVIEWER.search(name)) or any(
        reviewer_check(node, vocab, login) for vocab, login in HOSTED_REVIEWERS.items()
    )


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


def side_asked_on_head(pr: dict, body: str) -> bool:
    """Whether a request `body` was posted since the head was committed.

    The commit time is the one stamp that never moves once the head exists. A suite's creation
    comes later, so an ask posted before the first suite would read as older than the head. An ask
    for an earlier head, made after this one was committed, counts for this one too. That costs at
    most one missed ask, never a duplicate paid round.
    """
    since = head_commit(pr).get("committedDate") or ""
    return bool(since) and any((n.get("createdAt") or "") >= since for n in side_asks(pr, body))


def side_rounds(pr: dict, name: str, body: str) -> int:
    """Rounds asked of or run by `name` on this pull request, whichever count is larger.

    CodeRabbit's formal reviews carry their commit, so its distinct reviewed heads count too. Qodo
    edits one standing comment, so its own rounds count as one once that comment exists.
    """
    asked = len(side_asks(pr, body))
    if name != "coderabbit":
        return max(asked, 1 if qodo_comment(pr) else 0)
    return max(asked, reviewed_heads(pr, CODERABBIT_LOGIN))


def side_answered(pr: dict, name: str) -> bool:
    """Whether `name` has reviewed the current head, known for CodeRabbit alone.

    CodeRabbit answers with a formal review on the head's commit. Qodo edits one standing comment
    that names no head, so a request is the only per-head record of it.
    """
    return name == "coderabbit" and bool(
        head_nodes(pr, reviewer_nodes(pr, "reviews", CODERABBIT_LOGIN))
    )


def side_reviewers(pr: dict) -> list[tuple[str, review_policy.BotPolicy, str]]:
    """Each comment-triggered reviewer the payload's policy covers this base for, with its request.

    Empty where no policy rides the payload, since a reviewer the gate never named stays unnamed.
    """
    default, base = default_branch(pr), pr.get("baseRefName") or ""
    found = []
    for name in SIDE_LOGINS:
        bot = bot_entry(pr, name)
        if bot is not None and bot.covers(base, default):
            found.append((name, bot, side_command(name, bot.command)))
    return found


def asks_any(pr: dict) -> bool:
    """Whether the policy on `pr` may ask any comment-triggered reviewer on this base."""
    return any(bot.requests() for _, bot, _ in side_reviewers(pr))


def request_side_reviews(
    read: Callable[[], dict],
    await_green: Callable[[], str],
    post: Callable[[str], dict],
    phase: str,
) -> list[str]:
    """Ask each comment-triggered reviewer the policy asks explicitly in `phase`, once per head.

    `phase` is the `when` value this call serves, so an on-open ask never waits behind a check
    wait. A reviewer that answered or was asked since the head arrived is skipped before any wait,
    and judged again on a fresh read after one. So a request that a concurrent session sent during
    the wait is seen. Nothing is sent where its cap is spent. Nor where a cap applies and the comment
    window is cut, since the rounds spent cannot then be counted. The merge never waits on these
    reviewers, so a held ask is a note rather than an exit. `post` sends one comment and returns
    the created node, and an unconfirmed one ends the run, since it may have landed. A pull request
    no longer open is asked nothing, since a peer can merge or close it while `wait` polls.
    """
    lines: list[str] = []
    held: str | None = None
    pr = read()
    if pr.get("state", "OPEN") != "OPEN":
        return lines
    for name, bot, body in side_reviewers(pr):
        if not bot.requests() or bot.when != phase:
            continue
        if side_answered(pr, name) or side_asked_on_head(pr, body):
            continue
        if not bot.rounds_left(side_rounds(pr, name, body)):
            lines.append(
                f"note: the review policy allows {name} no further round, so none was asked"
            )
            continue
        if bot.capped() and page_flag(pr, "comments", "hasPreviousPage"):
            lines.append(
                f"note: {name}'s rounds are capped and the comments run past the window, so its "
                "rounds cannot be counted and none was asked"
            )
            continue
        if bot.waits_for_checks():
            if held is None:
                held, pr = await_green(), read()
            if held:
                lines.append(
                    f"note: {name} is asked only once the head's checks pass, and they read {held}"
                )
                continue
            if side_answered(pr, name) or side_asked_on_head(pr, body):
                continue
        made = post(body)
        if not made.get("url") or (made.get("body") or "") != body:
            raise SystemExit(
                f"the request to {name} returned no URL or a different body, so it is "
                "unconfirmed: inspect the conversation before asking again, since it may have landed"
            )
        lines.append(f"auto-request: asked {name} for a round on the current head ({made['url']})")
    return lines


def copilot_policy(pr: dict | None, requested: bool) -> review_policy.BotPolicy | None:
    """The policy's Copilot entry off `pr`, None where no policy rides it.

    A request can already be pending where the policy says Copilot is only asked explicitly. That
    draws a note, since unless a session or an earlier `wait` sent it, an automatic trigger is on.
    """
    copilot = bot_entry(pr, "copilot")
    if copilot is not None and copilot.trigger == "explicit" and requested:
        print(
            "note: a Copilot review was already requested when this wait started, while the "
            "review policy says Copilot is requested explicitly. If neither a session nor an "
            "earlier wait sent it, an automatic trigger is still on, in the account's Copilot "
            "settings or a ruleset"
        )
    return copilot


def holds_for_checks(copilot: review_policy.BotPolicy | None, forced: bool) -> bool:
    """Whether the Copilot request waits for green checks, which `--request` skips."""
    return copilot is not None and copilot.waits_for_checks() and not forced


def await_green(
    read: Callable[[], dict], start: float, args: Namespace, backoff: Callable[..., dict]
) -> str:
    """Poll the head's checks with `backoff` until none is settling, and name what holds a review.

    Empty once they pass. Otherwise what `checks_hold` reads, PENDING where `args.timeout` ran out
    first. So a failed build is never reviewed before it is fixed.
    """
    windows = (args.check_grace, args.check_stall)

    def holding(p: dict) -> bool:
        return checks_hold(p, datetime.now(UTC), *windows) == "PENDING"

    polled = backoff(read(), read, holding, start, args.timeout)
    return checks_hold(polled, datetime.now(UTC), *windows)


def ask(
    read: Callable[[], dict],
    args: Namespace,
    send: Callable[..., dict],
    dig: Callable[..., dict],
    pr: dict | bool,
    phase: str,
) -> None:
    """Send and print the `phase` requests, where `pr` is a payload whose policy asks any.

    The checks an after-checks ask waits on are read once rather than polled. So the ask never
    holds `wait` past the Copilot poll it follows, nor holds a wait that stops without one.
    """
    if not isinstance(pr, dict) or not asks_any(pr):
        return

    def post(body: str) -> dict:
        return dig(send(body=body), *COMMENT_PATH)

    def checks_now() -> str:
        return checks_hold(read(), datetime.now(UTC), args.check_grace, args.check_stall)

    for line in request_side_reviews(read, checks_now, post, phase):
        print(line)


def checks_note(held: str) -> str:
    return (
        "note: the review policy requests Copilot only once the head's checks pass, and they "
        f"read {held}, so this wait requested nothing and polls no review"
    )


def checks_status(held: str) -> int:
    """Print the status line for a head whose checks held the Copilot request back, and its exit."""
    then, code = {
        "PENDING": ("wait again once they conclude", 30),
        "FAILURE": ("fix the build and push", 40),
        "DIRTY": ("resolve the conflict with the base, since a conflicted head runs no checks", 44),
    }.get(held, ("read the checks, since that shape is one no wait clears", 44))
    print(
        f"status=CHECKS_BEFORE_REVIEW the head's checks read {held}, and the review policy "
        f"holds the review request until they pass: {then}"
    )
    return code
