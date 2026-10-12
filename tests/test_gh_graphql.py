"""Tests for scripts/gh_graphql.py, the shared GraphQL transport and cursor walker."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import gh_graphql


def answering(code: int, payload: object, stderr: str = ""):
    """A runner standing in for `gh`, answering `payload` as JSON."""
    return lambda argv: (code, json.dumps(payload), stderr)


class RequestCase(unittest.TestCase):
    def test_a_clean_response_returns_its_data(self) -> None:
        got = gh_graphql.request("q", {}, run=answering(0, {"data": {"a": 1}}))
        self.assertEqual(got.data, {"a": 1})
        self.assertEqual(got.tolerated, [])

    def test_the_failures_raise_and_never_exit(self) -> None:
        for label, run in (
            ("nonzero without errors", answering(1, {"data": {}}, "denied")),
            ("not json", lambda argv: (0, "nope", "")),
            ("not an object", answering(0, ["x"])),
            ("errors", answering(0, {"data": {"a": 1}, "errors": [{"path": ["a"]}]})),
            ("null data", answering(0, {"data": None})),
        ):
            with self.subTest(label=label), self.assertRaises(gh_graphql.GraphQLError):
                gh_graphql.request("q", {}, run=run)

    def test_the_detail_is_bounded(self) -> None:
        with self.assertRaises(gh_graphql.GraphQLError) as caught:
            gh_graphql.request("q", {}, run=answering(1, None, "x" * 5000))
        self.assertEqual(len(caught.exception.detail), gh_graphql.DETAIL_CAP)

    def test_only_errors_the_caller_names_are_tolerated(self) -> None:
        payload = {"data": {"a": 1}, "errors": [{"path": ["x"]}]}
        got = gh_graphql.request("q", {}, run=answering(1, payload), tolerate=lambda e: True)
        self.assertEqual(got.tolerated, payload["errors"])
        with self.assertRaises(gh_graphql.GraphQLError):
            gh_graphql.request("q", {}, run=answering(1, payload), tolerate=lambda e: False)

    def test_tolerated_errors_with_null_data_still_raise(self) -> None:
        payload = {"data": None, "errors": [{"path": ["x"]}]}
        with self.assertRaises(gh_graphql.GraphQLError):
            gh_graphql.request("q", {}, run=answering(1, payload), tolerate=lambda e: True)


class ArgvCase(unittest.TestCase):
    def test_types_and_lists(self) -> None:
        argv = gh_graphql.build_argv("Q", {"s": "123", "n": 5, "xs": ["a", "b"]})
        self.assertEqual(
            argv,
            ["gh", "api", "graphql", "-f", "query=Q", "-f", "s=123", "-F", "n=5"]
            + ["-f", "xs[]=a", "-f", "xs[]=b"],
        )


def pages(*cursors: tuple[bool, str | None]):
    """A fetch returning one page per call under `a.b`, each page shaped by `cursors`."""
    calls: list[str | None] = []

    def fetch(after: str | None) -> dict:
        calls.append(after)
        more, cursor = cursors[len(calls) - 1]
        return {
            "a": {"tag": len(calls), "b": {"pageInfo": {"hasNextPage": more, "endCursor": cursor}}}
        }

    return fetch, calls


class WalkCase(unittest.TestCase):
    def test_follows_every_page_and_yields_the_parent(self) -> None:
        fetch, calls = pages((True, "c1"), (True, "c2"), (False, None))
        got = list(gh_graphql.walk(fetch, ("a", "b")))
        self.assertEqual([p["tag"] for p in got], [1, 2, 3])
        self.assertEqual(calls, [None, "c1", "c2"])

    def test_starts_after_the_cursor_given(self) -> None:
        fetch, calls = pages((False, None))
        list(gh_graphql.walk(fetch, ("a", "b"), "start"))
        self.assertEqual(calls, ["start"])

    def test_the_path_is_a_parameter_at_any_depth(self) -> None:
        def fetch(after):
            return {"x": {"page": {"pageInfo": {"hasNextPage": False}}}}

        self.assertEqual(len(list(gh_graphql.walk(fetch, ("x", "page")))), 1)
        self.assertEqual(len(list(gh_graphql.walk(lambda a: {"page": {}}, ("page",)))), 1)

    def test_a_cursor_that_does_not_advance_raises(self) -> None:
        for cursor in (None, "", "start"):
            with self.subTest(cursor=cursor):
                fetch, calls = pages((True, cursor), (True, cursor), (True, cursor))
                with self.assertRaises(gh_graphql.GraphQLError):
                    list(gh_graphql.walk(fetch, ("a", "b"), "start"))
                self.assertLessEqual(len(calls), 1)

    def test_a_missing_connection_raises(self) -> None:
        for data in ({}, {"a": None}, {"a": {"b": None}}):
            with self.subTest(data=data), self.assertRaises(gh_graphql.GraphQLError):
                list(gh_graphql.walk(lambda after, d=data: d, ("a", "b")))  # type: ignore[misc]

    def test_a_strict_walk_refuses_a_page_without_page_info(self) -> None:
        for page in (None, {}, {"endCursor": "c"}):
            with self.subTest(page=page):
                data = {"a": {"b": {"pageInfo": page}}}

                def fetch(after, data=data):
                    return data

                self.assertEqual(len(list(gh_graphql.walk(fetch, ("a", "b")))), 1)
                with self.assertRaises(gh_graphql.GraphQLError):
                    list(gh_graphql.walk(fetch, ("a", "b"), strict_page=True))

    def test_stopping_early_reads_no_further_page(self) -> None:
        fetch, calls = pages((True, "c1"), (True, "c2"))
        next(gh_graphql.walk(fetch, ("a", "b")))
        self.assertEqual(calls, [None])


class SiblingImportCase(unittest.TestCase):
    """A script run by path from another directory still finds the module beside it."""

    def test_both_scripts_run_from_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            for script in ("handoff.py", "pr_review.py"):
                with self.subTest(script=script):
                    r = subprocess.run(
                        [sys.executable, str(SCRIPTS / script), "--help"],
                        cwd=elsewhere,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        check=False,
                    )
                    self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
