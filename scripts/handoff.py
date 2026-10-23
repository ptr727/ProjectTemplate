#!/usr/bin/env python3
"""The session handoff chain - one standing issue per track, its body the current handoff.

Why this exists: a handoff written to a file fails in ways that an issue does not. The file is
not found where the next session looks. More than one candidate is found and nothing says which is
current. And there is no history, so a later round re-runs a path an earlier round already tried
and already wrote down. The value of a handoff is the record of what has been attempted, and one
overwritten file on one machine is the shape that cannot hold it. See the `session-handoff` Skill
for what belongs in a handoff, and GOVERNANCE.md "Hub-Hosted Tooling" for why this is reached from
a hub checkout rather than carried.

A track holds one issue, created when the track starts and closed when its work is complete. Each
session close replaces the body in place and bumps the round, after first appending the outgoing
body to the same issue as a comment, so the track's history stays on one issue in order. An issue
filed per session costs a create, a comment, and a close every round, and those links crowd out
the issues that describe work.

The issue is machine-readable from one HTML comment on the body's last line, so a retitled or
hand-edited issue still reads:

  <!-- handoff: v1 track=<slug> round=<n> previous=<issue number or none> -->

`previous=` names the closed issue the track continues from, an issue filed per session before
the track's issue became a standing one, or a closed-out lane the track reopens, and `none`
otherwise. It stays fixed while the round moves. Each archived round carries its own line:

  <!-- handoff-round: v1 track=<slug> round=<n> closed=<YYYY-MM-DD> -->

The title is for humans and nothing parses it. The invariant a reader checks is that exactly one
open issue carries the `handoff` label for a given track in a given repository, read from the
metadata block rather than from the label, since an issue carrying the label and no block belongs
to no readable track. Zero open is two states rather than one: a track that has never had a
handoff, and a lane closed out whose newest issue is closed. `new` continues from that closed
issue where one exists. Two or more open on one track is a defect to report, never one to resolve
by picking, so every command that would have to choose refuses and names both.

A head resolved that way can still be one the track has moved past, where another issue already
names it as predecessor, holds a higher round, or holds the same round. `new` and `close` refuse
onto such a head, and `current`, `resume`, and `chain` answer from it with a notice naming each
such issue, since answering from it silently is how a search reads "no match" over text in the
lane.

Subcommands
  current  The open handoff issue for a track, number and URL. Read-only.
  resume   That issue's body, then a one-line index of the last N earlier rounds. Read-only.
  chain    List one track's rounds newest first, the current body, then each archived round,
           then the closed issue `previous=` names, optionally only those whose text matches a
           regular expression, which is how a session answers "has this already been tried"
           without reading every round. A walk that stopped at `--limit` names the round it
           stopped short of, since a capped search reading exactly like an exhaustive one is the
           false "not tried yet" this exists to prevent. Read-only.
  new      Create the track's issue where it has none open. Otherwise append the outgoing body
           as a comment, then replace the body and bump the round. Either way it prints the
           issue number. A rerun after an interrupted run finishes it rather than repeating it.
  close    Comment the track's outcome on its issue, then close it, which is the only way a
           track ends.
  tracks   Every open handoff with its track and age, so an abandoned lane is visible rather
           than silently current. `--json` prints the same rows as one JSON array carrying
           number, title, track, labels, and updatedAt, read from the same issue connection.
           Read-only.

Exit codes
  0  the command did what it says.
  1  a refusal the caller can act on: the label is missing, other than where `tracks` answers
     an unregistered fork under the fleet's owner or `new --create-label` creates the label there,
     no handoff is open on the track, a track is ambiguous, a handoff carries no metadata block, a
     body is over the hard cap, or the command line itself was wrong. A usage error is a refusal, so
     `Parser` below moves it here off argparse's own 2.
  2  the command did not run to an answer: `gh` failed, a write did not confirm, or an exception
     nobody modeled reached the top. A refusal and a failure to reach one never share a code,
     which is the whole reason the two are separated.

Usage
  python3 scripts/handoff.py current --repo OWNER/NAME [--track T]
  python3 scripts/handoff.py resume  --repo OWNER/NAME [--track T] [--history N]
  python3 scripts/handoff.py chain   --repo OWNER/NAME [--track T] [--limit N] [--grep PATTERN]
  python3 scripts/handoff.py new     --repo OWNER/NAME [--track T] --title S --body-file PATH
                                     [--create-label]
  python3 scripts/handoff.py close   --repo OWNER/NAME [--track T] --body-file PATH
  python3 scripts/handoff.py tracks  --repo OWNER/NAME [--json]

Every subcommand takes `--repo`, with no default, for the reason `pr_review.py` requires one: an
issue number resolves in every repository, and a chain read out of the wrong one is well formed.
Add `--dry-run` to any writing subcommand to print the calls it would make and write nothing.

A repository missing the label is drift on a fleet repository, one `registry/repos.json` lists, and
every subcommand refuses there. A fork under the registry's owner that the registry does not list,
such as one kept for an upstream contribution, can still use a chain to hold state, and the fleet
label set does not belong on it. There the reads warn and answer as an empty chain does, and
`new --create-label` creates the one `handoff` label and nothing else. Every other repository
missing the label refuses, a repository under another owner and an unregistered repository of the
owner's that is not a fork among them.

The two writing subcommands, `new` and `close`, are bounded whatever the label state, an
in-process refusal of the kind `pr_review.py` makes, with the owner read from the registry. They
refuse a repository under another owner before any call, and an unregistered repository of the
owner's unless it is a fork, since these writes run as subprocesses a write guard on the caller
never sees. That write scope bounds no read.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import subprocess
import sys
import tempfile
import typing
from datetime import UTC, datetime
from pathlib import Path

import gh_graphql as graphql
from carry import normalize_eol

# The label is the chain's index.
# Finding a link by label rather than by title search is what frees the title.
# It carries a human subject, and nothing parses it.
LABEL = "handoff"

# The command that fixes a repository missing the label.
# `decision` was declared on the hub and applied nowhere else.
# A downstream session then enumerated an empty queue and reported it healthy.
# A `handoff` label declared and not applied fails the same silent way, so this refuses instead.
APPLY = "repo-config/configure.sh apply OWNER/NAME release|operational"

REGISTRY = Path(__file__).resolve().parent.parent / "registry" / "repos.json"
LABELS = Path(__file__).resolve().parent.parent / "repo-config" / "labels.json"

READS = frozenset({"current", "resume", "chain", "tracks"})

DEFAULT_TRACK = "default"

# A track is a short kebab-case slug naming a lane of work.
TRACK = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# One HTML comment, rendered invisible, carrying what a title must never be parsed for.
# Matched anywhere in the body and required to occur once, then rewritten onto the last line.
MARKER = re.compile(
    r"^<!--\s*handoff:\s*v1\s+track=(?P<track>[a-z0-9]+(?:-[a-z0-9]+)*)\s+"
    r"round=(?P<round>[1-9]\d*)\s+previous=(?P<previous>[1-9]\d*|none)\s*-->[ \t]*\r?$",
    re.MULTILINE,
)

ROUND_MARK = re.compile(
    r"^<!--\s*handoff-round:\s*v1\s+track=(?P<track>[a-z0-9]+(?:-[a-z0-9]+)*)\s+"
    r"round=(?P<round>[1-9]\d*)\s+closed=(?P<closed>\d{4}-\d{2}-\d{2})\s*-->[ \t]*\r?$",
    re.MULTILINE,
)

TITLE = "Session Handoff [{track}]: {subject}"

WRITERS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# GitHub's own issue body limit is 65536 characters.
# The hard cap sits under it, so a refusal names the handoff rather than the API.
# The warning sits far below it, to be read while the cap is still nowhere near.
# Neither number bounds a handoff, and the `session-handoff` Skill's per-section rules do.
WARN_BYTES = 12 * 1024
MAX_BYTES = 60 * 1024

# A result filling the window is reported as truncated rather than treated as whole.
WINDOW = 100

# The closed side needs its own, much larger window, for a reason about the shape of the data.
# Open handoffs are bounded by the one-per-track invariant.
# Closed ones accumulate one per round and nothing prunes them.
# A window sized for the first is therefore a wall the second reaches by working.
CLOSED_WINDOW = 1000

# GitHub's own ceiling on `first` for one page of a connection, which a larger value fails.
PAGE = 100

LIST_QUERY = """
query($owner: String!, $name: String!, $labels: [String!], $states: [IssueState!],
      $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    hasIssuesEnabled
    issues(labels: $labels, states: $states, first: $first, after: $after,
           orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body state createdAt updatedAt lastEditedAt url
        labels(first: 100) { nodes { name } }
      }
    }
  }
}
"""

# How much of `gh`'s own stderr is passed through before it is cut.
# The cut says how much it left, and the count is of characters, since that is what is measured.
STDERR_CAP = 2000

HISTORY = 5
CHAIN_LIMIT = 20


class Parser(argparse.ArgumentParser):
    """An `ArgumentParser` whose usage errors exit 1 rather than argparse's own 2.

    A mistyped flag is a refusal the caller can act on, and 2 is reserved here for the command not
    having run at all. Sharing one code between the two is what makes a caller unable to tell them
    apart, which is the whole reason this script separates them.
    """

    def error(self, message: str) -> typing.NoReturn:
        self.print_usage(sys.stderr)
        sys.stderr.write(f"refused: {self.prog}: {message}\n")
        raise SystemExit(1)


class Refusal(Exception):
    """A state the caller can act on, reported at exit 1."""


class Execution(Exception):
    """The command did not run to an answer, reported at exit 2."""


def run_gh(argv: list[str]) -> str:
    """Run one `gh` command and return its stdout, raising rather than reporting a blank.

    Every read decodes as UTF-8 rather than as the platform's locale, since `gh` emits UTF-8
    everywhere and a Windows console locale is cp1252.

    Nothing here suppresses a write's output or converts a failure into success, per GOVERNANCE.md
    "Repository Boundaries and Write Safety". A `gh` failure raises, and the caller reports it.
    """
    try:
        r = subprocess.run(
            ["gh", *argv], capture_output=True, text=True, encoding="utf-8", check=False
        )
    except OSError as exc:
        raise Execution(f"could not run gh: {exc}") from exc
    if r.returncode != 0:
        detail = r.stderr.strip()
        sys.stderr.write(detail[:STDERR_CAP])
        if len(detail) > STDERR_CAP:
            sys.stderr.write(f" ... {len(detail) - STDERR_CAP} more character(s) not shown")
        sys.stderr.write("\n")
        raise Execution(f"gh {argv[0]} {argv[1] if len(argv) > 1 else ''} failed rc={r.returncode}")
    return r.stdout


def gh(argv: list[str], *, dry_run: bool = False) -> str:
    """`run_gh`, or, under a dry run, the call printed and nothing sent.

    The dry run is the first thing anyone wants before trusting this to close an issue, so it sits
    on the one path every read and every write goes through rather than on each write separately.
    """
    if dry_run:
        print(f"  would run: gh {' '.join(argv)}")
        return ""
    return run_gh(argv)


def gh_json(argv: list[str]) -> object:
    """Run one `gh` command whose output is JSON and parse it, naming the parse failure as one."""
    out = gh(argv)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise Execution(f"gh returned output that is not JSON: {exc}") from exc


def rows_of(data: object, what: str, field: str) -> list[dict]:
    """`data` as a list of rows each carrying `field`, refusing every other shape.

    A row that is not an object, or one missing the field its reader asks `gh` for, would
    otherwise raise out of a comprehension and reach the blind catch as an unmodeled error, which
    reports exit 2 without saying what `gh` actually returned.

    The guarantee stops at that one field. A command consuming these rows reads fields of its own,
    and an absent one still lands on the blind catch, which is the honest place for a shape nobody
    named.
    """
    if not isinstance(data, list):
        raise Execution(f"the {what} did not read as an array")
    for row in data:
        if not isinstance(row, dict) or field not in row:
            raise Execution(f"the {what} holds a row carrying no {field}: {cut(repr(row))}")
    return data


def label_present(repo: str) -> bool:
    """Whether the target repository carries the label the chain is indexed by.

    A read that filled its window refuses rather than answering, since there the label's absence is
    unproven rather than established.
    """
    rows = rows_of(
        gh_json(["label", "list", "--repo", repo, "--limit", str(WINDOW), "--json", "name"]),
        f"label list for {repo}",
        "name",
    )
    names = {row["name"] for row in rows}
    if LABEL not in names and len(rows) >= WINDOW:
        raise Refusal(
            f"{repo} has at least {WINDOW} labels, which fills the read window, so `{LABEL}` "
            "could sit past it. Reporting the label as absent here would send you to re-apply a "
            "set that may already be applied."
        )
    return LABEL in names


def registry() -> tuple[str, set[str]]:
    """The registry's owner and its repository names, lowercased, as GitHub compares them.

    An unreadable registry is a failure to answer rather than an answer, since reading it as empty
    would move every fleet repository onto the warning path.
    """
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
        owner = data["owner"]
        names = {row["name"].lower() for row in data["repos"]}
        if not isinstance(owner, str):
            raise TypeError(f"owner is {owner!r}, not a string")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Execution(f"could not read the fleet registry at {REGISTRY}: {exc}") from exc
    return owner.lower(), names


def repo_flags(a: argparse.Namespace) -> dict[str, bool]:
    """Whether the repository has issues turned on and whether it is a fork, read live once a run.

    The write scope and the missing-label path both ask, so the answer rides on the parsed
    arguments rather than costing a second request.
    """
    cached = getattr(a, "repo_flags", None)
    if cached is not None:
        return cached
    data = gh_json(["repo", "view", a.repo, "--json", "hasIssuesEnabled,isFork"])
    fields = ("hasIssuesEnabled", "isFork")
    if not isinstance(data, dict) or not all(isinstance(data.get(f), bool) for f in fields):
        raise Execution(f"repo view for {a.repo} returned no {' and '.join(fields)}: {data!r}")
    a.repo_flags = {field: data[field] for field in fields}
    return a.repo_flags


def require_write_scope(a: argparse.Namespace) -> None:
    """Refuse a write outside the registry's owner, or to an unregistered repository not a fork.

    It runs before the label is read, so a label a stranger's repository happens to carry opens no
    write there. A registered repository costs no request, and the reads are never bounded here.
    """
    if a.cmd in READS:
        return
    owner, names = registry()
    repo_owner, _, name = a.repo.partition("/")
    if repo_owner.lower() != owner:
        raise Refusal(
            f"{a.repo} is not under {owner}, so this script writes nothing there. A different "
            "owner goes through the runbook's own `gh` path, where the write guard reads the "
            "maintainer's grant."
        )
    if name.lower() in names:
        return
    if not repo_flags(a)["isFork"]:
        raise Refusal(
            f"{a.repo} is neither in registry/repos.json nor a fork, so it is registry drift "
            "rather than a fork keeping state, and this script writes nothing there. Register it, "
            f"then apply the fleet label set: {APPLY}"
        )


def label_definition() -> tuple[str, str]:
    """The `handoff` label's color and description, read from the fleet label set it belongs to.

    One definition keeps a label created here identical to the one the fleet set applies, should the
    repository it lands on join the fleet later.
    """
    try:
        rows = json.loads(LABELS.read_text(encoding="utf-8"))
        row = next(row for row in rows if row["name"] == LABEL)
        color, description = row["color"], row["description"]
        if not isinstance(color, str) or not isinstance(description, str):
            raise TypeError(f"the `{LABEL}` entry is {row!r}")
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        raise Execution(f"could not read the `{LABEL}` label from {LABELS}: {exc!r}") from exc
    return color, description


def create_label(repo: str, dry_run: bool) -> None:
    """Create the one `handoff` label and confirm it by reading the label set back."""
    color, description = label_definition()
    out = gh(
        [
            "label",
            "create",
            LABEL,
            "--repo",
            repo,
            "--description",
            description,
            "--color",
            color,
        ],
        dry_run=dry_run,
    ).strip()
    if dry_run:
        return
    print(f"  label created: {out or LABEL}")
    if not label_present(repo):
        raise Execution(
            f"{repo} still carries no `{LABEL}` label after the create, so the create is not "
            "confirmed. A write that appears to have failed is verified, never assumed harmless."
        )


def without_label(a: argparse.Namespace) -> int | None:
    """What a repository missing the label gets, or None where the subcommand goes on to run.

    On a fleet repository it is drift, and every subcommand refuses, since degrading there would
    report an empty chain on a repository that has one, the failure `decision` demonstrated.
    On an unregistered fork under the registry's owner no issue can carry a label that does not
    exist, so an empty chain is the true answer there rather than a degraded one, and only
    `new --create-label` writes anything.

    The body is read once, before the label is created, and that read is the one `new` files, so a
    body `new` would refuse leaves no label behind. Everything else refuses before any write. A
    repository under another owner, or an unregistered one of the owner's that is not a fork,
    refuses here for a read and in `require_write_scope` for a write, which runs first.
    """
    repo = a.repo
    create = getattr(a, "create_label", False)
    owner, names = registry()
    repo_owner, _, name = repo.partition("/")
    missing = f"{repo} carries no `{LABEL}` label, so no handoff can be found or filed there."
    if repo_owner.lower() != owner:
        raise Refusal(
            f"{missing} It is not under {owner}, so this script neither reads a chain there nor "
            "creates the label."
        )
    if name.lower() in names:
        flag = " `--create-label` is for an unregistered fork." if create else ""
        raise Refusal(f"{missing}{flag} Apply the fleet label set from a hub checkout: {APPLY}")
    flags = repo_flags(a)
    if not flags["isFork"]:
        raise Refusal(
            f"{missing} It is neither in registry/repos.json nor a fork, so it is registry drift "
            f"rather than a fork keeping state. Register it, then apply the fleet label set: {APPLY}"
        )
    fix = f"gh label create {LABEL} --repo {repo}"
    if not flags["hasIssuesEnabled"]:
        fix = f"gh repo edit {repo} --enable-issues, then {fix}"
        if create:
            raise Refusal(
                f"{repo} has issues turned off, so no handoff can be filed there. Run "
                f"gh repo edit {repo} --enable-issues first, then rerun with --create-label."
            )
    if a.cmd in READS:
        print(
            f"warning: {repo} is a fork outside the fleet and carries no `{LABEL}` label, so it "
            f"holds no handoff chain. `new --create-label` creates that one label, or run: {fix}",
            file=sys.stderr,
        )
        if a.cmd == "tracks":
            print("[]" if a.json else f"(no open `{LABEL}` issues in {repo})")
            return 0
        raise Refusal(
            f"{repo} has no handoff on track {a.track!r}. Zero is the state before the first "
            "handoff on a track, so `new` is what follows, not a retry of this."
        )
    if a.cmd == "close":
        raise Refusal(
            f"{repo} is a fork outside the fleet and carries no `{LABEL}` label, so it holds no "
            f"handoff on track {a.track!r} to close."
        )
    if a.cmd == "new" and create:
        a.body = body_from(Path(a.body_file))
        print(f"0. create the `{LABEL}` label on {repo}, a fork outside the fleet")
        create_label(repo, a.dry_run)
        a.fresh_label = True
        return None
    raise Refusal(
        f"{repo} is a fork outside the fleet and carries no `{LABEL}` label, so no handoff can be "
        f"filed there. Rerun `new` with --create-label to create that one label, or run: {fix}. "
        "The fleet label set does not belong on a fork outside the fleet."
    )


def parse_marker(body: str, number: int) -> dict[str, str] | None:
    """The metadata block in one body, or None where it carries none.

    More than one block is a refusal rather than a pick, since the two can disagree about the track
    and nothing else in the body decides between them.
    """
    found = MARKER.findall(body or "")
    if not found:
        return None
    if len(found) > 1:
        raise Refusal(
            f"issue #{number} carries {len(found)} handoff metadata blocks, so its track and round "
            "cannot be read. Leave exactly one, as the body's last line."
        )
    m = MARKER.search(body)
    assert m is not None
    return m.groupdict()


def render_marker(track: str, round_: int, previous: int | None) -> str:
    return f"<!-- handoff: v1 track={track} round={round_} previous={previous or 'none'} -->"


def round_text(body: str) -> str:
    """A handoff's own text, without the metadata block, as two rounds are compared by."""
    return MARKER.sub("", body or "").strip()


