"""The review gate's digest, rendered off one payload, for the gate in `pr_review.py`.

This is a library module and has no command line. `pr_review.py` imports it as a sibling, and
`pr_review.digest` does the reads: the payload, the normalized checks, and the run log behind a
refusal.
`render` takes those results as arguments and makes no call. The header is an ordered list of
fields, so a new one is one entry. The fixed advisory lines are an ordered table, one row each.
"""

from __future__ import annotations

from datetime import datetime

import pr_review_copilot as adapter
from pr_review_check_lines import check_lines
from pr_review_checks import checks_stuck, checks_tally
from pr_review_rounds import (
    CODERABBIT_LOGIN,
    KNOWN_REVIEWERS,
    OTHER_REVIEWERS,
    QODO_LOGIN,
    REVIEWER,
    answered_outside_review,
    collected,
    commit_oid,
    coverage,
    first_comment,
    head_nodes,
    head_reviews,
    is_open,
    newest_of,
    open_threads,
    opened_by,
    possible_quota,
    qodo_comment,
    qodo_comments_blind,
    quota_refusal,
    rate_limited_by,
    reading,
    refusing_review,
    review_effort,
    reviewer_login_drift,
    reviewer_nodes,
    reviewer_requested,
    reviews_truncated,
    shape_hold,
    stopping_refusal,
    thread_author,
    threads_truncated,
    uncounted_verdict,
    window_blind,
)

BEHIND_THE_WINDOW = (
    "  BEHIND THE WINDOW ({value}): the newest {window} carry "
    "none from the reviewer and older ones exist, so this cannot decide"
)
THREADS_TRUNCATED = (
    "  THREADS TRUNCATED: this pull request carries more review threads than the "
    "100 read here, and the connection reads oldest-first, so the ones cut off are "
    "the newest rather than the oldest, exactly the ones most likely to still be "
    "open. `threads=` and `unresolved=` above undercount. Read the rest with "
    "`reply`'s own paginated walk before trusting either number"
)
REVIEWS_TRUNCATED = (
    "  REVIEWS TRUNCATED: this pull request carries more reviews than the 100 read "
    "here, and the connection reads newest-first, so the ones cut off are the "
    "oldest, exactly where an unanswered suppressed or outside-diff-range finding is "
    "most likely to still sit unresolved. `suppressed=` and `cr_outside_diff=` above "
    "undercount"
)


def digest_refusal(pr: dict) -> dict | None:
    """The refusal the digest reports: a stopping one, else one standing in for a missing head round."""
    return stopping_refusal(pr) or (None if head_reviews(pr) else refusing_review(pr))


def state_fields(pr: dict) -> list[tuple[str, object]]:
    """`state=` and when it changed, where the pull request is no longer open, else nothing."""
    if is_open(pr):
        return []
    merged, commit = pr.get("mergedAt"), (pr.get("mergeCommit") or {}).get("oid")
    stamp = ("merged_at", merged) if merged else ("closed_at", pr.get("closedAt"))
    return [("state", pr["state"]), stamp, ("merge_commit", commit[:8] if commit else None)]


def header(fields: list[tuple[str, object]]) -> str:
    """Join each `key=value` pair by one space, leaving out a field whose value is None."""
    return " ".join(f"{key}={value}" for key, value in fields if value is not None)


