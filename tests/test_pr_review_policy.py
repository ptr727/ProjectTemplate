#!/usr/bin/env python3
"""Drive the review policy's reader and the gate's use of it against constructed policies.

The policy decides which bots a pull request waits on and which ones `wait` asks. A wrong reading
either asks a paid reviewer for a round nobody wanted or merges a head nobody reviewed. So each
case below pins one decision. The policy-free payloads the rest of the suite builds must keep the
behavior the gate had before policies existed, which the neutrality cases here hold.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import io
import json
import re
import subprocess
import sys
import unittest
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
    pr["commits"]["nodes"][-1]["commit"]["committedDate"] = at
    return pr


class TestParse(unittest.TestCase):
    def test_the_hub_default_parses_and_names_every_bot(self) -> None:
        rules = policy.load_default()
        self.assertEqual(set(policy.BOTS), set(rules.bots))
        self.assertEqual("copilot=auto coderabbit=auto qodo=auto", policy.describe(rules))

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
            bot = parsed(qodo={"trigger": trigger, "bases": [], "gate": gate}).bot("qodo")
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
        found = side.contents_reader(rest, pr_review.answered_absent)(
            "o", "r", policy.POLICY_PATH, ref
        )
        return found, rest

    def test_a_404_is_absent_and_the_ref_is_quoted(self) -> None:
        found, rest = self.read(proc(1, err="Not Found (HTTP 404)"), "feature/x")
        self.assertIsNone(found)
        self.assertIn("?ref=feature%2Fx", rest.call_args.args[0])

    def test_any_other_failure_ends_the_run_rather_than_reading_the_default(self) -> None:
        for answer in (proc(1, err="Server Error (HTTP 500)"), proc(0, out="")):
            with self.subTest(answer=answer), self.assertRaisesRegex(SystemExit, "was not read"):
                self.read(answer)

    def test_a_read_returns_the_content_field(self) -> None:
        self.assertEqual("abc=", self.read(proc(0, out="abc=\n"))[0])


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


EXPLICIT_QODO = {"trigger": "explicit", "bases": ["^develop$"], "gate": True}
EXPLICIT_RABBIT = {
    "trigger": "explicit",
    "bases": ["^develop$"],
    "gate": True,
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
        self.assertFalse(side.side_answered(moved, "coderabbit", body))
        on_head = fixtures.payload([fixtures.review(login=rounds.CODERABBIT_LOGIN)])
        self.assertTrue(side.side_answered(on_head, "coderabbit", body))
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


class TestSideUnmet(SideCase):
    def test_a_gated_bot_not_yet_asked_is_owed(self) -> None:
        self.assertEqual(["qodo"], side.side_unmet(self.ruled(qodo=EXPLICIT_QODO)))

    def test_an_ungated_or_uncovered_bot_is_not(self) -> None:
        self.assertEqual([], side.side_unmet(self.ruled(qodo={**EXPLICIT_QODO, "gate": False})))
        self.assertEqual(
            [], side.side_unmet(self.ruled(qodo={**EXPLICIT_QODO, "bases": ["^main$"]}))
        )

    def test_an_asked_bot_stays_owed_although_its_one_round_is_spent(self) -> None:
        asked = [trusted("/review", fixtures.LATE)]
        self.assertEqual(
            ["qodo"],
            side.side_unmet(self.ruled(asked, qodo={**EXPLICIT_QODO, "rounds": "once"})),
        )

    def test_a_spent_cap_with_no_request_on_this_head_owes_nothing(self) -> None:
        pr = self.ruled(
            [trusted("/review", "2026-08-02T09:00:00Z")], qodo={**EXPLICIT_QODO, "rounds": "once"}
        )
        self.assertEqual([], side.side_unmet(pr))

    def test_no_policy_on_the_payload_names_no_side_reviewer(self) -> None:
        self.assertEqual([], side.side_reviewers(fixtures.payload([])))


def posted(body: str) -> dict:
    return {
        "addComment": {
            "commentEdge": {"node": {"id": "IC_1", "url": "https://example.test/c/1", "body": body}}
        }
    }


class TestRequestSideReviews(SideCase):
    def run_requests(
        self, pr: dict, answer: dict | None = None, held: str = ""
    ) -> tuple[mock.MagicMock, mock.MagicMock]:
        def send(body: str) -> dict:
            node = posted(body)["addComment"]["commentEdge"]["node"]
            return answer if answer is not None else node

        post, green = mock.MagicMock(side_effect=send), mock.MagicMock(return_value=held)
        side.request_side_reviews(pr, green, post)
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
        sent, green = self.run_requests(pr, held="FAILURE")
        self.assertEqual(0, sent.call_count)
        green.assert_called_once()

    def test_an_unconfirmed_request_ends_the_run(self) -> None:
        wrong = {"url": "", "body": ""}
        with self.assertRaisesRegex(SystemExit, "unconfirmed"):
            self.run_requests(self.ruled(qodo=EXPLICIT_QODO), answer=wrong)


class TestHeadRollup(unittest.TestCase):
    def test_a_null_rollup_is_none_and_a_missing_head_is_unread(self) -> None:
        self.assertEqual("NONE", side.head_rollup(fixtures.payload([])))
        self.assertEqual("PENDING", side.head_rollup(fixtures.payload([], checks=[])))
        self.assertEqual("", side.head_rollup({"commits": {"nodes": []}}))

    def test_await_green_names_what_holds_the_review(self) -> None:
        def rolled(state: str | None) -> dict:
            pr = fixtures.payload([], checks=[])
            commit = pr["commits"]["nodes"][-1]["commit"]
            commit["statusCheckRollup"] = None if state is None else {"state": state}
            return pr

        cases = {"SUCCESS": "", None: "", "FAILURE": "FAILURE", "ERROR": "ERROR"}
        for state, want in cases.items():
            with self.subTest(state=state), mock.patch.object(pr_review.time, "sleep"):
                read = mock.MagicMock(return_value=rolled(state))
                self.assertEqual(want, side.await_green(read, 0.0, 0.0, pr_review.backoff))

    def test_checks_still_running_at_the_bound_read_pending(self) -> None:
        pending = fixtures.payload([], checks=[])
        with mock.patch.object(pr_review.time, "monotonic", return_value=10.0):
            held = side.await_green(lambda: pending, 0.0, 1.0, pr_review.backoff)
        self.assertEqual("PENDING", held)


class TestWaitHoldsForChecks(fixtures.GqlCase):
    """An explicit Copilot policy waiting for green checks requests nothing on a red or running head."""

    def run_wait(self, state: str) -> tuple[int, str, mock.MagicMock]:
        rules = parsed(
            copilot={"trigger": "explicit", "bases": ["^develop$"], "when": "after-checks"}
        )
        pr = with_policy(fixtures.payload([], checks=[]), rules)
        pr["commits"]["nodes"][-1]["commit"]["statusCheckRollup"]["state"] = state
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
        code, out, sent = self.run_wait("FAILURE")
        self.assertEqual(40, code, out)
        self.assertIn("status=CHECKS_BEFORE_REVIEW the head's checks read FAILURE", out)
        self.assertFalse(self.requested(sent))

    def test_checks_still_running_at_the_timeout_exit_30(self) -> None:
        code, out, sent = self.run_wait("PENDING")
        self.assertEqual(30, code, out)
        self.assertIn("checks read PENDING", out)
        self.assertFalse(self.requested(sent))


if __name__ == "__main__":
    unittest.main()
