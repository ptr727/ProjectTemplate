#!/usr/bin/env python3
"""Drive the review policy's reader and the gate's use of it against constructed policies.

The policy decides which bots a pull request waits on and which ones `wait` asks. A wrong reading
either asks a paid reviewer for a round nobody wanted or merges a head nobody reviewed. So each
case below pins one decision. The policy-free payloads the rest of the suite builds must keep the
behavior the gate had before policies existed, which the neutrality cases here hold.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import functools
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import time
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_review
import pr_review_payload as payload
import pr_review_policy as policy
import pr_review_rounds as rounds
import pr_review_side as side
import test_pr_review as fixtures

REPO = Path(__file__).resolve().parent.parent
MOVED = "b" * 40
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
WINDOWS = (300.0, 1800.0)
ARGS = argparse.Namespace(timeout=60.0, check_grace=WINDOWS[0], check_stall=WINDOWS[1])
DEFAULT = json.loads((REPO / "spec" / "agent-policy.default.json").read_text(encoding="utf-8"))
BOTS = DEFAULT["review"]["bots"]
SCHEMA = json.loads((REPO / "spec" / "agent-policy.schema.json").read_text(encoding="utf-8"))


def stated(**bots: dict) -> dict:
    """The default policy's data with each named bot's settings replaced by `bots`."""
    data = copy.deepcopy(DEFAULT)
    for name, settings in bots.items():
        data["review"]["bots"][name] = settings
    return data


def parsed(**bots: dict) -> policy.Policy:
    return policy.parse(stated(**bots), "test")


def with_policy(
    pr: dict, rules: policy.Policy, base: str = "develop", default: str = "main"
) -> dict:
    """`pr` into `base`, the default branch named, carrying `rules` the way `gql` attaches them."""
    pr["baseRefName"] = base
    pr["baseRepository"] = {"defaultBranchRef": {"name": default}}
    pr[payload.POLICY_KEY] = rules
    return pr


def trusted(body: str, at: str) -> dict:
    """A conversation comment from the repository's owner."""
    return {
        "author": {"login": "maintainer"},
        "authorAssociation": "OWNER",
        "createdAt": at,
        "body": body,
    }


def committed(pr: dict, at: str) -> dict:
    """`pr` on a branch of its own, its head pushed and its first suite created at `at`."""
    pr["headRepository"], pr["headRefName"] = {"nameWithOwner": "o/r"}, "feature/a"
    pr["commits"]["nodes"][-1]["commit"]["checkSuites"] = {"nodes": [{"createdAt": at}]}
    pr[side.PUSHED_KEY] = at
    return pr


