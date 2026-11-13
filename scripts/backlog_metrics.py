#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["markdown-it-py==4.3.0", "mdurl==0.1.2"]
# ///
"""Render a weekly backlog and review-cost report for the fleet's repositories.

Why this exists: a weekly count of issues filed against closed, and of issues per merged pull
request, shows the backlog's direction. Copilot review runs per merged pull request share that
window. The month's Actions minutes and Copilot gross cover the calendar month to date instead.
So the report prints them beside the window's counts rather than dividing them by its merges.

The report goes to stdout only, so a server timer can mail it. Nothing here runs from a workflow.
Counting and rendering are pure functions over parsed `gh` output. Only `run_gh` and `graphql`
start a process. `--format markdown` links each repository and each issue or pull request count to
its GitHub page. `--format json` carries the same figures, with each counted repository, issue,
pull request, and workflow run beside its `html_url`, for a consumer rendering its own report.
`scripts/README.md` documents the JSON shape.

Usage
  uv run --no-project --script scripts/backlog_metrics.py [--owner OWNER] [--repo NAME ...]
                                                           [--days N] [--format markdown|json]

It is a uv script because it imports the review gate's own readers, which parse Markdown. A copy
of those readers here would drift from the gate and report figures the gate would not.

markdown-it-py is pinned exactly, and so is mdurl, the one package it imports. So every run
imports only versions this repository reviewed, a run holding a `gh` token included. Run it as
above, with `--script`, so uv reads those pins. A bump is then a reviewed change. It moves every
script's pin and `tests/requirements.txt`'s with it, and pins exactly each package the new version
requires. A test holds all of that.

On SIGINT or SIGTERM it stops the `gh` call in flight, prints one line naming the signal, and ends
by that signal.

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
import contextlib
import datetime
import json
import os
import signal
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

GITHUB = "https://github.com"
DEFAULT_MARK = " (default)"
JSON_SCHEMA = 1
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
    url: str = ""
    items: Mapping[str, Items] = field(default_factory=dict)


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


def ratio(count: int, merged: int) -> float | None:
    """`count / merged` to one decimal, or None without a merge."""
    return None if merged == 0 else round(count / merged, 1)


def ratio_text(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def per_merged(count: int, merged: int) -> str:
    return ratio_text(ratio(count, merged))


def pr_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "number": item.get("number"),
        "title": str(item.get("title") or ""),
        "html_url": str(item.get("url") or ""),
    }


def issue_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {**pr_item(item), "labels": sorted(label_names(item))}


def run_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("databaseId"),
        "name": str(item.get("name") or ""),
        "html_url": str(item.get("url") or ""),
    }


def compute_metrics(
    name: str,
    opened_items: Items,
    closed_items: Items,
    open_items: Items,
    merged_prs: Items,
    runs: Items,
    url: str = "",
) -> RepoMetrics:
    """Reduce one repository's parsed lists to the report's counts and the items behind each."""
    lists = (opened_items, closed_items, open_items, merged_prs, runs)

    def issues(items: Items, handoff: bool) -> list[dict[str, Any]]:
        return [issue_item(i) for i in items if (HANDOFF_LABEL in label_names(i)) == handoff]

    open_issues = [item for item in open_items if HANDOFF_LABEL not in label_names(item)]
    return RepoMetrics(
        name=name,
        opened=count_without_label(opened_items, HANDOFF_LABEL),
        closed=count_without_label(closed_items, HANDOFF_LABEL),
        handoffs_created=count_with_label(opened_items, HANDOFF_LABEL),
        open_now=len(open_issues),
        open_by_label=count_by_label(open_issues, LABELS),
        merged=len(merged_prs),
        runs=len(runs),
        review_runs=count_named(runs, REVIEW_WORKFLOW),
        truncated=any(len(items) >= LIST_LIMIT for items in lists),
        url=url,
        items={
            "issues_opened": issues(opened_items, False),
            "issues_closed": issues(closed_items, False),
            "handoffs_created": issues(opened_items, True),
            "issues_open": issues(open_items, False),
            "prs_merged": [pr_item(pr) for pr in merged_prs],
            "workflow_runs": [run_item(run) for run in runs],
            "copilot_review_runs": [
                run_item(run) for run in runs if run.get("name") == REVIEW_WORKFLOW
            ],
        },
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
    url: str = ""


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
    """A row per repository and a totals row, each the `counts` the JSON output carries."""

    def cells(figures: Mapping[str, Any]) -> list[str]:
        return [
            ratio_text(value) if key.endswith("_per_merged_pr") else str(value)
            for key, value in figures.items()
        ]

    return [[m.name, *cells(counts([m]))] for m in metrics] + [["Total", *cells(counts(metrics))]]


def link(text: str, url: str) -> str:
    """`text` as a Markdown link to `url`, or plain where no URL is known."""
    return f"[{text}]({url})" if url else text


SEARCHES = {
    "Issues opened (count)": ("issues", f"is:issue created:{{window}} -label:{HANDOFF_LABEL}"),
    "Issues closed (count)": (
        "issues",
        f"is:issue is:closed closed:{{window}} -label:{HANDOFF_LABEL}",
    ),
    "Handoffs created (count)": ("issues", f"is:issue created:{{window}} label:{HANDOFF_LABEL}"),
    "Issues open now (count)": ("issues", f"is:issue is:open -label:{HANDOFF_LABEL}"),
    "PRs merged (count)": ("pulls", "is:pr is:merged merged:{window}"),
}


def markdown_rows(
    metrics: Sequence[RepoMetrics], start: datetime.date, end: datetime.date
) -> list[list[str]]:
    """The backlog table's rows, each repository and each count `SEARCHES` names linked.

    `SEARCHES` maps a column's header to the page and search listing what it counts. The workflow
    run counts come from `gh run list` rather than from a search, so they print plain. Each search
    ends at the report's own date, so a report read a week later still lists what it counted.
    """
    rows = table_rows(metrics)
    for m, row in zip(metrics, rows):
        row[0] = link(m.name, m.url)
        for header, (page, query) in SEARCHES.items():
            search = urllib.parse.quote(query.format(window=f"{start}..{end}"))
            column = HEADERS.index(header)
            row[column] = link(row[column], f"{m.url}/{page}?q={search}" if m.url else "")
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


def billing_data(
    month: str, repos: Sequence[str], usage: Mapping[str, Any] | None, error: str
) -> dict[str, Any]:
    """The billing figures `billing_lines` prints, as data, each null where billing failed."""
    if usage is None:
        return {"month": month, "error": error, "actions_minutes": None, "copilot_gross_usd": None}
    minutes = sum_actions_minutes(usage)
    return {
        "month": month,
        "error": None,
        "actions_minutes": {repo: round(minutes.get(repo, 0.0), 1) for repo in repos},
        "copilot_gross_usd": round(sum_copilot_gross(usage), 2),
    }


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
                " so the window may mix two review policies."
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
    rows = markdown_rows(metrics, start, end)
    out = [f"# Backlog report, {start} to {end}", ""]
    out.append("| " + " | ".join(HEADERS) + " |")
    out.append("|" + "|".join(" --- " for _ in HEADERS) + "|")
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    out.append("")
    out.extend(f"- {label_line(m)}" for m in metrics)
    out.append("")
    out.extend(f"- {line}" for line in [*billing, *notes(metrics), *review_notes(reviews)])
    if reviews:
        urls = {r.name: r.url for r in reviews}
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
            out.extend(
                "| " + " | ".join([link(row[0], urls.get(row[0], "")), *row[1:]]) + " |"
                for row in body
            )
    return "\n".join(out) + "\n"


def counts(m: Sequence[RepoMetrics]) -> dict[str, Any]:
    """The backlog table's figures for the repositories in `m`, summed, as JSON fields."""
    opened, merged = sum(r.opened for r in m), sum(r.merged for r in m)
    review_runs = sum(r.review_runs for r in m)
    return {
        "issues_opened": opened,
        "issues_closed": sum(r.closed for r in m),
        "handoffs_created": sum(r.handoffs_created for r in m),
        "issues_open": sum(r.open_now for r in m),
        "prs_merged": merged,
        "issues_opened_per_merged_pr": ratio(opened, merged),
        "workflow_runs": sum(r.runs for r in m),
        "copilot_review_runs": review_runs,
        "copilot_review_runs_per_merged_pr": ratio(review_runs, merged),
    }


