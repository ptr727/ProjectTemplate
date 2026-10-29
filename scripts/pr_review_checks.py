"""The pull request head's checks, read off one GraphQL payload, for the gate in `pr_review.py`.

This is a library module and has no command line. `pr_review.py` imports it as a sibling, which
works from any working directory because Python puts the running script's own directory on
`sys.path`. Everything here reads a payload already fetched and makes no call of its own. The base
branch's rules therefore reach `held_checks_open` as an argument rather than as a read.
"""

from __future__ import annotations

from datetime import datetime

# A check dispatched to a runner and not begun, in the spellings a CheckRun carries.
# A StatusContext's PENDING means the opposite, so `check_nodes` translates it away.
NOT_STARTED = frozenset({"QUEUED", "PENDING", "REQUESTED"})

# A required status nothing has posted, only ever a StatusContext's state, and not a starved job.
NOT_POSTED = frozenset({"EXPECTED"})

# SKIPPED and NEUTRAL count as passed, since the fleet aggregator skips conditional jobs.
CHECK_OK = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})

MERGE_UNGATED = frozenset({"CLEAN", "UNSTABLE", "HAS_HOOKS"})

WORKFLOW_KEYS_UNAVAILABLE = "workflowKeysUnavailable"

# How long a queued check is simply starting, the pickup grace's five minutes.
CHECK_GRACE = 300
# How long a running check runs before its duration is worth reporting, generous so it does not flag a slow job.
CHECK_STALL = 1800
# How many rollup contexts the full query asks for, built into the query so the two cannot drift.
CHECKS_WINDOW = 100

# What each stuck shape tells a reader to do, since one word for all of them would send a reader the wrong way.
# A shape this does not name reads as FAILED, the verdict no wait and no re-run clears.
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
    "FAILED": (
        "  CHECK FAILED ({name!r}, {state}/{conclusion}): a verdict rather than a stuck check, so "
        "read the run and fix what failed, since no wait and no re-run clears a real failure"
    ),
}


def age(stamp: str, now: datetime) -> float | None:
    """Seconds from `stamp` to `now`, or None where the stamp is absent or unparseable."""
    if not stamp:
        return None
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds()
    # ValueError is an unparseable stamp and TypeError a parseable one with no zone, and neither may take the digest down.
    except (ValueError, TypeError):
        return None


def head_commit(pr: dict) -> dict:
    """The commit object whose oid is the head's, or an empty dict where none is."""
    head = pr.get("headRefOid") or ""
    return next(
        (
            c.get("commit") or {}
            for c in ((pr.get("commits") or {}).get("nodes") or [])
            if (c.get("commit") or {}).get("oid") == head
        ),
        {},
    )


def unkeyed_actions_suite(pr: dict, suite: dict) -> bool:
    """True where `suite` is an Actions suite whose workflow run this read could not key it by."""
    return (
        bool(pr.get(WORKFLOW_KEYS_UNAVAILABLE))
        and (suite.get("app") or {}).get("slug") == "github-actions"
    )