def logged(at: str, code: int = 0) -> mock.MagicMock:
    """A REST read whose activity log prints `at`, as `PUSHED_JQ` prints a head pushed at `at`.

    Any other path reads as failed, as the suite's own `gh_rest` stand-in does.
    """

    def rest(path: str, *_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        if "/activity?" in path:
            return subprocess.CompletedProcess([], code, f"{at}\n", "")
        return subprocess.CompletedProcess([], 1, "", "no read in this suite")

    return mock.MagicMock(side_effect=rest)


def dated(pr: dict, at: str) -> dict:
    """`pr` with its head commit's own date, which its author's clock sets, at `at`."""
    pr["commits"]["nodes"][-1]["commit"]["committedDate"] = at
    return pr


class TestParse(unittest.TestCase):
    def test_the_hub_default_parses_and_names_every_bot(self) -> None:
        rules = policy.load_default()
        self.assertEqual(set(policy.BOTS), set(rules.bots))

    def test_the_default_matches_the_schema_file_it_names(self) -> None:
        self.assertEqual("./agent-policy.schema.json", DEFAULT["$schema"])
        self.assertTrue((REPO / "spec" / "agent-policy.schema.json").is_file())

    def test_a_file_with_no_review_section_reads_the_default_review(self) -> None:
        only_guard = {
            "version": 1,
            "guard": {"primaryCheckout": {"commitInPlace": {"branches": ["develop"]}}},
        }
        self.assertEqual(policy.load_default().bots, policy.parse(only_guard, "test").bots)
        self.assertTrue(policy.parse(only_guard, "test").from_default)
        self.assertFalse(parsed().from_default)

    def test_the_review_reader_leaves_the_guard_section_to_the_guard(self) -> None:
        data = {**stated(qodo={"trigger": "off", "bases": []}), "guard": {"anything": 1}}
        self.assertEqual("off", policy.parse(data, "test").bot("qodo").trigger)

    def test_unset_fields_take_their_documented_defaults(self) -> None:
        rules = parsed(qodo={"trigger": "explicit", "bases": ["^develop$"]})
        bot = rules.bot("qodo")
        self.assertEqual(
            ("on-open", "per-push", None, False, "review"),
            (bot.when, bot.rounds, bot.max_rounds, bot.gate, bot.command),
        )
        copilot = parsed(copilot={"trigger": "auto", "bases": ["@default"]}).bot("copilot")
        self.assertTrue(copilot.gate)

    def test_each_fault_is_refused_naming_where_it_sat(self) -> None:
        cases = {
            "unknown key(s) extra": {**DEFAULT, "extra": 1},
            "version is 2": {**DEFAULT, "version": 2},
            "bots is missing qodo": {
                **DEFAULT,
                "review": {"bots": {k: v for k, v in BOTS.items() if k != "qodo"}},
            },
            "unknown bot(s) other": {**DEFAULT, "review": {"bots": {**BOTS, "other": {}}}},
            "review is not an object": {**DEFAULT, "review": []},
            "review has unknown key(s) extra": {**DEFAULT, "review": {"bots": BOTS, "extra": 1}},
            "bots.qodo.trigger is 'sometimes'": stated(qodo={"trigger": "sometimes", "bases": []}),
            "bots.qodo.bases entry '(' is not a regex": stated(
                qodo={"trigger": "auto", "bases": ["("]}
            ),
            "bots.qodo.command applies to coderabbit only": stated(
                qodo={"trigger": "auto", "bases": [], "command": "review"}
            ),
            "bots.copilot.maxRounds is 0": stated(
                copilot={"trigger": "auto", "bases": [], "maxRounds": 0}
            ),
            "bots.copilot.maxRounds is True": stated(
                copilot={"trigger": "auto", "bases": [], "maxRounds": True}
            ),
            "bots.copilot has unknown key(s) often": stated(
                copilot={"trigger": "auto", "bases": [], "often": 1}
            ),
            "bots.copilot is missing bases": stated(copilot={"trigger": "auto"}),
            "bots.copilot is explicit with gate false": stated(
                copilot={"trigger": "explicit", "bases": [], "gate": False}
            ),
        }
        for want, data in cases.items():
            with self.subTest(want), self.assertRaisesRegex(policy.PolicyError, re.escape(want)):
                policy.parse(data, "where")


class TestBotDecisions(unittest.TestCase):
    def test_bases_match_the_default_token_and_full_regexes_only(self) -> None:
        bot = parsed(qodo={"trigger": "auto", "bases": ["@default", "^release/.*$", "dev"]}).bot(
            "qodo"
        )
        self.assertTrue(bot.covers("main", "main"))
        self.assertTrue(bot.covers("release/1", "main"))
        self.assertTrue(bot.covers("dev", "main"))
        self.assertFalse(bot.covers("develop", "main"), "a bare entry is a full match")
        self.assertFalse(bot.covers("main", ""), "an unread default branch matches no token")

    def test_off_covers_no_base(self) -> None:
        self.assertFalse(
            parsed(qodo={"trigger": "off", "bases": ["@default"]})
            .bot("qodo")
            .covers("main", "main")
        )

    def test_a_request_is_sent_for_explicit_and_for_a_gated_automatic_bot_only(self) -> None:
        cases = {
            ("explicit", True): True,
            ("auto", True): True,
            ("auto", False): False,
            ("off", True): False,
        }
        for (trigger, gate), want in cases.items():
            bot = parsed(copilot={"trigger": trigger, "bases": [], "gate": gate}).bot("copilot")
            with self.subTest(trigger=trigger, gate=gate):
                self.assertEqual(want, bot.requests())

    def test_rounds_stop_at_once_and_at_the_cap(self) -> None:
        once = parsed(qodo={"trigger": "explicit", "bases": [], "rounds": "once"}).bot("qodo")
        self.assertEqual((True, False), (once.rounds_left(0), once.rounds_left(1)))
        capped = parsed(qodo={"trigger": "explicit", "bases": [], "maxRounds": 2}).bot("qodo")
        self.assertEqual((True, False), (capped.rounds_left(1), capped.rounds_left(2)))
        free = parsed(qodo={"trigger": "explicit", "bases": []}).bot("qodo")
        self.assertTrue(free.rounds_left(99))

    def test_only_an_explicit_bot_waits_for_green_checks(self) -> None:
        after = {"bases": [], "when": "after-checks"}
        self.assertTrue(
            parsed(qodo={"trigger": "explicit", **after}).bot("qodo").waits_for_checks()
        )
        self.assertFalse(parsed(qodo={"trigger": "auto", **after}).bot("qodo").waits_for_checks())


class TestFetch(unittest.TestCase):
    def test_an_absent_file_reads_the_hub_default(self) -> None:
        rules = policy.fetch("o", "r", "develop", lambda *_: None)
        self.assertEqual(policy.load_default().bots, rules.bots)

    def test_a_carried_file_is_decoded_and_parsed(self) -> None:
        data = stated(qodo={"trigger": "off", "bases": []})
        content = base64.b64encode(json.dumps(data).encode()).decode()
        seen: list[tuple] = []

        def read(*args: str) -> str:
            seen.append(args)
            return content[:20] + "\n" + content[20:]

        rules = policy.fetch("o", "r", "feature/x", read)
        self.assertEqual("off", rules.bot("qodo").trigger)
        self.assertEqual([("o", "r", policy.POLICY_PATH, "feature/x")], seen)

    def test_a_file_that_is_not_json_is_refused(self) -> None:
        bad = base64.b64encode(b"{not json").decode()
        with self.assertRaisesRegex(policy.PolicyError, "o/r:.github/agent-policy.json@develop"):
            policy.fetch("o", "r", "develop", lambda *_: bad)


def proc(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, out, err)


class TestReadContents(unittest.TestCase):
    @staticmethod
    def read(
        answer: subprocess.CompletedProcess, ref: str = "develop"
    ) -> tuple[str | None, mock.MagicMock]:
        rest = mock.MagicMock(return_value=answer)
        found = policy.contents_reader(rest, pr_review.answered_absent)(
            "o", "r", policy.POLICY_PATH, ref
        )
        return found, rest

    def test_a_404_is_absent_and_the_ref_is_quoted(self) -> None:
        found, rest = self.read(proc(1, err="Not Found (HTTP 404)"), "feature/x")
        self.assertIsNone(found)
        self.assertIn("?ref=feature%2Fx", rest.call_args.args[0])

    def test_any_other_failure_ends_the_run_rather_than_reading_the_default(self) -> None:
        for answer in (
            proc(1, err="Server Error (HTTP 500)"),
            proc(0, out=""),
            proc(0, out="abc="),
        ):
            with self.subTest(answer=answer), self.assertRaisesRegex(SystemExit, "was not read"):
                self.read(answer)

    def test_a_read_returns_the_content_field(self) -> None:
        self.assertEqual("abc=", self.read(proc(0, out='"abc="\n'))[0])


class TestCopilotCover(unittest.TestCase):
    """`promotion` decides whether a Copilot round or a local pass covers the head."""

    def test_the_default_policy_keeps_the_pre_policy_answer_on_every_base(self) -> None:
        rules = policy.load_default()
        for base in ("main", "develop", "feature/x"):
            for reviews in ([], [fixtures.review()], [fixtures.review(oid=MOVED)]):
                bare = fixtures.payload(list(reviews))
                bare["baseRefName"] = base
                bare["baseRepository"] = {"defaultBranchRef": {"name": "main"}}
                ruled = with_policy(fixtures.payload(list(reviews)), rules, base)
                with self.subTest(base=base, reviews=len(reviews)):
                    self.assertEqual(rounds.promotion(bare), rounds.promotion(ruled))

    def test_a_base_the_policy_covers_wants_copilot_and_one_it_does_not_wants_a_local_pass(
        self,
    ) -> None:
        rules = parsed(copilot={"trigger": "explicit", "bases": ["^develop$"]})
        self.assertTrue(rounds.promotion(with_policy(fixtures.payload([]), rules, "develop")))
        self.assertFalse(rounds.promotion(with_policy(fixtures.payload([]), rules, "main")))

    def test_a_spent_cap_hands_a_new_head_to_the_local_pass(self) -> None:
        rules = parsed(copilot={"trigger": "explicit", "bases": ["@default"], "maxRounds": 1})
        spent = with_policy(fixtures.payload([fixtures.review(oid=MOVED)]), rules, "main")
        self.assertFalse(rounds.promotion(spent))
        self.assertTrue(rounds.holds(spent))

    def test_a_round_on_the_head_still_covers_it_once_the_cap_is_spent(self) -> None:
        rules = parsed(copilot={"trigger": "explicit", "bases": ["@default"], "rounds": "once"})
        self.assertTrue(
            rounds.promotion(with_policy(fixtures.payload([fixtures.review()]), rules, "main"))
        )

    def test_an_unread_default_branch_still_demands_the_round(self) -> None:
        rules = parsed(copilot={"trigger": "off", "bases": []})
        self.assertTrue(
            rounds.promotion(with_policy(fixtures.payload([]), rules, "main", default=""))
        )


EXPLICIT_QODO = {"trigger": "explicit", "bases": ["^develop$"]}
EXPLICIT_RABBIT = {
    "trigger": "explicit",
    "bases": ["^develop$"],
    "command": "full review",
}


class TestSideReaders(unittest.TestCase):
    def test_a_request_counts_for_the_head_only_once_it_postdates_the_push(self) -> None:
        pr = committed(
            fixtures.payload([], comments=[trusted("/review", fixtures.EARLY)]), fixtures.LATE
        )
        self.assertFalse(side.side_asked_on_head(pr, "/review"))
        pr = committed(
            fixtures.payload([], comments=[trusted("/review", fixtures.LATE)]), fixtures.EARLY
        )
        self.assertTrue(side.side_asked_on_head(pr, "/review"))

    def test_a_request_from_a_passer_by_or_with_extra_words_is_not_one(self) -> None:
        stranger = {**trusted("/review", fixtures.LATE), "authorAssociation": "NONE"}
        chatty = trusted("/review please", fixtures.LATE)
        pr = fixtures.payload([], comments=[stranger, chatty])
        self.assertEqual([], side.side_asks(pr, "/review"))

    def test_coderabbit_answers_with_a_review_on_the_head_commit(self) -> None:
        body = side.side_command("coderabbit", "full review")
        self.assertEqual("@coderabbitai full review", body)
        moved = fixtures.payload([fixtures.review(login=rounds.CODERABBIT_LOGIN, oid=MOVED)])
        self.assertFalse(side.side_answered(moved, "coderabbit"))
        on_head = fixtures.payload([fixtures.review(login=rounds.CODERABBIT_LOGIN)])
        self.assertTrue(side.side_answered(on_head, "coderabbit"))
        both = fixtures.payload(
            [
                fixtures.review(login=rounds.CODERABBIT_LOGIN, oid=MOVED),
                fixtures.review(login=rounds.CODERABBIT_LOGIN),
            ],
            comments=[trusted(body, fixtures.EARLY)],
        )
        self.assertEqual(2, side.side_rounds(both, "coderabbit", body))


class SideCase(unittest.TestCase):
    def ruled(self, comments: list[dict] | None = None, **bots: dict) -> dict:
        pr = fixtures.payload([], comments=comments or [])
        return committed(with_policy(pr, parsed(**bots), "develop"), fixtures.EARLY)


def posted(body: str) -> dict:
    """The answer an addComment mutation gives for `body`."""
    node = {"id": "IC_1", "url": "https://example.test/c/1", "body": body}
    return {"addComment": {"commentEdge": {"node": node}}}


class TestRequestSideReviews(SideCase):
    def run_requests(
        self, pr: dict, answer: dict | None = None, held: str = "", phase: str = "on-open"
    ) -> tuple[mock.MagicMock, mock.MagicMock]:
        def send(body: str) -> tuple[dict, bool]:
            reply = (
                posted(body)
                if answer is None
                else {"addComment": {"commentEdge": {"node": answer}}}
            )
            return side.post_comment(lambda **_: reply, pr_review.mutated, body)

        post, green = mock.MagicMock(side_effect=send), mock.MagicMock(return_value=held)
        side.request_side_reviews(lambda: pr, green, post, phase)
        return post, green

    def test_a_capped_bot_is_not_asked_once_the_comments_run_past_the_window(self) -> None:
        """Asks outside the window would go uncounted, so the cap could be passed unseen."""
        capped = {**EXPLICIT_QODO, "maxRounds": 3}
        for settings, asked in ((capped, False), (EXPLICIT_QODO, True)):
            pr = self.ruled(qodo=settings)
            pr["comments"]["pageInfo"] = {"hasPreviousPage": True}
            with self.subTest(capped="maxRounds" in settings):
                sent, _ = self.run_requests(pr)
                self.assertEqual(asked, sent.called)

    def test_an_explicit_bot_is_asked_with_its_own_command_on_the_pull_request(self) -> None:
        sent, _ = self.run_requests(self.ruled(coderabbit=EXPLICIT_RABBIT))
        self.assertEqual([mock.call("@coderabbitai full review")], sent.call_args_list)

    def test_nothing_is_sent_to_an_automatic_ungated_bot(self) -> None:
        auto = {"trigger": "auto", "bases": ["^develop$"], "gate": False}
        self.assertEqual(0, self.run_requests(self.ruled(qodo=auto))[0].call_count)

    def test_a_bot_already_asked_on_this_head_is_not_asked_again(self) -> None:
        pr = self.ruled([trusted("/review", fixtures.LATE)], qodo=EXPLICIT_QODO)
        self.assertEqual(0, self.run_requests(pr)[0].call_count)

    def test_failing_checks_hold_a_bot_that_waits_for_green(self) -> None:
        pr = self.ruled(
            qodo={**EXPLICIT_QODO, "when": "after-checks"},
            coderabbit={**EXPLICIT_RABBIT, "when": "after-checks"},
        )
        sent, green = self.run_requests(pr, held="FAILURE", phase="after-checks")
        self.assertEqual(0, sent.call_count)
        green.assert_called_once()

    def test_each_phase_asks_only_its_own_reviewers(self) -> None:
        pr = self.ruled(qodo=EXPLICIT_QODO, coderabbit={**EXPLICIT_RABBIT, "when": "after-checks"})
        sent, green = self.run_requests(pr, phase="on-open")
        self.assertEqual([mock.call("/review")], sent.call_args_list)
        green.assert_not_called()
        sent, _ = self.run_requests(pr, phase="after-checks")
        self.assertEqual([mock.call("@coderabbitai full review")], sent.call_args_list)

    def test_an_already_asked_reviewer_does_not_wait_for_the_checks(self) -> None:
        bot = {**EXPLICIT_QODO, "when": "after-checks"}
        pr = self.ruled([trusted("/review", fixtures.LATE)], qodo=bot)
        sent, green = self.run_requests(pr, phase="after-checks")
        self.assertEqual(0, sent.call_count)
        green.assert_not_called()

    def test_before_any_suite_the_push_bounds_an_ask(self) -> None:
        pr = fixtures.payload([], comments=[trusted("/review", fixtures.LATE)])
        pr[side.PUSHED_KEY] = fixtures.EARLY
        self.assertTrue(side.side_asked_on_head(pr, "/review"))
        pr[side.PUSHED_KEY] = "2026-08-02T12:00:00Z"
        self.assertFalse(side.side_asked_on_head(pr, "/review"))

    def test_an_unconfirmed_request_ends_the_run(self) -> None:
        wrong = {"url": "", "body": ""}
        with self.assertRaisesRegex(SystemExit, "unconfirmed"):
            self.run_requests(self.ruled(qodo=EXPLICIT_QODO), answer=wrong)


def checked(*nodes: dict, at: str = fixtures.EARLY) -> dict:
    """A payload whose head carries `nodes` as its checks, committed at `at`."""
    pr = fixtures.payload([], checks=list(nodes))
    pr["commits"]["nodes"][-1]["commit"]["checkSuites"] = {"nodes": [{"createdAt": at}]}
    return pr


class TestChecksHold(unittest.TestCase):
    def test_a_failed_check_holds_and_a_passing_one_does_not(self) -> None:
        self.assertEqual(
            "FAILURE",
            side.checks_hold(
                checked(fixtures.check(conclusion="FAILURE", started=fixtures.EARLY)), NOW, *WINDOWS
            ),
        )
        self.assertEqual(
            "", side.checks_hold(checked(fixtures.check(started=fixtures.EARLY)), NOW, *WINDOWS)
        )

    def test_a_running_check_holds_as_pending(self) -> None:
        running = fixtures.check(status="IN_PROGRESS", conclusion="", started=fixtures.EARLY)
        self.assertEqual("PENDING", side.checks_hold(checked(running), NOW, *WINDOWS))

    def test_no_check_yet_is_pending_inside_the_grace_and_clear_after_it(self) -> None:
        fresh = checked(at=(NOW - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual("PENDING", side.checks_hold(fresh, NOW, *WINDOWS))
        self.assertEqual("", side.checks_hold(checked(), NOW, *WINDOWS))

    def test_a_hosted_reviewer_check_is_not_the_build(self) -> None:
        review = fixtures.check(
            name="CodeRabbit", status="IN_PROGRESS", conclusion="", started=fixtures.EARLY
        )
        self.assertEqual(
            "",
            side.checks_hold(
                checked(fixtures.check(started=fixtures.EARLY), review), NOW, *WINDOWS
            ),
        )

    def test_the_head_arrives_with_its_first_check_not_its_commit(self) -> None:
        pr = checked(fixtures.check(started=fixtures.LATE), at=fixtures.LATE)
        pr["commits"]["nodes"][-1]["commit"]["committedDate"] = fixtures.EARLY
        self.assertEqual(fixtures.LATE, side.head_arrived(pr))
        self.assertEqual("", side.head_arrived(fixtures.payload([])))

    def test_an_ask_before_a_later_push_is_not_one_on_the_head(self) -> None:
        pr = checked(fixtures.check(started=fixtures.LATE), at=fixtures.LATE)
        pr[side.PUSHED_KEY] = fixtures.LATE
        pr["comments"]["nodes"] = [trusted("/review", "2026-08-02T10:30:00Z")]
        self.assertFalse(side.side_asked_on_head(pr, "/review"))

    def test_await_green_polls_until_nothing_settles(self) -> None:
        reads = iter(
            [
                checked(
                    fixtures.check(status="IN_PROGRESS", conclusion="", started=fixtures.EARLY)
                ),
                checked(fixtures.check(started=fixtures.EARLY)),
            ]
        )
        with mock.patch.object(pr_review.time, "sleep"):
            held = side.await_green(
                lambda: next(reads),
                time.monotonic(),
                ARGS,
                functools.partial(pr_review.backoff, delays=(0,)),
            )
        self.assertEqual("", held)


class TestSideCounts(SideCase):
    def test_an_automatic_qodo_round_counts_once_its_comment_exists(self) -> None:
        pr = self.ruled(qodo=EXPLICIT_QODO)
        with mock.patch.object(side, "qodo_comment", return_value={"updatedAt": fixtures.EARLY}):
            self.assertEqual(1, side.side_rounds(pr, "qodo", "/review"))
        self.assertEqual(0, side.side_rounds(pr, "qodo", "/review"))

    def test_only_a_policy_that_may_ask_a_side_reviewer_reads_again(self) -> None:
        self.assertTrue(side.asks_any(self.ruled(qodo=EXPLICIT_QODO)))
        self.assertFalse(side.asks_any(with_policy(fixtures.payload([]), policy.load_default())))

    def test_a_head_with_no_suite_yet_reads_as_just_arrived(self) -> None:
        bare = with_policy(fixtures.payload([]), parsed(qodo=EXPLICIT_QODO))
        self.assertEqual("PENDING", side.checks_hold(bare, NOW, *WINDOWS))

    def test_a_head_whose_push_is_unread_is_asked_nothing_and_says_so(self) -> None:
        """Unread, an ask already made for the head looks like one for an earlier head."""
        bare = with_policy(
            fixtures.payload([], comments=[trusted("/review", fixtures.LATE)]),
            parsed(qodo=EXPLICIT_QODO),
        )
        self.assertFalse(side.side_asked_on_head(bare, "/review"))
        post = mock.MagicMock()
        lines = side.request_side_reviews(lambda: bare, mock.MagicMock(), post, "on-open")
        post.assert_not_called()
        self.assertEqual(["note: the head's push could not be read, so qodo was not asked"], lines)


class TestUngatedSideReviewers(SideCase):
    def test_only_copilot_can_gate_the_merge(self) -> None:
        for name in ("coderabbit", "qodo"):
            with (
                self.subTest(name),
                self.assertRaisesRegex(policy.PolicyError, "only copilot can gate"),
            ):
                parsed(**{name: {"trigger": "explicit", "bases": [], "gate": True}})

    def test_an_automatic_side_reviewer_is_never_asked(self) -> None:
        post = mock.MagicMock()
        pr = self.ruled(qodo={"trigger": "auto", "bases": ["^develop$"]})
        side.request_side_reviews(lambda: pr, mock.MagicMock(return_value=""), post, "on-open")
        post.assert_not_called()

    def test_a_request_sent_during_the_wait_is_seen_before_asking(self) -> None:
        bot = {**EXPLICIT_QODO, "when": "after-checks"}
        before = self.ruled(qodo=bot)
        after = self.ruled([trusted("/review", fixtures.LATE)], qodo=bot)
        reads = iter([before, after])
        post = mock.MagicMock()
        side.request_side_reviews(
            lambda: next(reads), mock.MagicMock(return_value=""), post, "after-checks"
        )
        post.assert_not_called()

    def test_a_stuck_check_is_named_rather_than_polled(self) -> None:
        stale = fixtures.check(
            status="IN_PROGRESS", conclusion="", started=fixtures.EARLY, suite_status="COMPLETED"
        )
        held = side.checks_hold(checked(stale), NOW, *WINDOWS)
        self.assertEqual("STALE_RECORD", held)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(44, side.checks_status(held))


class TestReviewThreadFixes(unittest.TestCase):
    def test_a_boolean_version_is_refused(self) -> None:
        with self.assertRaisesRegex(policy.PolicyError, "version is True"):
            policy.parse({**DEFAULT, "version": True}, "where")

    def test_an_unreadable_check_holds_the_review(self) -> None:
        pr = checked(fixtures.check(started=fixtures.EARLY))
        pr["commits"]["nodes"][-1]["commit"]["statusCheckRollup"]["contexts"]["nodes"].append(
            {"__typename": "Mystery"}
        )
        self.assertEqual("UNREAD", side.checks_hold(pr, NOW, *WINDOWS))


class TestPolicyFreeReads(unittest.TestCase):
    def test_a_read_that_never_consults_the_policy_does_not_fetch_it(self) -> None:
        """So a malformed policy on the base cannot end `attest`, `comment`, or `claims`."""
        target = {"repository": {"pullRequest": {"headRefOid": fixtures.HEAD, "baseRefName": "x"}}}
        with (
            mock.patch.object(pr_review, "gh_graphql", return_value=target),
            mock.patch.object(pr_review.review_policy, "policy_for") as fetched,
        ):
            pr = pr_review.gql(pr_review.Q_ATTEST_TARGET, "o", "r", 7, policy=False)
            self.assertNotIn(payload.POLICY_KEY, pr)
            fetched.assert_not_called()
            pr_review.gql(pr_review.Q_ATTEST_TARGET, "o", "r", 7)
            fetched.assert_called_once()


class TestSchemaParity(unittest.TestCase):
    """The schema an editor validates against states exactly the rules the reader enforces."""

    def test_every_enum_and_key_matches_the_reader(self) -> None:
        schema = SCHEMA
        bot = schema["$defs"]["bot"]["properties"]
        self.assertEqual(set(policy.BOT_KEYS), set(bot))
        self.assertEqual(set(policy.TRIGGERS), set(bot["trigger"]["enum"]))
        self.assertEqual(set(policy.WHENS), set(bot["when"]["enum"]))
        self.assertEqual(set(policy.ROUNDS), set(bot["rounds"]["enum"]))
        bots = schema["properties"]["review"]["properties"]["bots"]["properties"]
        self.assertEqual(set(policy.BOTS), set(bots))
        self.assertEqual(
            set(policy.COMMANDS), set(bots["coderabbit"]["properties"]["command"]["enum"])
        )
        for name in ("copilot", "qodo"):
            self.assertEqual({"required": ["command"]}, bots[name]["not"], name)
        self.assertEqual({"trigger": {"const": "explicit"}}, bots["copilot"]["if"]["properties"])
        self.assertEqual({"gate": {"const": False}}, bots["copilot"]["then"]["not"]["properties"])

    def test_the_sections_match_the_reader(self) -> None:
        schema = SCHEMA
        self.assertEqual({"$schema", "version", *policy.SECTIONS}, set(schema["properties"]))

    def test_the_guard_reads_the_path_and_keys_the_schema_states(self) -> None:
        """The write guard is a standalone hook that cannot import this reader, so its reading is pinned here."""
        path = REPO / "host-setup" / "agent-safety" / "claude" / "gh-write-guard.py"
        spec = importlib.util.spec_from_file_location("gh_write_guard", path)
        assert spec and spec.loader
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        self.assertEqual(f"origin/main:{policy.POLICY_PATH}", guard._POLICY_BLOB)
        schema = SCHEMA
        section = schema["properties"]["guard"]["properties"]["primaryCheckout"]["properties"]
        branches = section["commitInPlace"]["properties"]["branches"]
        self.assertEqual({"not": {"enum": ["main", "master"]}}, {"not": branches["items"]["not"]})
        source = path.read_text(encoding="utf-8")
        self.assertIn('data["guard"]["primaryCheckout"]["commitInPlace"]["branches"]', source)
        self.assertIn('branch in ("main", "master")', source)


PARTIAL = f"{fixtures.OVERVIEW}\n<!-- fleet-review: reviewed=1 changed=3 -->"


def attested_on(pr: dict) -> dict:
    """`pr` with its owner attesting a local pass over its head against its base."""
    marker = (
        f"Attested.\n\n<!-- fleet-local-review: head={fixtures.HEAD} "
        f"base={pr['baseRefName']} findings=0 -->"
    )
    pr["comments"] = {
        "nodes": [trusted(marker, fixtures.LATE)],
        "pageInfo": {"hasPreviousPage": False},
    }
    return pr


class TestCopilotGate(unittest.TestCase):
    def test_an_ungated_copilot_hands_every_head_to_the_local_pass(self) -> None:
        rules = parsed(copilot={"trigger": "auto", "bases": ["@default"], "gate": False})
        self.assertFalse(rounds.promotion(with_policy(fixtures.payload([]), rules, "main")))

    def test_a_copilot_never_waited_on_lets_a_partial_or_cut_history_hold(self) -> None:
        """No round is coming to settle that history, so `wait` must not poll for one."""
        histories = {
            "partial": lambda: fixtures.payload([fixtures.review(oid=fixtures.OLD, body=PARTIAL)]),
            "truncated": lambda: fixtures.payload([fixtures.review(oid=MOVED)], older_reviews=True),
        }
        ungated = {
            "off": parsed(copilot={"trigger": "off", "bases": ["@default"]}),
            "auto ungated": parsed(copilot={"trigger": "auto", "bases": [], "gate": False}),
        }
        for history, build in histories.items():
            for name, rules in ungated.items():
                with self.subTest(history=history, policy=name):
                    pr = attested_on(with_policy(build(), rules))
                    self.assertTrue(rounds.holds(pr))
                    self.assertTrue(rounds.local_cover(pr))
            with self.subTest(history=history, policy="spent cap"):
                capped = parsed(copilot={"trigger": "explicit", "bases": [], "maxRounds": 1})
                pr = attested_on(with_policy(build(), capped))
                self.assertTrue(rounds.holds(pr))
                self.assertTrue(rounds.local_cover(pr))
            with self.subTest(history=history, policy="hub default"):
                pr = attested_on(with_policy(build(), policy.load_default()))
                self.assertFalse(rounds.holds(pr))
                self.assertFalse(rounds.local_cover(pr))


class TestCopilotOutputOnTheHead(unittest.TestCase):
    """Copilot's own word on the head settles nothing where the policy never waits on it."""

    def cases(self) -> dict[str, dict]:
        return {
            "refusal": fixtures.payload([fixtures.review(body=fixtures.QUOTA_REFUSED)]),
            "pending request": fixtures.payload([], pending=True),
            "partial round": fixtures.payload([fixtures.review(body=PARTIAL)]),
        }

    def test_an_ungated_copilot_leaves_the_attested_local_pass_covering(self) -> None:
        rules = parsed(copilot={"trigger": "auto", "bases": [], "gate": False})
        for name, pr in self.cases().items():
            with self.subTest(name):
                ruled = attested_on(with_policy(pr, rules))
                self.assertTrue(rounds.holds(ruled))
                self.assertTrue(rounds.local_cover(ruled))
                self.assertEqual("local", rounds.coverage(ruled)[0])

    def test_the_hub_default_still_lets_it_stop_the_local_pass(self) -> None:
        for name, pr in self.cases().items():
            with self.subTest(name):
                ruled = attested_on(with_policy(pr, policy.load_default()))
                self.assertFalse(rounds.holds(ruled) and rounds.local_cover(ruled))


class TestWaitUnderAnUngatedCopilot(fixtures.GqlCase):
    def test_a_pending_request_does_not_stop_the_hold(self) -> None:
        """An automatic request the policy does not wait on leaves `wait` on the attested pass."""
        rules = parsed(copilot={"trigger": "auto", "bases": [], "gate": False})
        pr = attested_on(with_policy(fixtures.payload([], pending=True), rules))
        self.answer(pr)
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            mock.patch.object(pr_review.time, "sleep"),
        ):
            pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"])
        self.assertIn("covers this head with an attested local pass", out.getvalue())


class TestWaitHoldsForChecks(fixtures.GqlCase):
    """An explicit Copilot policy waiting for green checks requests nothing on a red or running head."""

    def run_wait(self, node: dict, *extra: str) -> tuple[int, str, mock.MagicMock]:
        rules = parsed(
            copilot={"trigger": "explicit", "bases": ["^develop$"], "when": "after-checks"}
        )
        pr = with_policy(fixtures.payload([], checks=[node]), rules)
        self.answer(pr)
        bot = {"__typename": "Bot", "login": rounds.REVIEWER, "id": "BOT_123"}
        history = [{"number": 1, "reviews": {"nodes": [{"author": bot}]}}]

        def graphql(query: str, **variables: object) -> dict:
            if "pullRequests(first:$prs" in query:
                return {"repository": {"pullRequests": {"nodes": history}}}
            answered = fixtures.answer_request(query, variables, fixtures.RECORDED)
            return answered or {"repository": {"pullRequests": {"nodes": []}}}

        sent = mock.MagicMock(side_effect=graphql)
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            mock.patch.object(pr_review, "gh_graphql", sent),
            mock.patch.object(pr_review.time, "sleep"),
        ):
            code = pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0", *extra])
        return code, out.getvalue(), sent

    def requested(self, sent: mock.MagicMock) -> bool:
        return any("requestReviews" in str(c.args[0]) for c in sent.call_args_list if c.args)

    def test_a_failed_build_is_not_reviewed_and_exits_40(self) -> None:
        code, out, sent = self.run_wait(
            fixtures.check(conclusion="FAILURE", started=fixtures.EARLY)
        )
        self.assertEqual(40, code, out)
        self.assertIn("status=CHECKS_BEFORE_REVIEW the head's checks read FAILURE", out)
        self.assertFalse(self.requested(sent))

    def test_checks_still_running_at_the_timeout_exit_30(self) -> None:
        running = fixtures.check(status="IN_PROGRESS", conclusion="", started=fixtures.EARLY)
        code, out, sent = self.run_wait(running)
        self.assertEqual(30, code, out)
        self.assertIn("checks read PENDING", out)
        self.assertFalse(self.requested(sent))

    def test_green_checks_reach_the_request(self) -> None:
        code, out, sent = self.run_wait(fixtures.check(started=fixtures.EARLY))
        self.assertTrue(self.requested(sent), out)
        self.assertIn("auto-request: requested a Copilot review", out)
        self.assertNotIn("CHECKS_BEFORE_REVIEW", out)
        self.assertNotEqual(40, code)

    def test_request_skips_the_hold_on_a_failed_build(self) -> None:
        failed = fixtures.check(conclusion="FAILURE", started=fixtures.EARLY)
        code, out, sent = self.run_wait(failed, "--request")
        self.assertTrue(self.requested(sent), out)
        self.assertNotIn("CHECKS_BEFORE_REVIEW", out)
        self.assertNotEqual(40, code)


class TestWaitAsksSideReviewers(fixtures.GqlCase):
    """A side reviewer is asked whatever Copilot's own round has done, since nothing waits on it."""

    def test_an_explicit_qodo_is_asked_on_a_head_copilot_already_covered(self) -> None:
        rules = parsed(qodo={"trigger": "explicit", "bases": ["^develop$"]})
        pr = with_policy(fixtures.payload([fixtures.review()]), rules)
        committed(pr, fixtures.EARLY)
        self.answer(pr)

        def graphql(query: str, **variables: object) -> dict:
            if "addComment" in query:
                return posted(str(variables["body"]))
            return {"repository": {"pullRequests": {"nodes": []}}}

        sent = mock.MagicMock(side_effect=graphql)
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            mock.patch.object(pr_review, "gh_graphql", sent),
            mock.patch.object(pr_review, "gh_rest", logged(fixtures.EARLY)),
            mock.patch.object(pr_review.time, "sleep"),
        ):
            pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"])
        asks = [c for c in sent.call_args_list if c.args and "addComment" in c.args[0]]
        self.assertEqual(1, len(asks), out.getvalue())
        self.assertEqual("/review", asks[0].kwargs["body"])
        self.assertIn("auto-request: asked qodo", out.getvalue())