def trigger(shown: str | None) -> str | None:
    """A policy trigger as the policy states it, without the mark the report shows a default by."""
    return None if shown is None else shown.removesuffix(DEFAULT_MARK)


def review_data(r: ReviewMetrics) -> dict[str, Any]:
    """One repository's review figures, the two review tables' cells as JSON fields."""
    automatic = set(AUTOMATIC_ROUND_BOTS)
    return {
        "merged_prs_read": r.prs,
        "promotion_prs_excluded": r.promotions_skipped,
        "commits_per_merged_pr": ratio(r.commits, r.prs),
        "local_findings": r.local_findings,
        "local_attestations_unknown": r.local_unknown,
        "failing_heads": r.failing_heads,
        "failing_heads_after_clean_local_review": r.failing_after_clean_local,
        "policy_branch": r.policy_branch or None,
        "policy_error": r.policy_error or None,
        "policy_history_error": r.history_error or None,
        "policy_changes": list(r.policy_changes),
        "truncated": list(r.truncated),
        "bots": {
            bot: {
                "policy_trigger": None if r.policy_error else trigger(r.policy.get(bot)),
                "policy_from_default": None
                if r.policy_error or bot not in r.policy
                else r.policy[bot].endswith(DEFAULT_MARK),
                "requests_by_automation": stats.requests_by_automation,
                "requests_by_user": stats.requests_by_user,
                "automatic_rounds": stats.automatic_rounds if bot in automatic else None,
                "explicit_rounds": stats.explicit_rounds if bot in automatic else None,
                "unclassified_rounds": stats.unclassified_rounds if bot in automatic else None,
                "rounds_per_reviewed_pr_mean": ratio(sum(stats.rounds), len(stats.rounds)),
                "rounds_per_reviewed_pr_max": max(stats.rounds, default=None),
                "threaded_findings": stats.findings,
                "findings_missed_by_local_review": stats.missed_by_local,
                "findings_on_green_heads": stats.findings_on_green_heads,
            }
            for bot, stats in r.bots.items()
        },
    }