def archive(body: str, track: str, round_: int, closed: str, title: str) -> str:
    """The comment that keeps an outgoing round on the issue once its body is replaced.

    The heading keeps the round's own title, since `new` replaces the title with the next one.
    """
    mark = f"<!-- handoff-round: v1 track={track} round={round_} closed={closed} -->"
    heading = f"## Round {round_}, closed {closed}: {title}"
    return f"{heading}\n\n{round_text(body)}\n\n{mark}\n"


def archived_text(comment_body: str) -> str:
    """The round text an `archive` comment holds, its heading and its mark taken off."""
    text = ROUND_MARK.sub("", comment_body or "").strip()
    first, _, rest = text.partition("\n")
    return rest.strip() if first.startswith("## Round ") else text


def rounds_of(comments: list[dict], track: str) -> list[dict]:
    """The archived rounds on `track` among an issue's comments, newest first.

    A comment carrying no mark is an ordinary comment, such as a parking note, and is not a
    round. One carrying several is not a round either, since its round cannot be read. On a public
    repository anyone can comment, so a mark counts only from an account with write access and
    only on a comment nobody has hidden, which keeps an outsider from writing a round into the
    track's history or blocking `new` with a conflicting one.
    """
    found = []
    for row in comments:
        if row.get("authorAssociation") not in WRITERS or row.get("isMinimized"):
            continue
        marks = list(ROUND_MARK.finditer(row.get("body") or ""))
        if len(marks) == 1 and marks[0]["track"] == track:
            heading = (row.get("body") or "").strip().partition("\n")[0]
            _, _, title = heading.partition(": ")
            found.append({**row, "mark": marks[0].groupdict(), "title": title})
    found.reverse()
    return found