def render(
    owner: str,
    repo: str,
    num: int,
    pr: dict,
    checks: list[dict],
    now: datetime,
    grace: float,
    stall: float,
    quota: str,
    run_log: str,
    seen: set[str] | None,
    hub: str,
    window: int,
) -> tuple[str, int]:
    """Render the digest and return it with the unresolved count.

    `quota` is the run log's rate-limit sentence for the refusal `digest_refusal` names, read by the
    caller. `run_log` is the caller's `RUN LOG READING` line for that refusal, empty for none.
    Where `seen` is a set, only threads absent from it print, marked new, and are added.
    """
    head = pr["headRefOid"]
    revs = reviewer_nodes(pr, "reviews")
    on_head = head_reviews(pr)
    newest = newest_of(on_head)
    found = reading(newest, pr=pr) if newest is not None else None
    effort, effort_source = review_effort(pr)
    cover, cover_why = coverage(pr)
    hold = shape_hold(pr)
    # Every round's notes, since a renamed section on an earlier round hides a finding too.
    notes = [
        *dict.fromkeys(
            f"round {(commit_oid(node) or 'unknown')[:8]}: {n}"
            for node in revs
            for n in reading(node).notes
            if node not in on_head or n not in {p.note for p in reading(node).unplaced}
        ),
        *reviewer_login_drift(pr),
    ]
    threads = pr["reviewThreads"]["nodes"]
    truncated = threads_truncated(pr)
    revs_truncated = reviews_truncated(pr)
    unresolved = open_threads(threads)
    by_login = {
        login: sum(1 for t in unresolved if thread_author(t) == login) for login in KNOWN_REVIEWERS
    }
    by_login["other"] = sum(1 for t in unresolved if thread_author(t) not in KNOWN_REVIEWERS)
    contributors = {login: n for login, n in by_login.items() if n}
    breakdown = (
        " (" + " ".join(f"{login}={n}" for login, n in contributors.items()) + ")"
        if len(contributors) > 1 or "other" in contributors
        else ""
    )
    suppressed, suppressed_field = collected(pr, REVIEWER, "suppressed")
    missed, missed_field = collected(pr, REVIEWER, "previously_missed")
    outside, outside_field = collected(pr, CODERABBIT_LOGIN, "previously_missed")
    listed = len(opened_by(pr, newest)) if newest is not None else 0
    flagged = uncounted_verdict(pr)
    advisory = uncounted_verdict(pr, "clean")
    answer = answered_outside_review(pr)
    refusal = digest_refusal(pr)
    refusal_field = (
        "no"
        if not refusal
        else "QUOTA"
        if quota_refusal(refusal) or quota
        else "ERROR"
        if possible_quota(refusal)
        else "YES"
    )
    blind = [f for f in ("reviews", "comments") if window_blind(pr, f)]
    answered = "yes" if answer else ("unknown" if blind else "no")
    ok, total = checks_tally(checks)
    stuck = checks_stuck(checks, now, grace, stall)
    other_on_head = [
        login for login in OTHER_REVIEWERS if head_nodes(pr, reviewer_nodes(pr, "reviews", login))
    ]
    other_limited = {
        login: name for login in OTHER_REVIEWERS if (name := rate_limited_by(pr, login)) is not None
    }
    qodo = qodo_comment(pr)
    qodo_open = [s for s in reading(qodo, QODO_LOGIN).sections if s.kind == "open"] if qodo else []
    shown = []
    for t in unresolved:
        if seen is not None:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
        shown.append(t)
    plus = "+" if truncated else ""
    # The repository leads the line, since a digest of the wrong pull request is well-formed.
    fields: list[tuple[str, object]] = [
        ("repo", f"{owner}/{repo}"),
        ("pr", num),
        *state_fields(pr),
        ("head", head[:8]),
        ("rounds", len(revs)),
        ("review_on_head", "yes" if on_head else "local" if cover == "local" else "NO"),
        ("other_reviewed", ",".join(other_on_head) or None),
        (
            "other_rate_limited",
            ",".join(f"{login}:{name}" if name else login for login, name in other_limited.items())
            or None,
        ),
        ("effort", effort),
        ("effort_source", effort_source),
        ("coverage", cover.upper() if cover in ("partial", "unknown") else cover),
        (
            "shapes",
            ("ACKNOWLEDGED" if hold[2] else "HELD") if hold else "NOTED" if notes else "ok",
        ),
        ("refusal", refusal_field),
        ("threads", f"{len(threads)}{plus}"),
        ("unresolved", f"{len(unresolved)}{plus}{breakdown}"),
        ("suppressed", suppressed_field),
        ("previously_missed", missed_field if missed or revs_truncated else None),
        (
            "overview",
            f"{'?' if found.open is None else found.open}/{listed}"
            if found is not None and found.marked
            else None,
        ),
        ("cr_outside_diff", outside_field if outside or revs_truncated else None),
        ("qodo_open", len(qodo_open) if qodo else "unknown" if qodo_comments_blind(pr) else None),
        ("answered_outside_review", answered),
        ("requested", "yes" if reviewer_requested(pr) else "no"),
        ("merge", str(pr.get("mergeStateStatus"))),
        ("checks", f"{ok}/{total}"),
        ("stuck", ",".join(sorted({s for _, s in stuck})) or None),
        ("new", len(shown) if seen is not None else None),
    ]
    lines = [header(fields)]
    if refusal:
        lines.append(
            "  COPILOT REFUSED THIS ROUND: the newest Copilot review on this pull request says "
            "it did not review, so it covers nothing. The body below says which remedy applies, "
            "and any run log line under it says whether a re-request is owed"
        )
        lines += [
            f"    {ln.rstrip()}" for ln in (refusal.get("body") or "").splitlines() if ln.strip()
        ]
        if quota:
            lines.append(f"  RATE LIMIT FROM THE REVIEWER'S RUN LOG: {quota}")
        if run_log:
            lines.append(run_log)
    if notes:
        lines.append(
            f"  REVIEWER OUTPUT NOTED ({len(notes)}): the reader placed part of what the reviewer "
            "sent nowhere, so read the body itself before closing the review loop. A spelling "
            f"to add belongs in `scripts/reviewers/` on {hub}, which hosts this script"
        )
        lines += [f"    {note}" for note in notes]
    if hold:
        lines.append(
            f"  UNPLACED ON THE HEAD (fingerprint={hold[0]}): a round covering the head carries "
            + (
                "parts an owner, member, or collaborator acknowledged with written outcomes"
                if hold[2]
                else "text the reader cannot place, where a finding can sit. Read each part whole "
                "in the review body, the excerpt only locating it, and give each item an outcome, "
                "fixed with its SHA, declined with evidence, or listed as pre-existing in the PR "
                f"body. Post them with `comment {num} --repo {owner}/{repo} --fingerprint "
                f"{hold[0]} --body <outcomes>`"
            )
        )
        for part in hold[1]:
            lines += [f"    {part.note}", *([] if hold[2] else adapter.excerpt(part.text))]
    unmatched, marked = adapter.listed_lines(
        found.listed_items if found else (),
        threads,
        (pr.get("files") or {}).get("nodes") or [],
        truncated,
    )
    if found is not None and (owed := max(found.unthreaded, unmatched)):
        lines.append(
            f"  FINDINGS WITH NO THREAD ({owed}): the round covering the head names more findings "
            "than its open entries link to threads on this pull request, or lists one no thread "
            f"sits on, so {owed} {'is' if owed == 1 else 'are'} raised where polling "
            "threads cannot see them. Read the review body and give each the triage a suppressed "
            "finding gets. A thread past the hundred read here counts too, and `threads=` "
            "carries a trailing `+` where that page was cut"
            + (". Each findings list item prints below" if marked else "")
        )
        lines += marked
    if flagged and found is not None and found.listed:
        lines.append(
            f"  VERDICT WITH NO COUNTED FINDING: the round covering the head opens on "
            f"`{flagged[0]}` and states no open finding, so its headline and its findings list "
            "or file table are where what it flags is named. Give both the triage a suppressed "
            "finding gets"
        )
    elif flagged:
        lines.append(
            f"  VERDICT WITH NO COUNTED FINDING: the round covering the head opens on "
            f"`{flagged[0]}` and counts no finding, so its headline is the one place what it "
            "flags is named. Give the headline the triage a suppressed finding gets"
        )
    if flagged:
        lines.append(f"    {flagged[1] or '(the headline carries no prose)'}")
    if advisory and advisory[1]:
        lines.append(
            f"  CLEAN VERDICT HEADLINE: the round covering the head opens on `{advisory[0]}` and "
            "counts no finding. Its headline may name a remaining nit, new or already answered, "
            "which nothing here tells apart, so this is advisory and changes no exit code"
        )
        lines.append(f"    {advisory[1]}")
    if cover in ("table", "carried:table", "local", "partial", "unknown"):
        lines.append(f"  COVERAGE IS {cover.upper()}: {cover_why}")
    elif cover == "none" and on_head:
        lines.append(
            "  NO COVERAGE IS SHOWN: no round covering the head states coverage, and "
            f"{cover_why or 'nothing stands in'}. Merge Gate item 2 reads this field"
        )
    lines.extend(check_lines(pr, checks, stuck, now))
    for value, template in (
        (" and ".join(blind), BEHIND_THE_WINDOW),
        (truncated, THREADS_TRUNCATED),
        (revs_truncated, REVIEWS_TRUNCATED),
    ):
        if value:
            lines.append(template.format(value=value, window=window))
    if answer:
        # Printed whole, since its wording is the only thing separating a refusal from a remark.
        lines.append(
            f"  COPILOT COMMENT ({answer.get('createdAt')}, newer than any review): "
            "the reviewer answered without reviewing, so read the body below and "
            "decide, since a refusal is terminal and a remark is not"
        )
        lines += [
            f"    {ln.rstrip()}" for ln in (answer.get("body") or "").splitlines() if ln.strip()
        ]
    mark = "NEW " if seen is not None else ""
    for t in shown:
        c = first_comment(t)
        body = " ".join((c.get("body") or "").split())
        lines.append(f"  {mark}{t['id']} {c.get('path')}:{c.get('line')} {body[:160]}")
    # Printed whole, since a finding with no thread appears nowhere but here.
    for heading, entries in (
        ("SUPPRESSED", suppressed),
        ("PREVIOUSLY MISSED", missed),
        ("CODERABBIT OUTSIDE-DIFF", outside),
    ):
        for where, section in entries:
            lines.append(
                f"  {heading} ({where}): no thread to resolve, "
                "answer it in the PR conversation quoting the finding"
            )
            lines += [f"    {ln}" for ln in section.text.splitlines() if ln.strip()]
    for section in qodo_open:
        lines.append(
            "  QODO OPEN FINDING: neither Resolved nor Dismissed, no thread to resolve, "
            "answer it in the PR conversation quoting the finding, and spot-verify Qodo's "
            "own badge against `gh pr diff` rather than trusting it outright"
        )
        lines.append(f"    {section.title}")
    return "\n".join(lines), len(unresolved)
