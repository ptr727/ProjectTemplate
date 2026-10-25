"""The review rounds on a pull request, read off one GraphQL payload, for the gate in `pr_review.py`.

This is a library module and has no command line. `pr_review.py` imports it as a sibling, which
works from any working directory because Python puts the running script's own directory on
`sys.path`. It reads reviews, comments, threads, the request state, the reviewer's history, and the
local attestation. Each read takes a payload already fetched, and nothing here makes a call. What a
reviewer's body says is still `pr_review_copilot.py`'s, reached here through `reading`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pr_review_copilot as adapter

# Read from the vocabulary, so a login rename is one edit that the reader and the queries share.
REVIEWER = adapter.VOCABULARIES["copilot"]["login"]
CODERABBIT_LOGIN = adapter.VOCABULARIES["coderabbit"]["login"]
QODO_LOGIN = adapter.VOCABULARIES["qodo"]["login"]
OTHER_REVIEWERS = (CODERABBIT_LOGIN, QODO_LOGIN)
KNOWN_REVIEWERS = (REVIEWER, *OTHER_REVIEWERS)

# A login that reads as this reviewer without being the spelling every query filters on.
# `copilot-swe-agent` is the coding agent and does not match.
READS_AS_REVIEWER = re.compile(r"copilot.*review", re.IGNORECASE)
FILES_UNREAD = "unread"
ATTESTATION = re.compile(
    r"^<!-- fleet-local-review: head=([0-9a-f]{40}) base=(\S+) findings=(\d+|unknown) -->$",
    re.MULTILINE,
)
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
ACK_MARKER = "<!-- fleet-shape-ack: fingerprint={} -->"


def reviewer_requested(pr: dict) -> bool:
    """True where the reviewer sits in the pending request set."""
    return any(
        (n.get("requestedReviewer") or {}).get("login") == REVIEWER
        for n in ((pr.get("reviewRequests") or {}).get("nodes") or [])
    )


def history_entry(number: int, node: dict, kind: str) -> tuple[int, dict]:
    """One reviewer review or comment in the shape `copilot_history` returns, stamped and tagged."""
    stamp = node.get("submittedAt" if kind == "review" else "createdAt") or ""
    return number, {**node, "_at": stamp, "_kind": kind}


def newest_first(entries: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
    """History entries ordered newest first, the order `quota_signal` reads its first entry in."""
    return sorted(entries, key=lambda pair: pair[1]["_at"], reverse=True)


def copilot_bot_id(history: list[tuple[int, dict]]) -> str | None:
    """The Copilot reviewer bot's node id from an already-fetched history, or None."""
    for _number, entry in history:
        if entry.get("_kind") != "review":
            continue
        bot_id = (entry.get("author") or {}).get("id")
        if bot_id:
            return bot_id
    return None


def quota_signal(history: list[tuple[int, dict]]) -> tuple[int, dict] | None:
    """The reviewer's newest activity anywhere in this repository where it is a quota refusal.

    Returned paired with the pull request number it came from, else None. A genuine review or a
    later plain comment since the refusal spends it.
    """
    newest = history[0] if history else None
    return newest if newest and quota_refusal(newest[1]) else None


def standing_quota(
    history: list[tuple[int, dict]], pr: dict, number: int
) -> tuple[int, dict] | None:
    """`quota_signal` over `history` as this pull request's own read leaves it, or None.

    None where a genuine round covers the head. This pull request's reviews are merged in, so a
    review of it submitted after the refusal answers that refusal.
    """
    if reviewed_head(pr):
        return None
    own = [history_entry(number, n, "review") for n in reviewer_nodes(pr, "reviews")]
    return quota_signal(newest_first([*history, *own]))


def rate_limited_by(pr: dict, login: str) -> str | None:
    """The service a tracked reviewer's newest comment or review names as rate limiting it, or
    None.
    """
    nodes = reviewer_nodes(pr, "comments", login) + reviewer_nodes(pr, "reviews", login)
    if not nodes:
        return None
    newest = max(nodes, key=lambda n: n.get("createdAt") or n.get("submittedAt") or "")
    return reading(newest, login).rate_limited