def bodiless(pr: dict) -> Callable[..., dict]:
    """A `gql` answering `pr`, with every review body dropped from the live read as `Q_LIVE` drops it.

    That read then takes a refusal on the head for a round, the shape `polled_past` exists for.
    """
    live = copy.deepcopy(pr)
    for node in live["reviews"]["nodes"]:
        node.pop("body", None)
    return lambda query, *_args, **_kwargs: live if query is pr_review.Q_LIVE else pr


class TestLocalPassFixes(fixtures.GqlCase):
    """`wait` and `checks_hold` agree with the gate's own readings of a settled or still-running head."""

    def wait(self, *extra: str) -> tuple[int, str]:
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            mock.patch.object(pr_review.time, "sleep"),
        ):
            code = pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0", *extra])
        return code, out.getvalue()

    def test_a_settled_copilot_refusal_leaves_the_head_to_the_local_pass(self) -> None:
        """The quota refusal on the head is no exit 41 where no Copilot round is coming."""
        refused = [fixtures.review(body=fixtures.QUOTA_REFUSED)]
        cases = {"attested": (attested_on, "covers this head"), "unattested": (None, "")}
        for name, (attest, line) in cases.items():
            with self.subTest(name):
                ungated = parsed(copilot={"trigger": "auto", "bases": [], "gate": False})
                pr = with_policy(fixtures.payload(refused), ungated)
                self.answer(attest(pr) if attest else pr)
                self.enterContext(mock.patch.object(pr_review, "gql", side_effect=bodiless(pr)))
                code, out = self.wait()
                self.assertNotEqual(41, code, out)
                self.assertNotIn("COPILOT_QUOTA_EXHAUSTED", out)
                self.assertIn(line or "status=AWAITING_LOCAL_PASS", out)

    def test_an_open_workflow_run_holds_a_green_rollup(self) -> None:
        """Between `needs:` stages the rollup reads green while the run's later jobs do not exist yet."""
        between = fixtures.check(
            started=fixtures.EARLY, suite_status="IN_PROGRESS", workflow=1, run=99
        )
        self.assertEqual("PENDING", side.checks_hold(checked(between), NOW, *WINDOWS))
        done = fixtures.check(started=fixtures.EARLY, workflow=1, run=99)
        self.assertEqual("", side.checks_hold(checked(done), NOW, *WINDOWS))

    def test_the_after_checks_ask_runs_after_the_copilot_poll(self) -> None:
        events: list[str] = []
        real_backoff, real_requests = pr_review.backoff, side.request_side_reviews

        def polled(*args: Any, **kwargs: Any) -> Any:
            events.append("poll")
            return real_backoff(*args, **kwargs)

        def asked(*args: Any) -> list[str]:
            events.append(str(args[-1]))
            return real_requests(*args)

        rules = parsed(qodo={**EXPLICIT_QODO, "when": "after-checks"})
        self.answer(attested_on(with_policy(fixtures.payload([]), rules)))
        with (
            mock.patch.object(pr_review, "backoff", polled),
            mock.patch.object(side, "request_side_reviews", asked),
        ):
            self.wait()
        self.assertIn("after-checks", events)
        self.assertLess(events.index("poll"), events.index("after-checks"), events)

    def test_a_request_landing_during_the_check_wait_is_not_duplicated(self) -> None:
        rules = parsed(
            copilot={"trigger": "explicit", "bases": ["^develop$"], "when": "after-checks"}
        )
        green = [fixtures.check(started=fixtures.EARLY)]
        before = with_policy(fixtures.payload([], checks=green), rules)
        after = with_policy(fixtures.payload([], checks=green, pending=True), rules)
        self.answer(before)
        lives: list[int] = []

        def read(query: str, *_args: object, **_kwargs: object) -> dict:
            if query is pr_review.Q_LIVE:
                lives.append(1)
                return before if len(lives) == 1 else after
            return before

        bot = {"__typename": "Bot", "login": rounds.REVIEWER, "id": "BOT_123"}
        history = [{"number": 1, "reviews": {"nodes": [{"author": bot}]}}]

        def graphql(query: str, **variables: object) -> dict:
            if "pullRequests(first:$prs" in query:
                return {"repository": {"pullRequests": {"nodes": history}}}
            answered = fixtures.answer_request(query, variables, fixtures.RECORDED)
            return answered or {"repository": {"pullRequests": {"nodes": []}}}

        sent = mock.MagicMock(side_effect=graphql)
        with (
            mock.patch.object(pr_review, "gql", side_effect=read),
            mock.patch.object(pr_review, "gh_graphql", sent),
        ):
            _, out = self.wait()
        requests = [c for c in sent.call_args_list if c.args and "requestReviews" in c.args[0]]
        self.assertEqual([], requests, out)
        self.assertGreaterEqual(len(lives), 2, out)