def with_marker(body: str, track: str, round_: int, previous: int | None) -> str:
    """`body` with the metadata block as its last line, replacing any block already in it.

    Only newlines are stripped from the end. `MARKER`'s tail is an ordered suffix rather than a
    set of characters, so removing any character that suffix may itself hold can turn a line the
    grammar rejects into one it accepts: `-->\\r ` is not a block and `-->\\r` is, so taking off
    one space revives it, and the revived line then sits beside the one this appends and leaves a
    body no command can read. A newline is the one thing the tail can never hold, since `$` sits
    before it, so stripping newlines alone can revive nothing.
    """
    stripped = MARKER.sub("", body or "").rstrip("\n")
    return f"{stripped}\n\n{render_marker(track, round_, previous)}\n"


def issue(repo: str, number: int, *, comments: bool = False) -> dict:
    """One issue read live, which is where every identifier a later write consumes comes from.

    `comments` adds every comment, oldest first, which is where the archived rounds sit.
    """
    wanted = "number,title,body,state,createdAt,closedAt,updatedAt,url,labels"
    data = gh_json(
        [
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            f"{wanted},comments" if comments else wanted,
        ]
    )
    if not isinstance(data, dict):
        raise Execution(f"issue #{number} in {repo} did not read as an object")
    if comments:
        rows_of(data.get("comments"), f"comment list of #{number} in {repo}", "body")
    return data