def newest_request_event(pr: dict) -> str | None:
    """The id of the newest review-request event naming the reviewer, or None where there is none."""
    ids = [
        n.get("id")
        for n in ((pr.get("timelineItems") or {}).get("nodes") or [])
        if (n.get("requestedReviewer") or {}).get("login") == REVIEWER
    ]
    return ids[-1] if ids else None


def request_recorded(before: dict, after: dict) -> bool | None:
    """Whether a review request that returned success left anything on the pull request.

    True where the reviewer sits in the pending set afterwards, or a review-request event naming it
    is newer than the newest one read before. False where neither holds, the shape an exhausted
    Copilot allowance leaves. None where either field is missing or the login is spelled otherwise.
    """
    if after.get("reviewRequests") is None or after.get("timelineItems") is None:
        return None
    nodes = after["reviewRequests"].get("nodes") or []
    nodes = nodes + (after["timelineItems"].get("nodes") or [])
    for n in nodes:
        login = (n.get("requestedReviewer") or {}).get("login") or ""
        if login != REVIEWER and READS_AS_REVIEWER.search(login):
            return None
    if reviewer_requested(after):
        return True
    return newest_request_event(after) != newest_request_event(before)


def reviewer_nodes(pr: dict, field: str, login: str = REVIEWER) -> list[dict]:
    """This login's own nodes under `field`, oldest first as the API returns them."""
    return [
        n
        for n in ((pr.get(field) or {}).get("nodes") or [])
        if (n.get("author") or {}).get("login") == login
    ]


def answered_outside_review(pr: dict) -> dict | None:
    """The reviewer's newest plain comment, where it postdates its newest formal review."""
    comments = reviewer_nodes(pr, "comments")
    # A blind review window leaves no honest baseline to date a comment against, so report nothing.
    if not comments or window_blind(pr, "reviews"):
        return None
    newest = max(comments, key=lambda n: n.get("createdAt") or "")
    reviews = reviewer_nodes(pr, "reviews")
    latest_review = max((n.get("submittedAt") or "" for n in reviews), default="")
    return newest if (newest.get("createdAt") or "") > latest_review else None


def window_blind(pr: dict, field: str, login: str = REVIEWER) -> bool:
    """True where this login's own nodes can sit behind the window, so the view cannot decide.

    `login` defaults to the fully-modeled reviewer. Nodes arrive in creation order, so one of this
    login's in view bounds every hidden one as older. `hasPreviousPage` says whether anything is
    back there at all.
    """
    older = ((pr.get(field) or {}).get("pageInfo") or {}).get("hasPreviousPage")
    return bool(older) and not reviewer_nodes(pr, field, login)


def threads_truncated(pr: dict) -> bool:
    """True where `reviewThreads(first:100)` cut off before the actual thread count."""
    return bool(((pr.get("reviewThreads") or {}).get("pageInfo") or {}).get("hasNextPage"))


def reviews_truncated(pr: dict) -> bool:
    """True where the `reviews` connection cut off before this pull request's actual round count.

    A finding count needs every round read, so an older windowed-out round can hide a suppressed or
    outside-diff-range finding. `hasPreviousPage` means the unread reviews are the oldest.
    """
    return bool(((pr.get("reviews") or {}).get("pageInfo") or {}).get("hasPreviousPage"))


def refusal_of(node: dict) -> str:
    """The review's body where the adapter reads it as a refusal, otherwise empty."""
    body = node.get("body") or ""
    return body if reading(node).refusal != "none" else ""


def quota_refusal(node: dict) -> bool:
    """True where a refusal on this node names the account quota rather than another cause."""
    return reading(node).refusal == "quota"


def possible_quota(node: dict) -> bool:
    """True where a refusal on this node names an error rather than any cause."""
    return reading(node).refusal == "error"


def stopping_refusal(pr: dict) -> dict | None:
    """The reviewer's newest review on any head, where it is a quota refusal or an error."""
    if answered_outside_review(pr):
        return None
    newest = newest_of(reviewer_nodes(pr, "reviews"))
    if newest is None or not (quota_refusal(newest) or possible_quota(newest)):
        return None
    return newest