class TestSecondPassFixes(fixtures.GqlCase):
    """The findings of the pass over e4622ee6, each case failing with its fix reverted."""

    def test_a_closed_pull_request_is_asked_nothing(self) -> None:
        made = {"url": "https://example.invalid/c/1", "body": "/review"}
        for state, asked in (("MERGED", False), ("OPEN", True)):
            with self.subTest(state):
                pr = committed(
                    with_policy(fixtures.payload([]), parsed(qodo=EXPLICIT_QODO)), fixtures.EARLY
                )
                pr["state"] = state
                post = mock.MagicMock(return_value=(made, True))
                side.request_side_reviews(
                    mock.MagicMock(return_value=pr), mock.MagicMock(), post, "on-open"
                )
                self.assertEqual(asked, post.called)

    def test_a_capped_copilot_past_the_review_window_counts_as_spent(self) -> None:
        """Its rounds cannot be counted, so the cap is read as reached rather than as open."""
        bases = {"trigger": "explicit", "bases": ["@default"]}
        for rules, settled in (
            (parsed(copilot={**bases, "maxRounds": 3}), True),
            (parsed(copilot=bases), False),
        ):
            with self.subTest(settled=settled):
                cut = fixtures.payload([fixtures.review(oid=MOVED)], older_reviews=True)
                pr = with_policy(cut, rules, base="main")
                self.assertEqual(settled, rounds.copilot_settled(pr))
                self.assertEqual(not settled, rounds.promotion(pr))

    def test_a_conflicted_head_with_no_check_names_its_conflict(self) -> None:
        pr = fixtures.payload([])
        self.assertEqual("PENDING", side.checks_hold(pr, NOW, *WINDOWS))
        pr["mergeStateStatus"] = "DIRTY"
        self.assertEqual("DIRTY", side.checks_hold(pr, NOW, *WINDOWS))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(44, side.checks_status("DIRTY"))
        self.assertIn("resolve the conflict", out.getvalue())

    def test_an_answer_outside_a_review_leaves_a_settled_head_to_the_local_pass(self) -> None:
        ungated = parsed(copilot={"trigger": "auto", "bases": [], "gate": False})
        self.answer(with_policy(fixtures.payload([], comments=[fixtures.comment()]), ungated))
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            mock.patch.object(pr_review.time, "sleep"),
        ):
            code = pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"])
        self.assertEqual(49, code, out.getvalue())
        self.assertIn("status=AWAITING_LOCAL_PASS", out.getvalue())

    def test_an_after_checks_ask_given_no_poll_reads_the_checks_once(self) -> None:
        running = fixtures.check(status="IN_PROGRESS", conclusion="", started=fixtures.EARLY)
        pr = with_policy(checked(running), parsed(qodo={**EXPLICIT_QODO, "when": "after-checks"}))
        pr = committed(pr, fixtures.EARLY)
        read, send = mock.MagicMock(return_value=pr), mock.MagicMock()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            side.ask(
                read, ARGS, send, pr_review.mutated, pr, logged(fixtures.EARLY), "after-checks"
            )
        send.assert_not_called()
        self.assertIn("they read PENDING", out.getvalue())
        self.assertLessEqual(read.call_count, 3)