def labeled(repo: str, state: str, limit: int, what: str) -> list[dict]:
    """Up to `limit` issues in `state` carrying the label, newest first, read per `LIST_QUERY`.

    This reads the repository's own issue connection rather than `gh issue list --label`. That
    command answers from the search index, which is updated after a write lands. So a read straight
    after `new` returned the link it had just closed as still open.

    Paging stops at `limit` or at the last page, whichever comes first. A caller comparing the
    count against its own window therefore still sees a full window as full.

    A repository with issues turned off answers this connection empty rather than with an error.
    That is refused here, as `gh issue list` refused it, rather than read as a chain never started.
    """
    owner, _, name = repo.partition("/")
    rows: list[dict] = []

    def fetch(after: str | None) -> dict:
        variables: dict[str, object] = {
            "owner": owner,
            "name": name,
            "labels": [LABEL],
            "states": [state],
            "first": min(PAGE, limit - len(rows)),
        }
        if after is not None:
            variables["after"] = after
        return graphql.request(LIST_QUERY, variables, run=gh_runner).data

    try:
        for repository in graphql.walk(fetch, ("repository", "issues"), strict_page=True):
            if "hasIssuesEnabled" not in repository:
                raise graphql.GraphQLError("the repository carries no hasIssuesEnabled")
            if repository.get("hasIssuesEnabled") is not True:
                raise Refusal(
                    f"{repo} has issues turned off, so it holds no readable {what}. Run "
                    f"gh repo edit {repo} --enable-issues to read the chain there."
                )
            rows += rows_of(repository["issues"].get("nodes"), what, "number")
            if len(rows) >= limit:
                break
    except graphql.GraphQLError as exc:
        if exc.detail:
            sys.stderr.write(f"{exc.detail}\n")
        raise Execution(f"the {what} did not read as an issue connection: {exc}") from exc
    return rows


def gh_runner(argv: list[str]) -> tuple[int, str, str]:
    """`run_gh` in the shape the shared GraphQL transport runs, so every read keeps one path.

    `run_gh` raises `Execution` on a failed command, which is exit 2 here already.
    """
    return 0, run_gh(argv[1:]), ""


def open_handoffs(repo: str) -> list[dict]:
    """Every open issue carrying the label, newest first, with its metadata block parsed.

    A window that fills is reported as truncated rather than treated as whole. An open handoff past
    the window is exactly as invisible as one that does not exist.
    """
    rows = labeled(repo, "OPEN", WINDOW, f"handoff list for {repo}")
    if len(rows) >= WINDOW:
        raise Refusal(
            f"{repo} has at least {WINDOW} open `{LABEL}` issues, which fills the read window, so "
            "no count here is whole. The chain is meant to hold one open issue per track."
        )
    for row in rows:
        try:
            row["marker"] = parse_marker(row.get("body") or "", row["number"])
        except Refusal as exc:
            # `tracks` is the survey, and it is how an operator sees this state at all.
            # The refusal rides on the row and is raised by the callers that must not guess past it.
            row["marker"] = None
            row["malformed"] = str(exc)
    rows.sort(key=lambda row: row["number"], reverse=True)
    return rows


def require_marked(rows: list[dict]) -> None:
    """Refuse while any open handoff carries no metadata block.

    Such an issue belongs to an unknown track, so it could be the one the caller asked about, and
    picking around it would answer a question the data does not settle. Settling it is a hand edit,
    adding the block to that issue's body or taking the label off it, and `tracks` surveys without
    refusing so an operator can see the state at all.
    """
    broken = [row["malformed"] for row in rows if row.get("malformed")]
    if broken:
        raise Refusal(cut(" ".join(broken)))
    bare = [row for row in rows if row["marker"] is None]
    if bare:
        listed = ", ".join(f"#{row['number']}" for row in bare)
        raise Refusal(
            f"{listed} carries the `{LABEL}` label and no metadata block, so its track is unknown "
            "and no track can be read as unambiguous. Add the block to its body by hand, or "
            "take the label off it."
        )


def on_track(rows: list[dict], track: str) -> dict | None:
    """The one open handoff for a track, or None, refusing where there is more than one."""
    matches = [row for row in rows if row["marker"] and row["marker"]["track"] == track]
    if len(matches) > 1:
        listed = ", ".join(f"#{row['number']} ({row['url']})" for row in matches)
        raise Refusal(
            f"track {track!r} has {len(matches)} open handoffs: {listed}. Exactly one is the "
            "invariant, and which of these is current is not something this can decide."
        )
    return matches[0] if matches else None


def current_or_refuse(repo: str, track: str) -> tuple[dict, list[str]]:
    """The open handoff on `track`, with each reason `head_doubts` gives that it is not the head."""
    rows = open_handoffs(repo)
    require_marked(rows)
    found = on_track(rows, track)
    if found is None:
        raise Refusal(
            f"{repo} has no open handoff on track {track!r}. Zero is the state before the first "
            "handoff on a track, so `new` is what follows, not a retry of this."
        )
    return found, head_doubts(found, track_links(repo, track, rows))


