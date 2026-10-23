#!/usr/bin/env python3
"""Render a weekly backlog and review-cost report for the fleet's repositories.

Why this exists: a weekly measure of issues filed against closed, issues per merged pull request,
and review cost per merged pull request shows the backlog's direction without a hand count.

The report goes to stdout only, so a server timer can mail it. Nothing here runs from a workflow.
Counting and rendering are pure functions over parsed `gh` output, and `run_gh` is the one place
that starts a process.

Usage
  python3 scripts/backlog_metrics.py [--owner OWNER] [--repo NAME ...] [--days N]
                                     [--format markdown|html]

Exit codes
  0  the report was written.
  1  a `gh` call for issues, pull requests, or runs failed, so no number is guessed.
  2  the command line was wrong.
"""

import argparse
import datetime
import html
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gh_graphql import GraphQLError, run_subprocess

REGISTRY = Path(__file__).resolve().parent.parent / "registry" / "repos.json"

LIST_LIMIT = 1000
HANDOFF_LABEL = "handoff"
REVIEW_WORKFLOW = "Running Copilot Code Review"
LABELS = ("pre-existing", "prose", "documentation", "bug", "script", "enhancement", "decision")

Items = Sequence[Mapping[str, Any]]


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
    """Count per label, so an item carrying several labels counts under each."""
    return {label: count_with_label(items, label) for label in labels}


def count_named(runs: Items, workflow: str) -> int:
    return sum(1 for run in runs if run.get("name") == workflow)


def per_merged(opened: int, merged: int) -> str:
    if merged == 0:
        return "n/a"
    return f"{opened / merged:.1f}"


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
        ]
    )
    return rows


def label_line(m: RepoMetrics) -> str:
    counts = ", ".join(f"{label} {m.open_by_label.get(label, 0)}" for label in LABELS)
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


def render_markdown(
    start: datetime.date,
    end: datetime.date,
    metrics: Sequence[RepoMetrics],
    billing: Sequence[str],
) -> str:
    rows = table_rows(metrics)
    out = [f"# Backlog report, {start} to {end}", ""]
    out.append("| " + " | ".join(HEADERS) + " |")
    out.append("|" + "|".join(" --- " for _ in HEADERS) + "|")
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    out.append("")
    out.extend(f"- {label_line(m)}" for m in metrics)
    out.append("")
    out.extend(f"- {line}" for line in [*billing, *notes(metrics)])
    return "\n".join(out) + "\n"


def render_html(
    start: datetime.date,
    end: datetime.date,
    metrics: Sequence[RepoMetrics],
    billing: Sequence[str],
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
    out.extend(f"<li>{esc(line)}</li>" for line in [*billing, *notes(metrics)])
    out.append("</ul></body></html>")
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
    usage, error_text = fetch_billing(owner, end)
    billing = billing_lines(f"{end.year}-{end.month:02d}", names, usage, error_text)
    render = render_html if args.format == "html" else render_markdown
    sys.stdout.write(render(start, end, metrics, billing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
