"""One GraphQL transport and one cursor walker, shared by the scripts that read through `gh`.

This is a library module and has no command line. `pr_review.py` and `handoff.py` import it as a
sibling, which works from any working directory because Python puts the running script's own
directory on `sys.path`.

A failure raises `GraphQLError` and never exits. Each caller maps it to its own exit contract,
since `pr_review.py` reports a failed read as exit 1 while `handoff.py` reserves 1 for a refusal
and reports a read that did not run as 2. A transport that exited on its own would pick one of
those for both.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator, Sequence
from typing import NamedTuple

DETAIL_CAP = 800


class GraphQLError(Exception):
    """A read that did not produce an answer.

    `detail` is the stderr or error text worth showing beside the one-line message, and it is
    empty where the message already says everything.
    """

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class Response(NamedTuple):
    """The `data` of one response, and the errors it carried that the caller chose to tolerate."""

    data: dict
    tolerated: list


Runner = Callable[[list[str]], "tuple[int, str, str]"]


def run_subprocess(argv: list[str]) -> tuple[int, str, str]:
    """Run `argv` and return its exit code, stdout and stderr.

    Every read decodes as UTF-8 rather than as whatever the platform's locale is. `gh` emits UTF-8
    on every platform, where a Windows console locale is cp1252, and one typographic quote in a
    review body crashed the decode and left the caller reading a null stdout.
    """
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", check=False)
    return r.returncode, r.stdout, r.stderr


def build_argv(query: str, variables: dict[str, object]) -> list[str]:
    """The `gh api graphql` argv for `query` carrying `variables`.

    A string goes through `-f` and an int through `-F`, because `-F` infers a type from the text:
    a reply body of `123` or `true` arrives as an Int or a Boolean and the mutation fails on a
    type nobody passed it, and a body opening with `@` is read as a filename. A list is sent as
    one `name[]` field per element, which is how `gh` spells a list variable.
    """
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        if isinstance(value, list | tuple):
            for item in value:
                argv += ["-F" if isinstance(item, int) else "-f", f"{name}[]={item}"]
        else:
            argv += ["-F" if isinstance(value, int) else "-f", f"{name}={value}"]
    return argv


def request(
    query: str,
    variables: dict[str, object],
    *,
    run: Runner = run_subprocess,
    tolerate: Callable[[object], bool] | None = None,
) -> Response:
    """Run one GraphQL document and return its `data`, raising rather than reporting a blank.

    `errors` is checked rather than trusted to the exit code, since a GraphQL document can fail
    per-field while the request itself succeeds, and the caller would read the null that leaves.

    `gh api graphql` exits non-zero with the JSON still on stdout when the response carries
    errors, so that stdout is read first. An error is tolerated only where `tolerate` accepts it,
    and the tolerated ones come back beside the data for the caller to report.
    """
    code, stdout, stderr = run(build_argv(query, variables))
    try:
        payload = json.loads(stdout)
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or (code != 0 and not payload.get("errors")):
        raise GraphQLError(f"gh graphql failed rc={code}", stderr[:DETAIL_CAP])
    errors = payload.get("errors")
    if errors and not (tolerate and all(tolerate(e) for e in errors)):
        raise GraphQLError("gh graphql reported errors", json.dumps(errors)[:DETAIL_CAP])
    if payload.get("data") is None:
        raise GraphQLError("gh graphql returned no data", json.dumps(errors)[:DETAIL_CAP])
    return Response(payload["data"], list(errors or []))


def _descend(node: object, path: Sequence[str]) -> object:
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise GraphQLError(f"the response holds no {'.'.join(path)}")
        node = node[key]
    return node


def walk(
    fetch: Callable[[str | None], dict],
    path: Sequence[str],
    after: str | None = None,
) -> Iterator[dict]:
    """Each page of the connection at `path`, from the one past `after` to the last.

    `fetch` reads one page given the cursor to start after, or `None` for the first, and returns
    that response's `data`. `path` is the keys from `data` down to the connection, and what is
    yielded is the object holding the connection, the last key's parent, so a caller reads its
    siblings too. The connection is read for its `pageInfo`, so the walk is one place a fix to how
    a page is followed lands. A caller wanting fewer pages simply stops iterating.

    A page claiming another without a cursor that moves past the one just sent raises, since the
    same page would otherwise be read forever.
    """
    if not path:
        raise ValueError("a connection path names at least one key")
    while True:
        parent = _descend(fetch(after), path[:-1])
        if not isinstance(parent, dict):
            raise GraphQLError(f"the response holds no {'.'.join(path[:-1])}")
        yield parent
        connection = parent.get(path[-1])
        if not isinstance(connection, dict):
            raise GraphQLError(f"the response holds no {'.'.join(path)}")
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return
        cursor = page.get("endCursor")
        if not cursor or cursor == after:
            raise GraphQLError(
                f"the {'.'.join(path)} reported another page without a cursor that advances"
            )
        after = cursor
