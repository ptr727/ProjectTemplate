"""A develop -> main promotion's constituent pull requests, read for green Dependabot bumps.

This is a library module and has no command line. Merge Gate item 2 counts a Dependabot pull
request as covered where it changed only manifests and lockfiles and merged into `develop` green.
`pr_review_rounds.coverage` reads one pull request's own rounds, so nothing there can see such a
bump. This module reads the promotion's commits back to the pull requests that landed them. Each
path only such bumps changed then leaves the comparison a round's file table has to meet.

The manifests are what the fleet's Dependabot ecosystems name as a manifest or a lockfile. A
workflow or a Dockerfile is also bumped, and is left out on purpose, since either runs as written.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import NamedTuple

import pr_review_rounds as rounds
from pr_review_checks import head_commit
from pr_review_payload import default_branch

BUMPS_KEY = "_dependabotBumps"
DEVELOP = "develop"
DEPENDABOT = "dependabot"
MANIFEST = re.compile(
    r"pyproject\.toml|uv\.lock|requirements[^/]*\.txt|Directory\.Packages\.props"
    r"|packages\.lock\.json|dotnet-tools\.json"
)

Q_CONSTITUENTS = """
query($o:String!,$r:String!,$n:Int!,$after:String){
  repository(owner:$o,name:$r){ pullRequest(number:$n){ headRefOid
    commits(first:50, after:$after){ totalCount pageInfo{ hasNextPage endCursor } nodes{ commit{ oid
      associatedPullRequests(first:5){ nodes{ number baseRefName mergeCommit{ oid }
        author{ login }
        files(first:100){ pageInfo{ hasNextPage } nodes{ path } }
        headRefOid commits(last:1){ nodes{ commit{ oid statusCheckRollup{ state } } } }
      }}
    }}}
  }}}
"""


class Bumps(NamedTuple):
    """What a promotion's commits show: the paths only green bumps changed, or why none can be."""

    paths: frozenset[str] = frozenset()
    numbers: tuple[int, ...] = ()
    why: str = ""


def is_manifest(path: str) -> bool:
    """Whether `path`'s file name is a dependency manifest or lockfile's."""
    return MANIFEST.fullmatch(path.rsplit("/", 1)[-1]) is not None


def green_bump(landed: dict, paths: set[str]) -> bool:
    """Whether a merged pull request is Dependabot's, manifests and lockfiles only, and green.

    Dependabot is GraphQL's Bot login, which REST spells `dependabot[bot]`. Green is the head's
    whole rollup rather than its required checks alone, since which checks were required when it
    merged is not on the payload. A failing optional check therefore sends the bump to the
    maintainer, the safe side of that gap.
    """
    author = landed.get("author") or {}
    rollup = head_commit(landed).get("statusCheckRollup") or {}
    return (
        author.get("login") == DEPENDABOT
        and all(is_manifest(p) for p in paths)
        and rollup.get("state") == "SUCCESS"
    )


def landing(node: dict) -> dict | None:
    """The pull request that landed this commit on `develop`, None where none did."""
    commit = node.get("commit") or {}
    associated = (commit.get("associatedPullRequests") or {}).get("nodes") or []
    oid = rounds.commit_oid(node)
    for candidate in associated:
        merged = (candidate.get("mergeCommit") or {}).get("oid")
        if merged == oid and candidate.get("baseRefName") == DEVELOP:
            return candidate
    return None


def read_bumps(commits: list[dict]) -> Bumps:
    """The paths only green bumps changed across `commits`, each read to its landing pull request.

    A path any other pull request changed stays out, since that change needs its own evidence.
    One commit that cannot be read back withdraws the whole reading, a direct push among them.
    So does one pull request whose file list runs past its page. Neither change's files can be
    subtracted.
    """
    bumped: set[str] = set()
    other: set[str] = set()
    bumps: dict[int, set[str]] = {}
    for node in commits:
        landed = landing(node)
        if landed is None:
            oid = (rounds.commit_oid(node) or "")[:8]
            return Bumps(why=f"commit {oid} landed through no pull request into {DEVELOP}")
        changed, cut = rounds.changed_paths(landed)
        if cut:
            return Bumps(why=f"pull request #{landed.get('number')}'s file list runs past a page")
        paths = set(changed)
        if green_bump(landed, paths):
            bumped |= paths
            bumps[landed["number"]] = paths
        else:
            other |= paths
    only = bumped - other
    return Bumps(frozenset(only), tuple(sorted(n for n, p in bumps.items() if p & only)))


def eligible(pr: dict) -> bool:
    """Whether this head is a promotion a round covers, reading `none`, its whole list in view."""
    return (
        pr.get("headRefName") == DEVELOP
        and pr.get("baseRefName") == default_branch(pr)
        and bool(rounds.head_reviews(pr))
        and rounds.coverage(pr)[0] == "none"
        and not rounds.changed_paths(pr)[1]
        and not rounds.files_unread(pr)
        and not rounds.reviews_truncated(pr)
    )


def attach(
    pr: dict, owner: str, repo: str, num: int, follow: Callable[..., Iterator[dict]]
) -> dict:
    """Attach the bumps a promotion reading `none` carries, under `BUMPS_KEY`, and return `pr`.

    `follow` pages a connection and ends the run on a failed read, which here costs only the walk.
    Only a head otherwise reading `none` costs the walk. A head moved while the list was
    paged reads as no bumps at all, not a partial answer, as does a list GitHub cut short.
    """
    if not eligible(pr):
        return pr
    commits: list[dict] = []
    total = 0
    try:
        for page in follow(Q_CONSTITUENTS, owner, repo, num, "commits"):
            if page.get("headRefOid") != pr.get("headRefOid"):
                pr[BUMPS_KEY] = Bumps(why="the head moved while its commits were paged")
                return pr
            commits.extend(page["commits"]["nodes"])
            total = page["commits"].get("totalCount") or 0
    except SystemExit as exc:
        pr[BUMPS_KEY] = Bumps(why=f"its commits could not be read: {exc}")
        return pr
    cut = total != len(commits)
    pr[BUMPS_KEY] = Bumps(why="the commit list is cut short") if cut else read_bumps(commits)
    return pr


def coverage(pr: dict) -> tuple[str, str]:
    """`pr_review_rounds.coverage`, with a promotion's green Dependabot bumps counted as covered.

    A `none` becomes `dependabot` where every changed file is one only such bumps changed. It does
    too where a round's file table names exactly what is left once those are taken out. Every
    other reading stands, and a promotion that cannot be read back says why beside its `none`.
    """
    state, why = rounds.coverage(pr)
    bumps = pr.get(BUMPS_KEY)
    if state != "none" or bumps is None:
        return state, why
    if bumps.why:
        return state, f"{why}, and no Dependabot bump can be counted: {bumps.why}"
    files = pr.get("files") or {}
    nodes = files.get("nodes") or []
    kept = [n for n in nodes if n.get("path") not in bumps.paths]
    if len(kept) == len(nodes):
        return state, why
    numbers = ", ".join(f"#{n}" for n in bumps.numbers)
    note = f"{len(nodes) - len(kept)} changed only by green Dependabot bumps ({numbers})"
    if not kept:
        return "dependabot", f"every changed file is one of {note}"
    state, reason = rounds.table_cover({**pr, "files": {**files, "nodes": kept}}, bumps.paths)
    return ("dependabot" if state else "none"), f"{reason}, with {note} left out"
