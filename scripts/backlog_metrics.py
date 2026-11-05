#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["markdown-it-py>=4"]
# ///
"""Render a weekly backlog and review-cost report for the fleet's repositories.

Why this exists: a weekly count of issues filed against closed, and of issues per merged pull
request, shows the backlog's direction. Copilot review runs per merged pull request share that
window. The month's Actions minutes and Copilot gross cover the calendar month to date instead.
So the report prints them beside the window's counts rather than dividing them by its merges.

The report goes to stdout only, so a server timer can mail it. Nothing here runs from a workflow.
Counting and rendering are pure functions over parsed `gh` output. Only `run_gh` and `graphql`
start a process.

Usage
  uv run --no-project --script scripts/backlog_metrics.py [--owner OWNER] [--repo NAME ...]
                                                           [--days N] [--format markdown|html]

It is a uv script because it imports the review gate's own readers, which parse Markdown. A copy
of those readers here would drift from the gate and report figures the gate would not.

Exit codes
  0  the report was written.
  1  a `gh` call for issues, pull requests, or runs failed, so no number is guessed and no report
     is written. A failed review query still writes the backlog and billing report. It adds one
     line saying the review figures were unavailable and names the error on stderr, then exits 1.
     Four reads fail more softly: the default branch, whether `develop` exists, the policy file,
     and its history. A failed one still writes the report, names that read on stderr, and
     exits 1. Where the policy itself went unread, the report shows `error` in its place.
  2  the command line was wrong.
"""

import argparse
import datetime
import html
import json
import sys
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pr_review_rounds as rounds
import pr_review_side as side
from gh_graphql import GraphQLError, request, run_subprocess, said_absent, walk
from pr_review_checks import utc_instant
from pr_review_copilot import VOCABULARIES
from pr_review_policy import BOTS, COMMANDS, POLICY_PATH, PolicyError, fetch

REGISTRY = Path(__file__).resolve().parent.parent / "registry" / "repos.json"

LIST_LIMIT = 1000
HANDOFF_LABEL = "handoff"
REVIEW_WORKFLOW = "Running Copilot Code Review"
PAGE_SIZE = 50
PR_CAP = 500
COMMIT_CAP = 50
THREAD_CAP = 100
COMMENT_CAP = 100
REVIEW_CAP = 100
EVENT_CAP = 100
CONNECTION_CAPS = {
    "commits": COMMIT_CAP,
    "reviews": REVIEW_CAP,
    "reviewThreads": THREAD_CAP,
    "comments": COMMENT_CAP,
    "timelineItems": EVENT_CAP,
}
INTEGRATION_BRANCH = "develop"
HISTORY_CAP = 10
FAILED_STATES = frozenset({"FAILURE", "ERROR"})
LABELS = ("pre-existing", "prose", "documentation", "bug", "script", "enhancement", "decision")

Items = Sequence[Mapping[str, Any]]


BOT_LOGINS = {name: str(VOCABULARIES[name]["login"]) for name in BOTS}
AUTOMATIC_ROUND_BOTS = ("coderabbit",)


class GhError(RuntimeError):
    """A `gh` command that did not run, exited non-zero, or printed something other than JSON."""


@dataclass(frozen=True)
class RepoMetrics:
    name: str
    opened: int
    closed: int
    handoffs_created: int
    open_now: int
    open_by_label: Mapping[str, int]
    merged: int
    runs: int
    review_runs: int
    truncated: bool = False


