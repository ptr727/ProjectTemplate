"""What a driver has answered on a pull request, what stands unanswered, and where `merge` routes.

This is a library module and has no command line, and nothing here makes a call. Its importers
are `pr_review.py`, `pr_review_digest.py`, and `pr_merge.py`. A finding no thread carries has nothing
to resolve, so its answer is an acknowledgment record instead. That is a comment from an owner,
member, or collaborator carrying the written outcomes. Beneath them sits one
`<!-- fleet-finding-ack: key=<key> outcome=<outcome> -->` line per item, which
`pr_review.py comment --ack` posts. Each item's key is derived from the round or comment raising
it and from its own text. So a new round raising it again owes a new answer. Code checks that every item carries a recorded outcome. Whether that outcome is right stays the
driver's per `pr-review-conduct`. A verdict naming no counted finding is answered the same way.
It is never answered as `fixed`, since a fix pushes a new head that the verdict no longer covers.
`merge` then
prints one `next=` route its status lines decide, off the table here, so a skill follows that field
rather than listing every state.
"""

from __future__ import annotations

import hashlib
import re
from typing import NamedTuple

import pr_review_copilot as adapter
from pr_review_checks import MERGE_UNGATED
from pr_review_rounds import (
    CARRIED_PARTIAL,
    CODERABBIT_LOGIN,
    QODO_LOGIN,
    REVIEWER,
    answering_comments,
    collected,
    commit_oid,
    coverage,
    head_reviews,
    newest_of,
    qodo_comment,
    reading,
    refusal_of,
    reviewer_login_drift,
    reviewer_nodes,
    shape_hold,
    threads_truncated,
    uncounted_verdict,
)

FINDING_ACK = "<!-- fleet-finding-ack: key={} outcome={} -->"
ACK_LINE = re.compile(r"^<!-- fleet-finding-ack: key=([0-9a-f]{12}) outcome=([a-z]+) -->$")
OUTCOMES = ("fixed", "declined", "filed", "listed", "dropped")
NO_THREAD, NOTED, VERDICT = "FINDINGS WITH NO THREAD", "REVIEWER OUTPUT NOTED", "verdict"
DRIFT = "login drift"
# A digest count and the reviewer's sections it is read from, so an answer clears what it counts.
SOURCES = {
    "suppressed": (REVIEWER, "suppressed", "SUPPRESSED"),
    "previously_missed": (REVIEWER, "previously_missed", "PREVIOUSLY MISSED"),
    "cr_outside_diff": (CODERABBIT_LOGIN, "previously_missed", "CODERABBIT OUTSIDE-DIFF"),
}


MET_COVERAGE = ("full", "table", "carried:table", "local")
# The digest counts the gate reads, each nonzero one standing until every item under it is answered.
COUNTED = (*SOURCES, "qodo_open", "overview", NO_THREAD, NOTED)
# The digest builds these as None where the source they count is absent from the pull request.
ABSENT = ("previously_missed", "cr_outside_diff", "qodo_open")


class Item(NamedTuple):
    """One answerable item: the digest count it sits under, its key, where it was raised, its text.

    `answered` is True where an earlier acknowledgment already answers it, a noted part whose
    round's unplaced set was acknowledged while that round covered the head.
    """

    kind: str
    key: str
    where: str
    text: str
    answered: bool = False


def key(kind: str, anchor: str, text: str) -> str:
    """A short stable name for one item, over its kind, the round raising it, and its text."""
    words = " ".join(text.split())
    raw = f"{kind}\0{anchor}\0{words}".encode()
    return hashlib.sha256(raw).hexdigest()[: adapter.FINGERPRINT_HEX]


def acknowledged(pr: dict) -> dict[str, str]:
    """Each key a trusted comment carrying written outcomes acknowledges, with its outcome."""
    found: dict[str, str] = {}
    for lines in answering_comments(pr):
        for line in lines:
            if (m := ACK_LINE.match(line)) and m[2] in OUTCOMES:
                found.setdefault(m[1], m[2])
    return found