def walk(
    repo: str, start: int, limit: int, cap: str, *, skip_body: bool = False
) -> tuple[list[dict], str | None]:
    """The track's rounds from issue `start` backwards, at most `limit` of them.

    Each issue gives its body, which is the round it holds now, then its archived rounds newest
    first, and the walk then moves to the issue its block names as `previous=`. `skip_body` leaves
    out the start issue's own body, for a caller that has printed it already.

    Returns the rounds and, where the walk ended anywhere but at a `previous=none`, a phrase saying
    where and why. A caller that printed only the rounds could not tell a chain of four from the
    first four of forty, and a search reporting no match over a walk that stopped early is the
    false "not tried yet" this whole mechanism exists to prevent.

    `cap` is the flag the caller bounded the walk with, `--limit` for `chain` and `--history` for
    `resume`, since a message naming the other one sends a reader to a flag that subcommand
    rejects.

    Five things end a walk short. The cap, a link whose body carries no metadata block, which
    names nothing before it and is not guessed past, a link whose block cannot be read at all, an
    ancestor `gh` cannot resolve, and a number already seen, since a cycle would otherwise read as
    an endless chain. Each returns its own phrase rather than the clean end's None, because all
    five leave links unread and only the first is a number the caller chose.
    """
    links: list[dict] = []
    seen: set[int] = set()
    number = start
    # `number` starts as an int and is only reassigned from a `\d+` capture.
    # Every exit from this loop is therefore one of the returns inside it.
    while True:
        if number in seen:
            return links, f"a cycle back to #{number}, so the chain does not end where it should"
        if len(links) >= limit:
            return links, f"the {cap} cap of {limit}, with #{number} and earlier unread"
        seen.add(number)
        try:
            data = issue(repo, number, comments=True)
        except Execution as exc:
            return links, f"#{number}, which could not be read: {exc}"
        if LABEL not in {row["name"] for row in data.get("labels") or []}:
            return links, (
                f"#{number}, which carries no `{LABEL}` label, so the reads that index this "
                "chain cannot see it"
            )
        try:
            data["marker"] = parse_marker(data.get("body") or "", data["number"])
        except Refusal as exc:
            data["marker"] = None
            links.append(data)
            return links, f"#{number}, whose block cannot be read: {exc}"
        if not (skip_body and number == start):
            links.append(data)
        marker = data["marker"]
        if marker is None:
            return links, (
                f"#{number}, whose body carries no metadata block, so nothing names what "
                "precedes it"
            )
        for row in rounds_of(data["comments"], marker["track"]):
            if len(links) >= limit:
                return links, (
                    f"the {cap} cap of {limit}, with round {row['mark']['round']} of #{number} "
                    "and earlier unread"
                )
            links.append(
                {
                    "number": data["number"],
                    "title": row["title"],
                    "state": "ROUND",
                    "createdAt": row["mark"]["closed"],
                    "url": row.get("url"),
                    "body": row["body"],
                    "marker": {**marker, "round": row["mark"]["round"]},
                }
            )
        if marker["previous"] == "none":
            return links, None
        number = int(marker["previous"])


def age_days(stamp: str) -> int:
    """Whole days between an ISO-8601 GitHub timestamp and now, both in UTC."""
    when = datetime.fromisoformat(stamp)
    return (datetime.now(UTC) - when).days


def day(stamp: str | None) -> str:
    return (stamp or "")[:10] or "-"


def describe(link: dict) -> str:
    """One walked round as a line, carrying its own track.

    The track is printed rather than assumed from the track the walk started on, because `walk`
    follows each marker's `previous=` and nothing stops a hand-edited issue naming a
    predecessor on another lane. Printing it is what makes that visible to the reader. An
    archived round reads `ROUND` in the state column and its close date in the date column.
    """
    marker = link.get("marker")
    round_ = f"{marker['track']} round {marker['round']}" if marker else "unmarked"
    state = link.get("state", "OPEN")
    return (
        f"#{link['number']:<6} {state:<6} {day(link.get('createdAt'))}  "
        f"{round_:<24} {link['title']}"
    )


def body_from(path: Path) -> str:
    """The body a `new` will file, refused where it is over the hard cap or already carries a block.

    A body carrying its own block would leave two after the append, which `parse_marker` then reads
    as the refusal it is, one round later and on a live issue rather than here.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Refusal(f"could not read the body file {path}: {exc}") from exc
    size = len(text.encode("utf-8"))
    if size > MAX_BYTES:
        raise Refusal(
            f"the body is {size} bytes, over the {MAX_BYTES}-byte cap. The cap is a backstop, and "
            "what bounds a handoff is the per-section rules in the `session-handoff` Skill."
        )
    if size > WARN_BYTES:
        print(
            f"warning: the body is {size} bytes, over the {WARN_BYTES}-byte guidance. The two "
            "sections most often padded are what not to repeat and what was learned, which are "
            "also the two the chain exists for, so trim by the per-section rule rather than by "
            "cutting them.",
            file=sys.stderr,
        )
    if MARKER.search(text) or ROUND_MARK.search(text):
        raise Refusal(
            "the body file already carries a handoff metadata block or round mark. This writes its "
            "own, and two make the track unreadable. Remove it and let the chain write it."
        )
    return text


@contextlib.contextmanager
def body_arg(body: str, dry_run: bool):
    """The `--body-file` argument for one write, as a real temporary file or as a placeholder.

    A dry run writes no file, because the path it would print names a temporary directory the
    preview has already removed, and a reader checking a previewed call should find every part of
    it legible rather than one part pointing at nothing.
    """
    if dry_run:
        yield "<body-file>"
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "handoff.md"
        path.write_text(body, encoding="utf-8", newline="\n")
        yield str(path)


def create(repo: str, title: str, body: str, dry_run: bool) -> int | None:
    """File one issue and return the number read back from the URL `gh` prints.

    The number is read from the write's own confirmation rather than constructed, which is the one
    identifier here that no earlier read could have supplied.
    """
    with body_arg(body, dry_run) as path:
        out = gh(
            [
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body-file",
                path,
                "--label",
                LABEL,
            ],
            dry_run=dry_run,
        )
    if dry_run:
        return None
    print(f"  created: {out.strip()}")
    m = re.search(r"/issues/(\d+)\s*$", out.strip())
    if not m:
        raise Execution(
            f"issue create returned no issue URL, so nothing confirms what it filed: {out.strip()!r}"
        )
    return int(m.group(1))


def comment(repo: str, number: int, text: str, dry_run: bool) -> None:
    """Post one comment and confirm it by the URL the write returns.

    The text goes through a file, since an archived round can exceed a Windows command line.
    """
    with body_arg(text, dry_run) as path:
        out = gh(
            ["issue", "comment", str(number), "--repo", repo, "--body-file", path],
            dry_run=dry_run,
        ).strip()
    if dry_run:
        return
    if "/issues/" not in out:
        raise Execution(
            f"the comment on #{number} returned no URL, so nothing confirms it: {out!r}"
        )
    print(f"  commented: {out}")


def close(repo: str, number: int, dry_run: bool) -> None:
    """Close one issue and confirm it by reading its state back."""
    out = gh(["issue", "close", str(number), "--repo", repo], dry_run=dry_run).strip()
    if dry_run:
        return
    print(f"  closed: {out or f'#{number}'}")
    state = issue(repo, number).get("state")
    if state != "CLOSED":
        raise Execution(
            f"#{number} still reads {state} after the close, so the close is not confirmed. "
            "A write that appears to have failed is verified, never assumed harmless."
        )


def same_text(left: str, right: str) -> bool:
    """Whether two bodies read alike once line endings and surrounding blank space are set aside."""
    return normalize_eol(left).strip() == normalize_eol(right).strip()


def edit_issue(repo: str, number: int, title: str, body: str, dry_run: bool) -> None:
    """Replace one issue's title and body, and confirm the body by reading it back."""
    with body_arg(body, dry_run) as path:
        out = gh(
            ["issue", "edit", str(number), "--repo", repo, "--title", title, "--body-file", path],
            dry_run=dry_run,
        ).strip()
    if dry_run:
        return
    print(f"  body updated: {out or f'#{number}'}")
    if not same_text(issue(repo, number).get("body") or "", body):
        raise Execution(
            f"#{number}'s body does not read back as the one just written, so the edit is not "
            "confirmed. A write that appears to have failed is verified, never assumed harmless."
        )