def run_gh(args: Sequence[str]) -> Any:
    """Run `gh` and return its parsed JSON, raising with the stderr on a failure."""
    command = ["gh", *args]
    try:
        code, stdout, stderr = run_subprocess(command)
    except GraphQLError as error:
        raise GhError(str(error)) from error
    if code != 0:
        raise GhError(f"{' '.join(command)} exited {code}: {stderr.strip()}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as error:
        raise GhError(f"{' '.join(command)} printed no JSON: {error}") from error


def label_names(item: Mapping[str, Any]) -> set[str]:
    return {str(label.get("name", "")) for label in item.get("labels") or []}


def count_without_label(items: Items, label: str) -> int:
    return sum(1 for item in items if label not in label_names(item))


def count_with_label(items: Items, label: str) -> int:
    return sum(1 for item in items if label in label_names(item))


def count_by_label(items: Items, labels: Sequence[str]) -> dict[str, int]:
    """Count per label, so an item carrying several labels counts under each.

    The named labels always appear, a zero included, and every other label an item carries follows.
    """
    found = set().union(*(label_names(item) for item in items)) - set(labels)
    return {label: count_with_label(items, label) for label in [*labels, *sorted(found)]}


def count_named(runs: Items, workflow: str) -> int:
    return sum(1 for run in runs if run.get("name") == workflow)


def per_merged(count: int, merged: int) -> str:
    if merged == 0:
        return "n/a"
    return f"{count / merged:.1f}"


def compute_metrics(
    name: str,
    opened_items: Items,
    closed_items: Items,
    open_items: Items,
    merged_prs: Items,
    runs: Items,
) -> RepoMetrics:
    """Reduce one repository's parsed lists to the report's counts."""
    lists = (opened_items, closed_items, open_items, merged_prs, runs)
    return RepoMetrics(
        name=name,
        opened=count_without_label(opened_items, HANDOFF_LABEL),
        closed=count_without_label(closed_items, HANDOFF_LABEL),
        handoffs_created=count_with_label(opened_items, HANDOFF_LABEL),
        open_now=count_without_label(open_items, HANDOFF_LABEL),
        open_by_label=count_by_label(
            [item for item in open_items if HANDOFF_LABEL not in label_names(item)], LABELS
        ),
        merged=len(merged_prs),
        runs=len(runs),
        review_runs=count_named(runs, REVIEW_WORKFLOW),
        truncated=any(len(items) >= LIST_LIMIT for items in lists),
    )


def sum_actions_minutes(usage: Mapping[str, Any]) -> dict[str, float]:
    """Sum Actions minutes per repository from a billing usage response."""
    minutes: dict[str, float] = {}
    for item in usage.get("usageItems") or []:
        if item.get("product") == "actions" and item.get("unitType") == "Minutes":
            repo = str(item.get("repositoryName", ""))
            minutes[repo] = minutes.get(repo, 0.0) + float(item.get("quantity") or 0)
    return minutes


def sum_copilot_gross(usage: Mapping[str, Any]) -> float:
    return sum(
        float(item.get("grossAmount") or 0)
        for item in usage.get("usageItems") or []
        if item.get("product") == "copilot"
    )


@dataclass
class BotStats:
    requests_by_automation: int = 0
    requests_by_user: int = 0
    rounds: list[int] = field(default_factory=list)
    automatic_rounds: int = 0
    explicit_rounds: int = 0
    unclassified_rounds: int = 0
    findings: int = 0
    missed_by_local: int = 0
    findings_on_green_heads: int = 0


@dataclass
class ReviewMetrics:
    name: str
    prs: int = 0
    promotions_skipped: int = 0
    commits: int = 0
    local_findings: int = 0
    local_unknown: int = 0
    failing_heads: int = 0
    failing_after_clean_local: int = 0
    bots: dict[str, BotStats] = field(default_factory=dict)
    policy: dict[str, str] = field(default_factory=dict)
    policy_error: str = ""
    policy_read: str = "policy file"
    policy_branch: str = ""
    history_error: str = ""
    duplicates_skipped: int = 0
    policy_changes: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)


def is_automation(actor: Mapping[str, Any] | None) -> bool:
    return (actor or {}).get("__typename") == "Bot"


def count_request(stats: BotStats, actor: Mapping[str, Any] | None) -> None:
    if is_automation(actor):
        stats.requests_by_automation += 1
    else:
        stats.requests_by_user += 1