def noted(pr: dict) -> list[Item]:
    """Each part the reader placed nowhere outside a head hold, on every round, and login drift.

    Every round is read, since a renamed section on an earlier round hides a finding too.
    """
    on_head, found = head_reviews(pr), dict[str, Item]()
    for node in reviewer_nodes(pr, "reviews"):
        oid = commit_oid(node) or ""
        parts = {p.note: p.text for p in reading(node).unplaced}
        for n in reading(node).notes:
            if node in on_head and n in parts:
                continue
            note = f"round {(oid or 'unknown')[:8]}: {n}"
            own = oid and n in parts and not refusal_of(node)
            held = shape_hold(pr, oid) if own else None
            k = key(NOTED, node.get("id") or oid, f"{n}\0{parts.get(n, '')}")
            found.setdefault(k, Item(NOTED, k, "", note, bool(held and held[2])))
    # A drifted login's own reviews are never read, so no answer clears the note while it stands.
    for note in reviewer_login_drift(pr):
        found.setdefault(note, Item(NOTED, key(NOTED, "", note), DRIFT, note))
    return list(found.values())


def owed(pr: dict) -> tuple[int, list[str]]:
    """How many findings the head's newest round names that no thread carries, each item marked."""
    newest = newest_of(head_reviews(pr))
    if newest is None:
        return 0, []
    found = reading(newest, pr=pr)
    threads, files = pr["reviewThreads"]["nodes"], (pr.get("files") or {}).get("nodes") or []
    unmatched, marked = adapter.listed_lines(
        found.listed_items, threads, files, threads_truncated(pr)
    )
    return max(found.unthreaded, unmatched), marked


def items(pr: dict) -> list[Item]:
    """Every item an acknowledgment can answer, in the order the digest prints them."""
    found = []
    for kind, (login, section, _) in SOURCES.items():
        entries, _ = collected(pr, login, section)
        found += [Item(kind, key(kind, rid, s.text), where, s.text) for where, s, rid in entries]
    if qodo := qodo_comment(pr):
        anchor = qodo.get("id") or qodo.get("createdAt") or ""
        sections = [s for s in reading(qodo, QODO_LOGIN).sections if s.kind == "open"]
        found += [
            Item("qodo_open", key("qodo_open", anchor, f"{s.title}\0{s.text}"), "", s.title)
            for s in sections
        ]
    newest = newest_of(head_reviews(pr))
    anchor = (newest or {}).get("id") or ""
    if (unthreaded := owed(pr))[0]:
        text = "\n".join([str(unthreaded[0]), *unthreaded[1]])
        found.append(Item(NO_THREAD, key(NO_THREAD, anchor, text), "", ""))
    if flagged := uncounted_verdict(pr):
        found.append(Item(VERDICT, key(VERDICT, anchor, flagged[1]), "", flagged[1]))
    return found + noted(pr)


def takes(item: Item, outcome: str) -> bool:
    """Whether `outcome` answers `item`. A verdict is never `fixed` on the head it reviews.

    A login drift takes none, since the drifted login's rounds stay unread whatever answers it.
    """
    if item.where == DRIFT:
        return False
    return outcome in OUTCOMES and not (item.kind == VERDICT and outcome == "fixed")


def answer_of(item: Item, acks: dict[str, str]) -> str:
    """The outcome answering `item`, `shape` where its round's shape acknowledgment does, or empty."""
    if item.answered:
        return "shape"
    outcome = acks.get(item.key, "")
    return outcome if takes(item, outcome) else ""


def answered(pr: dict, kind: str) -> bool:
    """True where the pull request holds items under `kind` and each one carries an outcome."""
    acks = acknowledged(pr)
    found = [it for it in items(pr) if it.kind == kind and it.where != DRIFT]
    drift = kind == NOTED and bool(reviewer_login_drift(pr))
    return (bool(found) or drift) and all(answer_of(it, acks) for it in found)


def unanswered(pr: dict, fields: dict) -> tuple[list[str], list[str], list[str]]:
    """The counts `fields` holds that no acknowledgment answers, the noted ones apart, and the unread.

    A count not built, holding no number, or cut short by a window is unread rather than zero.
    `overview` is read for a number alone, since the threads and the no-thread count carry it.
    """
    counts: list[str] = []
    notes: list[str] = []
    unread: list[str] = []
    for name in COUNTED:
        absent = name in ABSENT or (name == "overview" and fields.get("review_on_head") != "yes")
        value = "0" if absent and name in fields and fields[name] is None else fields.get(name, "")
        if not (m := re.match(r"(\d+)(/\d+)?( |$)", str(value))):
            unread.append(f"{name}={value}" if value != "" else f"{name} (not built)")
        elif name != "overview" and m[1] != "0" and not answered(pr, name):
            (notes if name == NOTED else counts).append(f"{name}={value}")
    return counts, notes, unread