def refusing_review(pr: dict) -> dict | None:
    """The reviewer's newest review carrying the current head, where that review is a refusal."""
    head = pr["headRefOid"]
    newest = newest_of(
        [n for n in reviewer_nodes(pr, "reviews") if (n.get("commit") or {}).get("oid") == head]
    )
    return newest if newest is not None and refusal_of(newest) else None


def newest_of(nodes: list[dict]) -> dict | None:
    """The newest of these nodes by submission time, the later arrival winning a tie.

    `reviewer_nodes` returns oldest first and GitHub often stamps two rounds with the same second,
    so position breaks the tie. A node with no timestamp sorts oldest. Shared so `effort=` and
    `overview=` describe the same round.
    """
    if not nodes:
        return None
    return max(enumerate(nodes), key=lambda pair: (pair[1].get("submittedAt") or "", pair[0]))[1]


def head_reviews(pr: dict) -> list[dict]:
    """The reviewer's own reviews that cover the current head, refusals excluded.

    A refusal is a formal review with the head's commit and no threads, so it satisfies every check
    a clean pass does. One list serves coverage and the round count.
    """
    head = pr["headRefOid"]
    return [
        n
        for n in reviewer_nodes(pr, "reviews")
        if (n.get("commit") or {}).get("oid") == head and not refusal_of(n)
    ]


def reviewed_head(pr: dict) -> bool:
    """True where one of the reviewer's own reviews covers the current head's commit."""
    return bool(head_reviews(pr))


def head_review_done(pr: dict, min_rounds: int) -> bool:
    """True once a round past position `min_rounds` in the reviewer's own history covers the head.

    Positions are read off `reviewer_nodes(pr, "reviews")`, every review on any head, refusals
    included, oldest first, the list `status` counts for `rounds=`. One list is walked so the round
    satisfying `min_rounds` is the round satisfying coverage. A push adds to the list rather than
    resetting it, so positions stay valid when the head moves. The `last:100` window can undercount
    an aged-out round, which fails toward waiting longer. Passing 0 reduces to `reviewed_head`.
    """
    revs = reviewer_nodes(pr, "reviews")
    head = pr["headRefOid"]
    return any(
        i >= min_rounds and (n.get("commit") or {}).get("oid") == head and not refusal_of(n)
        for i, n in enumerate(revs)
    )


def review_effort(pr: dict) -> tuple[str, str]:
    """The newest head review's effective effort and selection source."""
    newest = newest_of(head_reviews(pr))
    if newest is None:
        return "unknown", "unknown"
    found = reading(newest)
    return found.effort, found.effort_source


def files_unread(pr: dict) -> bool:
    """Whether the changed-file list is to be read again before a file table is matched to it."""
    files = pr.get("files") or {}
    if files.get(FILES_UNREAD):
        return True
    return not files.get("nodes") and any(
        reading(n).table is not None for n in reviewer_nodes(pr, "reviews")
    )


def open_threads(threads: list[dict]) -> list[dict]:
    """Every unresolved thread, whoever opened it, since each one blocks a ruleset-gated merge."""
    return [t for t in threads if not t.get("isResolved")]


def changed_paths(pr: dict) -> tuple[list[str], bool]:
    """The paths this pull request changes, and whether that list is incomplete."""
    files = pr.get("files") or {}
    paths = [n.get("path") or "" for n in (files.get("nodes") or [])]
    return [p for p in paths if p], bool((files.get("pageInfo") or {}).get("hasNextPage"))


def reviewer_login_drift(pr: dict) -> list[str]:
    """Logins that read as this reviewer without being the spelling every query filters on."""
    logins = {
        (n.get("author") or {}).get("login") or ""
        for field in ("reviews", "comments")
        for n in ((pr.get(field) or {}).get("nodes") or [])
    }
    return [
        f"reviewer login: {login}, where every query here filters on {REVIEWER}"
        for login in sorted(logins)
        if login != REVIEWER and READS_AS_REVIEWER.search(login)
    ]


