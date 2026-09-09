#!/usr/bin/env python3
"""Exercise handoff.py's chain model, refusals, and write ordering against a stubbed `gh`.

Every case here drives the real subcommands through a fake `gh` holding an in-memory repository,
because the properties under test are about what the script decides and in what order it writes,
not about GitHub. The one thing a stub cannot prove is that `gh` accepts these argument lists, so
each write case asserts the argv it produced rather than only its effect.

Two refusals earn cases of their own because the chain exists to fix them. An ambiguous track is
never resolved by picking, and a repository missing the label refuses rather than reporting an
empty chain, which is the silent failure the `decision` label already demonstrated fleet-wide.

Run as `python3 scripts/tests/test_handoff.py`, or under
`python3 -m unittest discover -s scripts/tests`.
"""

import argparse
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import handoff


def marked(body: str, track: str, round_: int, previous: int | None) -> str:
    return handoff.with_marker(body, track, round_, previous)


def read_marker(body: str, number: int) -> dict[str, str]:
    """The metadata block a case expects to be there, asserted rather than assumed."""
    marker = handoff.parse_marker(body, number)
    assert marker is not None, f"#{number} carries no handoff metadata block"
    return marker


def subcommands() -> dict[str, argparse.ArgumentParser]:
    """The declared subcommands, read off the parser rather than restated here."""
    for action in handoff.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("handoff.build_parser declares no subcommands")


def projected(row: dict, argv: list[str]) -> dict:
    """`row` cut down to the fields `--json` asked for, the way real `gh` answers.

    A fake returning every field regardless is a fake that models a superset of the tool, and a
    caller reading a field it never asked for then passes here and raises against `gh`. That is
    exactly what happened: `open_handoffs` did not request `state`, `cmd_new` read it, and 65
    green cases said nothing because this stub handed the field over anyway.
    """
    if "--json" not in argv:
        return dict(row)
    wanted = argv[argv.index("--json") + 1].split(",")
    return {field: row[field] for field in wanted if field in row}


class FakeGh:
    """An in-memory repository `handoff.gh` reads and writes, recording every argv it is given.

    It honors `--json`, `--state`, `--label`, and `--limit`, because a stub looser than the tool
    it stands in for is a stub that green-lights a crash.

    It also refuses any call carrying no `--repo`, which real `gh` would answer by resolving the
    repository from the working directory's remote. That is the failure this whole script is built
    against, and without this the argument could be dropped from any of nine call sites with every
    case still green.
    """

    def __init__(self, issues: dict[int, dict] | None = None, *, label: bool = True) -> None:
        self.issues = issues or {}
        self.label = label
        self.calls: list[list[str]] = []
        self.next_number = 1001
        # A close that reports success and leaves the issue open.
        # That is the state the read-back after every close exists to catch.
        self.refuse_close = False

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        if "--repo" not in argv:
            raise AssertionError(
                f"{' '.join(argv)} carries no --repo, so real gh would resolve the repository "
                "from the working directory instead of the one named"
            )
        head = (argv[0], argv[1])
        if head == ("label", "list"):
            return json.dumps([{"name": handoff.LABEL}] if self.label else [{"name": "bug"}])
        if head == ("issue", "list"):
            return json.dumps([projected(row, argv) for row in self._list(argv)])
        if head == ("issue", "view"):
            # An issue this repository does not hold is what `gh` exits non-zero on.
            # `run_gh` turns that into an Execution, so the fake stands in for the same thing.
            # Raising a KeyError instead would model a failure no caller has a reader for.
            if int(argv[2]) not in self.issues:
                raise handoff.Execution(f"gh issue view failed rc=1 for #{argv[2]}")
            return json.dumps(projected(self.issues[int(argv[2])], argv))
        if head == ("issue", "create"):
            return self._create(argv)
        if head == ("issue", "comment"):
            return f"https://github.com/o/r/issues/{argv[2]}#issuecomment-1\n"
        if head == ("issue", "close"):
            if not self.refuse_close:
                self.issues[int(argv[2])]["state"] = "CLOSED"
            return f"Closed issue #{argv[2]}\n"
        if head == ("issue", "edit"):
            return self._edit(argv)
        raise AssertionError(f"the fake was given an unmodeled call: {argv}")

    def _list(self, argv: list[str]) -> list[dict]:
        state = argv[argv.index("--state") + 1].upper()
        rows = [
            row
            for row in self.issues.values()
            if row["state"] == state
            and ("--label" not in argv or self._labeled(row, argv[argv.index("--label") + 1]))
        ]
        limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else len(rows)
        return rows[:limit]

    @staticmethod
    def _labeled(row: dict, name: str) -> bool:
        return any(label["name"] == name for label in row.get("labels") or [])

    def _create(self, argv: list[str]) -> str:
        number = self.next_number
        self.next_number += 1
        body = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
        # The labels come from argv rather than from a constant.
        # That is `projected`'s reason applied to a write.
        # A fake labeling an issue the call never asked to label proves nothing.
        # An unlabeled handoff is invisible to every read the script makes.
        labels = [{"name": argv[i + 1]} for i, arg in enumerate(argv) if arg == "--label"]
        self.issues[number] = {
            "number": number,
            "title": argv[argv.index("--title") + 1],
            "body": body,
            "state": "OPEN",
            "createdAt": "2026-09-09T00:00:00Z",
            "updatedAt": "2026-09-09T00:00:00Z",
            "closedAt": None,
            "url": f"https://github.com/o/r/issues/{number}",
            "labels": labels,
        }
        return f"https://github.com/o/r/issues/{number}\n"

    def _edit(self, argv: list[str]) -> str:
        row = self.issues[int(argv[2])]
        if "--add-label" in argv:
            row["labels"] = [*row["labels"], {"name": argv[argv.index("--add-label") + 1]}]
        if "--body-file" in argv:
            row["body"] = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
        return f"https://github.com/o/r/issues/{argv[2]}\n"


def doubled(track: str) -> str:
    """A body carrying two metadata blocks, which is the shape a hand edit produces."""
    return f"{handoff.render_marker(track, 1, None)}\n{handoff.render_marker(track, 2, None)}\n"


def link(number: int, track: str, round_: int, previous: int | None, **over) -> dict:
    """One handoff issue as `gh` renders it, adopted unless a case overrides the body."""
    row = {
        "number": number,
        "title": handoff.TITLE.format(track=track, subject=f"round {round_}"),
        "body": marked(f"body of round {round_}", track, round_, previous),
        "state": "OPEN",
        "createdAt": "2026-09-01T00:00:00Z",
        "updatedAt": "2026-09-01T00:00:00Z",
        "closedAt": None,
        "url": f"https://github.com/o/r/issues/{number}",
        "labels": [{"name": handoff.LABEL}],
    }
    row.update(over)
    return row


def body_file(case: unittest.TestCase, text: str) -> str:
    """A handoff body on disk, removed when the case that asked for it finishes."""
    directory = tempfile.mkdtemp()
    path = Path(directory) / "handoff.md"
    path.write_text(text, encoding="utf-8")
    case.addCleanup(shutil.rmtree, directory)
    return str(path)


