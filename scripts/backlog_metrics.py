#!/usr/bin/env python3
"""Render a weekly backlog and review-cost report for the fleet's repositories.

Why this exists: a weekly count of issues filed against closed, and of issues per merged pull
request, shows the backlog's direction. Copilot review runs per merged pull request share that
window. The month's Actions minutes and Copilot gross cover the calendar month to date instead.
So the report prints them beside the window's counts rather than dividing them by its merges.

The report goes to stdout only, so a server timer can mail it. Nothing here runs from a workflow.
Counting and rendering are pure functions over parsed `gh` output, and `run_gh` is the one place
that starts a process.

Usage
  python3 scripts/backlog_metrics.py [--owner OWNER] [--repo NAME ...] [--days N]
                                     [--format markdown|html]

Exit codes
  0  the report was written.
  1  a `gh` call for issues, pull requests, runs, or the review pass failed, so no number is
     guessed. A failed policy-file read still writes the report, with `error` in place of the
     policy, and then exits 1.
  2  the command line was wrong.
"""

import argparse
import base64
import binascii
import datetime
import html
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gh_graphql import GraphQLError, run_subprocess

REGISTRY = Path(__file__).resolve().parent.parent / "registry" / "repos.json"

LIST_LIMIT = 1000
HANDOFF_LABEL = "handoff"
REVIEW_WORKFLOW = "Running Copilot Code Review"
REVIEWERS_DIR = Path(__file__).resolve().parent / "reviewers"
PR_CAP = 50
COMMIT_CAP = 50
THREAD_CAP = 100
COMMENT_CAP = 100
REVIEW_CAP = 100
EVENT_CAP = 100
SUITE_CAP = 10
POLICY_PATH = ".github/pr-review.json"
ATTESTATION = re.compile(
    r"^<!-- fleet-local-review: head=([0-9a-f]{40}) base=(\S+) findings=(\d+|unknown) -->$",
    re.MULTILINE,
)
FAILED_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "STARTUP_FAILURE"})
GREEN_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
LABELS = ("pre-existing", "prose", "documentation", "bug", "script", "enhancement", "decision")

Items = Sequence[Mapping[str, Any]]


def reviewer_logins() -> dict[str, str]:
    """Each bot's short name mapped to its login, read from the adapter files that own it.

    `pr_review_rounds.py` holds the same mapping but imports a Markdown parser at load, which
    this report has no use for, so the JSON is read directly.
    """
    names = ("copilot", "coderabbit", "qodo")
    return {
        name: str(json.loads((REVIEWERS_DIR / f"{name}.json").read_text(encoding="utf-8"))["login"])
        for name in names
    }


BOT_LOGINS = reviewer_logins()
EXPLICIT_COMMENT = {
    "coderabbit": re.compile(
        rf"^\s*@{re.escape(BOT_LOGINS['coderabbit'])}\s+review\b", re.IGNORECASE
    ),
    "qodo": re.compile(r"^\s*/review\b"),
}


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
    findings: int = 0
    missed_by_local: int = 0
    findings_on_green_heads: int = 0


@dataclass
class ReviewMetrics:
    name: str
    prs: int = 0
    pushes: int = 0
    local_findings: int = 0
    local_unknown: int = 0
    failing_heads: int = 0
    failing_after_clean_local: int = 0
    bots: dict[str, BotStats] = field(default_factory=dict)
    policy: dict[str, str] = field(default_factory=dict)
    policy_error: str = ""
    truncated: list[str] = field(default_factory=list)


def bare_login(actor: Mapping[str, Any] | None) -> str:
    """A login without the `[bot]` suffix or `app/` prefix some surfaces add."""
    login = str((actor or {}).get("login") or "")
    return login.removeprefix("app/").removesuffix("[bot]")


def is_automation(actor: Mapping[str, Any] | None) -> bool:
    return (actor or {}).get("__typename") == "Bot"


