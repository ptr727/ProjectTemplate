#!/usr/bin/env python3
"""The session handoff chain - one open issue per track, each link naming the one before it.

Why this exists: a handoff written to a file fails three ways that a chained issue does not. The
file is not found where the next session looks. More than one candidate is found and nothing says
which is current. And there is no history, so a later round re-runs a path an earlier round already
tried and already wrote down. The value of a handoff is the record of what has been attempted, and
one overwritten file on one machine is the shape that cannot hold it. See the `session-handoff`
Skill for what belongs in a handoff, and GOVERNANCE.md "Hub-Hosted Tooling" for why this is reached
from a hub checkout rather than carried.

The chain is machine-readable from one HTML comment on the body's last line, so a retitled or
hand-edited issue still chains:

  <!-- handoff: v1 track=<slug> round=<n> previous=<issue number or none> -->

The title is for humans and nothing parses it. The invariant a reader checks is that exactly one
open issue carries the `handoff` label for a given track in a given repository, read from the
metadata block rather than from the label, since an issue carrying the label and no block belongs
to no readable track. Zero open is two states rather than one: a track that has never had a
handoff, and a lane closed out whose newest link is closed. `new` chains onto the newest link
either way. Two or more open on one track is a defect to report, never one to resolve by picking,
so every command that would have to choose refuses and names both.

Subcommands
  current  The open handoff issue for a track, number and URL. Read-only.
  resume   That issue's body, then a one-line index of the last N closed links. Read-only.
  chain    Walk one track's `previous=` backwards and list each link, optionally only those
           whose body matches a regular expression, which is how a session answers "has this
           already been tried" without reading every round. A walk that stopped at `--limit`
           names the link it stopped short of, since a capped search reading exactly like an
           exhaustive one is the false "not tried yet" this exists to prevent. Read-only.
  new      Create the next link, comment the forward link on the previous one, then close it,
           in that order and with each step's own result printed.
  link     Finish a chain that half-applied, so a failed `new` is recoverable without a second
           issue.
  tracks   Every open handoff with its track and age, so an abandoned lane is visible rather
           than silently current. Read-only.
  adopt    Put the label and the metadata block on an existing hand-written handoff.

Exit codes
  0  the command did what it says.
  1  a refusal the caller can act on: the label is missing from the repository, no handoff is
     open on the track, a track is ambiguous, an open handoff carries no metadata block, a body
     is over the hard cap, or the command line itself was wrong. A usage error is a refusal, so
     `Parser` below moves it here off argparse's own 2.
  2  the command did not run to an answer: `gh` failed, a write did not confirm, or an exception
     nobody modeled reached the top. A refusal and a failure to reach one never share a code,
     which is the whole reason the two are separated.

Usage
  python3 scripts/handoff.py current --repo OWNER/NAME [--track T]
  python3 scripts/handoff.py resume  --repo OWNER/NAME [--track T] [--history N]
  python3 scripts/handoff.py chain   --repo OWNER/NAME [--track T] [--limit N] [--grep PATTERN]
  python3 scripts/handoff.py new     --repo OWNER/NAME [--track T] --title S --body-file PATH
  python3 scripts/handoff.py link    --repo OWNER/NAME --new N --previous N
  python3 scripts/handoff.py tracks  --repo OWNER/NAME
  python3 scripts/handoff.py adopt   ISSUE --repo OWNER/NAME --track T [--round N] [--previous N]

Every subcommand takes `--repo`, with no default, for the reason `pr_review.py` requires one: an
issue number resolves in every repository, and a chain read out of the wrong one is well formed.
Add `--dry-run` to any writing subcommand to print the calls it would make and write nothing.
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

# The label is the chain's index.
# Finding a link by label rather than by title search is what frees the title.
# It carries a human subject, and nothing parses it.
LABEL = "handoff"

# The command that fixes a repository missing the label.
# `decision` was declared on the hub and applied nowhere else (#1434).
# A downstream session then enumerated an empty queue and reported it healthy.
# A `handoff` label declared and not applied fails the same silent way, so this refuses instead.
APPLY = "repo-config/configure.sh apply OWNER/NAME release|operational"

DEFAULT_TRACK = "default"

# A track is a short kebab-case slug naming a lane of work.
# Parallel lanes each name their own, so each closes its own predecessor.
TRACK = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# One HTML comment, rendered invisible, carrying what a title must never be parsed for.
# Matched anywhere in the body and required to occur once, then rewritten onto the last line.
MARKER = re.compile(
    r"^<!--\s*handoff:\s*v1\s+track=(?P<track>[a-z0-9]+(?:-[a-z0-9]+)*)\s+"
    r"round=(?P<round>[1-9]\d*)\s+previous=(?P<previous>[1-9]\d*|none)\s*-->[ \t]*\r?$",
    re.MULTILINE,
)

TITLE = "Session Handoff [{track}]: {subject}"

# GitHub's own issue body limit is 65536 characters.
# The hard cap sits under it, so a refusal names the handoff rather than the API.
# The warning sits far below it, to be read while the cap is still nowhere near.
# Neither number bounds a handoff, and the `session-handoff` Skill's per-section rules do.
WARN_BYTES = 12 * 1024
MAX_BYTES = 60 * 1024

# An explicit page size, because `gh issue list` defaults to 30.
# A default that truncates reads exactly like a repository with 30 issues.
# A result filling the page is reported as truncated rather than treated as whole.
WINDOW = 100

# The closed side needs its own, much larger window, for a reason about the shape of the data.
# Open handoffs are bounded by the one-per-track invariant.
# Closed ones accumulate one per round and nothing prunes them.
# A window sized for the first is therefore a wall the second reaches by working.
# `gh issue list` paginates internally above one page, so this costs requests rather than accuracy.
CLOSED_WINDOW = 1000

# How much of `gh`'s own stderr is passed through before it is cut.
# The cut says how much it left, and the count is of characters, since that is what is measured.
STDERR_CAP = 2000

# How many closed links `resume` indexes and `chain` walks unless told otherwise.
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


def require_label(repo: str) -> None:
    """Refuse where the target repository does not carry the label the chain is indexed by.

    Degrading instead would report an empty chain on a repository that has one, which is the
    failure mode `decision` already demonstrated fleet-wide.
    """
    rows = gh_json(["label", "list", "--repo", repo, "--limit", str(WINDOW), "--json", "name"])
    if not isinstance(rows, list):
        raise Execution(f"the label list for {repo} did not read as an array")
    names = {row["name"] for row in rows}
    if LABEL not in names and len(rows) >= WINDOW:
        raise Refusal(
            f"{repo} has at least {WINDOW} labels, which fills the read window, so `{LABEL}` "
            "could sit past it. Reporting the label as absent here would send you to re-apply a "
            "set that may already be applied."
        )
    if LABEL not in names:
        raise Refusal(
            f"{repo} carries no `{LABEL}` label, so no handoff can be found or filed there. "
            f"Apply the fleet label set from a hub checkout: {APPLY}"
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


def with_marker(body: str, track: str, round_: int, previous: int | None) -> str:
    """`body` with the metadata block as its last line, replacing any block already in it."""
    stripped = MARKER.sub("", body or "").rstrip()
    return f"{stripped}\n\n{render_marker(track, round_, previous)}\n"


def issue(repo: str, number: int) -> dict:
    """One issue read live, which is where every identifier a later write consumes comes from."""
    data = gh_json(
        [
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,state,createdAt,closedAt,updatedAt,url,labels",
        ]
    )
    if not isinstance(data, dict):
        raise Execution(f"issue #{number} in {repo} did not read as an object")
    return data


def open_handoffs(repo: str) -> list[dict]:
    """Every open issue carrying the label, newest first, with its metadata block parsed.

    A page that fills is reported as truncated rather than treated as whole, since an open handoff
    past the window is exactly as invisible as one that does not exist.
    """
    rows = gh_json(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            LABEL,
            "--state",
            "open",
            "--limit",
            str(WINDOW),
            "--json",
            "number,title,body,state,createdAt,updatedAt,url",
        ]
    )
    if not isinstance(rows, list):
        raise Execution(f"the handoff list for {repo} did not read as an array")
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


def require_adopted(rows: list[dict]) -> None:
    """Refuse while any open handoff carries no metadata block.

    Such an issue belongs to an unknown track, so it could be the one the caller asked about, and
    picking around it would answer a question the data does not settle. `adopt` is the fix, and
    `tracks` surveys without refusing.
    """
    broken = [row["malformed"] for row in rows if row.get("malformed")]
    if broken:
        raise Refusal(" ".join(broken))
    bare = [row for row in rows if row["marker"] is None]
    if bare:
        listed = ", ".join(f"#{row['number']}" for row in bare)
        raise Refusal(
            f"{listed} carries the `{LABEL}` label and no metadata block, so its track is unknown "
            "and no track can be read as unambiguous. Run `adopt` on it first."
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


def current_or_refuse(repo: str, track: str) -> dict:
    rows = open_handoffs(repo)
    require_adopted(rows)
    found = on_track(rows, track)
    if found is None:
        raise Refusal(
            f"{repo} has no open handoff on track {track!r}. Zero is the state before the first "
            "handoff on a track, so `new` is what follows, not a retry of this."
        )
    return found


def walk(repo: str, start: int, limit: int, cap: str) -> tuple[list[dict], str | None]:
    """The chain from `start` backwards along `previous=`, at most `limit` links.

    Returns the links and, where the walk ended anywhere but at a `previous=none`, a phrase saying
    where and why. A caller that printed only the links could not tell a chain of four from the
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
    number: int | None = start
    while number is not None:
        if number in seen:
            return links, f"a cycle back to #{number}, so the chain does not end where it should"
        if len(links) >= limit:
            return links, f"the {cap} cap of {limit}, with #{number} and earlier unread"
        seen.add(number)
        try:
            data = issue(repo, number)
        except Execution as exc:
            return links, f"#{number}, which could not be read: {exc}"
        try:
            data["marker"] = parse_marker(data.get("body") or "", data["number"])
        except Refusal as exc:
            data["marker"] = None
            links.append(data)
            return links, f"#{number}, whose block cannot be read: {exc}"
        links.append(data)
        marker = data["marker"]
        if marker is None:
            return links, (
                f"#{number}, whose body carries no metadata block, so nothing names what "
                "precedes it"
            )
        if marker["previous"] == "none":
            return links, None
        number = int(marker["previous"])
    return links, None