def nodes_of(connection: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    return list((connection or {}).get("nodes") or [])


def head_state(commit: Mapping[str, Any]) -> str:
    """Classify a head by its rolled-up status as `failed`, `green`, or `other`."""
    state = str((commit.get("statusCheckRollup") or {}).get("state") or "")
    if state in FAILED_STATES:
        return "failed"
    return "green" if state == "SUCCESS" else "other"


def attestations(pr: Mapping[str, Any]) -> dict[str, str]:
    """Each read commit's sha to the findings text the gate's `attestation` reads for it as head.

    Calling the gate's function keeps what counts as an attestation in one place. A head outside
    the commits window is not read, and that window is already noted where it was cut.
    """
    found: dict[str, str] = {}
    for node in nodes_of(pr.get("commits")):
        oid = str((node.get("commit") or {}).get("oid") or "")
        value = rounds.attestation({**pr, "headRefOid": oid}) if oid else None
        if value is not None:
            found[oid] = value
    return found


def is_promotion(pr: Mapping[str, Any], default_branch: str) -> bool:
    return pr.get("headRefName") == INTEGRATION_BRANCH and pr.get("baseRefName") == default_branch


def classify_rounds(
    pr: Mapping[str, Any],
    login: str,
    triggers: Sequence[str],
    stats: BotStats,
    heads: set[str],
) -> None:
    """Sort each head the bot reviewed into automatic, explicit or unclassified.

    `heads` is the gate's set of reviewed heads, so a refusal never counts as a round.

    A head arrives at its commit date. It is explicit when a trigger falls between that arrival
    and the bot's first review of it. A head without both times cannot be classified. Every time is
    parsed before it is compared. A commit date keeps the committer's own offset, so its text does
    not order against a UTC stamp.
    """
    arrival = {
        str((n.get("commit") or {}).get("oid")): utc_instant(
            str((n.get("commit") or {}).get("committedDate") or "")
        )
        for n in nodes_of(pr.get("commits"))
    }
    asked = [t for t in (utc_instant(s) for s in triggers) if t is not None]
    first: dict[str, datetime.datetime | None] = {}
    for review in rounds.reviewer_nodes(dict(pr), "reviews", login):
        oid = str(rounds.commit_oid(review))
        if oid not in heads or rounds.refusal_of(review):
            continue
        at = utc_instant(str(review.get("submittedAt") or ""))
        known = first.get(oid)
        if oid not in first or (at is not None and (known is None or at < known)):
            first[oid] = at
    for oid, reviewed_at in first.items():
        arrived = arrival.get(oid)
        if arrived is None or reviewed_at is None:
            stats.unclassified_rounds += 1
        elif any(arrived <= t <= reviewed_at for t in asked):
            stats.explicit_rounds += 1
        else:
            stats.automatic_rounds += 1


def review_pr(pr: Mapping[str, Any], result: ReviewMetrics, default_branch: str = "") -> None:
    """Fold one merged pull request's review payload into the repository's tallies."""
    if is_promotion(pr, default_branch):
        result.promotions_skipped += 1
        return
    number = pr.get("number")
    for key, cap in CONNECTION_CAPS.items():
        if rounds.page_flag(dict(pr), key, "hasNextPage"):
            result.truncated.append(f"PR {number} {key} past {cap}")
    commits = [n.get("commit") or {} for n in nodes_of(pr.get("commits"))]
    states = {str(c.get("oid")): head_state(c) for c in commits}
    attested = attestations(pr)
    result.prs += 1
    result.commits += len(commits)
    for value in attested.values():
        if value == "unknown":
            result.local_unknown += 1
        else:
            result.local_findings += int(value)
    for oid, state in states.items():
        if state == "failed":
            result.failing_heads += 1
            if attested.get(oid) == "0":
                result.failing_after_clean_local += 1
    for name, login in BOT_LOGINS.items():
        stats = result.bots.setdefault(name, BotStats())
        heads = {
            str(rounds.commit_oid(n))
            for n in rounds.reviewer_nodes(dict(pr), "reviews", login)
            if not rounds.refusal_of(n) and rounds.commit_oid(n)
        }
        events = [
            e
            for e in nodes_of(pr.get("timelineItems"))
            if (e.get("requestedReviewer") or {}).get("login") == login
        ]
        for event in events:
            count_request(stats, event.get("actor"))
        asks: list[Mapping[str, Any]] = []
        for command in (
            COMMANDS if name == "coderabbit" else (("review",) if name == "qodo" else ())
        ):
            asks += side.side_asks(dict(pr), side.side_command(name, command))
        stats.requests_by_user += len(asks)
        if name == "qodo":
            total = side.side_rounds(dict(pr), name, side.side_command(name))
            if total > 0:
                stats.rounds.append(total)
        elif heads:
            stats.rounds.append(len(heads))
        if name in AUTOMATIC_ROUND_BOTS:
            triggers = [str(n.get("createdAt") or "") for n in (*asks, *events)]
            classify_rounds(pr, login, triggers, stats, heads)
        for thread in nodes_of(pr.get("reviewThreads")):
            if rounds.thread_author(dict(thread)) != login:
                continue
            opening = rounds.first_comment(dict(thread))
            head = str((opening.get("originalCommit") or {}).get("oid"))
            stats.findings += 1
            if attested.get(head) == "0":
                stats.missed_by_local += 1
            if states.get(head) == "green":
                stats.findings_on_green_heads += 1


def window_start(today: datetime.date, days: int) -> datetime.date:
    return today - datetime.timedelta(days=days)


HEADERS = (
    "Repository",
    "Issues opened (count)",
    "Issues closed (count)",
    "Handoffs created (count)",
    "Issues open now (count)",
    "PRs merged (count)",
    "Issues opened per merged PR (ratio)",
    "Workflow runs (count)",
    "Copilot review runs (count)",
    "Copilot review runs per merged PR (ratio)",
)


def table_rows(metrics: Sequence[RepoMetrics]) -> list[list[str]]:
    rows = [
        [
            m.name,
            str(m.opened),
            str(m.closed),
            str(m.handoffs_created),
            str(m.open_now),
            str(m.merged),
            per_merged(m.opened, m.merged),
            str(m.runs),
            str(m.review_runs),
            per_merged(m.review_runs, m.merged),
        ]
        for m in metrics
    ]
    opened = sum(m.opened for m in metrics)
    merged = sum(m.merged for m in metrics)
    rows.append(
        [
            "Total",
            str(opened),
            str(sum(m.closed for m in metrics)),
            str(sum(m.handoffs_created for m in metrics)),
            str(sum(m.open_now for m in metrics)),
            str(merged),
            per_merged(opened, merged),
            str(sum(m.runs for m in metrics)),
            str(sum(m.review_runs for m in metrics)),
            per_merged(sum(m.review_runs for m in metrics), merged),
        ]
    )
    return rows


def label_line(m: RepoMetrics) -> str:
    counts = ", ".join(f"{label} {count}" for label, count in m.open_by_label.items())
    return f"{m.name}, open issues by label (count): {counts}"


def billing_lines(
    month: str, repos: Sequence[str], usage: Mapping[str, Any] | None, error: str
) -> list[str]:
    if usage is None:
        return [f"Billing was unavailable for {month}: {error}"]
    minutes = sum_actions_minutes(usage)
    per_repo = ", ".join(f"{repo} {minutes.get(repo, 0.0):.1f}" for repo in repos)
    return [
        f"Actions minutes, {month} (minutes): {per_repo}",
        f"Copilot gross, {month} (USD): {sum_copilot_gross(usage):.2f}",
    ]


def notes(metrics: Sequence[RepoMetrics]) -> list[str]:
    return [
        f"{m.name}: a list reached the {LIST_LIMIT} item limit, so its counts may be low."
        for m in metrics
        if m.truncated
    ]


REVIEW_BOT_HEADERS = (
    "Repository",
    "Bot",
    "Policy trigger",
    "Policy branch",
    "Requests by automation (count)",
    "Requests by user (count)",
    "Automatic rounds (count)",
    "Explicit rounds (count)",
    "Unclassified rounds (count)",
    "Rounds per reviewed PR (mean)",
    "Rounds per reviewed PR (max)",
    "Threaded findings (count)",
    "Findings missed by local review (count)",
    "Findings on all-green heads (count)",
)
REVIEW_REPO_HEADERS = (
    "Repository",
    "Merged PRs read (count)",
    "Promotion PRs excluded (count)",
    "Commits per merged PR (mean)",
    "Local findings (sum)",
    "Local attestations with unknown findings (count)",
    "Failing heads (count)",
    "Failing heads after clean local review (count)",
)


def review_bot_rows(reviews: Sequence[ReviewMetrics]) -> list[list[str]]:
    return [
        [
            r.name,
            bot,
            "error" if r.policy_error else r.policy.get(bot, "n/a"),
            r.policy_branch or "n/a",
            str(stats.requests_by_automation),
            str(stats.requests_by_user),
            str(stats.automatic_rounds) if bot in AUTOMATIC_ROUND_BOTS else "n/a",
            str(stats.explicit_rounds) if bot in AUTOMATIC_ROUND_BOTS else "n/a",
            str(stats.unclassified_rounds) if bot in AUTOMATIC_ROUND_BOTS else "n/a",
            per_merged(sum(stats.rounds), len(stats.rounds)),
            str(max(stats.rounds, default=0)) if stats.rounds else "n/a",
            str(stats.findings),
            str(stats.missed_by_local),
            str(stats.findings_on_green_heads),
        ]
        for r in reviews
        for bot, stats in r.bots.items()
    ]


def review_repo_rows(reviews: Sequence[ReviewMetrics]) -> list[list[str]]:
    return [
        [
            r.name,
            str(r.prs),
            str(r.promotions_skipped),
            per_merged(r.commits, r.prs),
            str(r.local_findings),
            str(r.local_unknown),
            str(r.failing_heads),
            str(r.failing_after_clean_local),
        ]
        for r in reviews
    ]


def review_notes(reviews: Sequence[ReviewMetrics]) -> list[str]:
    out = []
    for r in reviews:
        if r.truncated:
            out.append(
                f"{r.name}: review figures are truncated, since a cap was reached:"
                f" {', '.join(r.truncated)}."
            )
        if r.policy_changes:
            out.append(
                f"{r.name}: {POLICY_PATH} changed on {', '.join(r.policy_changes)} on {r.policy_branch},"
                " inside the window,"
                " so the window mixes two policies."
            )
        if r.policy_error:
            out.append(f"{r.name}: reading the {r.policy_read} failed: {r.policy_error}")
        if r.history_error:
            out.append(f"{r.name}: reading the {POLICY_PATH} history failed: {r.history_error}")
        if r.duplicates_skipped:
            out.append(
                f"{r.name}: {r.duplicates_skipped} pull requests came twice while paging"
                " and were counted once, so the walk shifted."
            )
    return out


def render_markdown(
    start: datetime.date,
    end: datetime.date,
    metrics: Sequence[RepoMetrics],
    billing: Sequence[str],
    reviews: Sequence[ReviewMetrics] = (),
) -> str:
    rows = table_rows(metrics)
    out = [f"# Backlog report, {start} to {end}", ""]
    out.append("| " + " | ".join(HEADERS) + " |")
    out.append("|" + "|".join(" --- " for _ in HEADERS) + "|")
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    out.append("")
    out.extend(f"- {label_line(m)}" for m in metrics)
    out.append("")
    out.extend(f"- {line}" for line in [*billing, *notes(metrics), *review_notes(reviews)])
    if reviews:
        for title, headers, body in (
            ("Review return on cost, per bot", REVIEW_BOT_HEADERS, review_bot_rows(reviews)),
            (
                "Review return on cost, per repository",
                REVIEW_REPO_HEADERS,
                review_repo_rows(reviews),
            ),
        ):
            out.extend(["", f"## {title}", "", "| " + " | ".join(headers) + " |"])
            out.append("|" + "|".join(" --- " for _ in headers) + "|")
            out.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(out) + "\n"


def render_html(
    start: datetime.date,
    end: datetime.date,
    metrics: Sequence[RepoMetrics],
    billing: Sequence[str],
    reviews: Sequence[ReviewMetrics] = (),
) -> str:
    esc = html.escape
    out = [
        "<!doctype html>",
        '<html><head><meta charset="utf-8"><title>Backlog report</title></head><body>',
        f"<h1>Backlog report, {start} to {end}</h1>",
        '<table border="1">',
        "<tr>" + "".join(f"<th>{esc(h)}</th>" for h in HEADERS) + "</tr>",
    ]
    out.extend(
        "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>"
        for row in table_rows(metrics)
    )
    out.append("</table>")
    out.append("<ul>")
    out.extend(f"<li>{esc(label_line(m))}</li>" for m in metrics)
    out.extend(
        f"<li>{esc(line)}</li>" for line in [*billing, *notes(metrics), *review_notes(reviews)]
    )
    out.append("</ul>")
    if reviews:
        for title, headers, body in (
            ("Review return on cost, per bot", REVIEW_BOT_HEADERS, review_bot_rows(reviews)),
            (
                "Review return on cost, per repository",
                REVIEW_REPO_HEADERS,
                review_repo_rows(reviews),
            ),
        ):
            out.extend([f"<h2>{esc(title)}</h2>", '<table border="1">'])
            out.append("<tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>")
            out.extend(
                "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in body
            )
            out.append("</table>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"


def collect(owner: str, name: str, since: datetime.date) -> RepoMetrics:
    repo = f"{owner}/{name}"
    date = since.isoformat()

    def issues(state: str, search: str | None, fields: str) -> Any:
        args = ["issue", "list", "--repo", repo, "--state", state, "--limit", str(LIST_LIMIT)]
        if search:
            args += ["--search", search]
        return run_gh([*args, "--json", fields])

    opened = issues("all", f"created:>={date}", "number,labels")
    closed = issues("closed", f"closed:>={date}", "number,labels")
    open_now = issues("open", None, "number,labels")
    merged = run_gh(
        ["pr", "list", "--repo", repo, "--state", "merged", "--search", f"merged:>={date}"]
        + ["--limit", str(LIST_LIMIT), "--json", "number"]
    )
    runs = run_gh(
        ["run", "list", "--repo", repo, "--created", f">={date}", "--limit", str(LIST_LIMIT)]
        + ["--json", "name"]
    )
    return compute_metrics(name, opened, closed, open_now, merged, runs)


REVIEW_QUERY = """
query($search: String!, $owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) { defaultBranchRef { name } }
  search(query: $search, type: ISSUE, first: @PAGE@, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number
      headRefName
      baseRefName
      commits(first: @COMMIT@) {
        pageInfo { hasNextPage }
        nodes { commit { oid committedDate statusCheckRollup { state } } }
      }
      reviews(first: @REVIEW@) {
        pageInfo { hasNextPage }
        nodes { author { login } submittedAt body commit { oid } }
      }
      reviewThreads(first: @THREAD@) {
        pageInfo { hasNextPage }
        nodes { comments(first: 1) { nodes { author { login } originalCommit { oid } } } }
      }
      comments(first: @COMMENT@) {
        pageInfo { hasNextPage }
        nodes { body createdAt authorAssociation isMinimized author { login __typename } }
      }
      timelineItems(first: @EVENT@, itemTypes: [REVIEW_REQUESTED_EVENT]) {
        pageInfo { hasNextPage }
        nodes { ... on ReviewRequestedEvent {
          createdAt
          actor { login __typename }
          requestedReviewer { ... on Bot { login } ... on User { login } }
        } }
      }
    } }
  }
}
"""
for _token, _cap in (
    ("PAGE", PAGE_SIZE),
    ("COMMIT", COMMIT_CAP),
    ("REVIEW", REVIEW_CAP),
    ("THREAD", THREAD_CAP),
    ("COMMENT", COMMENT_CAP),
    ("EVENT", EVENT_CAP),
):
    REVIEW_QUERY = REVIEW_QUERY.replace(f"@{_token}@", str(_cap))


def graphql(query: str, variables: dict[str, object]) -> dict[str, Any]:
    """Run one GraphQL document through the shared reader, so a null node or error raises.

    The one caller reads through `walked`, which names a `GraphQLError` as a `GhError`.
    """
    return dict(request(query, variables).data)


def walked(fetch: Callable[[str | None], dict]) -> Iterator[dict]:
    """Each page of the search, a walk's own `GraphQLError` raised as a `GhError`.

    The walk raises for a page claiming another without a moving cursor, as `request` raises for
    a failed query. Both then reach `main` as the one error type it names on stderr.
    """
    try:
        yield from walk(fetch, ["search"])
    except GraphQLError as error:
        raise GhError(f"{error} {error.detail}".strip()) from error


def policy_branch(owner: str, name: str, default: str) -> tuple[str, str]:
    """The branch whose policy a feature PR sees: `develop` where it exists, else the default.

    A probe that failed names no branch, since which one a feature PR sees is then unknown.
    """
    try:
        run_gh(["api", f"repos/{owner}/{name}/branches/{INTEGRATION_BRANCH}"])
    except GhError as error:
        if said_absent(str(error)):
            return default, ""
        return "", str(error)
    return INTEGRATION_BRANCH, ""


def fetch_policy(owner: str, name: str, ref: str) -> tuple[dict[str, str], str]:
    """Each bot's trigger in the policy file at `ref`, read the way the review gate reads it.

    The gate's own `fetch` does the read, so a file it would refuse reads as an error here too. A
    file it would take as absent reads as the hub default, each trigger marked `(default)`.
    """
    absent: list[str] = []

    def read(owner: str, repo: str, path: str, ref: str) -> str | None:
        quoted = urllib.parse.quote(ref, safe="")
        try:
            payload = run_gh(["api", f"repos/{owner}/{repo}/contents/{path}?ref={quoted}"])
        except GhError as error:
            if said_absent(str(error)):
                absent.append(path)
                return None
            raise
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str) or not content:
            raise GhError(f"{owner}/{repo} {path} at {ref} carried no content")
        return content

    try:
        policy = fetch(owner, name, ref, read)
    except GhError as error:
        return {}, str(error)
    except PolicyError as error:
        return {}, f"unreadable policy file: {error}"
    mark = " (default)" if absent else ""
    return {bot: policy.bot(bot).trigger + mark for bot in BOTS}, ""


def fetch_policy_changes(
    owner: str, name: str, branch: str, since: datetime.date
) -> tuple[list[str], str]:
    """The dates the policy file changed on `branch` inside the window."""
    query = urllib.parse.urlencode(
        {
            "path": POLICY_PATH,
            "sha": branch,
            "since": f"{since.isoformat()}T00:00:00Z",
            "per_page": HISTORY_CAP + 1,
        }
    )
    try:
        commits = run_gh(["api", f"repos/{owner}/{name}/commits?{query}"])
    except GhError as error:
        return [], str(error)
    if not isinstance(commits, list):
        return [], "the policy history was not a list"
    dates = [
        str(((c.get("commit") or {}).get("committer") or {}).get("date") or "unknown")[:10]
        for c in commits
    ]
    if len(dates) > HISTORY_CAP:
        dates = [*dates[:HISTORY_CAP], f"more than {HISTORY_CAP} commits"]
    return dates, ""


def collect_reviews(owner: str, name: str, since: datetime.date) -> ReviewMetrics:
    """Read the window's merged pull requests, newest first, up to `PR_CAP`."""
    search = f"repo:{owner}/{name} is:pr is:merged merged:>={since.isoformat()} sort:created-desc"

    def fetch(after: str | None) -> dict:
        variables: dict[str, object] = {"search": search, "owner": owner, "name": name}
        if after is not None:
            variables["after"] = after
        return graphql(REVIEW_QUERY, variables)

    result = ReviewMetrics(name=name)
    for bot in BOT_LOGINS:
        result.bots[bot] = BotStats()
    seen = 0
    numbers: set[object] = set()
    branch = ""
    for page in walked(fetch):
        branch = str(
            ((page.get("repository") or {}).get("defaultBranchRef") or {}).get("name") or ""
        )
        connection = page["search"]
        for pr in connection.get("nodes") or []:
            if pr.get("number") in numbers:
                result.duplicates_skipped += 1
            elif seen < PR_CAP:
                numbers.add(pr.get("number"))
                seen += 1
                review_pr(pr, result, branch)
        if seen >= PR_CAP:
            total = int(connection.get("issueCount") or 0)
            if total > PR_CAP:
                result.truncated.append(f"{total} merged PRs, {PR_CAP} read")
            break
    if not branch:
        result.policy_read, result.policy_error = "default branch", "the repository has none"
        return result
    result.policy_branch, branch_error = policy_branch(owner, name, branch)
    if branch_error:
        result.policy_read, result.policy_error = f"{INTEGRATION_BRANCH} branch", branch_error
        return result
    result.policy, result.policy_error = fetch_policy(owner, name, result.policy_branch)
    result.policy_changes, result.history_error = fetch_policy_changes(
        owner, name, result.policy_branch, since
    )
    return result


def fetch_billing(owner: str, today: datetime.date) -> tuple[Mapping[str, Any] | None, str]:
    path = f"/users/{owner}/settings/billing/usage?year={today.year}&month={today.month:02d}"
    try:
        return run_gh(["api", path]), ""
    except GhError as error:
        return None, str(error)


def registry_defaults() -> tuple[str, list[str]]:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    names = [r["name"] for r in data["repos"] if r.get("status") == "cataloged"]
    return str(data["owner"]), names


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backlog_metrics.py",
        description="Print a weekly backlog and review-cost report to stdout.",
    )
    parser.add_argument("--owner", help="repository owner, default from registry/repos.json")
    parser.add_argument(
        "--repo",
        action="append",
        metavar="NAME",
        help="repository name under the owner, repeatable, default every cataloged repository",
    )
    parser.add_argument("--days", type=positive_int, default=7, help="window length in days")
    parser.add_argument("--format", choices=("markdown", "html"), default="markdown")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    default_owner, default_names = registry_defaults()
    owner = args.owner or default_owner
    names = args.repo or default_names
    end = datetime.datetime.now(datetime.UTC).date()
    start = window_start(end, args.days)
    try:
        metrics = [collect(owner, name, start) for name in names]
    except GhError as error:
        print(f"backlog_metrics.py: {error}", file=sys.stderr)
        return 1
    reviews: list[ReviewMetrics] = []
    review_error = ""
    try:
        reviews = [collect_reviews(owner, name, start) for name in names]
    except GhError as error:
        review_error = str(error)
        print(f"backlog_metrics.py: reading the review figures failed: {error}", file=sys.stderr)
    usage, error_text = fetch_billing(owner, end)
    billing = billing_lines(f"{end.year}-{end.month:02d}", names, usage, error_text)
    if review_error:
        billing.append(f"Review figures were unavailable: {review_error}")
    render = render_html if args.format == "html" else render_markdown
    sys.stdout.write(render(start, end, metrics, billing, reviews))
    failed = [
        f"backlog_metrics.py: {r.name}: reading the {what} failed"
        for r in reviews
        for what, error in ((r.policy_read, r.policy_error), ("policy history", r.history_error))
        if error
    ]
    for line in failed:
        print(line, file=sys.stderr)
    return 1 if failed or review_error else 0


if __name__ == "__main__":
    sys.exit(main())
