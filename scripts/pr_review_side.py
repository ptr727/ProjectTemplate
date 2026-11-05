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
    UNFINISHED,
    age,
    check_nodes,
    check_shape,
    checks_settling,
    head_commit,
    hosted_reviews_running,
)
from pr_review_rounds import (
    CODERABBIT_LOGIN,
    QODO_LOGIN,
    READS_AS_REVIEWER,
    TRUSTED_ASSOCIATIONS,
    commit_oid,
    head_nodes,
    qodo_comment,
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

    FAILURE where a check finished without passing, PENDING where one is still settling or none
    has posted inside the grace after the head arrived. A hosted reviewer's own check is left out,
    since the review it stands for is the one being held.
    """
    nodes = [n for n in check_nodes(pr) if not hosted_check(n)]
    if not nodes:
        since = age(head_arrived(pr), now) if head_arrived(pr) else None
        return "PENDING" if since is None or since <= grace else ""
    if any(check_shape(n, now, grace, stall) == "FAILED" for n in nodes):
        return "FAILURE"
    return "PENDING" if checks_settling(nodes, now, grace, stall, UNFINISHED) else ""


def hosted_check(node: dict) -> bool:
    """Whether a check is a hosted reviewer's own, Copilot's review run included."""
    name = (node.get("name") or "").casefold()
    return bool(READS_AS_REVIEWER.search(name)) or any(
        node.get("poster") == login or name == vocab for vocab, login in HOSTED_REVIEWERS.items()
    )


def started(pr: dict, name: str, now: datetime, grace: float, stall: float) -> bool:
    """Whether an automatic reviewer has begun on this head, or may still be about to.

    Its check running, or its own comment since the head arrived, means it began. Inside the grace
    after arrival it may still start, so asking then would buy a second round.
    """
    login, since = SIDE_LOGINS[name], head_arrived(pr)
    elapsed = age(since, now) if since else None
    if elapsed is None or elapsed <= grace:
        return True
    running = {r.casefold() for r in hosted_reviews_running(pr, check_nodes(pr), now, grace, stall)}
    vocab = {v for v, lg in HOSTED_REVIEWERS.items() if lg == login}
    if login.casefold() in running or vocab & running:
        return True
    own = reviewer_nodes(pr, "comments", login)
    return any((n.get("updatedAt") or n.get("createdAt") or "") >= since for n in own)


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
    """Whether a request `body` was posted since the head arrived, False while it has not."""
    since = head_arrived(pr)
    return bool(since) and any((n.get("createdAt") or "") >= since for n in side_asks(pr, body))


def side_rounds(pr: dict, name: str, body: str) -> int:
    """Rounds asked of or run by `name` on this pull request, whichever count is larger.

    CodeRabbit's formal reviews carry their commit, so its distinct reviewed heads count too. Qodo
    edits one standing comment, so its own rounds count as one once that comment exists.
    """
    asked = len(side_asks(pr, body))
    if name != "coderabbit":
        return max(asked, 1 if qodo_comment(pr) else 0)
    heads = {commit_oid(n) for n in reviewer_nodes(pr, "reviews", CODERABBIT_LOGIN)} - {None}
    return max(asked, len(heads))


def side_answered(pr: dict, name: str, body: str) -> bool:
    """Whether `name` has reviewed the current head.

    CodeRabbit answers with a formal review on the head's commit. Qodo edits its standing comment,
    so it has answered where that comment changed after both the head and the newest request.
    """
    if name == "coderabbit":
        return bool(head_nodes(pr, reviewer_nodes(pr, "reviews", CODERABBIT_LOGIN)))
    found = qodo_comment(pr)
    if found is None:
        return False
    asks = side_asks(pr, body)
    after = max(head_arrived(pr), (asks[-1].get("createdAt") or "") if asks else "")
    return (found.get("updatedAt") or found.get("createdAt") or "") >= after


def side_reviewers(pr: dict) -> list[tuple[str, review_policy.BotPolicy, str]]:
    """Each comment-triggered reviewer the payload's policy covers this base for, with its request.

    Empty where no policy rides the payload, since a reviewer the gate never named stays unnamed.
    """
    policy = pr.get(review_policy.PAYLOAD_KEY)
    if policy is None:
        return []
    default = ((pr.get("baseRepository") or {}).get("defaultBranchRef") or {}).get("name") or ""
    base = pr.get("baseRefName") or ""
    found = []
    for name in SIDE_LOGINS:
        bot = policy.bot(name)
        if bot.covers(base, default):
            found.append((name, bot, side_command(name, bot.command)))
    return found


def side_unmet(pr: dict) -> list[str]:
    """Each comment-triggered reviewer the policy gates the merge on that has not answered the head.

    One asked on this head is owed until it answers, whatever its cap reads, since the request
    already spent the round. One not yet asked is owed only while a round is still allowed.
    """
    return [
        name
        for name, bot, body in side_reviewers(pr)
        if bot.gate
        and not side_answered(pr, name, body)
        and (side_asked_on_head(pr, body) or bot.rounds_left(side_rounds(pr, name, body)))
    ]


def asks_any(pr: dict) -> bool:
    """Whether the policy on `pr` may ask any comment-triggered reviewer on this base."""
    return any(bot.requests() for _, bot, _ in side_reviewers(pr))


def request_side_reviews(
    read: Callable[[], dict],
    await_green: Callable[[], str],
    post: Callable[[str], dict],
    windows: tuple[float, float],
) -> tuple[list[str], str]:
    """Ask each comment-triggered reviewer the policy owes a round on this head, once per head.

    Returns the lines to print and what held a request back on the checks. Each reviewer is judged
    on a fresh read at the current time, read again after any wait for green checks. Nothing is sent
    where it answered or was asked since the head arrived, or its cap is spent. An automatic one is
    not asked while it has begun or may still begin, per `started`. `post` sends one comment and
    returns the created node, and an unconfirmed one ends the run, since it may have landed.
    """
    lines: list[str] = []
    held: str | None = None
    pr = read()
    for name, bot, body in side_reviewers(pr):
        if not bot.requests() or side_answered(pr, name, body) or side_asked_on_head(pr, body):
            continue
        if bot.trigger == "auto" and started(pr, name, datetime.now(UTC), *windows):
            continue
        if not bot.rounds_left(side_rounds(pr, name, body)):
            lines.append(
                f"note: the review policy allows {name} no further round, so none was asked"
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
        made = post(body)
        if not made.get("url") or (made.get("body") or "") != body:
            raise SystemExit(
                f"the request to {name} returned no URL or a different body, so it is "
                "unconfirmed: inspect the conversation before asking again, since it may have landed"
            )
        lines.append(f"auto-request: asked {name} for a round on the current head ({made['url']})")
    return lines, held or ""


def copilot_policy(pr: dict | None, requested: bool) -> review_policy.BotPolicy | None:
    """The policy's Copilot entry off `pr`, None where no policy rides it.

    A request can already be pending where the policy says Copilot is only asked explicitly. That
    draws a note, since unless a session sent it by hand, an automatic trigger was left on.
    """
    policy = (pr or {}).get(review_policy.PAYLOAD_KEY)
    copilot = policy.bot("copilot") if policy is not None else None
    if copilot is not None and copilot.trigger == "explicit" and requested:
        print(
            "note: a Copilot review was already requested when this wait started, while the "
            "review policy says Copilot is requested explicitly. If no session sent it, an "
            "automatic trigger is still on, in the account's Copilot settings or a ruleset"
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
    await_green: Callable[[], str],
    post: Callable[[str], dict],
    args: Namespace,
) -> str:
    """Send and print `request_side_reviews`'s requests, and return what held one on the checks."""
    lines, held = request_side_reviews(
        read, await_green, post, (args.check_grace, args.check_stall)
    )
    for line in lines:
        print(line)
    return held


def checks_note(held: str) -> str:
    return (
        "note: the review policy requests Copilot only once the head's checks pass, and they "
        f"read {held}, so this wait requested nothing and polls no review"
    )


def checks_status(held: str) -> int:
    """Print the status line for a head whose checks held the Copilot request back, and its exit."""
    pending = held == "PENDING"
    then = "wait again once they conclude" if pending else "fix the build and push"
    print(
        f"status=CHECKS_BEFORE_REVIEW the head's checks read {held}, and the review policy "
        f"holds the review request until they pass: {then}"
    )
    return 30 if pending else 40


def side_status(owing: list[str]) -> int:
    """Print the status line for a gated reviewer that has not answered the head, and exit 30."""
    print(
        "status=SIDE_REVIEW_PENDING the review policy gates the merge on a round from "
        f"{', '.join(owing)} on this head, and none has landed by the timeout: wait again"
    )
    return 30