def check_nodes(pr: dict) -> list[dict]:
    """The head commit's checks, each a dict with `name`, `state`, `conclusion`, and `since`.

    A readable node also carries `required`, false only where the rollup marks `isRequired: false`,
    and a CheckRun carries `app_id`, matched against a rule's `integration_id`. A node of an
    unrecognized union member has empty values and an `unreadable` key naming its type.

    A CheckRun and a StatusContext spell every field differently, so both are normalized here. Runs
    group by workflow and name, or by app and name where the suite has no workflow run. An
    unreadable workflow groups an Actions run by its own suite. Each group keeps every run of its
    highest suite id, since `startedAt` misorders overlapping runs. A name only an older suite
    carries stays, since the merge gate still reads it.

    A finished check only an older suite carries is marked `superseded`, with `newer_run`, while
    the workflow's newest suite has not completed. The reason is that a cancelled run's
    aggregator fails over jobs nobody waits on. The head commit's own suites count toward the newest one as well.
    """
    # No match reports nothing rather than another commit's rollup, and `checks_unreadable` says so.
    commit = head_commit(pr)
    rollup = commit.get("statusCheckRollup") or {}
    out: list[dict] = []
    newest_suite: dict[tuple, int] = {}
    suites: dict[int, tuple[tuple, int]] = {}
    newest_run: dict[tuple, tuple[int, str, int | None]] = {}

    def note_run(suite: dict) -> int | None:
        run = suite.get("workflowRun") or {}
        workflow = (run.get("workflow") or {}).get("databaseId")
        key = ("workflow", workflow)
        suite_id = suite.get("databaseId") or 0
        if workflow and suite_id > newest_run.get(key, (0, "", None))[0]:
            newest_run[key] = (suite_id, suite.get("status") or "", run.get("databaseId"))
        return workflow

    for n in (rollup.get("contexts") or {}).get("nodes") or []:
        if n.get("__typename") == "CheckRun":
            suite = n.get("checkSuite") or {}
            workflow = note_run(suite)
            if workflow:
                origin: tuple = ("workflow", workflow)
            elif unkeyed_actions_suite(pr, suite):
                origin = ("suite", suite.get("databaseId"))
            else:
                origin = ("app", (suite.get("app") or {}).get("slug"))
            suite_id = suite.get("databaseId") or 0
            name = n.get("name") or ""
            group = (origin, name)
            newest_suite[group] = max(newest_suite.get(group, 0), suite_id)
            suites[len(out)] = (group, suite_id)
            out.append(
                {
                    "name": n.get("name") or "",
                    "state": n.get("status") or "",
                    "conclusion": n.get("conclusion") or "",
                    "since": n.get("startedAt") or "",
                    "required": n.get("isRequired") is not False,
                    "app_id": (suite.get("app") or {}).get("databaseId"),
                }
            )
        elif n.get("__typename") == "StatusContext":
            # A StatusContext's PENDING means under way and a CheckRun's means not begun, knowable only here.
            state = n.get("state") or ""
            out.append(
                {
                    "name": n.get("context") or "",
                    "state": "IN_PROGRESS" if state == "PENDING" else state,
                    "conclusion": state,
                    "since": n.get("createdAt") or "",
                    "required": n.get("isRequired") is not False,
                }
            )
        else:
            # A third union member is skipped rather than forced into a shape, but not quietly.
            # The name carries the typename so the digest can say which member it could not read.
            out.append(
                {
                    "name": "",
                    "state": "",
                    "conclusion": "",
                    "since": "",
                    "unreadable": n.get("__typename") or "an unnamed type",
                }
            )
    for suite in (commit.get("checkSuites") or {}).get("nodes") or []:
        note_run(suite)
    stale = {i for i, (group, sid) in suites.items() if sid < newest_suite[group]}
    for i, (group, sid) in suites.items():
        suite_id, status, run = newest_run.get(group[0], (0, "", None))
        finished = out[i]["state"] == "COMPLETED"
        if finished and sid < suite_id and status != "COMPLETED":
            out[i]["superseded"] = True
            out[i]["newer_run"] = run
    return [node for i, node in enumerate(out) if i not in stale]


def check_shape(node: dict, now: datetime, grace: float, stall: float) -> str:
    """How this check is stuck, or the empty string where it is not.

    `mergeStateStatus` collapses every shape into `BLOCKED`, and each wants a different response.

    NOT_POSTED is a required status whose poster has not spoken, a StatusContext's `EXPECTED`. It
    is not a starved job, so re-running a workflow clears nothing.

    NOT_PICKED_UP is a job dispatched but assigned no runner, read from the queued state. A job
    behind `needs:` is absent from the rollup, so a queue is never dependency-blocked. Inside the
    pickup grace a queued job is simply starting.

    GATED is a job WAITING on its environment's protection rule, a reviewer or a timer. No runner
    is owed it, so the starved remedy is wrong for it too.

    RUNNING_LONG is deliberately weaker, since duration alone cannot separate a stalled job from a
    slow one. The elapsed time is printed for the reader to judge and no fault is asserted.

    FAILED is any finished check that did not pass. A check marked superseded is none of these.
    """
    if node.get("superseded"):
        return ""
    state, conclusion = node.get("state") or "", node.get("conclusion") or ""
    elapsed = age(node.get("since") or "", now)
    if state in NOT_POSTED:
        # Its own shape, since the starved remedy is wrong for it in both directions.
        return "NOT_POSTED" if elapsed is not None and elapsed > grace else ""
    if state == "WAITING":
        return "GATED" if elapsed is not None and elapsed > grace else ""
    if state in NOT_STARTED:
        return "NOT_PICKED_UP" if elapsed is not None and elapsed > grace else ""
    if state == "IN_PROGRESS":
        return "RUNNING_LONG" if elapsed is not None and elapsed > stall else ""
    # A finished check with no conclusion yet is still settling, not a failure.
    if not conclusion:
        return ""
    # A conclusion this does not recognize is reported rather than passed over as a pass.
    return "" if conclusion in CHECK_OK else "FAILED"