def cmd_current(a: argparse.Namespace) -> int:
    found, doubts = current_or_refuse(a.repo, a.track)
    marker = found["marker"]
    print(f"#{found['number']} {found['url']}")
    print(f"track={marker['track']} round={marker['round']} previous={marker['previous']}")
    print(found["title"])
    if doubts:
        print(doubt_notice(found, a.track, doubts))
    return 0


def cmd_resume(a: argparse.Namespace) -> int:
    found, doubts = current_or_refuse(a.repo, a.track)
    print(f"# {found['title']}")
    print(f"# {found['url']}")
    print()
    print((found.get("body") or "").rstrip())
    print()
    print(f"## Earlier rounds, last {a.history}")
    links, stopped = walk(a.repo, found["number"], a.history, "--history", skip_body=True)
    if not links and stopped is None:
        print("(none - this is the first round on this track)")
    for link in links:
        print(describe(link))
    if stopped is not None:
        print(f"(index stopped at {stopped})")
    if doubts:
        print(doubt_notice(found, a.track, doubts))
    return 0


def cmd_chain(a: argparse.Namespace) -> int:
    try:
        pattern = re.compile(a.grep, re.IGNORECASE) if a.grep else None
    except re.error as exc:
        raise Refusal(f"--grep is not a valid regular expression: {exc}") from exc
    rows = open_handoffs(a.repo)
    require_marked(rows)
    head = on_track(rows, a.track)
    closed = None if head else read_closed(a.repo)
    head = head or newest_closed(a.repo, a.track, closed)
    if head is None:
        raise Refusal(
            f"{a.repo} has no handoff on track {a.track!r}, open or closed, so there is no chain "
            "to walk."
        )
    lane = track_links(a.repo, a.track, rows, closed)
    doubts = head_doubts(head, lane)
    links, stopped = walk(a.repo, head["number"], a.limit, "--limit")
    shown = 0
    for link in links:
        if pattern is not None and not pattern.search(link.get("body") or ""):
            continue
        shown += 1
        print(describe(link))
    if pattern is not None:
        print(f"{shown} of {len(links)} rounds walked match /{a.grep}/")
    strayed = {
        link["number"]: link["marker"]["track"]
        for link in links
        if link["marker"] and link["marker"]["track"] != a.track
    }
    if strayed:
        listed = ", ".join(f"#{number} on {track}" for number, track in strayed.items())
        print(f"(the walk left track {a.track!r}: {listed})")
    if stopped is not None:
        print(
            f"(the walk stopped at {stopped}, so it read {len(links)} round(s) rather than the "
            "chain, and a search over it is not a search over the chain)"
        )
    if doubts:
        print(doubt_notice(head, a.track, doubts))
    walked = {int(link["number"]) for link in links}
    missed = [row for row in lane.links if int(row["number"]) not in walked]
    if stopped is None and missed:
        listed = ", ".join(f"#{row['number']}" for row in missed)
        print(
            cut(
                f"(the walk did not reach {listed}, on track {a.track!r} and outside the chain "
                "walked, so a search over it is not a search over the track)"
            )
        )
    return 0


def cut(text: str) -> str:
    """`text` bounded the way `gh`'s own stderr is, since a refusal nobody reads does not land."""
    if len(text) <= STDERR_CAP:
        return text
    return f"{text[:STDERR_CAP]} ... {len(text) - STDERR_CAP} more character(s) not shown."


def read_closed(repo: str) -> list[dict]:
    """The closed `handoff` issues, up to `CLOSED_WINDOW`, with their bodies."""
    return labeled(repo, "CLOSED", CLOSED_WINDOW, f"closed handoff list for {repo}")


def newest_closed(repo: str, track: str, closed: list[dict] | None = None) -> dict | None:
    """The newest closed handoff on a track, or None where the track has never had one.

    A track with no open handoff is either one that has never had a handoff or one whose lane was
    closed out, and the two are not the same. Reading the second as the first makes the next `new`
    start a second chain at round 1 with `previous=none`, orphaning every link already written,
    which is the failure the chain exists to prevent.

    The head is the highest round rather than the highest issue number, because a chain is ordered
    by round everywhere else and an issue number only agrees with that where the links were filed
    in order. They disagree wherever a lower-numbered issue joined the lane later, and picking by
    number there hands `new` a predecessor that is not the head, so it files a second link at a
    round already taken and the real chain goes unreachable while every step exits 0. The number
    breaks a tie between two links claiming one round, which is itself a defect the reader should
    not have to resolve.

    A caller that already read the closed list passes it as `closed`, and it is used as given
    rather than read again.
    """
    rows = closed if closed is not None else read_closed(repo)
    malformed: list[str] = []
    bare: list[str] = []
    found: list[dict] = []
    for row in rows:
        try:
            marker = parse_marker(row.get("body") or "", row["number"])
        except Refusal as exc:
            # Its own message already says what settles it, so no remedy is appended for these.
            malformed.append(str(exc))
            continue
        if marker is None:
            # A block that is absent and one that cannot be read are the same hazard here.
            # Either way the link's track and round are unknown.
            # So either could be this track's real head.
            bare.append(
                f"#{row['number']} carries the `{LABEL}` label and no metadata block, so its "
                "track cannot be read."
            )
            continue
        if marker["track"] == track:
            row["marker"] = marker
            found.append(row)
    # An unreadable link's round compares with nothing, so it could outrank every readable one.
    # That makes "this is the head" unprovable rather than merely uncertain.
    if malformed or bare:
        raise Refusal(
            cut(" ".join([*malformed, *bare]))
            + f" Any of those could be the newest closed link on track {track!r}, so which link "
            "is its head cannot be read, and chaining onto the wrong one forks the chain."
            + (
                " Add the block to each body that carries none by hand, or take the label off it."
                if bare
                else ""
            )
        )
    if found:
        return max(found, key=lambda row: (int(row["marker"]["round"]), row["number"]))
    # Only a full window leaves "no link on this track" unproven, and only then is it refused.
    # Refusing on a full window regardless would wall off a brand-new track for good.
    # A repository reaches that many closed handoffs by working rather than by going wrong.
    if len(rows) >= CLOSED_WINDOW:
        raise Refusal(
            f"{repo} has at least {CLOSED_WINDOW} closed `{LABEL}` issues, which fills the read "
            f"window, and none on track {track!r} was in it, so a link could sit past it unseen. "
            "Reading a track that has one as a track that has none files a second chain beside "
            "the first."
        )
    return None