def age_days(stamp: str) -> int:
    """Whole days between an ISO-8601 GitHub timestamp and now, both in UTC."""
    when = datetime.fromisoformat(stamp)
    return (datetime.now(UTC) - when).days


def day(stamp: str | None) -> str:
    return (stamp or "")[:10] or "-"


def describe(link: dict) -> str:
    """One walked link as a line, carrying its own track.

    The track is printed rather than assumed from the track the walk started on, because `walk`
    follows each marker's `previous=` and nothing stops a hand-edited or adopted link naming a
    predecessor on another lane. Printing it is what makes that visible to the reader.
    """
    marker = link.get("marker")
    round_ = f"{marker['track']} round {marker['round']}" if marker else "unadopted"
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
    if MARKER.search(text):
        raise Refusal(
            "the body file already carries a handoff metadata block. This appends its own, and two "
            "blocks make the track unreadable. Remove it and let the chain write it."
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
    """Post one comment and confirm it by the URL the write returns."""
    out = gh(
        ["issue", "comment", str(number), "--repo", repo, "--body", text], dry_run=dry_run
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


def edit_body(repo: str, number: int, body: str, dry_run: bool) -> None:
    with body_arg(body, dry_run) as path:
        out = gh(
            ["issue", "edit", str(number), "--repo", repo, "--body-file", path],
            dry_run=dry_run,
        ).strip()
    if not dry_run:
        print(f"  body updated: {out or f'#{number}'}")


def cmd_current(a: argparse.Namespace) -> int:
    found = current_or_refuse(a.repo, a.track)
    marker = found["marker"]
    print(f"#{found['number']} {found['url']}")
    print(f"track={marker['track']} round={marker['round']} previous={marker['previous']}")
    print(found["title"])
    return 0


def cmd_resume(a: argparse.Namespace) -> int:
    found = current_or_refuse(a.repo, a.track)
    print(f"# {found['title']}")
    print(f"# {found['url']}")
    print()
    print((found.get("body") or "").rstrip())
    marker = found["marker"]
    previous = None if marker["previous"] == "none" else int(marker["previous"])
    print()
    print(f"## Chain history, last {a.history} closed links")
    if previous is None:
        print("(none - this is the first handoff on this track)")
        return 0
    links, stopped = walk(a.repo, previous, a.history, "--history")
    for link in links:
        print(describe(link))
    if stopped is not None:
        print(f"(index stopped at {stopped})")
    return 0


def cmd_chain(a: argparse.Namespace) -> int:
    try:
        pattern = re.compile(a.grep, re.IGNORECASE) if a.grep else None
    except re.error as exc:
        raise Refusal(f"--grep is not a valid regular expression: {exc}") from exc
    rows = open_handoffs(a.repo)
    require_adopted(rows)
    head = on_track(rows, a.track) or newest_closed(a.repo, a.track)
    if head is None:
        raise Refusal(
            f"{a.repo} has no handoff on track {a.track!r}, open or closed, so there is no chain "
            "to walk."
        )
    links, stopped = walk(a.repo, head["number"], a.limit, "--limit")
    shown = 0
    for link in links:
        if pattern is not None and not pattern.search(link.get("body") or ""):
            continue
        shown += 1
        print(describe(link))
    if pattern is not None:
        print(f"{shown} of {len(links)} links walked match /{a.grep}/")
    strayed = [link for link in links if link["marker"] and link["marker"]["track"] != a.track]
    if strayed:
        listed = ", ".join(f"#{link['number']} on {link['marker']['track']}" for link in strayed)
        print(f"(the walk left track {a.track!r}: {listed})")
    if stopped is not None:
        print(
            f"(the walk stopped at {stopped}, so it read {len(links)} link(s) rather than the "
            "chain, and a search over it is not a search over the chain)"
        )
    return 0


def newest_closed(repo: str, track: str) -> dict | None:
    """The newest closed handoff on a track, or None where the track has never had one.

    A track with no open handoff is either one that has never had a handoff or one whose lane was
    closed out, and the two are not the same. Reading the second as the first makes the next `new`
    start a second chain at round 1 with `previous=none`, orphaning every link already written,
    which is the failure the chain exists to prevent.
    """
    rows = gh_json(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            LABEL,
            "--state",
            "closed",
            "--limit",
            str(CLOSED_WINDOW),
            "--json",
            "number,body,state",
        ]
    )
    if not isinstance(rows, list):
        raise Execution(f"the closed handoff list for {repo} did not read as an array")
    unreadable: list[str] = []
    for row in sorted(rows, key=lambda row: row["number"], reverse=True):
        try:
            marker = parse_marker(row.get("body") or "", row["number"])
        except Refusal as exc:
            unreadable.append(str(exc))
            continue
        if marker is None:
            # A block that is absent and one that cannot be read are the same hazard here.
            # Either way the link's track is unknown, so either could be this track's real newest.
            unreadable.append(
                f"#{row['number']} carries the `{LABEL}` label and no metadata block, so its "
                "track cannot be read."
            )
            continue
        if marker["track"] == track:
            if unreadable:
                raise Refusal(
                    f"#{row['number']} is the newest readable link on track {track!r}, and "
                    + " ".join(unreadable)
                    + f" Each of those is newer than it, so one could be {track!r}'s real newest "
                    "and chaining onto this one would fork the chain."
                )
            row["marker"] = marker
            return row
    if unreadable:
        raise Refusal(
            f"no closed link on track {track!r} was readable, and "
            + " ".join(unreadable)
            + f" A link on {track!r} could be one of those, so this cannot say the track has none."
        )
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
    if not rows:
        print(f"(no open `{LABEL}` issues in {a.repo})")
        return 0
    for row in rows:
        marker = row["marker"]
        track = (
            marker["track"]
            if marker
            else ("(malformed)" if row.get("malformed") else "(unadopted)")
        )
        updated = age_days(row["updatedAt"])
        print(f"{track:<20} #{row['number']:<6} updated {updated}d ago  {row['title']}")
    return 0


def cmd_new(a: argparse.Namespace) -> int:
    """Create, then comment the forward link, then close, in that order.

    Creating first means a failure at any later step leaves a discoverable new issue rather than a
    closed chain with no successor, and `link` finishes what a failure interrupted.

    The read of the predecessor and the create that follows it are not serialized, so two sessions
    filing on one track at once both resolve the same predecessor and both create. That leaves the
    two open handoffs every later command refuses over, which is detectable rather than silent, and
    naming a track per lane is what keeps two sessions off one chain in the first place.
    """
    body = body_from(Path(a.body_file))
    rows = open_handoffs(a.repo)
    require_adopted(rows)
    previous = on_track(rows, a.track) or newest_closed(a.repo, a.track)
    previous_number = previous["number"] if previous else None
    round_ = int(previous["marker"]["round"]) + 1 if previous else 1
    title = TITLE.format(track=a.track, subject=a.title)
    print(f"1. create the new handoff on track {a.track!r}, round {round_}")
    number = create(a.repo, title, with_marker(body, a.track, round_, previous_number), a.dry_run)
    if previous is None:
        print(f"2. no previous handoff on track {a.track!r}, so nothing to link or close")
        return 0
    settled = int(previous["number"])
    print(f"2. comment the forward link on #{settled}")
    comment(
        a.repo,
        settled,
        f"Continued in #{number or '<the new issue>'}, round {round_} on track `{a.track}`.",
        a.dry_run,
    )
    if previous["state"] == "CLOSED":
        print(f"3. #{settled} is already closed, so the chain is finished")
        return 0
    print(f"3. close #{settled}")
    close(a.repo, settled, a.dry_run)
    return 0


def cmd_link(a: argparse.Namespace) -> int:
    """Finish a chain that half-applied, without filing a second issue for it."""
    if a.new == a.previous:
        raise Refusal(
            f"--new and --previous are both #{a.new}, and an issue cannot succeed itself. "
            "That would close the track's only open link into a cycle."
        )
    new = issue(a.repo, a.new)
    previous = issue(a.repo, a.previous)
    marker = parse_marker(new.get("body") or "", new["number"])
    if marker is None:
        raise Refusal(
            f"#{new['number']} carries no handoff metadata block, so it is not a chain link. "
            "Run `adopt` on it first."
        )
    if LABEL not in {row["name"] for row in new.get("labels") or []}:
        raise Refusal(
            f"#{new['number']} carries a handoff metadata block and no `{LABEL}` label, so no "
            "read here can find it. Closing the predecessor would leave this track with no "
            f"findable head. Run `adopt {new['number']} --track {marker['track']} --round "
            f"{marker['round']}"
            + ("" if marker["previous"] == "none" else f" --previous {marker['previous']}")
            + "` to finish labeling it first."
        )
    # `link` takes no --track, so the two blocks are all that can say they are one lane.
    # Without this it comments on and closes another lane's open handoff and exits 0.
    before = parse_marker(previous.get("body") or "", previous["number"])
    if before is None:
        raise Refusal(
            f"#{previous['number']} carries no handoff metadata block, so nothing says it is on "
            f"track {marker['track']!r}. Run `adopt` on it first."
        )
    if before["track"] != marker["track"]:
        raise Refusal(
            f"#{new['number']} is on track {marker['track']!r} and #{previous['number']} is on "
            f"{before['track']!r}. Linking them would close one lane's handoff into another's."
        )
    # A chain runs one way, and transposing the two numbers is the ordinary slip here.
    # Without this the older link is rewritten to name the newer.
    # The newer is then closed and the run exits 0 over a two-link cycle.
    if int(marker["round"]) <= int(before["round"]):
        raise Refusal(
            f"#{new['number']} is round {marker['round']} and #{previous['number']} is round "
            f"{before['round']}, so #{new['number']} does not succeed it. Check --new and "
            "--previous are not transposed."
        )
    if before["previous"] == str(new["number"]):
        raise Refusal(
            f"#{previous['number']} already names #{new['number']} as its predecessor, so linking "
            "them this way would close the chain into a cycle."
        )
    if marker["previous"] not in ("none", str(previous["number"])):
        raise Refusal(
            f"#{new['number']} already names #{marker['previous']} as its predecessor, not "
            f"#{previous['number']}. Two predecessors is not something this can decide between."
        )
    if marker["previous"] == "none":
        print(f"1. point #{new['number']} at #{previous['number']}")
        edit_body(
            a.repo,
            new["number"],
            with_marker(
                new.get("body") or "", marker["track"], int(marker["round"]), previous["number"]
            ),
            a.dry_run,
        )
    else:
        print(f"1. #{new['number']} already names #{previous['number']}, nothing to edit")
    print(f"2. comment the forward link on #{previous['number']}")
    comment(
        a.repo,
        previous["number"],
        f"Continued in #{new['number']}, round {marker['round']} on track `{marker['track']}`.",
        a.dry_run,
    )
    if previous["state"] == "CLOSED":
        print(f"3. #{previous['number']} is already closed, so the chain is finished")
        return 0
    print(f"3. close #{previous['number']}")
    close(a.repo, previous["number"], a.dry_run)
    return 0


def require_track_free(repo: str, track: str, adopting: int) -> None:
    """Refuse where adopting onto `track` would leave two open handoffs on it.

    Both of `adopt`'s branches run this, the first-order one and the half-applied recovery, since
    a recovery is not a licence to skip the invariant. A lane can acquire an open link between a
    half-applied run and the re-run that finishes it, and finishing regardless is how a silent
    fork becomes a hard ambiguity.
    """
    rows = open_handoffs(repo)
    broken = [row for row in rows if row.get("malformed")]
    if broken:
        listed = ", ".join(f"#{row['number']}" for row in broken)
        raise Refusal(
            f"{listed} is open, carries the `{LABEL}` label, and has an unreadable block, so "
            f"whether track {track!r} already has one open cannot be read. Repair that block "
            "before adopting onto any track."
        )
    standing = on_track(rows, track)
    if standing is not None and standing["number"] != adopting:
        raise Refusal(
            f"track {track!r} already has #{standing['number']} open, so adopting #{adopting} "
            "onto it would make two. Adopt it onto a track of its own, or close the standing "
            "link first."
        )


def cmd_adopt(a: argparse.Namespace) -> int:
    """Put the label and the metadata block on a handoff that predates both."""
    target = issue(a.repo, a.issue)
    labeled = LABEL in {row["name"] for row in target.get("labels") or []}
    standing_block = parse_marker(target.get("body") or "", target["number"])
    if target["state"] == "OPEN":
        require_track_free(a.repo, a.track, target["number"])
    if standing_block is not None:
        wanted = {"track": a.track, "round": str(a.round), "previous": str(a.previous or "none")}
        if labeled:
            raise Refusal(
                f"#{target['number']} already carries a handoff metadata block and the "
                f"`{LABEL}` label, so there is nothing to adopt. Edit the block in place where a "
                "field is wrong."
            )
        if standing_block != wanted:
            block = standing_block
            raise Refusal(
                f"#{target['number']} carries a block reading track={block['track']} "
                f"round={block['round']} previous={block['previous']} and no `{LABEL}` label, so "
                "the label is the step still owed. Re-run with exactly those values to finish it, "
                "or edit the block in place where a field is wrong."
            )
        # The block is this run's own, written before a label write that did not land.
        # Finishing it is what makes the body-first order recoverable rather than a dead end.
        print(f"1. #{target['number']} already carries this block, so only the label is left")
        print(f"2. add the `{LABEL}` label to #{target['number']}")
        out = gh(
            ["issue", "edit", str(target["number"]), "--repo", a.repo, "--add-label", LABEL],
            dry_run=a.dry_run,
        ).strip()
        if not a.dry_run:
            print(f"  labeled: {out or '#' + str(target['number'])}")
        return 0
    # The predecessor is read live before its number is stamped into the block.
    # A number nothing read is an identifier the write consumes and the caller constructed.
    # An issue number resolves in every repository, so a mistyped one is well formed, not absent.
    # This read comes before the label write rather than after it.
    # A failure between the two leaves a labeled issue carrying no block.
    # `require_adopted` then refuses every read on every track in the repository until this re-runs.
    previous = None
    if a.previous is not None:
        if a.previous == target["number"]:
            raise Refusal(
                f"--previous is #{a.previous}, the issue being adopted, and an issue cannot "
                "succeed itself. That writes a chain that reads one link forever."
            )
        before = issue(a.repo, a.previous)
        earlier = parse_marker(before.get("body") or "", before["number"])
        if earlier is not None:
            if earlier["track"] != a.track:
                raise Refusal(
                    f"#{before['number']} is on track {earlier['track']!r} and this would adopt "
                    f"#{target['number']} onto {a.track!r}. A chain does not cross lanes."
                )
            if int(earlier["round"]) >= a.round:
                raise Refusal(
                    f"#{before['number']} is round {earlier['round']} and this would make "
                    f"#{target['number']} round {a.round}, so it would not succeed it. Check "
                    "--round and --previous."
                )
        previous = int(before["number"])
        print(f"1. read #{previous}, the predecessor this block will name")
    else:
        print("1. no --previous given, so this block names none")
    print(f"2. write the metadata block on #{target['number']}")
    edit_body(
        a.repo,
        target["number"],
        with_marker(target.get("body") or "", a.track, a.round, previous),
        a.dry_run,
    )
    if labeled:
        print(f"3. #{target['number']} already carries the `{LABEL}` label")
        return 0
    print(f"3. add the `{LABEL}` label to #{target['number']}")
    out = gh(
        ["issue", "edit", str(target["number"]), "--repo", a.repo, "--add-label", LABEL],
        dry_run=a.dry_run,
    ).strip()
    if not a.dry_run:
        print(f"  labeled: {out or '#' + str(target['number'])}")
    return 0


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


def add_track(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument(
        "--track",
        default=None if required else DEFAULT_TRACK,
        required=required,
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
        help=f"closed links to index after the body (default {HISTORY})",
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

    p = sub.add_parser("new", help="create the next link, then link and close the previous one")
    add_repo(p)
    add_track(p)
    add_dry_run(p)
    p.add_argument("--title", required=True, metavar="SUBJECT", help="the human half of the title")
    p.add_argument(
        "--body-file", required=True, metavar="PATH", help="the handoff body, per the skill"
    )

    p = sub.add_parser("link", help="finish a chain that half-applied")
    add_repo(p)
    add_dry_run(p)
    p.add_argument("--new", type=int, required=True, metavar="N", help="the successor issue")
    p.add_argument("--previous", type=int, required=True, metavar="N", help="the issue it succeeds")

    p = sub.add_parser("tracks", help="every open handoff with its track and age")
    add_repo(p)

    p = sub.add_parser("adopt", help="label and mark an existing hand-written handoff")
    p.add_argument("issue", type=int, metavar="ISSUE", help="the issue to adopt")
    add_repo(p)
    add_track(p, required=True)
    add_dry_run(p)
    p.add_argument("--round", type=int, default=1, metavar="N", help="its round number (default 1)")
    p.add_argument(
        "--previous", type=int, default=None, metavar="N", help="the issue it succeeds, if any"
    )
    return ap


HANDLERS = {
    "current": cmd_current,
    "resume": cmd_resume,
    "chain": cmd_chain,
    "new": cmd_new,
    "link": cmd_link,
    "tracks": cmd_tracks,
    "adopt": cmd_adopt,
}


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    # A bare name is the near-miss a required argument still admits.
    # Unpacking it raises a ValueError traceback rather than saying which half is missing.
    owner, _, name = a.repo.partition("/")
    if not owner or not name or "/" in name:
        ap.error(f"--repo takes OWNER/NAME, not {a.repo!r}")
    if getattr(a, "track", None) is not None and not TRACK.match(a.track):
        ap.error(f"--track takes a kebab-case slug, not {a.track!r}")
    for flag in ("history", "limit", "round"):
        if getattr(a, flag, None) is not None and getattr(a, flag) < 1:
            ap.error(f"--{flag} counts links or rounds, so it cannot be below 1")
    for flag in ("issue", "new", "previous"):
        if getattr(a, flag, None) is not None and getattr(a, flag) < 1:
            named = flag if flag == "issue" else f"--{flag}"
            ap.error(f"{named} takes an issue number, so it cannot be below 1")
    try:
        require_label(a.repo)
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