def thread_ids(pr: dict) -> tuple[str, ...]:
    """The database id of each thread's first comment, which an overview entry's link names."""
    return tuple(
        str(c["fullDatabaseId"])
        for t in (pr.get("reviewThreads") or {}).get("nodes") or []
        if (c := first_comment(t)).get("fullDatabaseId")
    )


def reading(node: dict, login: str = REVIEWER, pr: dict | None = None) -> adapter.Reading:
    """The adapter's reading of one body, by the vocabulary for its author's login."""
    threads = thread_ids(pr) if pr is not None else None
    return adapter.read(node.get("body") or "", adapter.LOGINS.get(login, "copilot"), threads)


def opened_by(pr: dict, node: dict) -> list[dict]:
    """The threads this round opened, by the review id each thread's first comment carries."""
    return [
        t
        for t in (pr.get("reviewThreads") or {}).get("nodes") or []
        if node.get("id")
        and (first_comment(t).get("pullRequestReview") or {}).get("id") == node["id"]
    ]


def shape_hold(pr: dict) -> tuple[str, list[adapter.Unplaced], bool] | None:
    """The parts the head's rounds leave unplaced, their fingerprint, and whether it is answered.

    None where those rounds place every part. A trusted comment is what answers a fingerprint.
    """
    parts = list(dict.fromkeys(p for node in head_reviews(pr) for p in reading(node).unplaced))
    if not parts:
        return None
    fp = adapter.fingerprint(parts)
    marker = ACK_MARKER.format(fp)
    acked = any(
        marker in lines and any(ln.strip() not in ("", marker) for ln in body.splitlines())
        for body, lines in trusted_comments(pr)
    )
    return fp, parts, acked


def trusted_comments(pr: dict) -> Iterator[tuple[str, list[str]]]:
    """Each owner's, member's, or collaborator's shown comment body, with its HTML lines outside code."""
    for node in (pr.get("comments") or {}).get("nodes") or []:
        if (node.get("authorAssociation") or "") in TRUSTED_ASSOCIATIONS and not node.get(
            "isMinimized"
        ):
            body = node.get("body") or ""
            yield body, adapter.html_lines(body)


def partial_on_record(pr: dict) -> bool:
    """Whether any Copilot round on the pull request, on any commit, states partial coverage."""
    return any(
        reading(n).coverage == "partial" for n in reviewer_nodes(pr, "reviews") if not refusal_of(n)
    )


def table_matches(named: tuple[str, ...], changed: list[str]) -> bool:
    """Whether a file table names exactly the changed files, a glob row matching what it names.

    A glob matches one segment at a time, `*` and `?` never crossing a `/`, and every other
    character is literal, so a row of `src/*` names `src/a.py` and not `src/sub/b.py`.
    """

    def hits(entry: str, path: str) -> bool:
        if entry == path:
            return True
        want, have = entry.split("/"), path.split("/")
        return len(want) == len(have) and all(
            re.fullmatch(re.escape(w).replace(r"\*", "[^/]*").replace(r"\?", "[^/]"), h)
            for w, h in zip(want, have, strict=True)
        )

    return all(any(hits(e, p) for e in named) for p in changed) and all(
        any(hits(e, p) for p in changed) for e in named
    )


def table_cover(pr: dict) -> tuple[str, str]:
    """Whether a Copilot file table stands in for a missing statement, and the reason either way.

    The head's own table is read first, else the newest round's that carries one (`carried:table`).
    Either counts only where it names exactly the files changed now and no round states partial
    coverage.
    """
    if partial_on_record(pr):
        return "", "a round on this pull request states partial coverage, so no table stands in"
    if reviews_truncated(pr):
        return "", "the review history runs past the window, so a partial round could sit unseen"
    head = pr["headRefOid"]
    tabled = [
        n
        for n in reviewer_nodes(pr, "reviews")
        if not refusal_of(n) and reading(n).table is not None
    ]
    own = [n for n in tabled if (n.get("commit") or {}).get("oid") == head]
    node = newest_of(own) or newest_of(tabled)
    if node is None:
        return "", "no round carries a file table"
    changed, cut = changed_paths(pr)
    if cut or files_unread(pr) or not changed:
        return "", "the changed-file list is incomplete or empty, so no table can be matched"
    named = reading(node).table or ()
    where = ((node.get("commit") or {}).get("oid") or "")[:8] or "a commit this cannot name"
    if not table_matches(named, changed):
        return "", (
            f"the table on {where} names {len(named)} path(s) where the pull request changes "
            f"{len(changed)} file(s), and the two sets differ"
        )
    return ("table" if node in own else "carried:table"), (
        f"the table on {where} names exactly the {len(changed)} changed file(s)"
    )