def cmd_tracks(a: argparse.Namespace) -> int:
    rows = open_handoffs(a.repo)
    if a.json:
        print(
            json.dumps(
                [
                    {
                        "number": row["number"],
                        "title": row["title"],
                        "track": row["marker"]["track"] if row["marker"] else None,
                        "labels": [node["name"] for node in row["labels"]["nodes"]],
                        "updatedAt": row["updatedAt"],
                    }
                    for row in rows
                ],
                indent=2,
            )
        )
        return 0
    if not rows:
        print(f"(no open `{LABEL}` issues in {a.repo})")
        return 0
    for row in rows:
        marker = row["marker"]
        track = (
            marker["track"] if marker else ("(malformed)" if row.get("malformed") else "(unmarked)")
        )
        updated = age_days(row["updatedAt"])
        print(f"{track:<20} #{row['number']:<6} updated {updated}d ago  {row['title']}")
    return 0


def cmd_new(a: argparse.Namespace) -> int:
    """Create the track's issue, or archive its outgoing round and replace the body in place.

    The archive comment is written before the body is replaced, so a failure between the two
    leaves the outgoing round both archived and still in the body rather than in neither. A rerun
    then finds that round archived and only replaces the body, and a rerun after a run that wrote
    everything finds the body already current and writes nothing.

    The read of the head and the writes that follow it are not serialized, so two sessions closing
    on one track at once can both archive the same round. The body is re-read immediately before
    it is replaced, and a change since the archive refuses rather than overwriting text nobody here
    has seen. Naming a track per lane is what keeps two sessions off one issue in the first place.

    After `--create-label` the chain is empty by construction, since a label this run just created
    sits on no issue yet, so no chain read is made, which also keeps a dry run from querying a label
    it never created.
    """
    body = getattr(a, "body", None)
    if body is None:
        body = body_from(Path(a.body_file))
    title = TITLE.format(track=a.track, subject=a.title)
    closed = None
    if getattr(a, "fresh_label", False):
        rows, head = [], None
    else:
        rows = open_handoffs(a.repo)
        require_marked(rows)
        head = on_track(rows, a.track)
        closed = None if head else read_closed(a.repo)
    previous = head or (None if closed is None else newest_closed(a.repo, a.track, closed))
    # A head can already have a successor, whatever key resolved it.
    # A lane somebody edited by hand does reach it, on either side.
    # Filing onto such a head forks the lane there and loses the branch it skipped.
    if previous is not None:
        doubts = head_doubts(previous, track_links(a.repo, a.track, rows, closed))
        if doubts:
            raise Refusal(
                cut(
                    f"#{previous['number']} may not be the head of track {a.track!r}, so writing "
                    f"the next round onto it could fork the chain there. {' '.join(doubts)}"
                )
            )
    if head is None:
        round_ = int(previous["marker"]["round"]) + 1 if previous else 1
        since = int(previous["number"]) if previous else None
        print(f"1. create the handoff issue for track {a.track!r}, round {round_}")
        number = create(a.repo, title, with_marker(body, a.track, round_, since), a.dry_run)
        print(f"#{number}" if number is not None else "(dry run, so no issue number yet)")
        return 0
    return replace_round(a, head, title, body)


def replace_round(a: argparse.Namespace, head: dict, title: str, body: str) -> int:
    """Archive the head's outgoing round as a comment, then write the next round over its body.

    Every identifier written to is the head's own number, read in this run, and the outgoing body
    archived is the one read immediately before, never one carried from earlier in the run.
    """
    number = int(head["number"])
    fresh = issue(a.repo, number, comments=True)
    outgoing = fresh.get("body") or ""
    marker = parse_marker(outgoing, number)
    if marker is None or marker["track"] != a.track:
        raise Refusal(
            f"#{number}'s body carries no block on track {a.track!r} when read again, so its round "
            "cannot be read. Restore the block by hand before writing the next round."
        )
    round_ = int(marker["round"])
    if same_text(round_text(outgoing), body):
        print(f"#{number} already holds this body as round {round_}, so nothing is written")
        print(f"#{number}")
        return 0
    previous = None if marker["previous"] == "none" else int(marker["previous"])
    kept = [
        row for row in rounds_of(fresh["comments"], a.track) if row["mark"]["round"] == str(round_)
    ]
    if kept and any(
        not same_text(archived_text(row["body"]), round_text(outgoing)) for row in kept
    ):
        raise Refusal(
            f"#{number} already archives a round {round_} whose text differs from its body, at "
            f"{kept[0].get('url')}. Two texts for one round is not something this can decide "
            "between, so settle which one stands by hand."
        )
    if kept:
        print(f"1. round {round_} is already archived at {kept[0].get('url')}, so not again")
    else:
        stamp = head.get("lastEditedAt") or head.get("createdAt") or fresh.get("createdAt")
        print(f"1. archive round {round_} as a comment on #{number}")
        title_then = fresh.get("title") or ""
        comment(
            a.repo, number, archive(outgoing, a.track, round_, day(stamp), title_then), a.dry_run
        )
    print(f"2. write round {round_ + 1} over the body of #{number}")
    if not a.dry_run and not same_text(issue(a.repo, number).get("body") or "", outgoing):
        raise Refusal(
            f"#{number}'s body changed since this run archived it, and the next round replaces "
            "the whole body. Re-run to work from the current text."
        )
    edit_issue(a.repo, number, title, with_marker(body, a.track, round_ + 1, previous), a.dry_run)
    print(f"#{number}")
    return 0