def unmet(pr: dict, fields: dict, opened: list[dict]) -> list[tuple[str, str]]:
    """Each Merge Gate item `wait`'s 0 leaves undecided that `fields` and `opened` read unmet.

    `fields` is the digest's, and `opened` every unresolved thread. A count every item under which
    an acknowledgment answers is met. Shapes no value here names are unread rather than met.
    """
    found: list[tuple[str, str]] = []
    counts, notes, unread = unanswered(pr, fields)
    if (cover := fields.get("coverage")) not in MET_COVERAGE:
        found.append(
            ("COVERAGE_NOT_SHOWN", f"the digest reads coverage={cover}, so item 2 is unmet")
        )
    if opened:
        found.append(("THREADS_UNRESOLVED", f"{len(opened)} review thread(s) are unresolved"))
    if (shapes := fields.get("shapes")) == "HELD":
        found.append(("UNPLACED_PARTS_UNACKNOWLEDGED", "shapes=HELD owes item 4 an acknowledgment"))
    elif shapes not in ("ok", "NOTED", "ACKNOWLEDGED"):
        unread.insert(0, f"shapes={shapes}")
    if (asked := fields.get("requested")) != "no":
        found.append(("REVIEW_PENDING", f"the digest reads requested={asked}"))
    if unread:
        found.append(
            ("COUNT_UNREAD", f"the digest holds no readable count for {', '.join(unread)}")
        )
    if counts:
        found.append(
            ("UNTHREADED_FINDINGS", f"the digest reads {', '.join(counts)} with an item unanswered")
        )
    if notes:
        found.append(
            ("OUTPUT_NOTED", f"the digest reads {notes[0]} with a part no acknowledgment answers")
        )
    if drifted := reviewer_login_drift(pr):
        found.append(("REVIEWER_LOGIN_DRIFT", f"{drifted[0]}, which only a login change clears"))
    if (state := pr.get("mergeStateStatus")) not in MERGE_UNGATED:
        found.append(("MERGE_NOT_CLEAN", f"the merge state reads {state}, not a finished one"))
    return found


def ack_lines(pr: dict, pairs: list[str], body: str) -> tuple[list[str], str]:
    """The marker lines for `--ack KEY=OUTCOME` pairs, or empty with the reason one is refused.

    A `body` with no prose outside code would post a record the gate never counts.
    """
    if not adapter.has_prose(body):
        return [], "the body holds no written outcome outside code, so the gate would not count it"
    known = {it.key: it for it in items(pr)}
    lines = []
    for pair in pairs:
        k, _, outcome = pair.partition("=")
        if (item := known.get(k)) is None:
            return [], f"{k!r} names no item `status` prints a key for on this pull request"
        if not takes(item, outcome):
            allowed = ", ".join(o for o in OUTCOMES if takes(item, o)) or "none while it stands"
            return [], f"{outcome!r} does not answer {k}, which takes {allowed}"
        lines.append(FINDING_ACK.format(k, outcome))
    return lines, ""


def mark(item: Item, acks: dict[str, str]) -> str:
    """` key=<key>`, with the outcome answering the item where one does, and nothing for a drift."""
    if item.where == DRIFT:
        return ""
    outcome = answer_of(item, acks)
    said = ", answered by its round's shape acknowledgment" if outcome == "shape" else ""
    return f" key={item.key}" + (said or (f", acknowledged as {outcome}" if outcome else ""))


def unthreaded_lines(pr: dict, owner: str, repo: str, num: int) -> list[str]:
    """The digest's block for each finding no thread carries, printed whole until it is answered."""
    acks, lines = acknowledged(pr), []
    hint = f"`comment {num} --repo {owner}/{repo} --ack <key>=<outcome> --body <outcomes>`"
    for item in items(pr):
        if item.kind in SOURCES:
            label = f"{SOURCES[item.kind][2]} ({item.where}){mark(item, acks)}"
        elif item.kind == "qodo_open":
            label = f"QODO OPEN FINDING{mark(item, acks)}"
        else:
            continue
        if answer_of(item, acks):
            lines.append(f"  {label}")
            continue
        own = (
            "spot-verify Qodo's own badge against `gh pr diff` rather than trusting it outright, "
            if item.kind == "qodo_open"
            else ""
        )
        lines.append(
            f"  {label}: no thread to resolve, {own}answer it in the PR conversation quoting the "
            f"finding, with {hint}"
        )
        lines += [f"    {ln}" for ln in item.text.splitlines() if ln.strip()]
    return lines


