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
import io
import json
import re
import subprocess
import sys
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_review
import pr_review_policy as policy
import pr_review_rounds as rounds
import pr_review_side as side
import test_pr_review as fixtures

REPO = Path(__file__).resolve().parent.parent
MOVED = "b" * 40
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
WINDOWS = (300.0, 1800.0)
ARGS = argparse.Namespace(timeout=60.0, check_grace=WINDOWS[0], check_stall=WINDOWS[1])
DEFAULT = json.loads((REPO / "spec" / "pr-review.default.json").read_text(encoding="utf-8"))


def stated(**bots: dict) -> dict:
    """The default policy's data with each named bot's settings replaced by `bots`."""
    data = copy.deepcopy(DEFAULT)
    for name, settings in bots.items():
        data["bots"][name] = settings
    return data


def parsed(**bots: dict) -> policy.Policy:
    return policy.parse(stated(**bots), "test")


def with_policy(
    pr: dict, rules: policy.Policy, base: str = "develop", default: str = "main"
) -> dict:
    """`pr` into `base`, the default branch named, carrying `rules` the way `gql` attaches them."""
    pr["baseRefName"] = base
    pr["baseRepository"] = {"defaultBranchRef": {"name": default}}
    pr[policy.PAYLOAD_KEY] = rules
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
    """`pr` with its head commit stamped as committed at `at`."""
    pr["commits"]["nodes"][-1]["commit"]["checkSuites"] = {"nodes": [{"createdAt": at}]}
    return pr


class TestParse(unittest.TestCase):
    def test_the_hub_default_parses_and_names_every_bot(self) -> None:
        rules = policy.load_default()
        self.assertEqual(set(policy.BOTS), set(rules.bots))

    def test_the_default_matches_the_schema_file_it_names(self) -> None:
        self.assertEqual("./pr-review.schema.json", DEFAULT["$schema"])
        self.assertTrue((REPO / "spec" / "pr-review.schema.json").is_file())

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
                "bots": {k: v for k, v in DEFAULT["bots"].items() if k != "qodo"},
            },
            "unknown bot(s) other": {**DEFAULT, "bots": {**DEFAULT["bots"], "other": {}}},
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
            ("explicit", False): True,
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
        with self.assertRaisesRegex(policy.PolicyError, "o/r:.github/pr-review.json@develop"):
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

    def test_the_payload_key_matches_the_reader_constant(self) -> None:
        self.assertEqual(policy.PAYLOAD_KEY, rounds.POLICY_KEY)


EXPLICIT_QODO = {"trigger": "explicit", "bases": ["^develop$"]}
EXPLICIT_RABBIT = {
    "trigger": "explicit",
    "bases": ["^develop$"],
    "command": "full review",
}


class TestSideReaders(unittest.TestCase):
    def test_a_request_counts_for_the_head_only_once_it_postdates_the_commit(self) -> None:
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
        def send(body: str) -> dict:
            node = posted(body)["addComment"]["commentEdge"]["node"]
            return answer if answer is not None else node

        post, green = mock.MagicMock(side_effect=send), mock.MagicMock(return_value=held)
        side.request_side_reviews(lambda: pr, green, post, phase)
        return post, green

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

    def test_before_any_suite_the_commit_time_bounds_an_ask(self) -> None:
        pr = fixtures.payload([], comments=[trusted("/review", fixtures.LATE)])
        pr["commits"]["nodes"][-1]["commit"]["committedDate"] = fixtures.EARLY
        self.assertTrue(side.side_asked_on_head(pr, "/review"))
        pr["commits"]["nodes"][-1]["commit"]["committedDate"] = "2026-08-02T12:00:00Z"
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
        bare = with_policy(
            fixtures.payload([], comments=[trusted("/review", fixtures.LATE)]),
            parsed(qodo=EXPLICIT_QODO),
        )
        self.assertFalse(side.side_asked_on_head(bare, "/review"))
        self.assertEqual("PENDING", side.checks_hold(bare, NOW, *WINDOWS))


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

    def test_copilot_is_wanted_only_on_a_base_it_covers(self) -> None:
        bot = parsed(copilot={"trigger": "explicit", "bases": ["@default"]}).bot("copilot")
        main = with_policy(fixtures.payload([]), policy.load_default(), "main")
        develop = with_policy(fixtures.payload([]), policy.load_default(), "develop")
        self.assertTrue(side.copilot_wanted(bot, main))
        self.assertFalse(side.copilot_wanted(bot, develop))


class TestSchemaParity(unittest.TestCase):
    """The schema an editor validates against states exactly the rules the reader enforces."""

    def test_every_enum_and_key_matches_the_reader(self) -> None:
        schema = json.loads((REPO / "spec" / "pr-review.schema.json").read_text(encoding="utf-8"))
        bot = schema["$defs"]["bot"]["properties"]
        self.assertEqual(set(policy.BOT_KEYS), set(bot))
        self.assertEqual(set(policy.TRIGGERS), set(bot["trigger"]["enum"]))
        self.assertEqual(set(policy.WHENS), set(bot["when"]["enum"]))
        self.assertEqual(set(policy.ROUNDS), set(bot["rounds"]["enum"]))
        bots = schema["properties"]["bots"]["properties"]
        self.assertEqual(set(policy.BOTS), set(bots))
        self.assertEqual(
            set(policy.COMMANDS), set(bots["coderabbit"]["properties"]["command"]["enum"])
        )
        for name in ("copilot", "qodo"):
            self.assertEqual({"required": ["command"]}, bots[name]["not"], name)


class TestCopilotGate(unittest.TestCase):
    def test_an_ungated_copilot_hands_every_head_to_the_local_pass(self) -> None:
        rules = parsed(copilot={"trigger": "auto", "bases": ["@default"], "gate": False})
        self.assertFalse(rounds.promotion(with_policy(fixtures.payload([]), rules, "main")))


class TestWaitHoldsForChecks(fixtures.GqlCase):
    """An explicit Copilot policy waiting for green checks requests nothing on a red or running head."""

    def run_wait(self, node: dict) -> tuple[int, str, mock.MagicMock]:
        rules = parsed(
            copilot={"trigger": "explicit", "bases": ["^develop$"], "when": "after-checks"}
        )
        pr = with_policy(fixtures.payload([], checks=[node]), rules)
        self.answer(pr)
        sent = mock.MagicMock(return_value={"repository": {"pullRequests": {"nodes": []}}})
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            mock.patch.object(pr_review, "gh_graphql", sent),
            mock.patch.object(pr_review.time, "sleep"),
        ):
            code = pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"])
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
            mock.patch.object(pr_review.time, "sleep"),
        ):
            pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"])
        asks = [c for c in sent.call_args_list if c.args and "addComment" in c.args[0]]
        self.assertEqual(1, len(asks), out.getvalue())
        self.assertEqual("/review", asks[0].kwargs["body"])
        self.assertIn("auto-request: asked qodo", out.getvalue())


if __name__ == "__main__":
    unittest.main()