def outcome_from(path: Path) -> str:
    """The closing comment `close` posts, refused where it is empty or over the hard cap."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise Refusal(f"could not read the outcome file {path}: {exc}") from exc
    if not text:
        raise Refusal("the outcome file is empty, and a track closes with its outcome stated.")
    if ROUND_MARK.search(text):
        raise Refusal(
            "the outcome carries a handoff round mark, which would read as an archived round of "
            "the track. Remove it."
        )
    size = len(text.encode("utf-8"))
    if size > MAX_BYTES:
        raise Refusal(f"the outcome is {size} bytes, over the {MAX_BYTES}-byte cap.")
    return text


def cmd_close(a: argparse.Namespace) -> int:
    """Comment the track's outcome on its issue, then close it, which is how a track ends.

    A rerun after the comment landed and the close did not finds the same text as the newest
    comment and goes straight to the close rather than posting it twice.
    """
    text = outcome_from(Path(a.body_file))
    found, doubts = current_or_refuse(a.repo, a.track)
    if doubts:
        raise Refusal(
            cut(
                f"#{found['number']} may not be the head of track {a.track!r}, so closing it "
                f"could leave the track's real head open. {' '.join(doubts)}"
            )
        )
    number = int(found["number"])
    comments = issue(a.repo, number, comments=True)["comments"]
    if comments and same_text(comments[-1].get("body") or "", text):
        print(f"1. #{number}'s newest comment is this outcome already, so not again")
    else:
        print(f"1. comment the outcome on #{number}")
        comment(a.repo, number, text, a.dry_run)
    print(f"2. close #{number}")
    close(a.repo, number, a.dry_run)
    print(f"#{number}")
    return 0


class Lane(typing.NamedTuple):
    """Every readable link on one track, and what kept that read from being whole."""

    links: list[dict]
    unreadable: list[str]
    full: bool


def track_links(repo: str, track: str, rows: list[dict], closed: list[dict] | None = None) -> Lane:
    """The links on `track`, the open ones taken from `rows` and the closed ones from `closed`.

    A row whose block is absent or cannot be read could be on this track, so it is named in
    `unreadable` rather than skipped, and a closed read that fills its window sets `full`, since a
    link past it is unseen. Each caller decides whether those refuse or are reported. A caller
    that already read the closed list passes it as `closed`, and one that passes none has it read here.
    """
    if closed is None:
        closed = read_closed(repo)
    links: list[dict] = []
    unreadable: list[str] = []
    for row in [*rows, *closed]:
        marker = row.get("marker")
        if marker is None:
            try:
                marker = parse_marker(row.get("body") or "", row["number"])
            except Refusal as exc:
                unreadable.append(str(exc))
                continue
        if marker is None:
            unreadable.append(
                f"#{row['number']} carries the `{LABEL}` label and no metadata block, so its "
                "track cannot be read. Add one to its body by hand, or take the label off it."
            )
            continue
        if marker["track"] == track:
            row["marker"] = marker
            links.append(row)
    return Lane(links, unreadable, len(closed) >= CLOSED_WINDOW)


def head_doubts(head: dict, lane: Lane) -> list[str]:
    """Each reason `head` may not be its track's head, one sentence apiece, empty where none.

    `on_track` takes an open link whatever round it holds, and `newest_closed` breaks a tie on one
    round by issue number, so either can resolve a link the chain has already moved past. Three
    states show it: a link already naming it as predecessor, a link at a higher round, and another
    link at its own round. Answering from such a head is how a search reads "no match" over text
    in the lane, so the reads print these, and `new` and `close` refuse on them.
    """
    number = int(head["number"])
    round_ = int(head["marker"]["round"])
    doubts: list[str] = []
    for row in lane.links:
        if int(row["number"]) == number:
            continue
        other = int(row["marker"]["round"])
        if row["marker"]["previous"] == str(number):
            doubts.append(f"#{row['number']} already succeeds #{number}.")
        elif other > round_:
            doubts.append(f"#{row['number']} holds round {other}, above #{number}'s {round_}.")
        elif other == round_:
            doubts.append(f"#{row['number']} also holds round {round_}.")
    if lane.full:
        doubts.append(
            f"The closed `{LABEL}` issues fill the read window of {CLOSED_WINDOW}, so a link past "
            "it went unchecked."
        )
    doubts.extend(lane.unreadable)
    return doubts


def doubt_notice(head: dict, track: str, doubts: list[str]) -> str:
    """The line a read prints over a head `head_doubts` questioned, bounded like a refusal."""
    return cut(
        f"(#{head['number']} may not be the head of track {track!r}, so this read may miss the "
        f"chain's newest links. {' '.join(doubts)})"
    )


def add_repo(parser: argparse.ArgumentParser) -> None:
    # No default, for the reason `pr_review.py` states about a pull request number.
    # An issue number resolves in every repository.
    # A chain read out of the wrong one is well formed.
    parser.add_argument(
        "--repo",
        required=True,
        metavar="OWNER/NAME",
        help="the repository the chain lives in, since an issue number identifies no repository",
    )


def add_track(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--track",
        default=DEFAULT_TRACK,
        metavar="SLUG",
        help=f"the lane of work this handoff belongs to, kebab-case (default {DEFAULT_TRACK!r})",
    )


def add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the calls this would make and write nothing",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = Parser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("current", help="the open handoff issue for a track")
    add_repo(p)
    add_track(p)

    p = sub.add_parser("resume", help="the current handoff body, then the recent chain")
    add_repo(p)
    add_track(p)
    p.add_argument(
        "--history",
        type=int,
        default=HISTORY,
        metavar="N",
        help=f"earlier rounds to index after the body (default {HISTORY})",
    )

    p = sub.add_parser("chain", help="walk the chain backwards, optionally searching bodies")
    add_repo(p)
    add_track(p)
    p.add_argument(
        "--limit",
        type=int,
        default=CHAIN_LIMIT,
        metavar="N",
        help=f"links to walk (default {CHAIN_LIMIT})",
    )
    p.add_argument(
        "--grep",
        metavar="PATTERN",
        help="show only links whose body matches this regular expression, case-insensitively",
    )

    p = sub.add_parser(
        "new", help="create the track's issue, or archive its round and write the next one"
    )
    add_repo(p)
    add_track(p)
    add_dry_run(p)
    p.add_argument("--title", required=True, metavar="SUBJECT", help="the human half of the title")
    p.add_argument(
        "--body-file", required=True, metavar="PATH", help="the handoff body, per the skill"
    )
    p.add_argument(
        "--create-label",
        action="store_true",
        help="on an unregistered fork with no handoff label, create that one label first",
    )

    p = sub.add_parser("close", help="comment the track's outcome, then close its issue")
    add_repo(p)
    add_track(p)
    add_dry_run(p)
    p.add_argument(
        "--body-file", required=True, metavar="PATH", help="the closing comment, the outcome"
    )

    p = sub.add_parser("tracks", help="every open handoff with its track and age")
    add_repo(p)
    p.add_argument(
        "--json",
        action="store_true",
        help="print number, title, track, labels, and updatedAt per handoff as a JSON array",
    )

    return ap


HANDLERS = {
    "current": cmd_current,
    "resume": cmd_resume,
    "chain": cmd_chain,
    "new": cmd_new,
    "close": cmd_close,
    "tracks": cmd_tracks,
}


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    # A bare name is the near-miss a required argument still admits.
    # Unpacking it raises a ValueError traceback rather than saying which half is missing.
    owner, _, name = a.repo.partition("/")
    if not owner or not name or "/" in name:
        ap.error(f"--repo takes OWNER/NAME, not {a.repo!r}")
    if getattr(a, "track", None) is not None and not TRACK.fullmatch(a.track):
        ap.error(f"--track takes a kebab-case slug, not {a.track!r}")
    for flag in ("history", "limit"):
        if getattr(a, flag, None) is not None and getattr(a, flag) < 1:
            ap.error(f"--{flag} counts links, so it cannot be below 1")
    try:
        require_write_scope(a)
        if not label_present(a.repo):
            answered = without_label(a)
            if answered is not None:
                return answered
        return HANDLERS[a.cmd](a)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except Execution as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 2
    # The blind catch is the point rather than an oversight.
    # An unmodeled exception would otherwise reach CPython's own exit 1, which reads as a refusal.
    except Exception as exc:  # noqa: BLE001
        print(f"failed: unmodeled {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