def report_verdict(pr: dict) -> int:
    """Print the blocking verdict's own status line, and return the exit code it carries.

    A missing head review has no verdict yet, which the wait path owns. Coverage shown nowhere is a
    note rather than an exit. A head round's unplaced part is an exit until it is acknowledged, and
    a verdict naming no counted finding until its key is.
    """
    if not head_reviews(pr):
        return 0
    state, why = coverage(pr)
    if state == "partial":
        if why.startswith(CARRIED_PARTIAL):
            print(
                f"status=COVERAGE_IS_PARTIAL the head reads as partial because {why}, so part of "
                "the diff may have no review. A round on the head stating full coverage clears "
                "this, unless a round on it already states a reading this cannot believe. "
                "Otherwise this is the maintainer's call"
            )
        else:
            print(
                f"status=COVERAGE_IS_PARTIAL the review covering the head states it read fewer "
                f"files than the pull request changed ({why}), so part of the diff has no review "
                "at all. A re-request is not the remedy it reads as. Splitting is the remedy where "
                "it applies, and it does not apply to a promotion. Otherwise this is the "
                "maintainer's call"
            )
        return 42
    verdict = next((it for it in items(pr) if it.kind == VERDICT), None)
    flagged = (
        uncounted_verdict(pr) if verdict and not answer_of(verdict, acknowledged(pr)) else None
    )
    if flagged and verdict:
        print(
            f"status=VERDICT_NAMES_UNCOUNTED_FINDINGS the round covering the head opens on "
            f"`{flagged[0]}`, a verdict that is not the clean one, while it states no open "
            "finding, so what it flags is named in its headline, printed above under VERDICT "
            "WITH NO COUNTED FINDING, and in any findings list or file table in the review body. "
            "Fix what it names and push. Otherwise answer it with `comment --ack "
            f"{verdict.key}=<outcome>` and the evidence, which clears this code"
        )
    if (hold := shape_hold(pr)) and not hold[2]:
        print(
            f"status=UNPLACED_PARTS_UNACKNOWLEDGED the round covering the head carries text the "
            f"reader cannot place, under UNPLACED ON THE HEAD. Acknowledging fingerprint {hold[0]} "
            "with `comment --fingerprint` and each item's outcome clears this code"
        )
    if flagged or (hold and not hold[2]):
        return 50
    if state in ("none", "unknown"):
        print(
            "note: no statement, file table, or attested local pass shows full coverage of this "
            "head, so Merge Gate item 2 is unmet unless the promotion case it states applies"
        )
    return 0


# Each `merge` status line's `next=` route, `wait`'s too, so a skill follows the field, not a list.
ROUTES = ("readback", "cleanup", "redrive", "retry", "maintainer")
NEXT = {
    "MERGE_NOT_CONFIRMED": "readback",
    "PEER_MERGED": "cleanup",
    **dict.fromkeys(("UNREAD", "MERGE_REFUSED"), "retry"),
    **dict.fromkeys(
        (
            *("HEAD_MOVED", "THREADS_UNRESOLVED", "UNPLACED_PARTS_UNACKNOWLEDGED"),
            *("REVIEW_PENDING", "UNTHREADED_FINDINGS", "OUTPUT_NOTED", "MERGE_NOT_CLEAN"),
            *("VERDICT_NAMES_UNCOUNTED_FINDINGS", "AWAITING_LOCAL_PASS", "PENDING"),
            *("CHECKS_PENDING", "CHECKS_NOT_MERGEABLE", "CHECKS_MISSING", "BAD_CHECKOUT"),
            "COPILOT_ERROR_TRANSIENT",
        ),
        "redrive",
    ),
}
# A refusal naming only that the pull request already merged owes the cleanup a merge would have run.
MERGED_ELSEWHERE = re.compile(
    r"^status=REFUSED nothing was merged into \S+ #\d+: it reads MERGED, not OPEN$"
)
STATUS = re.compile(r"^status=([A-Z_]+)")


def route(code: int, cleanup: bool, text: str) -> str:
    """The `next=` route for a run that exited `code` having printed `text`.

    A confirmed merge is `done` once `cleanup` ran clean, else `cleanup`, and the maintainer's
    where `cleanup` stopped. A run printing no status line goes to the maintainer.
    """
    lines = [ln for ln in text.splitlines() if STATUS.match(ln)]
    if any(ln.startswith("status=MERGED ") for ln in lines):
        return "maintainer" if code else "done" if cleanup else "cleanup"
    named = {
        "cleanup" if MERGED_ELSEWHERE.match(ln) else NEXT.get(ln[7:].split()[0], "maintainer")
        for ln in lines
    }
    return next((r for r in ROUTES if r in named), "maintainer")