RUNNING = fixtures.check(status="IN_PROGRESS", conclusion="", started=fixtures.EARLY)
AFTER_CHECKS_QODO = {**EXPLICIT_QODO, "when": "after-checks"}


class TestSideAskTiming(fixtures.GqlCase):
    """An after-checks ask outlasts the CI it follows, and an ask dates against its head by instant."""

    def covered(self, check: dict) -> dict:
        pr = with_policy(
            fixtures.payload([fixtures.review()], checks=[check]), parsed(qodo=AFTER_CHECKS_QODO)
        )
        return committed(pr, fixtures.EARLY)

    def test_a_head_whose_checks_outlast_the_copilot_round_is_asked_once_they_pass(self) -> None:
        """The checks pass only once the wait has slept, as CI that outlasts the round does.

        The running check started a moment ago, so the CLI's own stall window reads it as settling.
        """
        just = (datetime.now(UTC) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        moving = fixtures.check(status="IN_PROGRESS", conclusion="", started=just)
        running, green = self.covered(moving), self.covered(fixtures.check(started=fixtures.EARLY))
        slept: list[float] = []
        reads: list[tuple[str, bool]] = []

        def read(query: str, *_args: object, **_kwargs: object) -> dict:
            reads.append((query, bool(slept)))
            return green if slept else running

        def graphql(query: str, **variables: object) -> dict:
            if "addComment" in query:
                return posted(str(variables["body"]))
            return {"repository": {"pullRequests": {"nodes": []}}}

        self.answer(running)
        sent = mock.MagicMock(side_effect=graphql)
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            mock.patch.object(pr_review, "gql", side_effect=read),
            mock.patch.object(pr_review, "gh_graphql", sent),
            mock.patch.object(pr_review, "gh_rest", logged(fixtures.EARLY)),
            mock.patch.object(pr_review.time, "sleep", side_effect=slept.append),
        ):
            pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "60"])
        asks = [c.kwargs["body"] for c in sent.call_args_list if "addComment" in c.args[0]]
        self.assertEqual(["/review"], asks, out.getvalue())
        self.assertIn("auto-request: asked qodo", out.getvalue())
        self.assertIn(
            (pr_review.Q_FULL, True), reads, "the digest read a payload from before the poll"
        )

    def test_only_a_wait_that_polls_copilot_hands_the_ask_a_poll(self) -> None:
        """A held head's own poll already waited the checks out, so it reads them once."""
        ungated = parsed(copilot={"trigger": "auto", "bases": [], "gate": False})
        cases = {
            "covered": (fixtures.payload([fixtures.review()]), True),
            "stopped": (fixtures.payload([fixtures.review(body=fixtures.QUOTA_REFUSED)]), False),
            "held": (attested_on(with_policy(fixtures.payload([]), ungated)), False),
        }
        for name, (pr, polls) in cases.items():
            with self.subTest(name):
                self.answer(pr)
                spy = self.enterContext(mock.patch.object(side, "ask", wraps=side.ask))
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    mock.patch.object(pr_review.time, "sleep"),
                ):
                    pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"])
                after = [c for c in spy.call_args_list if "after-checks" in c.args]
                self.assertEqual(1, len(after))
                self.assertEqual(polls, after[0].args[-1] is not None)

    def test_a_pull_request_merged_while_its_checks_settle_is_asked_nothing(self) -> None:
        bot = {**EXPLICIT_QODO, "when": "after-checks"}
        before = with_policy(fixtures.payload([]), parsed(qodo=bot))
        merged = {**copy.deepcopy(before), "state": "MERGED"}
        made = ({"url": "https://example.test/c/1", "body": "/review"}, True)
        reads, post = iter([before, merged]), mock.MagicMock(return_value=made)
        side.request_side_reviews(
            lambda: next(reads), mock.MagicMock(return_value=""), post, "after-checks"
        )
        post.assert_not_called()

    def test_a_poll_handed_to_the_ask_decides_the_checks(self) -> None:
        pr = committed(
            with_policy(checked(RUNNING), parsed(qodo=AFTER_CHECKS_QODO)), fixtures.EARLY
        )
        poll = mock.MagicMock(return_value="")
        send = mock.MagicMock(side_effect=lambda body: posted(body))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            side.ask(
                mock.MagicMock(return_value=pr),
                ARGS,
                send,
                pr_review.mutated,
                pr,
                logged(fixtures.EARLY),
                "after-checks",
                poll,
            )
        poll.assert_called_once()
        self.assertEqual("/review", send.call_args.kwargs["body"], out.getvalue())

    def test_an_ask_and_a_push_in_different_offsets_are_compared_as_instants(self) -> None:
        """10:00 at -07:00 is 17:00 UTC, so a 12:00 UTC ask is five hours older than the head.

        10:00 at +05:00 is 05:00 UTC, so a 06:00 UTC ask is an hour newer than the head.
        """
        cases = {
            "west": ("2026-01-01T10:00:00-07:00", "2026-01-01T12:00:00Z", False),
            "east": ("2026-01-01T10:00:00+05:00", "2026-01-01T06:00:00Z", True),
        }
        for name, (head, at, asked) in cases.items():
            with self.subTest(name):
                pr = committed(fixtures.payload([], comments=[trusted("/review", at)]), head)
                self.assertEqual(asked, side.side_asked_on_head(pr, "/review"))

    def test_a_stamp_naming_no_zone_counts_no_ask(self) -> None:
        pr = committed(
            fixtures.payload([], comments=[trusted("/review", "2026-01-01T12:00:00")]),
            fixtures.EARLY,
        )
        self.assertFalse(side.side_asked_on_head(pr, "/review"))

    def test_a_conflicted_or_unreadable_hold_exits_44(self) -> None:
        for held in ("DIRTY", "UNREAD", "NOT_POSTED"):
            with self.subTest(held), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(44, side.checks_status(held))

    def test_a_comment_confirms_only_with_its_url_and_exact_body(self) -> None:
        good = posted("/review")
        self.assertTrue(side.post_comment(lambda **_: good, pr_review.mutated, "/review")[1])
        for node in (
            {"url": "", "body": "/review"},
            {"url": "https://example.test/c/1", "body": "x"},
        ):
            with self.subTest(node):
                reply = {"addComment": {"commentEdge": {"node": node}}}
                made, confirmed = side.post_comment(
                    lambda reply=reply, **_: reply, pr_review.mutated, "/review"
                )
                self.assertFalse(confirmed)
                self.assertEqual(node, made)