def checks_stuck(
    nodes: list[dict], now: datetime, grace: float, stall: float
) -> list[tuple[dict, str]]:
    """Every check in a stuck shape, as (node, shape), in the order the rollup returns them."""
    return [
        (n, s)
        for n in nodes
        if not n.get("unreadable") and (s := check_shape(n, now, grace, stall))
    ]


def checks_settling(nodes: list[dict], now: datetime, grace: float, stall: float) -> list[dict]:
    """Every check still on its way to a conclusion that waiting can reach."""
    return [
        n
        for n in nodes
        if not n.get("unreadable")
        and check_shape(n, now, grace, stall) in ("", "RUNNING_LONG")
        and (n.get("conclusion") or "") not in CHECK_OK
    ]


def held_checks_open(
    pr: dict,
    nodes: list[dict],
    now: datetime,
    grace: float,
    stall: float,
    required: frozenset[tuple[str, int | None]] = frozenset(),
) -> bool:
    """True where a covered head's checks can still move its merge, so a held wait polls on.

    A BLOCKED merge with a stuck required check has its exit 44 decided already. A check running
    long or gated is the exception, since either can still clear. Short of that, a workflow run on the
    head that has not completed holds, a gated one included. A run's later jobs enter the rollup
    only as their `needs:` finish, so a green rollup proves nothing while one is open. A CLEAN,
    UNSTABLE, or HAS_HOOKS merge has no required check outstanding, so an unrequired check does not
    hold it. Otherwise a required check still settling holds, as does any settling check while no
    required check has posted. A rollup with no check holds while the merge
    reads BLOCKED, BEHIND, or DRAFT. An UNKNOWN merge holds whatever the rollup carries, and a pull
    request no longer open holds nothing.

    A BLOCKED merge also holds while a context the base branch requires is unmet, which `required`
    carries as (context, integration id) pairs. An empty `required` holds nothing.
    """
    if pr.get("state", "OPEN") != "OPEN":
        return False
    merge = pr.get("mergeStateStatus")
    if merge == "UNKNOWN":
        return True
    if merge == "BLOCKED" and any(
        n.get("required") and shape not in ("RUNNING_LONG", "GATED")
        for n, shape in checks_stuck(nodes, now, grace, stall)
    ):
        return False
    if runs_open(pr):
        return True
    if merge in MERGE_UNGATED:
        return False
    settling = checks_settling(nodes, now, grace, stall)
    if any(n.get("required") for n in settling):
        return True
    if settling and not any(n.get("required") for n in nodes):
        return True
    if merge == "BLOCKED" and required_unregistered(pr, nodes, required):
        return True
    return not nodes and merge in ("BLOCKED", "BEHIND", "DRAFT") and not checks_unreadable(pr)


def checks_never_ran(
    pr: dict, nodes: list[dict], required: frozenset[tuple[str, int | None]]
) -> str:
    """Why an open head's gated merge lacks a check it is owed, with its remedy, or "" for none.

    A CLEAN, UNSTABLE, or HAS_HOOKS merge is owed nothing. The caller passes `required` only for a
    merge whose release judges registration, and a context is judged only on the head's own
    rollup, not cut.
    """
    if pr.get("state", "OPEN") != "OPEN" or pr.get("mergeStateStatus") in MERGE_UNGATED:
        return ""
    if checks_unreadable(pr):
        return "the payload carries no commit matching the head, so read the check runs on the head commit"
    if not nodes:
        return (
            "no check has posted, so resolve the head's conflict with its base, since a DIRTY "
            "merge runs no pull_request workflow until then"
        )
    unmet = ", ".join(required_unregistered(pr, nodes, required))
    unread = checks_unread(nodes)
    where = ", sits in the rollup's unread part," if unread else ""
    read = "read the head commit's checks and statuses first, then " if unread else ""
    return unmet and (
        f"a required check has not registered{where} or came from another app, {unmet}, so "
        f"{read}update a BEHIND head, resolve a DIRTY one's conflict, or correct the check's "
        "poster or its rule"
    )