def coverage(pr: dict) -> tuple[str, str]:
    """The head's coverage reading and the statement or reason it rests on.

    A statement on a round covering the head decides it, the worst winning, but `unknown` yields to
    an earlier partial. Where none states any, a file table stands in under `table_cover`, then
    an attested local pass under `local_cover`.
    """
    rounds = head_reviews(pr)
    rank = ("full", "unknown", "partial")
    stated = [(r.coverage, r.coverage_line) for n in rounds if (r := reading(n)).coverage != "none"]
    worst = max(stated, key=lambda s: rank.index(s[0])) if stated else None
    if worst and worst[0] != "unknown":
        return worst
    if rounds and partial_on_record(pr):
        none = (
            "a round on the head states a reading this cannot believe"
            if worst
            else "no round on the head states any"
        )
        return "partial", f"an earlier round states partial coverage, and {none}"
    if worst:
        return worst
    why = ""
    if rounds:
        state, why = table_cover(pr)
        if state:
            return state, why
    if local_cover(pr) and not any(reading(n).table is not None for n in rounds):
        return "local", f"an attested local pass recorded {attestation(pr)} finding(s)"
    return "none", why


def uncounted_verdict(pr: dict, kind: str = "flagging") -> tuple[str, str] | None:
    """The head round's verdict of `kind` and its headline, where that round counts no finding.

    A flagging round that states, lists, and opens no finding names what it flags in its headline
    alone. An open thread defers the reading. Passed `clean`, it reads a clean round the same way.
    """
    node = newest_of(head_reviews(pr))
    if node is None:
        return None
    found = reading(node, pr=pr)
    counts = found.counts
    if found.verdict != kind or found.entries or any(counts.values()) or opened_by(pr, node):
        return None
    if open_threads((pr.get("reviewThreads") or {}).get("nodes") or []):
        return None
    return found.verdict_heading, found.headline


def qodo_comment(pr: dict) -> dict | None:
    """Qodo's newest findings comment, told from its summary comment by the vocabulary's heading."""
    heading = adapter.VOCABULARIES["qodo"]["comment_heading"]
    comments = [
        c for c in reviewer_nodes(pr, "comments", QODO_LOGIN) if heading.search(c.get("body") or "")
    ]
    return max(comments, key=lambda c: c.get("createdAt") or "", default=None)


def qodo_comments_blind(pr: dict) -> bool:
    """True where Qodo's findings comment can sit behind the comments window unseen."""
    if window_blind(pr, "comments", QODO_LOGIN):
        return True
    older = ((pr.get("comments") or {}).get("pageInfo") or {}).get("hasPreviousPage")
    return bool(older) and bool(reviewer_nodes(pr, "comments", QODO_LOGIN)) and not qodo_comment(pr)


def collected(pr: dict, login: str, kind: str) -> tuple[list[tuple[str, adapter.Section]], str]:
    """Each section of `kind` in this login's rounds, with where it was raised, and the field.

    Every round rather than the head's, since an unanswered finding survives a push. The field
    reads `N (on_head=X earlier=Y)`, a trailing `+` on N where the reviews window was cut. A tally
    holding a count the reader cannot believe reads `?`.
    """
    head = pr["headRefOid"]
    found = []
    for node in reviewer_nodes(pr, "reviews", login):
        oid = (node.get("commit") or {}).get("oid") or ""
        where = (
            "commit unknown, treat as outstanding"
            if not oid
            else "on head"
            if oid == head
            else f"raised on {oid[:8]}, earlier round"
        )
        found += [(where, s) for s in reading(node, login).sections if s.kind == kind]

    def tally(on: bool | None) -> str:
        n = adapter.total(s.findings for w, s in found if on is None or (w == "on head") == on)
        return "?" if n is None else str(n)

    mark = "+" if reviews_truncated(pr) else ""
    return found, f"{tally(None)}{mark} (on_head={tally(True)} earlier={tally(False)})"