def nodes_of(connection: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    return list((connection or {}).get("nodes") or [])


def cut(connection: Mapping[str, Any] | None) -> bool:
    return bool(((connection or {}).get("pageInfo") or {}).get("hasNextPage"))


def head_state(commit: Mapping[str, Any]) -> str:
    """Classify a head's check suites as `failed`, `green`, or `other` (pending or absent)."""
    conclusions = [str(s.get("conclusion") or "") for s in nodes_of(commit.get("checkSuites"))]
    if any(c in FAILED_CONCLUSIONS for c in conclusions):
        return "failed"
    if conclusions and all(c in GREEN_CONCLUSIONS for c in conclusions):
        return "green"
    return "other"


def attestations(comments: Items) -> dict[str, str]:
    """Head sha to the findings text of its newest attestation, `unknown` kept as its own value."""
    found: dict[str, str] = {}
    for comment in comments:
        for match in ATTESTATION.finditer(str(comment.get("body") or "")):
            found[match.group(1)] = match.group(3)
    return found


def review_pr(pr: Mapping[str, Any], result: ReviewMetrics) -> None:
    """Fold one merged pull request's review payload into the repository's tallies."""
    number = pr.get("number")
    for key in ("commits", "reviews", "reviewThreads", "comments", "timelineItems"):
        if cut(pr.get(key)):
            result.truncated.append(f"PR {number} {key}")
    commits = [n.get("commit") or {} for n in nodes_of(pr.get("commits"))]
    states = {str(c.get("oid")): head_state(c) for c in commits}
    comments = nodes_of(pr.get("comments"))
    attested = attestations(comments)
    result.prs += 1
    result.pushes += len(commits)
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
        reviewed = {
            str((r.get("commit") or {}).get("oid"))
            for r in nodes_of(pr.get("reviews"))
            if bare_login(r.get("author")) == login
        }
        if reviewed:
            stats.rounds.append(len(reviewed))
        for event in nodes_of(pr.get("timelineItems")):
            requested = bare_login(event.get("requestedReviewer"))
            if requested == login:
                if is_automation(event.get("actor")):
                    stats.requests_by_automation += 1
                else:
                    stats.requests_by_user += 1
        pattern = EXPLICIT_COMMENT.get(name)
        if pattern:
            stats.requests_by_user += sum(
                1
                for c in comments
                if not is_automation(c.get("author")) and pattern.search(str(c.get("body") or ""))
            )
        for thread in nodes_of(pr.get("reviewThreads")):
            first = nodes_of(thread.get("comments"))[:1]
            if not first or bare_login(first[0].get("author")) != login:
                continue
            head = str((first[0].get("originalCommit") or {}).get("oid"))
            stats.findings += 1
            if attested.get(head) == "0":
                stats.missed_by_local += 1
            if states.get(head) == "green":
                stats.findings_on_green_heads += 1


def policy_triggers(text: str) -> dict[str, str]:
    """Each bot's `trigger` from the policy file, `unspecified` where the file omits the bot.

    A bot's entry may sit at the top level or under a `reviewers` object.
    The file's schema is owned by the change that adds it.
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError("the policy file is not a JSON object")
    nested = data.get("reviewers")
    scope: dict[str, Any] = nested if isinstance(nested, dict) else data
    result: dict[str, str] = {}
    for name in BOT_LOGINS:
        entry = scope.get(name)
        trigger = entry.get("trigger") if isinstance(entry, dict) else None
        result[name] = str(trigger) if trigger is not None else "unspecified"
    return result


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
    "Requests by automation (count)",
    "Requests by user (count)",
    "Rounds per reviewed PR (mean)",
    "Rounds per reviewed PR (max)",
    "Findings (count)",
    "Findings missed by local review (count)",
    "Findings on all-green heads (count)",
)
REVIEW_REPO_HEADERS = (
    "Repository",
    "Merged PRs read (count)",
    "Pushes per merged PR (mean)",
    "Local findings (sum)",
    "Local attestations with unknown findings (count)",
    "Failing heads (count)",
    "Failing heads after clean local review (count)",
)


def mean(values: Sequence[int]) -> str:
    return f"{sum(values) / len(values):.1f}" if values else "n/a"


def review_bot_rows(reviews: Sequence[ReviewMetrics]) -> list[list[str]]:
    return [
        [
            r.name,
            bot,
            "error" if r.policy_error else r.policy.get(bot, "none"),
            str(stats.requests_by_automation),
            str(stats.requests_by_user),
            mean(stats.rounds),
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
            per_merged(r.pushes, r.prs),
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
                f"{r.name}: review figures are truncated, since a cap was reached"
                f" (at most {PR_CAP} merged PRs per repository): {', '.join(r.truncated)}."
            )
        if r.policy_error:
            out.append(f"{r.name}: reading {POLICY_PATH} failed: {r.policy_error}")
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
query($search: String!) {
  search(query: $search, type: ISSUE, first: @PR@) {
    issueCount
    nodes { ... on PullRequest {
      number
      commits(first: @COMMIT@) {
        pageInfo { hasNextPage }
        nodes { commit { oid checkSuites(first: @SUITE@) { nodes { conclusion } } } }
      }
      reviews(first: @REVIEW@) {
        pageInfo { hasNextPage }
        nodes { author { login } commit { oid } }
      }
      reviewThreads(first: @THREAD@) {
        pageInfo { hasNextPage }
        nodes { comments(first: 1) { nodes { author { login } originalCommit { oid } } } }
      }
      comments(first: @COMMENT@) {
        pageInfo { hasNextPage }
        nodes { body author { login __typename } }
      }
      timelineItems(first: @EVENT@, itemTypes: [REVIEW_REQUESTED_EVENT]) {
        pageInfo { hasNextPage }
        nodes { ... on ReviewRequestedEvent {
          actor { login __typename }
          requestedReviewer { ... on Bot { login } ... on User { login } }
        } }
      }
    } }
  }
}
"""
for _token, _cap in (
    ("PR", PR_CAP),
    ("COMMIT", COMMIT_CAP),
    ("SUITE", SUITE_CAP),
    ("REVIEW", REVIEW_CAP),
    ("THREAD", THREAD_CAP),
    ("COMMENT", COMMENT_CAP),
    ("EVENT", EVENT_CAP),
):
    REVIEW_QUERY = REVIEW_QUERY.replace(f"@{_token}@", str(_cap))


def fetch_policy(owner: str, name: str) -> tuple[dict[str, str], str]:
    """Read the policy file once at the default branch head. A 404 means absent, nothing else does."""
    try:
        payload = run_gh(["api", f"repos/{owner}/{name}/contents/{POLICY_PATH}"])
    except GhError as error:
        if "HTTP 404" in str(error):
            return {bot: "none" for bot in BOT_LOGINS}, ""
        return {}, str(error)
    try:
        text = base64.b64decode(str(payload["content"])).decode("utf-8")
        return policy_triggers(text), ""
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        return {}, f"unreadable policy file: {error}"


def collect_reviews(owner: str, name: str, since: datetime.date) -> ReviewMetrics:
    """Read the window's merged pull requests, at most `PR_CAP`, and tally the review figures."""
    search = f"repo:{owner}/{name} is:pr is:merged merged:>={since.isoformat()}"
    payload = run_gh(["api", "graphql", "-f", f"query={REVIEW_QUERY}", "-f", f"search={search}"])
    if not isinstance(payload, dict) or payload.get("errors") or not payload.get("data"):
        raise GhError(f"review query for {owner}/{name} returned errors: {payload!r:.300}")
    found = payload["data"]["search"]
    result = ReviewMetrics(name=name)
    for bot in BOT_LOGINS:
        result.bots[bot] = BotStats()
    if int(found.get("issueCount") or 0) > PR_CAP:
        result.truncated.append(f"{found['issueCount']} merged PRs, {PR_CAP} read")
    for pr in found.get("nodes") or []:
        review_pr(pr, result)
    result.policy, result.policy_error = fetch_policy(owner, name)
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
        reviews = [collect_reviews(owner, name, start) for name in names]
    except GhError as error:
        print(f"backlog_metrics.py: {error}", file=sys.stderr)
        return 1
    usage, error_text = fetch_billing(owner, end)
    billing = billing_lines(f"{end.year}-{end.month:02d}", names, usage, error_text)
    render = render_html if args.format == "html" else render_markdown
    sys.stdout.write(render(start, end, metrics, billing, reviews))
    failed = [r.name for r in reviews if r.policy_error]
    if failed:
        print(
            f"backlog_metrics.py: policy file unreadable for {', '.join(failed)}", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