PUSHED = "2026-08-02T10:20:00Z"


class TestSideAskServerStamp(unittest.TestCase):
    """An ask is placed against the head by GitHub's stamps on both sides, never by a commit's date."""

    def asked(self, commit: str, ask: str) -> mock.MagicMock:
        """The posts `ask` makes for a head pushed at `PUSHED` and dated `commit`, asked at `ask`."""
        pr = fixtures.payload([], comments=[trusted("/review", ask)])
        pr = dated(committed(with_policy(pr, parsed(qodo=EXPLICIT_QODO)), PUSHED), commit)
        del pr[side.PUSHED_KEY]
        send = mock.MagicMock(side_effect=lambda body: posted(body))
        with contextlib.redirect_stdout(io.StringIO()):
            side.ask(lambda: pr, ARGS, send, pr_review.mutated, pr, logged(PUSHED), "on-open")
        return send

    def test_a_backdated_commit_does_not_carry_an_earlier_ask_onto_the_head(self) -> None:
        """Committed at 10:00 and pushed at 10:20, so a 10:05 ask was for the head before it."""
        send = self.asked(commit="2026-08-02T10:00:00Z", ask="2026-08-02T10:05:00Z")
        self.assertEqual(["/review"], [c.kwargs["body"] for c in send.call_args_list])

    def test_a_future_dated_commit_does_not_hide_an_ask_on_the_head(self) -> None:
        """Committed at 10:30 and pushed at 10:20, so a 10:25 ask was for this head."""
        send = self.asked(commit="2026-08-02T10:30:00Z", ask="2026-08-02T10:25:00Z")
        send.assert_not_called()

    def test_the_push_is_read_off_the_head_branch_activity_log(self) -> None:
        pr = committed(fixtures.payload([]), fixtures.EARLY)
        pr["headRepository"], pr["headRefName"] = {"nameWithOwner": "fork/r"}, "fix/a&b"
        rest = logged(fixtures.LATE)
        self.assertEqual(fixtures.LATE, side.pushed(pr, rest)[side.PUSHED_KEY])
        path, jq = rest.call_args.args
        self.assertEqual("repos/fork/r/activity?ref=refs%2Fheads%2Ffix%2Fa%26b&per_page=100", path)
        self.assertIn(f'"{fixtures.HEAD}"', jq)

    def test_a_push_that_cannot_be_read_reads_empty(self) -> None:
        pr = committed(fixtures.payload([]), fixtures.EARLY)
        self.assertEqual("", side.pushed(pr, logged(fixtures.LATE, code=1))[side.PUSHED_KEY])
        for field, value in (("headRepository", None), ("headRefName", ""), ("headRefOid", "x")):
            with self.subTest(field):
                rest = logged(fixtures.LATE)
                self.assertEqual("", side.pushed({**pr, field: value}, rest)[side.PUSHED_KEY])
                rest.assert_not_called()

    @unittest.skipUnless(shutil.which("jq"), "jq is not installed")
    def test_the_projection_prints_the_newest_push_that_left_the_head(self) -> None:
        """A force-push away and back leaves the head twice, and the later arrival is the one."""
        log = [
            {"after": MOVED, "timestamp": "2026-08-02T12:00:00Z"},
            {"after": fixtures.HEAD, "timestamp": fixtures.LATE},
            {"after": fixtures.HEAD, "timestamp": fixtures.EARLY},
        ]
        jq = side.PUSHED_JQ.replace("@HEAD@", fixtures.HEAD)
        for entries, want in ((log, fixtures.LATE), (log[:1], ""), ([], "")):
            with self.subTest(entries=len(entries)):
                proc = subprocess.run(
                    ["jq", "-r", jq],
                    input=json.dumps(entries),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=30,
                )
                self.assertEqual((0, want), (proc.returncode, proc.stdout.strip()))


if __name__ == "__main__":
    unittest.main()