def run(fake: FakeGh, *argv: str) -> tuple[int, str, str]:
    """One `main` call against the fake, returning its code and both streams."""
    out, err = io.StringIO(), io.StringIO()
    with (
        unittest.mock.patch.object(handoff, "run_gh", fake),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        code = handoff.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class MarkerCase(unittest.TestCase):
    """The chain is read from the metadata block, so a body edit never breaks it."""

    def test_the_block_is_the_last_line(self) -> None:
        body = marked("first\n\nsecond", "default", 3, 12)
        self.assertTrue(body.rstrip().endswith("previous=12 -->"))
        self.assertIn("second", body)

    def test_rewriting_replaces_rather_than_appends(self) -> None:
        once = marked("text", "lane", 1, None)
        twice = marked(once, "lane", 2, 9)
        self.assertEqual(len(handoff.MARKER.findall(twice)), 1)
        self.assertEqual(
            handoff.parse_marker(twice, 1),
            {
                "track": "lane",
                "round": "2",
                "previous": "9",
            },
        )

    def test_a_body_with_no_block_reads_as_none(self) -> None:
        self.assertIsNone(handoff.parse_marker("nothing here", 1))

    def test_a_crlf_body_still_chains(self) -> None:
        """GitHub returns CRLF for anything typed in its web UI, and one Edit must not unchain."""
        body = "notes\r\n\r\n<!-- handoff: v1 track=default round=2 previous=11 -->\r\n"
        self.assertEqual(
            read_marker(body, 12), {"track": "default", "round": "2", "previous": "11"}
        )

    def test_a_track_outside_the_slug_grammar_reads_as_no_block(self) -> None:
        """A lane no command can address must refuse rather than list as current."""
        for track in ("Lane", "lane_two", "lane.two", "-lane"):
            with self.subTest(track=track):
                body = f"<!-- handoff: v1 track={track} round=1 previous=none -->\n"
                self.assertIsNone(handoff.parse_marker(body, 1))

    def test_two_blocks_refuse_rather_than_pick(self) -> None:
        body = f"{handoff.render_marker('a', 1, None)}\n{handoff.render_marker('b', 2, None)}\n"
        with self.assertRaises(handoff.Refusal) as caught:
            handoff.parse_marker(body, 7)
        self.assertIn("#7", str(caught.exception))

    def test_a_retitled_issue_still_chains(self) -> None:
        """Nothing parses the title, which is what lets it carry a human subject."""
        row = link(20, "default", 2, 19, title="Something a maintainer renamed it to")
        self.assertEqual(read_marker(row["body"], 20)["track"], "default")


class LabelCase(unittest.TestCase):
    """A repository missing the label refuses rather than reporting an empty chain."""

    def test_a_missing_label_refuses_and_names_the_fix(self) -> None:
        code, _, err = run(FakeGh(label=False), "current", "--repo", "o/r")
        self.assertEqual(code, 1)
        self.assertIn("configure.sh apply", err)
        self.assertIn(handoff.LABEL, err)

    def test_a_full_label_window_refuses_rather_than_reporting_the_label_absent(self) -> None:
        """Otherwise the caller is sent to re-apply a set that may already be applied."""

        def crowded(argv):
            if argv[:2] == ["label", "list"]:
                return json.dumps([{"name": f"l{n}"} for n in range(handoff.WINDOW)])
            raise AssertionError("should not get past the label read")

        err = io.StringIO()
        with (
            unittest.mock.patch.object(handoff, "run_gh", crowded),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            code = handoff.main(["current", "--repo", "o/r"])
        self.assertEqual(code, 1)
        self.assertIn("fills the read window", err.getvalue())

    def test_the_label_is_checked_before_every_subcommand(self) -> None:
        """Every one of them, read off the parser, rather than the two that are easy to drive."""
        extra = {
            "resume": [],
            "chain": [],
            "new": ["--title", "T", "--body-file", body_file(self, "w")],
            "link": ["--new", "2", "--previous", "1"],
            "adopt": ["1", "--track", "lane"],
        }
        names = set(subcommands())
        self.assertGreaterEqual(len(names), 7)
        for cmd in sorted(names):
            with self.subTest(cmd=cmd):
                fake = FakeGh(label=False)
                argv = [cmd, *extra.get(cmd, [])]
                if cmd == "adopt":
                    argv = ["adopt", "1", "--track", "lane"]
                self.assertEqual(run(fake, *argv, "--repo", "o/r")[0], 1)
                self.assertEqual(fake.calls[0][:2], ["label", "list"])


class CurrentCase(unittest.TestCase):
    """Exactly one open handoff per track, and anything else is reported rather than resolved."""

    def test_no_open_handoff_is_a_refusal_naming_the_track(self) -> None:
        code, _, err = run(FakeGh(), "current", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 1)
        self.assertIn("'lane'", err)

    def test_two_on_one_track_refuses_and_names_both(self) -> None:
        fake = FakeGh({4: link(4, "lane", 1, None), 5: link(5, "lane", 2, 4)})
        code, _, err = run(fake, "current", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 1)
        self.assertIn("#4", err)
        self.assertIn("#5", err)

    def test_an_unadopted_open_handoff_refuses_and_names_adopt(self) -> None:
        fake = FakeGh({6: link(6, "lane", 1, None, body="hand-written, no block")})
        code, _, err = run(fake, "current", "--repo", "o/r")
        self.assertEqual(code, 1)
        self.assertIn("#6", err)
        self.assertIn("adopt", err)

    def test_one_on_the_track_prints_number_and_url(self) -> None:
        fake = FakeGh({8: link(8, "default", 4, 7)})
        code, out, _ = run(fake, "current", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("#8", out)
        self.assertIn("https://github.com/o/r/issues/8", out)
        self.assertIn("round=4", out)

    def test_a_sibling_track_does_not_answer_for_this_one(self) -> None:
        """Parallel lanes each close their own predecessor, so neither reads the other's state."""
        fake = FakeGh({9: link(9, "other", 1, None)})
        self.assertEqual(run(fake, "current", "--repo", "o/r", "--track", "lane")[0], 1)


class ExitCodeCase(unittest.TestCase):
    """A finding and the check not having run never share a code."""

    def test_an_execution_failure_is_two(self) -> None:
        def boom(argv):
            raise handoff.Execution("gh issue list failed rc=1")

        out, err = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(handoff, "run_gh", boom),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = handoff.main(["current", "--repo", "o/r"])
        self.assertEqual(code, 2)
        self.assertIn("failed", err.getvalue())

    def test_a_full_read_window_refuses_rather_than_undercounting(self) -> None:
        rows = {n: link(n, f"lane-{n}", 1, None) for n in range(1, handoff.WINDOW + 1)}
        code, _, err = run(FakeGh(rows), "tracks", "--repo", "o/r")
        self.assertEqual(code, 1)
        self.assertIn("window", err)

    def test_a_full_closed_window_refuses_only_where_no_link_was_found(self) -> None:
        """A closed link past the window reads as a track that never had one, which orphans it."""
        rows = {
            n: link(n, f"lane-{n}", 1, None, state="CLOSED")
            for n in range(1, handoff.CLOSED_WINDOW + 1)
        }
        code, _, err = run(FakeGh(rows), "chain", "--repo", "o/r", "--track", "absent-lane")
        self.assertEqual(code, 1)
        self.assertIn("fills the read window", err)
        self.assertIn("second chain beside the first", err)

    def test_a_full_closed_window_does_not_block_a_track_it_did_contain(self) -> None:
        """Closed links accumulate one per round, so refusing on a full page alone is a wall."""
        rows = {
            n: link(n, f"lane-{n}", 1, None, state="CLOSED")
            for n in range(1, handoff.CLOSED_WINDOW + 1)
        }
        code, out, _ = run(FakeGh(rows), "chain", "--repo", "o/r", "--track", "lane-7")
        self.assertEqual(code, 0)
        self.assertIn("#7", out)

    def test_a_usage_error_refuses_at_one_rather_than_sharing_argparse_s_two(self) -> None:
        """Two is the command not having run, so a mistyped flag must not land on it."""
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(err):
            handoff.main(["current", "--repo", "justaname"])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("refused:", err.getvalue())

    def test_a_below_one_argument_names_the_flag_the_caller_typed(self) -> None:
        """`new` and `previous` without their dashes name no flag the caller could have passed."""
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            handoff.main(["link", "--repo", "o/r", "--new", "0", "--previous", "1"])
        self.assertIn("--new takes an issue number", err.getvalue())

    def test_a_round_below_one_never_reaches_the_block(self) -> None:
        """`round=0` parses nowhere, so writing it labels an issue no read can ever see again."""
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            handoff.main(["adopt", "1", "--repo", "o/r", "--track", "lane", "--round", "0"])

    def test_a_history_or_limit_below_one_is_a_usage_error(self) -> None:
        for flag, cmd in (("--history", "resume"), ("--limit", "chain")):
            with (
                self.subTest(flag=flag),
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                handoff.main([cmd, "--repo", "o/r", flag, "0"])

    def test_a_three_part_repo_is_a_usage_error(self) -> None:
        """OWNER/NAME has one slash, and `o/r/x` is the near-miss a bare check still admits."""
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            handoff.main(["current", "--repo", "o/r/x"])

    def test_an_unknown_flag_refuses_at_one_too(self) -> None:
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            handoff.main(["current", "--repo", "o/r", "--nonsense"])
        self.assertEqual(caught.exception.code, 1)

    def test_an_unmodeled_exception_is_two_rather_than_cpython_s_one(self) -> None:
        """CPython exits 1 on an uncaught exception, which would read as a refusal."""

        def wrong_shape(argv):
            if argv[:2] == ["label", "list"]:
                return json.dumps([{"name": handoff.LABEL}])
            return json.dumps([{"no": "number key here"}])

        out, err = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(handoff, "run_gh", wrong_shape),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = handoff.main(["tracks", "--repo", "o/r"])
        self.assertEqual(code, 2)
        self.assertIn("unmodeled", err.getvalue())

    def test_a_track_that_is_not_a_slug_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            handoff.main(["current", "--repo", "o/r", "--track", "Not A Slug"])


class NewCase(unittest.TestCase):
    """Create, comment, close, in that order, so a failure leaves a discoverable successor."""

    def body_file(self, text: str) -> str:
        return body_file(self, text)

    def test_the_three_steps_run_in_order(self) -> None:
        fake = FakeGh({30: link(30, "default", 2, 29)})
        code, _, _ = run(
            fake, "new", "--repo", "o/r", "--title", "Next", "--body-file", self.body_file("work")
        )
        self.assertEqual(code, 0)
        writes = [
            c[:2]
            for c in fake.calls
            if c[:2] in (["issue", "create"], ["issue", "comment"], ["issue", "close"])
        ]
        self.assertEqual(writes, [["issue", "create"], ["issue", "comment"], ["issue", "close"]])
        self.assertEqual(fake.issues[30]["state"], "CLOSED")
        body = next(c for c in fake.calls if c[:2] == ["issue", "comment"])
        self.assertIn("Continued in #1001", body[body.index("--body") + 1])

    def test_the_round_increments_from_the_previous_link(self) -> None:
        fake = FakeGh({30: link(30, "default", 7, 29)})
        run(fake, "new", "--repo", "o/r", "--title", "Next", "--body-file", self.body_file("work"))
        self.assertEqual(read_marker(fake.issues[1001]["body"], 1001)["round"], "8")
        self.assertEqual(read_marker(fake.issues[1001]["body"], 1001)["previous"], "30")

    def test_a_closed_out_lane_chains_on_rather_than_starting_a_second_chain(self) -> None:
        """The orphan case: no open handoff on a track is not the same as no handoff at all.

        Reading the second as the first files round 1 with `previous=none` beside a chain that
        already exists, which is exactly the lost history the chain replaces a file to avoid.
        """
        fake = FakeGh({30: link(30, "default", 4, 29, state="CLOSED")})
        code, out, _ = run(
            fake, "new", "--repo", "o/r", "--title", "Next", "--body-file", self.body_file("work")
        )
        self.assertEqual(code, 0)
        marker = read_marker(fake.issues[1001]["body"], 1001)
        self.assertEqual(marker["round"], "5")
        self.assertEqual(marker["previous"], "30")
        self.assertIn("already closed", out)

    def test_a_closed_predecessor_is_linked_but_not_closed_again(self) -> None:
        fake = FakeGh({30: link(30, "default", 1, None, state="CLOSED")})
        run(fake, "new", "--repo", "o/r", "--title", "Next", "--body-file", self.body_file("work"))
        writes = [c[:2] for c in fake.calls]
        self.assertIn(["issue", "comment"], writes)
        self.assertNotIn(["issue", "close"], writes)

    def test_a_closed_lane_on_another_track_does_not_chain_this_one(self) -> None:
        fake = FakeGh({30: link(30, "other", 4, 29, state="CLOSED")})
        run(
            fake,
            "new",
            "--repo",
            "o/r",
            "--track",
            "lane",
            "--title",
            "First",
            "--body-file",
            self.body_file("work"),
        )
        marker = read_marker(fake.issues[1001]["body"], 1001)
        self.assertEqual(marker, {"track": "lane", "round": "1", "previous": "none"})

    def test_the_first_handoff_on_a_track_closes_nothing(self) -> None:
        fake = FakeGh()
        code, out, _ = run(
            fake, "new", "--repo", "o/r", "--title", "First", "--body-file", self.body_file("work")
        )
        self.assertEqual(code, 0)
        self.assertNotIn(["issue", "close"], [c[:2] for c in fake.calls])
        self.assertNotIn(["issue", "comment"], [c[:2] for c in fake.calls])
        self.assertIn("nothing to link or close", out)
        self.assertEqual(read_marker(fake.issues[1001]["body"], 1001)["previous"], "none")

    def test_the_new_issue_carries_the_label(self) -> None:
        """An unlabeled handoff is invisible to every read this script makes."""
        fake = FakeGh()
        run(fake, "new", "--repo", "o/r", "--title", "First", "--body-file", self.body_file("w"))
        self.assertEqual(fake.issues[1001]["labels"], [{"name": handoff.LABEL}])

    def test_the_title_carries_the_track_and_the_subject(self) -> None:
        fake = FakeGh()
        run(
            fake,
            "new",
            "--repo",
            "o/r",
            "--track",
            "lane",
            "--title",
            "A subject",
            "--body-file",
            self.body_file("work"),
        )
        self.assertEqual(fake.issues[1001]["title"], "Session Handoff [lane]: A subject")

    def test_a_body_over_the_hard_cap_refuses(self) -> None:
        fake = FakeGh()
        code, _, err = run(
            fake,
            "new",
            "--repo",
            "o/r",
            "--title",
            "Big",
            "--body-file",
            self.body_file("x" * (handoff.MAX_BYTES + 1)),
        )
        self.assertEqual(code, 1)
        self.assertIn(str(handoff.MAX_BYTES), err)
        self.assertNotIn(["issue", "create"], [c[:2] for c in fake.calls])

    def test_a_body_over_the_guidance_warns_and_still_files(self) -> None:
        fake = FakeGh()
        code, _, err = run(
            fake,
            "new",
            "--repo",
            "o/r",
            "--title",
            "Padded",
            "--body-file",
            self.body_file("x" * (handoff.WARN_BYTES + 1)),
        )
        self.assertEqual(code, 0)
        self.assertIn("warning:", err)

    def test_a_body_carrying_its_own_block_refuses(self) -> None:
        fake = FakeGh()
        code, _, err = run(
            fake,
            "new",
            "--repo",
            "o/r",
            "--title",
            "Doubled",
            "--body-file",
            self.body_file(marked("work", "default", 1, None)),
        )
        self.assertEqual(code, 1)
        self.assertIn("metadata block", err)

    def test_dry_run_writes_nothing(self) -> None:
        fake = FakeGh({30: link(30, "default", 2, 29)})
        code, out, _ = run(
            fake,
            "new",
            "--repo",
            "o/r",
            "--dry-run",
            "--title",
            "Next",
            "--body-file",
            self.body_file("work"),
        )
        self.assertEqual(code, 0)
        self.assertIn("would run: gh issue create", out)
        self.assertEqual(fake.issues[30]["state"], "OPEN")
        self.assertNotIn(1001, fake.issues)
        self.assertIn("<the new issue>", out)
        self.assertNotIn("#0", out)
        self.assertIn("--body-file <body-file>", out)

    def test_an_ambiguous_track_refuses_before_any_write(self) -> None:
        fake = FakeGh({4: link(4, "default", 1, None), 5: link(5, "default", 2, 4)})
        code, _, err = run(
            fake, "new", "--repo", "o/r", "--title", "Next", "--body-file", self.body_file("work")
        )
        self.assertEqual(code, 1)
        self.assertIn("#4", err)
        self.assertNotIn(["issue", "create"], [c[:2] for c in fake.calls])


class ChainCase(unittest.TestCase):
    """Walking `previous=` is how a session answers whether a path has already been tried."""

    def repo(self) -> FakeGh:
        return FakeGh(
            {
                11: link(
                    11,
                    "default",
                    1,
                    None,
                    state="CLOSED",
                    body=marked("tried the vendored copy, it went stale", "default", 1, None),
                ),
                12: link(
                    12,
                    "default",
                    2,
                    11,
                    state="CLOSED",
                    body=marked("ordinary round", "default", 2, 11),
                ),
                13: link(13, "default", 3, 12, body=marked("current round", "default", 3, 12)),
            }
        )

    def test_the_walk_reaches_every_link(self) -> None:
        code, out, _ = run(self.repo(), "chain", "--repo", "o/r")
        self.assertEqual(code, 0)
        for number in ("#11", "#12", "#13"):
            self.assertIn(number, out)

    def test_each_link_prints_its_own_track(self) -> None:
        """A `previous=` pointing at another lane is visible rather than silently walked past."""
        fake = self.repo()
        fake.issues[11]["body"] = marked("a stray lane", "other", 1, None)
        code, out, _ = run(fake, "chain", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("other round 1", out)
        self.assertIn("default round 3", out)

    def test_grep_finds_a_string_only_in_a_closed_link_two_back(self) -> None:
        """The repeated-work failure this whole mechanism exists to fix, demonstrated."""
        code, out, _ = run(self.repo(), "chain", "--repo", "o/r", "--grep", "vendored copy")
        self.assertEqual(code, 0)
        self.assertIn("#11", out)
        self.assertNotIn("#12", out)
        self.assertIn("1 of 3 links walked match", out)

    def test_a_capped_walk_says_it_stopped_short(self) -> None:
        """A truncated search reading like an exhaustive one is the false "not tried yet"."""
        code, out, _ = run(self.repo(), "chain", "--repo", "o/r", "--limit", "2")
        self.assertEqual(code, 0)
        self.assertIn("--limit cap of 2", out)
        self.assertIn("not a search over the chain", out)

    def test_each_subcommand_names_its_own_cap_flag(self) -> None:
        """`resume` caps with --history and rejects --limit, so naming --limit misdirects."""
        fake = FakeGh(
            {
                11: link(11, "default", 1, None, state="CLOSED"),
                12: link(12, "default", 2, 11, state="CLOSED"),
                13: link(13, "default", 3, 12),
            }
        )
        code, out, _ = run(fake, "resume", "--repo", "o/r", "--history", "1")
        self.assertEqual(code, 0)
        self.assertIn("--history cap of 1", out)
        self.assertNotIn("--limit", out)

    def test_an_exhaustive_walk_says_nothing_about_stopping(self) -> None:
        code, out, _ = run(self.repo(), "chain", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertNotIn("the walk stopped at", out)

    def test_a_link_with_no_block_ends_the_walk_and_says_so(self) -> None:
        """The cap is not the only early end, and the other one used to return silence."""
        fake = self.repo()
        fake.issues[12]["body"] = "a link somebody edited the block out of"
        code, out, _ = run(fake, "chain", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("carries no metadata block", out)
        self.assertIn("the walk stopped at", out)

    def test_grep_filters_the_listing_and_never_the_notices(self) -> None:
        """A stop the filter hid is a stopped search that reads as an exhaustive one."""
        fake = self.repo()
        fake.issues[12]["body"] = "a link somebody edited the block out of"
        code, out, _ = run(fake, "chain", "--repo", "o/r", "--grep", "current round")
        self.assertEqual(code, 0)
        self.assertNotIn("#12  ", out)
        self.assertIn("the walk stopped at", out)

    def test_a_walk_that_left_the_track_says_so_even_under_grep(self) -> None:
        fake = self.repo()
        fake.issues[11]["body"] = marked("a stray lane", "other", 1, None)
        code, out, _ = run(fake, "chain", "--repo", "o/r", "--grep", "current round")
        self.assertEqual(code, 0)
        self.assertIn("left track 'default'", out)
        self.assertIn("#11 on other", out)

    def test_a_cycle_ends_the_walk_and_says_so(self) -> None:
        fake = FakeGh(
            {
                21: link(21, "default", 1, 22),
                22: link(22, "default", 2, 21, state="CLOSED"),
            }
        )
        code, out, _ = run(fake, "chain", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("a cycle back to", out)

    def test_an_invalid_pattern_refuses_rather_than_matching_nothing(self) -> None:
        code, _, err = run(self.repo(), "chain", "--repo", "o/r", "--grep", "(unclosed")
        self.assertEqual(code, 1)
        self.assertIn("--grep", err)

    def test_a_closed_out_lane_still_walks_from_its_newest_link(self) -> None:
        fake = self.repo()
        fake.issues[13]["state"] = "CLOSED"
        code, out, _ = run(fake, "chain", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("#13", out)

    def test_a_track_with_no_link_at_all_refuses(self) -> None:
        code, _, err = run(self.repo(), "chain", "--repo", "o/r", "--track", "absent")
        self.assertEqual(code, 1)
        self.assertIn("absent", err)


class ResumeCase(unittest.TestCase):
    """The read side of the repeated-work problem: the body, then what came before it."""

    def test_the_body_and_the_history_both_print(self) -> None:
        fake = FakeGh(
            {
                11: link(11, "default", 1, None, state="CLOSED"),
                12: link(12, "default", 2, 11),
            }
        )
        code, out, _ = run(fake, "resume", "--repo", "o/r", "--history", "3")
        self.assertEqual(code, 0)
        self.assertIn("body of round 2", out)
        self.assertIn("#11", out)

    def test_a_first_round_says_so_rather_than_printing_an_empty_index(self) -> None:
        code, out, _ = run(FakeGh({12: link(12, "default", 1, None)}), "resume", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("first handoff on this track", out)


class TracksCase(unittest.TestCase):
    """An abandoned lane is visible rather than silently current."""

    def test_every_open_handoff_is_listed_with_its_track(self) -> None:
        fake = FakeGh({4: link(4, "lane-a", 1, None), 5: link(5, "lane-b", 1, None)})
        code, out, _ = run(fake, "tracks", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("lane-a", out)
        self.assertIn("lane-b", out)
        self.assertIn("updated", out)

    def test_the_open_read_is_filtered_to_the_label(self) -> None:
        """Without the filter every open issue in the repository reads as a handoff."""
        fake = FakeGh(
            {
                4: link(4, "lane", 1, None),
                5: link(5, "lane", 1, None, labels=[], body="an ordinary issue"),
            }
        )
        code, out, _ = run(fake, "tracks", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("#4", out)
        self.assertNotIn("#5", out)

    def test_an_unadopted_issue_is_surveyed_rather_than_refused(self) -> None:
        """`tracks` is the survey, so it reports what every other subcommand refuses over."""
        fake = FakeGh({6: link(6, "lane", 1, None, body="hand-written")})
        code, out, _ = run(fake, "tracks", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("(unadopted)", out)

    def test_an_empty_repository_says_so(self) -> None:
        code, out, _ = run(FakeGh(), "tracks", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("no open", out)


class GhBoundaryCase(unittest.TestCase):
    """`run_gh` and the write confirmations, which patching `run_gh` wholesale never executes.

    Every other case here replaces `run_gh`, so its own body, and the confirmations the write
    helpers make on top of it, ran in no test at all. Four guards could be deleted outright with
    the suite green, one of them the close read-back `GOVERNANCE.md` "Repository Boundaries and
    Write Safety" requires.
    """

    def completed(self, code: int, out: str = "", err: str = "") -> object:
        return subprocess.CompletedProcess(args=["gh"], returncode=code, stdout=out, stderr=err)

    def test_a_nonzero_gh_exit_raises_rather_than_returning_a_blank(self) -> None:
        err = io.StringIO()
        with (
            unittest.mock.patch(
                "subprocess.run", return_value=self.completed(1, "", "gh: bad credentials")
            ),
            contextlib.redirect_stderr(err),
            self.assertRaises(handoff.Execution),
        ):
            handoff.run_gh(["issue", "list"])
        self.assertIn("gh: bad credentials", err.getvalue())

    def test_a_long_gh_error_says_how_much_it_left(self) -> None:
        long = "x" * (handoff.STDERR_CAP + 50)
        err = io.StringIO()
        with (
            unittest.mock.patch("subprocess.run", return_value=self.completed(1, "", long)),
            contextlib.redirect_stderr(err),
            self.assertRaises(handoff.Execution),
        ):
            handoff.run_gh(["issue", "list"])
        self.assertIn("50 more character(s) not shown", err.getvalue())
        self.assertTrue(err.getvalue().endswith("\n"))

    def test_gh_missing_from_the_path_is_an_execution_failure(self) -> None:
        with (
            unittest.mock.patch("subprocess.run", side_effect=OSError("no gh")),
            self.assertRaises(handoff.Execution) as caught,
        ):
            handoff.run_gh(["issue", "list"])
        self.assertIn("could not run gh", str(caught.exception))

    def test_output_that_is_not_json_raises_rather_than_reading_as_empty(self) -> None:
        """A degraded empty answer is the failure the label refusal exists to prevent."""
        with (
            unittest.mock.patch.object(handoff, "run_gh", lambda argv: "not json"),
            self.assertRaises(handoff.Execution) as caught,
        ):
            handoff.gh_json(["issue", "list"])
        self.assertIn("not JSON", str(caught.exception))

    def test_a_comment_returning_no_url_is_not_taken_as_posted(self) -> None:
        with (
            unittest.mock.patch.object(handoff, "run_gh", lambda argv: ""),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(handoff.Execution) as caught,
        ):
            handoff.comment("o/r", 5, "body", dry_run=False)
        self.assertIn("nothing confirms it", str(caught.exception))

    def test_a_close_that_did_not_take_is_reported_rather_than_assumed(self) -> None:
        """A write that appears to have failed is verified, never assumed harmless."""
        fake = FakeGh({5: link(5, "default", 1, None)})
        fake.refuse_close = True
        with (
            unittest.mock.patch.object(handoff, "run_gh", fake),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(handoff.Execution) as caught,
        ):
            handoff.close("o/r", 5, dry_run=False)
        self.assertIn("not confirmed", str(caught.exception))

    def test_a_label_list_that_is_not_an_array_is_an_execution_failure(self) -> None:
        """Reading a non-array as empty would report the label absent, the degraded answer."""
        with (
            unittest.mock.patch.object(handoff, "run_gh", lambda argv: '{"not": "an array"}'),
            self.assertRaises(handoff.Execution) as caught,
        ):
            handoff.require_label("o/r")
        self.assertIn("did not read as an array", str(caught.exception))

    def test_an_unreadable_body_file_refuses_rather_than_filing_an_empty_handoff(self) -> None:
        code, _, err = run(
            FakeGh(), "new", "--repo", "o/r", "--title", "T", "--body-file", "/no/such/path.md"
        )
        self.assertEqual(code, 1)
        self.assertIn("could not read the body file", err)

    def test_the_create_url_is_read_from_the_end_of_the_output(self) -> None:
        """An unanchored match would take an issue number out of any URL gh happened to print."""
        noise = (
            "see https://github.com/o/r/issues/99 for context\nhttps://github.com/o/r/issues/7\n"
        )
        with (
            unittest.mock.patch.object(handoff, "run_gh", lambda argv: noise),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(handoff.create("o/r", "t", "b", dry_run=False), 7)

    def test_a_long_gh_error_is_actually_cut(self) -> None:
        """Asserting only the notice lets an uncut dump carrying that notice pass."""
        long = "x" * (handoff.STDERR_CAP + 50)
        err = io.StringIO()
        with (
            unittest.mock.patch("subprocess.run", return_value=self.completed(1, "", long)),
            contextlib.redirect_stderr(err),
            self.assertRaises(handoff.Execution),
        ):
            handoff.run_gh(["issue", "list"])
        self.assertEqual(err.getvalue().count("x"), handoff.STDERR_CAP)

    def test_gh_output_is_decoded_as_utf8_rather_than_the_platform_locale(self) -> None:
        """A Windows console locale is cp1252, and `gh` emits UTF-8 on every platform."""
        with unittest.mock.patch("subprocess.run") as ran:
            ran.return_value = self.completed(0, "ok")
            handoff.run_gh(["issue", "list", "--repo", "o/r"])
        self.assertEqual(ran.call_args.kwargs["encoding"], "utf-8")

    def test_a_body_file_is_written_with_lf_whatever_the_platform_does(self) -> None:
        """Text mode translates LF to CRLF on Windows, and the marker would stop parsing.

        The newline argument is asserted rather than the bytes, because on Linux the two are
        identical and the check would pass without the argument on the only platform CI runs.
        """
        with (
            unittest.mock.patch.object(Path, "write_text") as wrote,
            handoff.body_arg("one\ntwo\n", dry_run=False),
        ):
            pass
        self.assertEqual(wrote.call_args.kwargs.get("newline"), "\n")
        with handoff.body_arg("one\ntwo\n", dry_run=False) as path:
            self.assertEqual(Path(path).read_bytes(), b"one\ntwo\n")

    def test_a_view_that_is_not_an_object_is_an_execution_failure(self) -> None:
        with (
            unittest.mock.patch.object(handoff, "run_gh", lambda argv: "[]"),
            self.assertRaises(handoff.Execution) as caught,
        ):
            handoff.issue("o/r", 5)
        self.assertIn("did not read as an object", str(caught.exception))

    def test_a_list_that_is_not_an_array_is_an_execution_failure(self) -> None:
        for reader, phrase in (
            (handoff.open_handoffs, "handoff list"),
            (lambda repo: handoff.newest_closed(repo, "lane"), "closed handoff list"),
        ):
            with self.subTest(phrase=phrase):
                with (
                    unittest.mock.patch.object(handoff, "run_gh", lambda argv: '{"a": 1}'),
                    self.assertRaises(handoff.Execution) as caught,
                ):
                    reader("o/r")
                self.assertIn(phrase, str(caught.exception))

    def test_a_create_returning_no_url_is_not_taken_as_filed(self) -> None:
        with (
            unittest.mock.patch.object(handoff, "run_gh", lambda argv: "queued\n"),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(handoff.Execution) as caught,
        ):
            handoff.create("o/r", "t", "b", dry_run=False)
        self.assertIn("no issue URL", str(caught.exception))


class MalformedCase(unittest.TestCase):
    """One unreadable block must not take down the lanes that have nothing to do with it."""

    def test_tracks_surveys_a_doubled_block_rather_than_dying_on_it(self) -> None:
        fake = FakeGh({50: link(50, "foo", 1, None, body=doubled("foo"))})
        code, out, _ = run(fake, "tracks", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("(malformed)", out)

    def test_a_reader_refuses_over_a_doubled_block_rather_than_guessing(self) -> None:
        fake = FakeGh({50: link(50, "foo", 1, None, body=doubled("foo"))})
        code, _, err = run(fake, "current", "--repo", "o/r", "--track", "foo")
        self.assertEqual(code, 1)
        self.assertIn("2 handoff metadata blocks", err)

    def test_a_doubled_closed_link_does_not_refuse_a_track_that_was_found(self) -> None:
        """`newest_closed` scans every lane, so one bad link must not block the others."""
        fake = FakeGh(
            {
                50: link(50, "foo", 1, None, state="CLOSED", body=doubled("foo")),
                51: link(51, "bar", 4, None, state="CLOSED"),
            }
        )
        code, out, _ = run(fake, "chain", "--repo", "o/r", "--track", "bar")
        self.assertEqual(code, 0)
        self.assertIn("#51", out)

    def test_an_unreadable_link_newer_than_the_match_refuses_rather_than_forking(self) -> None:
        """The scan runs newest first, so anything skipped before a match is newer than it."""
        fake = FakeGh(
            {
                59: link(59, "default", 3, None, state="CLOSED"),
                60: link(60, "default", 4, None, state="CLOSED", body=doubled("default")),
            }
        )
        code, _, err = run(
            fake,
            "new",
            "--repo",
            "o/r",
            "--title",
            "Next",
            "--body-file",
            body_file(self, "work"),
        )
        self.assertEqual(code, 1)
        self.assertIn("would fork the chain", err)
        self.assertNotIn(1001, fake.issues)

    def test_a_bare_closed_link_newer_than_the_match_refuses_too(self) -> None:
        """A block absent and a block unreadable are the same hazard, so both must refuse."""
        fake = FakeGh(
            {
                59: link(59, "default", 3, None, state="CLOSED"),
                60: link(60, "default", 4, None, state="CLOSED", body="block edited out"),
            }
        )
        code, _, err = run(
            fake, "new", "--repo", "o/r", "--title", "Next", "--body-file", body_file(self, "w")
        )
        self.assertEqual(code, 1)
        self.assertIn("would fork the chain", err)
        self.assertNotIn(1001, fake.issues)

    def test_a_doubled_closed_link_refuses_only_where_the_track_was_not_found(self) -> None:
        fake = FakeGh({50: link(50, "foo", 1, None, state="CLOSED", body=doubled("foo"))})
        code, _, err = run(fake, "chain", "--repo", "o/r", "--track", "bar")
        self.assertEqual(code, 1)
        self.assertIn("could be one of those", err)

    def test_a_doubled_block_mid_chain_ends_the_walk_with_a_phrase(self) -> None:
        """`walk` promises a stop phrase for every early end, so this is not an exception."""
        fake = FakeGh(
            {
                60: link(60, "default", 1, None, state="CLOSED", body=doubled("default")),
                61: link(61, "default", 2, 60),
            }
        )
        code, out, _ = run(fake, "chain", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("#61", out)
        self.assertIn("cannot be read", out)

    def test_an_ancestor_that_cannot_be_read_ends_the_walk_with_a_phrase(self) -> None:
        """The fifth early end, which used to discard every link already walked."""
        fake = FakeGh({61: link(61, "default", 2, 60)})
        code, out, _ = run(fake, "chain", "--repo", "o/r")
        self.assertEqual(code, 0)
        self.assertIn("#61", out)
        self.assertIn("could not be read", out)

    def test_a_zero_predecessor_is_not_a_block_at_all(self) -> None:
        """Zero is no issue number, and reaching gh with it exits 2 for a fixable block."""
        self.assertIsNone(
            handoff.parse_marker("<!-- handoff: v1 track=a round=1 previous=0 -->\n", 1)
        )
        self.assertIsNone(
            handoff.parse_marker("<!-- handoff: v1 track=a round=0 previous=none -->\n", 1)
        )


class LinkCase(unittest.TestCase):
    """A half-applied chain finishes without a second issue being filed for it."""

    def test_it_points_the_successor_and_closes_the_predecessor(self) -> None:
        fake = FakeGh(
            {
                20: link(20, "default", 1, None),
                21: link(21, "default", 2, None, body=marked("orphan", "default", 2, None)),
            }
        )
        code, _, _ = run(fake, "link", "--repo", "o/r", "--new", "21", "--previous", "20")
        self.assertEqual(code, 0)
        self.assertEqual(read_marker(fake.issues[21]["body"], 21)["previous"], "20")
        self.assertEqual(fake.issues[20]["state"], "CLOSED")

    def test_an_already_linked_pair_only_finishes_what_is_left(self) -> None:
        fake = FakeGh({20: link(20, "default", 1, None), 21: link(21, "default", 2, 20)})
        code, out, _ = run(fake, "link", "--repo", "o/r", "--new", "21", "--previous", "20")
        self.assertEqual(code, 0)
        self.assertIn("nothing to edit", out)
        self.assertEqual(fake.issues[20]["state"], "CLOSED")

    def test_an_issue_cannot_succeed_itself(self) -> None:
        """Otherwise the track's only open link closes into a cycle and the run exits 0."""
        fake = FakeGh({21: link(21, "default", 2, None)})
        code, _, err = run(fake, "link", "--repo", "o/r", "--new", "21", "--previous", "21")
        self.assertEqual(code, 1)
        self.assertIn("cannot succeed itself", err)
        self.assertEqual(fake.issues[21]["state"], "OPEN")

    def test_linking_across_tracks_refuses_rather_than_closing_another_lane(self) -> None:
        """`link` takes no --track, so the two blocks are all that can say they are one lane."""
        fake = FakeGh(
            {
                20: link(20, "other", 1, None),
                21: link(21, "default", 2, None, body=marked("orphan", "default", 2, None)),
            }
        )
        code, _, err = run(fake, "link", "--repo", "o/r", "--new", "21", "--previous", "20")
        self.assertEqual(code, 1)
        self.assertIn("'other'", err)
        self.assertEqual(fake.issues[20]["state"], "OPEN")

    def test_a_predecessor_with_no_block_refuses(self) -> None:
        fake = FakeGh(
            {
                20: link(20, "default", 1, None, body="hand-written"),
                21: link(21, "default", 2, None, body=marked("orphan", "default", 2, None)),
            }
        )
        code, _, err = run(fake, "link", "--repo", "o/r", "--new", "21", "--previous", "20")
        self.assertEqual(code, 1)
        self.assertIn("adopt", err)

    def test_a_transposed_pair_refuses_rather_than_closing_the_newer_link(self) -> None:
        """The ordinary slip under the one condition `link` exists for."""
        fake = FakeGh({20: link(20, "default", 1, None), 21: link(21, "default", 2, 20)})
        code, _, err = run(fake, "link", "--repo", "o/r", "--new", "20", "--previous", "21")
        self.assertEqual(code, 1)
        self.assertIn("transposed", err)
        self.assertEqual(fake.issues[21]["state"], "OPEN")

    def test_a_predecessor_already_naming_the_successor_refuses(self) -> None:
        fake = FakeGh(
            {
                20: link(20, "default", 3, 21),
                21: link(21, "default", 9, None, body=marked("orphan", "default", 9, None)),
            }
        )
        code, _, err = run(fake, "link", "--repo", "o/r", "--new", "21", "--previous", "20")
        self.assertEqual(code, 1)
        self.assertIn("cycle", err)

    def test_a_different_predecessor_refuses_rather_than_overwriting(self) -> None:
        fake = FakeGh({20: link(20, "default", 1, None), 21: link(21, "default", 2, 19)})
        code, _, err = run(fake, "link", "--repo", "o/r", "--new", "21", "--previous", "20")
        self.assertEqual(code, 1)
        self.assertIn("#19", err)

    def test_a_successor_with_no_block_refuses(self) -> None:
        fake = FakeGh(
            {
                20: link(20, "default", 1, None),
                21: link(21, "default", 2, None, body="no block"),
            }
        )
        code, _, err = run(fake, "link", "--repo", "o/r", "--new", "21", "--previous", "20")
        self.assertEqual(code, 1)
        self.assertIn("adopt", err)

    def test_a_closed_predecessor_still_gets_the_forward_comment(self) -> None:
        """`new` comments before it tests the state, so the recovery path has to as well.

        Testing first made the one write `link` exists to finish the one write it could not make.
        """
        fake = FakeGh(
            {
                20: link(20, "default", 1, None, state="CLOSED"),
                21: link(21, "default", 2, 20),
            }
        )
        code, out, _ = run(fake, "link", "--repo", "o/r", "--new", "21", "--previous", "20")
        self.assertEqual(code, 0)
        self.assertIn("already closed", out)
        posted = [c for c in fake.calls if c[:2] == ["issue", "comment"]]
        self.assertEqual(len(posted), 1)
        self.assertIn("Continued in #21", posted[0][posted[0].index("--body") + 1])


class AdoptCase(unittest.TestCase):
    """A hand-written handoff predating both the label and the block joins the chain."""

    def test_it_adds_the_label_and_the_block(self) -> None:
        fake = FakeGh({40: link(40, "default", 1, None, body="hand-written", labels=[])})
        code, _, _ = run(fake, "adopt", "40", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 0)
        self.assertIn({"name": handoff.LABEL}, fake.issues[40]["labels"])
        marker = read_marker(fake.issues[40]["body"], 40)
        self.assertEqual(marker, {"track": "lane", "round": "1", "previous": "none"})
        self.assertIn("hand-written", fake.issues[40]["body"])

    def test_an_issue_already_carrying_the_label_only_gains_the_block(self) -> None:
        fake = FakeGh({40: link(40, "default", 1, None, body="hand-written")})
        code, out, _ = run(fake, "adopt", "40", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 0)
        self.assertIn("already carries", out)
        self.assertNotIn("--add-label", [arg for call in fake.calls for arg in call])

    def test_a_round_and_a_predecessor_can_be_stated(self) -> None:
        fake = FakeGh(
            {
                39: link(39, "lane", 2, None, state="CLOSED"),
                40: link(40, "default", 1, None, body="hand-written"),
            }
        )
        run(
            fake,
            "adopt",
            "40",
            "--repo",
            "o/r",
            "--track",
            "lane",
            "--round",
            "3",
            "--previous",
            "39",
        )
        marker = read_marker(fake.issues[40]["body"], 40)
        self.assertEqual(marker, {"track": "lane", "round": "3", "previous": "39"})

    def test_the_named_predecessor_is_read_before_its_number_is_stamped(self) -> None:
        """A number nothing read is one the caller constructed, whatever the write does with it."""
        fake = FakeGh(
            {
                39: link(39, "lane", 2, None, state="CLOSED"),
                40: link(40, "default", 1, None, body="hand-written", labels=[]),
            }
        )
        run(
            fake,
            "adopt",
            "40",
            "--repo",
            "o/r",
            "--track",
            "lane",
            "--round",
            "3",
            "--previous",
            "39",
        )
        views = [c[2] for c in fake.calls if c[:2] == ["issue", "view"]]
        self.assertIn("39", views)

    def test_a_predecessor_that_does_not_resolve_stops_before_every_write(self) -> None:
        """The fixture carries no label, so the label write is a write this can actually rule out.

        With it, an ordering that labelled first left #40 labeled and block-less, and
        `require_adopted` then refused every read on every track in the repository.
        """
        fake = FakeGh({40: link(40, "default", 1, None, body="hand-written", labels=[])})
        code, _, _ = run(
            fake, "adopt", "40", "--repo", "o/r", "--track", "lane", "--previous", "999"
        )
        self.assertEqual(code, 2)
        flat = [arg for call in fake.calls for arg in call]
        self.assertNotIn("--body-file", flat)
        self.assertNotIn("--add-label", flat)
        self.assertEqual(fake.issues[40]["labels"], [])

    def test_adopting_onto_an_occupied_track_refuses(self) -> None:
        """`adopt` takes its track from a human, so it is the likeliest source of an ambiguity."""
        fake = FakeGh(
            {
                30: link(30, "lane", 1, None),
                40: link(40, "default", 1, None, body="hand-written"),
            }
        )
        code, _, err = run(fake, "adopt", "40", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 1)
        self.assertIn("#30", err)
        self.assertNotIn("--body-file", [arg for call in fake.calls for arg in call])

    def test_adopt_writes_the_block_before_the_label(self) -> None:
        """A failure between them then leaves an unlabeled issue with a block, blocking nothing.

        The other order leaves a labeled issue with no block, and `require_adopted` refuses every
        read on every track in the repository until someone re-runs this.
        """
        fake = FakeGh({40: link(40, "default", 1, None, body="hand-written", labels=[])})
        code, _, _ = run(fake, "adopt", "40", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 0)
        edits = [c for c in fake.calls if c[:2] == ["issue", "edit"]]
        flags = ["--body-file" if "--body-file" in c else "--add-label" for c in edits]
        self.assertEqual(flags, ["--body-file", "--add-label"])

    def test_adopt_refuses_while_an_open_link_has_an_unreadable_block(self) -> None:
        """`on_track` reads only parsed rows, so a malformed one is invisible to the guard."""
        fake = FakeGh(
            {
                30: link(30, "lane", 1, None, body=doubled("lane")),
                40: link(40, "default", 1, None, body="hand-written", labels=[]),
            }
        )
        code, _, err = run(fake, "adopt", "40", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 1)
        self.assertIn("#30", err)
        self.assertNotIn("--body-file", [arg for call in fake.calls for arg in call])

    def test_a_half_applied_adopt_finishes_on_a_re_run(self) -> None:
        """Body-first is only safe if the label write can still be made afterwards.

        Refusing the re-run left an issue no label-filtered read can see, which the next `new`
        then forks the chain around in silence.
        """
        fake = FakeGh(
            {40: link(40, "lane", 1, None, body=marked("hand-written", "lane", 1, None), labels=[])}
        )
        code, out, _ = run(fake, "adopt", "40", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 0)
        self.assertIn("only the label is left", out)
        self.assertIn({"name": handoff.LABEL}, fake.issues[40]["labels"])

    def test_the_recovery_still_refuses_a_lane_that_has_since_filled(self) -> None:
        """A lane can acquire an open link between a half-applied run and the re-run.

        Finishing regardless converts the silent fork into two open handoffs on one track.
        """
        fake = FakeGh(
            {
                40: link(
                    40, "lane", 1, None, body=marked("half applied", "lane", 1, None), labels=[]
                ),
                50: link(50, "lane", 1, None),
            }
        )
        code, _, err = run(fake, "adopt", "40", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 1)
        self.assertIn("#50", err)
        self.assertEqual(fake.issues[40]["labels"], [])

    def test_a_block_that_is_not_this_run_s_still_refuses(self) -> None:
        fake = FakeGh(
            {
                40: link(
                    40, "lane", 9, None, body=marked("someone else's", "lane", 9, None), labels=[]
                )
            }
        )
        code, _, err = run(fake, "adopt", "40", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 1)
        self.assertIn("nothing to adopt", err)

    def test_adopt_numbers_its_steps_from_one(self) -> None:
        """A run opening at step 2 reads as one that was interrupted."""
        fake = FakeGh({40: link(40, "default", 1, None, body="hand-written", labels=[])})
        code, out, _ = run(fake, "adopt", "40", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("1. "), out.splitlines()[0])

    def test_adopting_a_closed_issue_ignores_the_open_invariant(self) -> None:
        """A closed link joins a lane's history rather than competing to be its current one."""
        fake = FakeGh(
            {
                30: link(30, "lane", 2, None),
                40: link(40, "default", 1, None, body="hand-written", state="CLOSED"),
            }
        )
        code, _, _ = run(fake, "adopt", "40", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 0)
        self.assertEqual(read_marker(fake.issues[40]["body"], 40)["track"], "lane")

    def test_adopt_refuses_an_issue_named_as_its_own_predecessor(self) -> None:
        fake = FakeGh({40: link(40, "default", 1, None, body="hand-written")})
        code, _, err = run(
            fake, "adopt", "40", "--repo", "o/r", "--track", "lane", "--previous", "40"
        )
        self.assertEqual(code, 1)
        self.assertIn("cannot succeed itself", err)

    def test_adopt_refuses_a_cross_track_predecessor(self) -> None:
        fake = FakeGh(
            {
                39: link(39, "other", 1, None, state="CLOSED"),
                40: link(40, "default", 1, None, body="hand-written"),
            }
        )
        code, _, err = run(
            fake, "adopt", "40", "--repo", "o/r", "--track", "lane", "--previous", "39"
        )
        self.assertEqual(code, 1)
        self.assertIn("'other'", err)

    def test_adopt_refuses_a_predecessor_it_would_not_succeed(self) -> None:
        fake = FakeGh(
            {
                39: link(39, "lane", 9, None, state="CLOSED"),
                40: link(40, "default", 1, None, body="hand-written"),
            }
        )
        code, _, err = run(
            fake,
            "adopt",
            "40",
            "--repo",
            "o/r",
            "--track",
            "lane",
            "--round",
            "2",
            "--previous",
            "39",
        )
        self.assertEqual(code, 1)
        self.assertIn("would not succeed it", err)

    def test_an_already_adopted_issue_refuses(self) -> None:
        fake = FakeGh({40: link(40, "default", 1, None)})
        code, _, err = run(fake, "adopt", "40", "--repo", "o/r", "--track", "lane")
        self.assertEqual(code, 1)
        self.assertIn("already carries", err)

    def test_adopt_requires_a_track_rather_than_defaulting(self) -> None:
        """A lane named by default is a lane nobody chose, and `adopt` runs once per chain."""
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            handoff.main(["adopt", "40", "--repo", "o/r"])


class SurfaceCase(unittest.TestCase):
    """A floor on what a healthy run reaches, since a shrunken table reports what a whole one does."""

    def test_every_subcommand_has_a_handler(self) -> None:
        declared = set(subcommands())
        self.assertEqual(declared, set(handoff.HANDLERS))
        self.assertGreaterEqual(len(declared), 7)

    def test_the_writing_subcommands_all_take_a_dry_run(self) -> None:
        choices = subcommands()
        for name in ("new", "link", "adopt"):
            flags = {s for action in choices[name]._actions for s in action.option_strings}
            self.assertIn("--dry-run", flags, f"{name} can write and takes no --dry-run")

    def test_every_subcommand_requires_a_repo(self) -> None:
        for name, sub in subcommands().items():
            flags = {s for action in sub._actions for s in action.option_strings}
            self.assertIn("--repo", flags, f"{name} takes no --repo, so it resolves anywhere")


if __name__ == "__main__":
    unittest.main(verbosity=2)