def thread_author(t: dict) -> str:
    """The login on a thread's opening comment, or `""` where there is none to read."""
    return (first_comment(t).get("author") or {}).get("login") or ""


def first_comment(thread: dict) -> dict:
    """The thread's opening comment, which is the finding itself."""
    return ((thread.get("comments") or {}).get("nodes") or [{}])[0]


def describe(thread: dict) -> str:
    """One line naming a thread by what a reader recognizes it as, never by its id."""
    c = first_comment(thread)
    body = " ".join((c.get("body") or "").split())
    return (
        f"{thread.get('path')}:{thread.get('line')} "
        f"by {(c.get('author') or {}).get('login')}: {body[:120]}"
    )


_TYPOGRAPHIC_FOLD = str.maketrans(
    {
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2026": "...",  # horizontal ellipsis
    }
)


def matching_threads(threads: list[dict], match: str, path: str | None) -> list[dict]:
    """Threads whose finding text contains `match`, narrowed by `path` where one is given.

    Matched on the finding's words rather than a line number, which a fix push moves.
    Case-insensitive, and both sides fold typographic characters through `_TYPOGRAPHIC_FOLD` and
    whitespace to a single space, as `describe()` does, so a pattern copied from a printed line
    matches the raw body.
    """
    needle = fold_finding(match)
    return [
        t for t in threads if needle in finding_text(t) and (path is None or t.get("path") == path)
    ]


def fold_finding(text: str) -> str:
    """The form `--match` compares in, as `matching_threads` describes."""
    return " ".join(text.split()).translate(_TYPOGRAPHIC_FOLD).lower()


def finding_text(thread: dict) -> str:
    """A thread's opening comment in the form `--match` compares against."""
    return fold_finding(first_comment(thread).get("body") or "")


def attested(pr: dict) -> bool:
    """Whether an attestation vouches for this pull request's head, per `attestation`."""
    return attestation(pr) is not None


def attestation(pr: dict) -> str | None:
    """The findings count an owner's, member's, or collaborator's attestation of this head carries.

    None where no attestation vouches for the head against the pull request's current base, and
    "unknown" where the pass recorded no count.

    Read over every comment not hidden, with the author association keeping a passer-by's marker
    from vouching. The marker counts only as a line of its own outside a fence.
    """
    head, base = pr.get("headRefOid") or "", pr.get("baseRefName") or ""
    for _, lines in trusted_comments(pr):
        for line in lines:
            m = ATTESTATION.fullmatch(line)
            if m and m.group(1) == head and m.group(2) == base:
                return m.group(3)
    return None


def promotion(pr: dict) -> bool:
    """Whether the pull request merges into the repository's default branch."""
    default = ((pr.get("baseRepository") or {}).get("defaultBranchRef") or {}).get("name") or ""
    return not default or pr.get("baseRefName") == default


def holds(pr: dict) -> bool:
    """Whether a local pass rather than a Copilot round is to cover this head, attested or not."""
    return (
        not promotion(pr)
        and not reviews_truncated(pr)
        and not partial_on_record(pr)
        and not reviewer_requested(pr)
    )


def local_cover(pr: dict) -> bool:
    """Whether a recorded local pass stands in for a Copilot round on this head.

    Only on a pull request into a non-default branch, on a head its writer attests, where no round
    on record read part of a diff and the whole review history is in view. A refusal on this head
    is Copilot's own word, which only a later Copilot round on the head spends.
    """
    return (
        not refusing_review(pr)
        and not promotion(pr)
        and attested(pr)
        and not reviews_truncated(pr)
        and not partial_on_record(pr)
    )