def render_json(
    start: datetime.date,
    end: datetime.date,
    metrics: Sequence[RepoMetrics],
    billing: Sequence[str],
    reviews: Sequence[ReviewMetrics],
    billing_figures: Mapping[str, Any],
) -> str:
    """The report as one JSON document, the headline totals at its top level.

    `notes` holds the billing lines and the notes the Markdown report lists after its label lines.
    A consumer can then print what this run noted without re-deriving it. `scripts/README.md`
    documents each field.
    """
    by_name = {r.name: r for r in reviews}
    document = {
        "schema": JSON_SCHEMA,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "totals": counts(metrics),
        "billing": dict(billing_figures),
        "notes": [*billing, *notes(metrics), *review_notes(reviews)],
        "repositories": [
            {
                "name": m.name,
                "html_url": m.url,
                **counts([m]),
                "open_issues_by_label": dict(m.open_by_label),
                "truncated": m.truncated,
                "items": {key: list(value) for key, value in m.items.items()},
                "review": review_data(by_name[m.name]) if m.name in by_name else None,
            }
            for m in metrics
        ],
    }
    return json.dumps(document, indent=2) + "\n"


def collect(owner: str, name: str, since: datetime.date) -> RepoMetrics:
    repo = f"{owner}/{name}"
    date = since.isoformat()

    def issues(state: str, search: str | None, fields: str) -> Any:
        args = ["issue", "list", "--repo", repo, "--state", state, "--limit", str(LIST_LIMIT)]
        if search:
            args += ["--search", search]
        return run_gh([*args, "--json", fields])

    fields = "number,title,url,labels"
    opened = issues("all", f"created:>={date}", fields)
    closed = issues("closed", f"closed:>={date}", fields)
    open_now = issues("open", None, fields)
    merged = run_gh(
        ["pr", "list", "--repo", repo, "--state", "merged", "--search", f"merged:>={date}"]
        + ["--limit", str(LIST_LIMIT), "--json", "number,title,url"]
    )
    runs = run_gh(
        ["run", "list", "--repo", repo, "--created", f">={date}", "--limit", str(LIST_LIMIT)]
        + ["--json", "databaseId,name,url"]
    )
    return compute_metrics(name, opened, closed, open_now, merged, runs, f"{GITHUB}/{repo}")


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
    file it would take as absent, or one stating no review section, reads as the hub default, each
    trigger marked `(default)`.
    """

    def read(owner: str, repo: str, path: str, ref: str) -> str | None:
        quoted = urllib.parse.quote(ref, safe="")
        try:
            payload = run_gh(["api", f"repos/{owner}/{repo}/contents/{path}?ref={quoted}"])
        except GhError as error:
            if said_absent(str(error)):
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
    mark = DEFAULT_MARK if policy.from_default else ""
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

    result = ReviewMetrics(name=name, url=f"{GITHUB}/{owner}/{name}")
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
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
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
    month = f"{end.year}-{end.month:02d}"
    billing = billing_lines(month, names, usage, error_text)
    if review_error:
        billing.append(f"Review figures were unavailable: {review_error}")
    if args.format == "json":
        figures = billing_data(month, names, usage, error_text)
        sys.stdout.write(render_json(start, end, metrics, billing, reviews, figures))
    else:
        sys.stdout.write(render_markdown(start, end, metrics, billing, reviews))
    failed = [
        f"backlog_metrics.py: {r.name}: reading the {what} failed"
        for r in reviews
        for what, error in ((r.policy_read, r.policy_error), ("policy history", r.history_error))
        if error
    ]
    for line in failed:
        print(line, file=sys.stderr)
    return 1 if failed or review_error else 0


received: list[int] = []


def on_signal(signum: int, _frame: object) -> None:
    """Record the signal in `received` and raise KeyboardInterrupt, so both unwind one path.

    A later signal is passed over, since it would cut the unwinding short. One is the SIGTERM that
    `uv run` forwards to a process group the terminal's SIGINT already reached.
    """
    if received:
        return
    received.append(signum)
    raise KeyboardInterrupt


def run() -> int | signal.Signals:
    """Run main, returning its status, or the signal that interrupted it once the unwind ran.

    The unwind stops the `gh` call in flight, since `subprocess.run` kills its child on any
    exception. A SIGINT inherited as ignored, as a background child's is, stays ignored.
    """
    signal.signal(signal.SIGTERM, on_signal)
    if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
        signal.signal(signal.SIGINT, on_signal)
    try:
        return main()
    except KeyboardInterrupt:
        signum = signal.Signals(received[0] if received else signal.SIGINT)
        print(f"backlog_metrics.py: interrupted by {signum.name}.", file=sys.stderr)
        return signum


def reraise(signum: signal.Signals) -> None:
    """End this process by `signum`, so a caller sees the signal rather than an exit status."""
    with contextlib.suppress(OSError):
        sys.stdout.flush()
        sys.stderr.flush()
    if sys.platform == "win32":
        sys.exit(130)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    sys.exit(128 + signum)


if __name__ == "__main__":
    status = run()
    if isinstance(status, signal.Signals):
        reraise(status)
    sys.exit(status)