def runs_open(pr: dict) -> list[str]:
    """The head's workflow runs whose suite has not completed, each named `run <id>`, sorted.

    Read from the head's own suites and each rollup check run's suite, since a fresh head's run
    exists before most of its jobs do. A suite with no workflow run counts for nothing, so another
    app's suite that never reports holds nothing. Where `workflowRun` was unreadable, an Actions
    suite from either source stands in as `suite <id>`, the fallback `check_nodes` takes too.
    """
    commit = head_commit(pr)
    rollup = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
    suites = [n.get("checkSuite") or {} for n in rollup if n.get("__typename") == "CheckRun"]
    named = set()
    for s in suites + ((commit.get("checkSuites") or {}).get("nodes") or []):
        if s.get("status") == "COMPLETED":
            continue
        if run := (s.get("workflowRun") or {}).get("databaseId"):
            named.add(f"run {run}")
        elif unkeyed_actions_suite(pr, s) and s.get("databaseId"):
            named.add(f"suite {s['databaseId']}")
    return sorted(named)


def required_unregistered(
    pr: dict, nodes: list[dict], required: frozenset[tuple[str, int | None]]
) -> list[str]:
    """The `required` contexts no node satisfies, sorted, on the head's own rollup, not cut."""
    if checks_unreadable(pr) or checks_truncated(pr):
        return []
    seen = [n for n in nodes if not n.get("unreadable")]
    return sorted(
        {
            context
            for context, integration in required
            if not any(
                n["name"] == context and (integration is None or n.get("app_id") == integration)
                for n in seen
            )
        }
    )


def _unmet_note(pr: dict, required: frozenset[tuple[str, int | None]], nodes: list[dict]) -> str:
    """Names the required contexts still unmet, so a reader sees what the hold waits on."""
    unmet = required_unregistered(pr, nodes, required)
    where = ", which may sit in the rollup's unread part" if checks_unread(nodes) else ""
    return f" (required, unmet: {', '.join(unmet)}{where})" if unmet else ""


def checks_truncated(pr: dict) -> bool:
    """True where the head's rollup carries more contexts than the query asked for."""
    contexts = (head_commit(pr).get("statusCheckRollup") or {}).get("contexts") or {}
    return bool((contexts.get("pageInfo") or {}).get("hasNextPage"))


def checks_unread(nodes: list[dict]) -> list[str]:
    """The rollup typenames this could not read, in the order they appeared."""
    return [n["unreadable"] for n in nodes if n.get("unreadable")]


def checks_unreadable(pr: dict) -> bool:
    """True where the payload carries commits but none of them is the head."""
    return bool((pr.get("commits") or {}).get("nodes") or []) and not head_commit(pr)


def checks_tally(nodes: list[dict]) -> tuple[int, int]:
    """(checks that have passed, checks there are), so a bare count says how far the head is."""
    read = [n for n in nodes if not n.get("unreadable")]
    return sum(1 for n in read if (n.get("conclusion") or "") in CHECK_OK), len(read)


def check_lines(
    pr: dict, checks: list[dict], stuck: list[tuple[dict, str]], now: datetime
) -> list[str]:
    """The digest's lines about the head's checks, each naming what a reader should do about it."""
    lines = []
    for node, shape in stuck:
        elapsed = age(node.get("since") or "", now)
        lines.append(
            STUCK_LINES.get(shape, STUCK_LINES["FAILED"]).format(
                # Read with `.get`, since an odd node shape should cost a field and never the digest.
                name=node.get("name") or "unnamed",
                # Clamped for display only, since a skewed clock renders a negative age.
                mins="age unknown" if elapsed is None else f"{int(max(elapsed, 0) // 60)}m",
                state=node.get("state"),
                conclusion=node.get("conclusion"),
            )
        )
    for node in checks:
        if node.get("superseded") and (node.get("conclusion") or "") not in CHECK_OK:
            lines.append(
                f"  CHECK SUPERSEDED ({node.get('name') or 'unnamed'!r}, {node.get('state')}/"
                f"{node.get('conclusion')} in an older run): run {node.get('newer_run')} of the "
                "same workflow on this head has not finished, so this is not a verdict on the "
                "head, and that run's own check replaces it once it posts"
            )
    unread = checks_unread(checks)
    if unread:
        lines.append(
            f"  CHECKS PARTIALLY UNREAD ({', '.join(sorted(set(unread)))}): the rollup "
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
