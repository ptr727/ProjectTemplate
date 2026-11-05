"""The review policy's side of the gate: CodeRabbit and Qodo requests, and the checks they wait on.

This is a library module and has no command line. It is apart from `pr_review.py` for a reason.
Copilot is requested through a review-request mutation. CodeRabbit and Qodo are asked by a
comment and answer in their own shapes. Keeping their reading and asking here keeps the gate's
Copilot loop the size it was. `pr_review.py` hands in the reads and the one write. So nothing here
reaches GitHub on its own, and every case can drive it with constructed payloads. `ROLLUP_PENDING`
holds the rollup states a running check reports, which a wait for green checks polls past.
"""

from __future__ import annotations

import functools
import subprocess
from collections.abc import Callable
from urllib.parse import quote

import pr_review_policy as review_policy
from pr_review_rounds import (
    CODERABBIT_LOGIN,
    QODO_LOGIN,
    TRUSTED_ASSOCIATIONS,
    commit_oid,
    head_nodes,
    qodo_comment,
    reviewer_nodes,
)

SIDE_LOGINS = {"coderabbit": CODERABBIT_LOGIN, "qodo": QODO_LOGIN}
ROLLUP_PENDING = frozenset({"PENDING", "EXPECTED"})


def side_command(name: str, command: str = "review") -> str:
    """The exact comment body that asks `name` for a round, `command` naming CodeRabbit's kind."""
    return f"@coderabbitai {command}" if name == "coderabbit" else "/review"


def head_committed(pr: dict) -> str:
    """The head commit's committer timestamp, empty where the payload does not carry it."""
    nodes = (pr.get("commits") or {}).get("nodes") or []
    return ((nodes[-1] if nodes else {}).get("commit") or {}).get("committedDate") or ""


def head_rollup(pr: dict) -> str:
    """The head commit's check rollup state, NONE where the head carries no check at all.

    Empty where the payload holds no head commit, so an unread rollup never reads as green.
    """
    nodes = (pr.get("commits") or {}).get("nodes") or []
    if not nodes:
        return ""
    rollup = (nodes[-1].get("commit") or {}).get("statusCheckRollup")
    return "NONE" if rollup is None else (rollup.get("state") or "")


def green(state: str) -> str:
    """What holds a review back under a rollup `state`: nothing once it passes or holds no check."""
    return "" if state in ("SUCCESS", "NONE") else (state or "UNREAD")


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
    """Whether a request `body` was posted since the head was committed."""
    since = head_committed(pr)
    return any((n.get("createdAt") or "") >= since for n in side_asks(pr, body))


def side_rounds(pr: dict, name: str, body: str) -> int:
    """Rounds asked of or run by `name` on this pull request, whichever count is larger.

    CodeRabbit's formal reviews carry their commit, so its distinct reviewed heads count too. Qodo
    edits one standing comment, so only the requests sent to it can be counted.
    """
    asked = len(side_asks(pr, body))
    if name != "coderabbit":
        return asked
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
    after = max(head_committed(pr), (asks[-1].get("createdAt") or "") if asks else "")
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


def request_side_reviews(
    pr: dict, await_green: Callable[[], str], post: Callable[[str], dict]
) -> list[str]:
    """Ask each comment-triggered reviewer the policy owes a round on this head, once per head.

    Returns the lines to print. Nothing is sent where the reviewer answered since the head was
    committed, or was asked since then. Nothing is sent where its cap is spent, either. Nor where it
    waits for green checks that are not. `await_green` is called at most once. `post` sends one
    comment and returns the created node. An unconfirmed one ends the run, since it may have landed.
    """
    lines: list[str] = []
    held: str | None = None
    for name, bot, body in side_reviewers(pr):
        if not bot.requests() or side_answered(pr, name, body) or side_asked_on_head(pr, body):
            continue
        if not bot.rounds_left(side_rounds(pr, name, body)):
            lines.append(
                f"note: the review policy allows {name} no further round here, so none was asked"
            )
            continue
        if bot.waits_for_checks():
            held = await_green() if held is None else held
            if held:
                lines.append(
                    f"note: the review policy asks {name} only once the head's checks pass, "
                    f"and they read {held}, so it was not asked yet"
                )
                continue
        made = post(body)
        if not made.get("url") or (made.get("body") or "") != body:
            raise SystemExit(
                f"the request to {name} returned no URL or a different body, so it is "
                "unconfirmed: inspect the conversation before asking again, since it may have landed"
            )
        lines.append(f"auto-request: asked {name} for a round on the current head ({made['url']})")
    return lines


def contents_reader(
    rest: Callable[[str, str], subprocess.CompletedProcess],
    absent: Callable[[subprocess.CompletedProcess], bool],
) -> review_policy.Reader:
    """A policy reader over `rest`, one REST read with a jq projection, and `absent`.

    A file GitHub says is absent reads None. Any other failure ends the run. So a policy that could
    not be read is never taken for the hub default. That would quietly run a repository under rules
    it opted out of.
    """

    def read(owner: str, repo: str, path: str, ref: str) -> str | None:
        proc = rest(f"repos/{owner}/{repo}/contents/{path}?ref={quote(ref, safe='')}", ".content")
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out:
            return out
        if proc.returncode != 0 and absent(proc):
            return None
        detail = (proc.stderr or out or "no output").strip()[:300]
        raise SystemExit(f"review policy: {owner}/{repo} {path} at {ref} was not read ({detail})")

    return read


@functools.cache
def policy_for(
    owner: str,
    repo: str,
    base: str,
    rest: Callable[[str, str], subprocess.CompletedProcess],
    absent: Callable[[subprocess.CompletedProcess], bool],
) -> review_policy.Policy:
    """The review policy a pull request into `base` runs under, read once per run.

    Read from the base rather than the head, so a pull request cannot change its own policy.
    """
    try:
        return review_policy.fetch(owner, repo, base, contents_reader(rest, absent))
    except review_policy.PolicyError as exc:
        raise SystemExit(f"review policy: {exc}") from exc


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
    read: Callable[[], dict], start: float, timeout: float, backoff: Callable[..., dict]
) -> str:
    """Poll the head's checks with `backoff` until they leave pending, and name what holds a review.

    Empty once they pass or the head carries none. Otherwise the state they read, PENDING where the
    bound ran out first, so a failed build is never reviewed before it is fixed.
    """
    polled = backoff(read(), read, lambda p: head_rollup(p) in ROLLUP_PENDING, start, timeout)
    return green(head_rollup(polled))


def ask(pr: dict, await_green: Callable[[], str], post: Callable[[str], dict]) -> None:
    """Send and print `request_side_reviews`'s requests."""
    for line in request_side_reviews(pr, await_green, post):
        print(line)


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
        f"holds the Copilot request until they pass: {then}"
    )
    return 30 if pending else 40


def side_status(owing: list[str]) -> int:
    """Print the status line for a gated reviewer that has not answered the head, and exit 30."""
    print(
        "status=SIDE_REVIEW_PENDING the review policy gates the merge on a round from "
        f"{', '.join(owing)} on this head, and none has landed by the timeout: wait again"
    )
    return 30
