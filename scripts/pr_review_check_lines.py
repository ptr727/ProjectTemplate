"""The digest's lines about the head's checks, rendered for `pr_review_digest.py`.

This is a library module and has no command line. `pr_review_digest.py` imports it as a sibling.
It renders the shapes `pr_review_checks.py` reads and makes no read or call of its own. A new
shape's wording is therefore one entry in `STUCK_LINES` rather than a branch in the reader.

`STUCK_LINES` says what each stuck shape tells a reader to do, since one word for all of them would
send a reader the wrong way. A shape it does not name reads as FAILED, the verdict no wait and no
re-run clears.
"""

from __future__ import annotations

from datetime import datetime

from pr_review_checks import (
    CHECK_OK,
    CHECKS_WINDOW,
    WORKFLOW_KEYS_UNAVAILABLE,
    age,
    checks_truncated,
    checks_unread,
    checks_unreadable,
)

STUCK_LINES = {
    "NOT_PICKED_UP": (
        "  CHECK NOT PICKED UP ({name!r}, queued {mins} with no runner assigned): nothing here "
        "starts it, because the runner pool is GitHub-hosted, so re-run the workflow or wait on "
        "that capacity"
    ),
    "NOT_POSTED": (
        "  CHECK NEVER POSTED ({name!r}, expected {mins} and not reported): a required status "
        "whose poster has not spoken, so no runner is owed it and re-running a workflow here "
        "clears nothing"
    ),
    "GATED": (
        "  CHECK WAITING ON ITS ENVIRONMENT ({name!r}, waiting {mins}): a protection rule holds "
        "it, a reviewer or a timer, so approve it in the run or let the timer pass, since a re-run "
        "meets the same rule"
    ),
    "RUNNING_LONG": (
        "  CHECK RUNNING LONG ({name!r}, running {mins}): it has a runner, so it is not starved, "
        "and whether this is hung or merely slow is a judgment against what this job normally "
        "costs"
    ),
    "STALE_RECORD": (
        "  CHECK RECORD STALE ({name!r}, in progress {mins} in run {run}, which has completed): "
        "the job's record outlived its run, so waiting longer cannot end it, and re-running that "
        "job or pushing a new head replaces it"
    ),
    "FAILED": (
        "  CHECK FAILED ({name!r}, {state}/{conclusion}): a verdict rather than a stuck check, so "
        "read the run and fix what failed, since no wait and no re-run clears a real failure"
    ),
}


def check_lines(
    pr: dict, checks: list[dict], stuck: list[tuple[dict, str]], now: datetime
) -> list[str]:
    """The digest's lines about the head's checks, each naming what a reader should do about it.

    A node's fields are read with `.get`, since an odd node shape should cost a field and never the
    digest. An age is clamped for display only, since a skewed clock renders a negative one.
    """
    lines = []
    for node, shape in stuck:
        elapsed = age(node.get("since") or "", now)
        lines.append(
            STUCK_LINES.get(shape, STUCK_LINES["FAILED"]).format(
                name=node.get("name") or "unnamed",
                mins="age unknown" if elapsed is None else f"{int(max(elapsed, 0) // 60)}m",
                state=node.get("state"),
                conclusion=node.get("conclusion"),
                run=node.get("run") or "unknown",
            )
        )
    for node in checks:
        if node.get("superseded") and (node.get("conclusion") or "") not in CHECK_OK:
            reading = "/".join(v for v in (node.get("state"), node.get("conclusion")) if v)
            lines.append(
                f"  CHECK SUPERSEDED ({node.get('name') or 'unnamed'!r}, {reading} in an older "
                f"run): run {node.get('newer_run')} of the "
                "same workflow on this head has not finished, so this is not a verdict on the "
                "head, and that run's own check replaces it once it posts"
            )
    unread = checks_unread(checks)
    if unread:
        lines.append(
            f"  CHECKS PARTIALLY UNREAD ({', '.join(unread)}): the rollup "
            "carries a context type this cannot normalize, so it is absent from the "
            "tally and the stuck reading alike and neither speaks for it"
        )
    if checks_truncated(pr):
        lines.append(
            f"  CHECKS TRUNCATED: the head carries more than the {CHECKS_WINDOW} contexts "
            "this query asks for, so a check past the window is missing from the tally "
            "and from the stuck reading alike, and neither reports what it did not see"
        )
    if checks_unreadable(pr):
        lines.append(
            "  CHECKS UNREADABLE: the payload carries commits and none of them is the "
            "head, so no rollup here describes this head and `checks=0/0` is this "
            "reading failing rather than a pull request with no checks"
        )
    if pr.get(WORKFLOW_KEYS_UNAVAILABLE):
        lines.append(
            "  CHECKS DEDUPED WITHOUT WORKFLOW KEYS: `workflowRun` was unreadable, "
            "so each Actions run is kept per suite and a superseded failure may still "
            "show, which reads red rather than hiding a failing check"
        )
    return lines
