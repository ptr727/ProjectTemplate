#!/usr/bin/env python3
"""Exercise backlog_metrics.py's counting, rendering, and refusals on constructed fixtures.

No case starts `gh`. The counting and rendering functions take parsed lists, and the cases that
reach `run_gh` replace the subprocess call. The fixtures are invented names and counts.

Run as `uv run --no-project --with-requirements tests/requirements.txt python
tests/test_backlog_metrics.py`, or under the same command with `-m unittest discover -s tests`.
The script imports the review gate's Markdown readers, so it needs those requirements.
"""

import base64
import contextlib
import datetime
import io
import json
import sys
import typing
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import backlog_metrics as bm
import gh_graphql
import pr_review_copilot
import pr_review_rounds


def issue(*labels: str) -> dict[str, object]:
    return {"number": 1, "labels": [{"name": label} for label in labels]}


DEFAULT_POLICY = SCRIPTS.parent / "spec" / "pr-review.default.json"


def policy_text(**triggers: str) -> str:
    """The hub default policy as JSON text, each named bot's trigger replaced."""
    data = json.loads(DEFAULT_POLICY.read_text(encoding="utf-8"))
    for bot, trigger in triggers.items():
        data["bots"][bot]["trigger"] = trigger
    return json.dumps(data)


def metrics(**overrides: object) -> bm.RepoMetrics:
    fields: dict[str, object] = {
        "name": "alpha",
        "opened": 4,
        "closed": 2,
        "handoffs_created": 1,
        "open_now": 6,
        "open_by_label": {label: 0 for label in bm.LABELS},
        "merged": 2,
        "runs": 10,
        "review_runs": 3,
    }
    fields.update(overrides)
    return bm.RepoMetrics(**fields)  # type: ignore[arg-type]


class CountingTests(unittest.TestCase):
    def test_handoff_issues_are_excluded_from_opened_and_counted_apart(self) -> None:
        opened = [issue("bug"), issue("handoff"), issue(), issue("handoff", "decision")]
        result = bm.compute_metrics("alpha", opened, [], [], [], [])
        self.assertEqual(result.opened, 2)
        self.assertEqual(result.handoffs_created, 2)

    def test_closed_and_open_now_exclude_handoff(self) -> None:
        closed = [issue("handoff"), issue("bug"), issue("bug")]
        open_now = [issue("handoff"), issue("prose")]
        result = bm.compute_metrics("alpha", [], closed, open_now, [], [])
        self.assertEqual(result.closed, 2)
        self.assertEqual(result.open_now, 1)

    def test_multi_label_issue_counts_under_each_label(self) -> None:
        open_now = [issue("bug", "script"), issue("bug"), issue("other"), issue("handoff", "bug")]
        result = bm.compute_metrics("alpha", [], [], open_now, [], [])
        self.assertEqual(result.open_by_label["bug"], 2)
        self.assertEqual(result.open_by_label["script"], 1)
        self.assertEqual(result.open_by_label["prose"], 0)
        self.assertEqual(list(result.open_by_label)[: len(bm.LABELS)], list(bm.LABELS))

    def test_a_label_outside_the_named_set_is_counted_after_it(self) -> None:
        open_now = [issue("comments"), issue("agents", "bug"), issue("handoff", "skills")]
        result = bm.compute_metrics("alpha", [], [], open_now, [], [])
        self.assertEqual(list(result.open_by_label)[len(bm.LABELS) :], ["agents", "comments"])
        self.assertEqual(result.open_by_label["comments"], 1)
        self.assertEqual(result.open_by_label["pre-existing"], 0)
        self.assertNotIn("skills", result.open_by_label)
        line = bm.label_line(result)
        self.assertIn("pre-existing 0", line)
        self.assertTrue(line.endswith("agents 1, comments 1"))

    def test_review_runs_match_the_workflow_name_exactly(self) -> None:
        runs = [
            {"name": "Running Copilot Code Review"},
            {"name": "Running Copilot Code Review "},
            {"name": "running copilot code review"},
            {"name": "Validate"},
        ]
        result = bm.compute_metrics("alpha", [], [], [], [{"number": 1}], runs)
        self.assertEqual(result.runs, 4)
        self.assertEqual(result.review_runs, 1)
        self.assertEqual(result.merged, 1)

    def test_a_full_list_marks_the_result_truncated(self) -> None:
        full = [issue()] * bm.LIST_LIMIT
        self.assertTrue(bm.compute_metrics("alpha", full, [], [], [], []).truncated)
        self.assertFalse(bm.compute_metrics("alpha", [issue()], [], [], [], []).truncated)

    def test_per_merged_has_one_decimal_and_na_without_a_merge(self) -> None:
        self.assertEqual(bm.per_merged(7, 2), "3.5")
        self.assertEqual(bm.per_merged(1, 3), "0.3")
        self.assertEqual(bm.per_merged(0, 4), "0.0")
        self.assertEqual(bm.per_merged(5, 0), "n/a")

    def test_the_mean_of_rounds_is_per_merged_with_no_helper_of_its_own(self) -> None:
        stats = {"copilot": bm.BotStats(rounds=[1, 2]), "qodo": bm.BotStats()}
        row = bm.review_bot_rows([bm.ReviewMetrics(name="alpha", bots=stats)])
        index = bm.REVIEW_BOT_HEADERS.index("Rounds per reviewed PR (mean)")
        self.assertEqual([r[index] for r in row], ["1.5", "n/a"])


class BillingTests(unittest.TestCase):
    USAGE: typing.ClassVar[dict[str, object]] = {
        "usageItems": [
            {
                "product": "actions",
                "unitType": "Minutes",
                "repositoryName": "alpha",
                "quantity": 10,
            },
            {
                "product": "actions",
                "unitType": "Minutes",
                "repositoryName": "alpha",
                "quantity": 5.5,
            },
            {"product": "actions", "unitType": "Minutes", "repositoryName": "beta", "quantity": 2},
            {
                "product": "actions",
                "unitType": "GigabyteHours",
                "repositoryName": "beta",
                "quantity": 99,
            },
            {"product": "copilot", "unitType": "Requests", "grossAmount": 1.25},
            {"product": "copilot", "unitType": "Requests", "grossAmount": 0.5},
            {"product": "packages", "unitType": "Minutes", "repositoryName": "beta", "quantity": 7},
        ]
    }

    def test_actions_minutes_sum_per_repository(self) -> None:
        self.assertEqual(bm.sum_actions_minutes(self.USAGE), {"alpha": 15.5, "beta": 2.0})

    def test_copilot_gross_sums_only_copilot(self) -> None:
        self.assertEqual(bm.sum_copilot_gross(self.USAGE), 1.75)

    def test_unavailable_billing_is_one_line_and_no_zero(self) -> None:
        lines = bm.billing_lines("2026-01", ["alpha"], None, "403")
        self.assertEqual(len(lines), 1)
        self.assertIn("unavailable", lines[0])
        self.assertNotIn("0.00", lines[0])

    def test_available_billing_lists_each_repository(self) -> None:
        lines = bm.billing_lines("2026-01", ["alpha", "gamma"], self.USAGE, "")
        self.assertIn("alpha 15.5", lines[0])
        self.assertIn("gamma 0.0", lines[0])
        self.assertIn("1.75", lines[1])


class RenderingTests(unittest.TestCase):
    START = datetime.date(2026, 1, 1)
    END = datetime.date(2026, 1, 8)

    def fixtures(self) -> list[bm.RepoMetrics]:
        return [metrics(), metrics(name="beta", opened=3, merged=0, runs=5, review_runs=1)]

    def test_totals_row_sums_and_recomputes_the_ratio(self) -> None:
        total = bm.table_rows(self.fixtures())[-1]
        self.assertEqual(total[0], "Total")
        self.assertEqual(total[1], "7")
        self.assertEqual(total[6], "3.5")
        self.assertEqual(total[7], "15")
        self.assertEqual(total[9], "2.0")

    def test_markdown_has_dates_once_units_and_label_lines(self) -> None:
        text = bm.render_markdown(self.START, self.END, self.fixtures(), ["Billing line"])
        self.assertEqual(text.count("2026-01-01"), 1)
        self.assertIn("2026-01-08", text)
        self.assertIn("Issues opened per merged PR (ratio)", text)
        self.assertIn("Copilot review runs per merged PR (ratio)", text)
        self.assertIn("| alpha | 4 | 2 | 1 | 6 | 2 | 2.0 | 10 | 3 | 1.5 |", text)
        self.assertIn("| beta | 3 | 2 | 1 | 6 | 0 | n/a | 5 | 1 | n/a |", text)
        self.assertIn("alpha, open issues by label (count): pre-existing 0", text)
        self.assertIn("- Billing line", text)

    def test_html_is_one_table_and_escapes(self) -> None:
        text = bm.render_html(self.START, self.END, [metrics(name="a<b")], ["x & y"])
        self.assertEqual(text.count("<table"), 1)
        self.assertNotIn("<style", text)
        self.assertIn("a&lt;b", text)
        self.assertIn("x &amp; y", text)

    def test_truncation_note_names_the_repository(self) -> None:
        text = bm.render_markdown(self.START, self.END, [metrics(truncated=True)], [])
        self.assertIn("alpha: a list reached", text)

    def test_window_start_subtracts_days(self) -> None:
        self.assertEqual(bm.window_start(self.END, 7), self.START)


class RunGhTests(unittest.TestCase):
    def test_returns_parsed_json(self) -> None:
        with unittest.mock.patch.object(bm, "run_subprocess", return_value=(0, '[{"a": 1}]', "")):
            self.assertEqual(bm.run_gh(["x"]), [{"a": 1}])

    def test_non_zero_exit_raises_with_stderr(self) -> None:
        with (
            unittest.mock.patch.object(bm, "run_subprocess", return_value=(1, "", "boom")),
            self.assertRaisesRegex(bm.GhError, "boom"),
        ):
            bm.run_gh(["x"])

    def test_non_json_output_raises(self) -> None:
        with (
            unittest.mock.patch.object(bm, "run_subprocess", return_value=(0, "not json", "")),
            self.assertRaisesRegex(bm.GhError, "no JSON"),
        ):
            bm.run_gh(["x"])

    def test_a_missing_binary_raises(self) -> None:
        with (
            unittest.mock.patch.object(
                bm, "run_subprocess", side_effect=bm.GraphQLError("could not run gh")
            ),
            self.assertRaisesRegex(bm.GhError, "could not run"),
        ):
            bm.run_gh(["x"])

    def test_billing_failure_degrades_to_none(self) -> None:
        with unittest.mock.patch.object(bm, "run_gh", side_effect=bm.GhError("denied")):
            usage, error = bm.fetch_billing("o", datetime.date(2026, 3, 2))
        self.assertIsNone(usage)
        self.assertIn("denied", error)

    def test_billing_path_zero_pads_the_month(self) -> None:
        with unittest.mock.patch.object(bm, "run_gh", return_value={}) as run:
            bm.fetch_billing("o", datetime.date(2026, 3, 2))
        run.assert_called_once_with(["api", "/users/o/settings/billing/usage?year=2026&month=03"])


class CollectTests(unittest.TestCase):
    def test_each_call_carries_its_repo_filter_and_limit(self) -> None:
        with unittest.mock.patch.object(bm, "run_gh", return_value=[]) as run:
            bm.collect("o", "alpha", datetime.date(2026, 1, 1))
        calls = [call.args[0] for call in run.call_args_list]
        joined = [" ".join(args) for args in calls]
        self.assertEqual(len(calls), 5)
        for args in calls:
            self.assertIn("o/alpha", args)
            self.assertIn("1000", args)
        self.assertTrue(any("--state all" in t and "created:>=2026-01-01" in t for t in joined))
        self.assertTrue(
            any("--state closed --limit 1000 --search closed:>=2026-01-01" in t for t in joined)
        )
        self.assertTrue(
            any("--state merged --search merged:>=2026-01-01" in text for text in joined)
        )
        self.assertTrue(any("--created >=2026-01-01" in text for text in joined))
        self.assertTrue(any("--state open" in text and "--search" not in text for text in joined))
        self.assertTrue(any(text.endswith("--json name") for text in joined))


COPILOT = bm.BOT_LOGINS["copilot"]
RABBIT = bm.BOT_LOGINS["coderabbit"]
QODO = bm.BOT_LOGINS["qodo"]
REFUSED = (
    "Copilot was unable to review this pull request because the user who requested the "
    "review has reached their quota limit."
)
QODO_COMMENT = "<h3>Code Review by Qodo</h3>\nOne finding."
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def comment(
    body: str,
    association: str = "OWNER",
    minimized: bool = False,
    login: str = "p",
    kind: str = "User",
    at: str = "2026-01-01T00:00:00Z",
) -> dict[str, object]:
    return {
        "createdAt": at,
        "body": body,
        "authorAssociation": association,
        "isMinimized": minimized,
        "author": {"login": login, "__typename": kind},
    }


def marker(head: str, findings: str, base: str = "develop") -> str:
    return f"<!-- fleet-local-review: head={head} base={base} findings={findings} -->"


def attest(head: str, findings: str) -> dict[str, object]:
    return comment(marker(head, findings))


def commit(oid: str, state: str | None = None, at: str = "") -> dict[str, object]:
    rollup = {"state": state} if state else None
    return {"commit": {"oid": oid, "statusCheckRollup": rollup, "committedDate": at}}


def thread(login: str, head: str) -> dict[str, object]:
    first = {"author": {"login": login}, "originalCommit": {"oid": head}}
    return {"comments": {"nodes": [first]}}


def pr_payload(**parts: object) -> dict[str, object]:
    base: dict[str, object] = {
        "number": 1,
        "headRefName": parts.pop("head", "feature/x"),
        "baseRefName": "develop",
    }
    for key in ("commits", "reviews", "reviewThreads", "comments", "timelineItems"):
        base[key] = {"pageInfo": {"hasNextPage": False}, "nodes": parts.get(key, [])}
    return base


def requested(actor_type: str, login: str) -> dict[str, object]:
    return {
        "actor": {"login": "x", "__typename": actor_type},
        "requestedReviewer": {"login": login},
    }


class ReviewTests(unittest.TestCase):
    def tally(self, **parts: object) -> bm.ReviewMetrics:
        result = bm.ReviewMetrics(name="alpha")
        for bot in bm.BOT_LOGINS:
            result.bots[bot] = bm.BotStats()
        bm.review_pr(pr_payload(**parts), result, "main")
        return result

    def test_logins_come_from_the_adapter_files(self) -> None:
        self.assertEqual(
            set(bm.BOT_LOGINS.values()),
            {"copilot-pull-request-reviewer", "coderabbitai", "qodo-free-for-open-source-projects"},
        )

    def test_logins_are_the_gates_vocabulary_logins_with_no_local_reader(self) -> None:
        for name, login in bm.BOT_LOGINS.items():
            self.assertIs(login, pr_review_copilot.VOCABULARIES[name]["login"])

    def test_rounds_count_distinct_heads_per_bot_and_commits_are_counted(self) -> None:
        reviews = [
            {"author": {"login": COPILOT}, "commit": {"oid": SHA_A}},
            {"author": {"login": COPILOT}, "commit": {"oid": SHA_A}},
            {"author": {"login": COPILOT}, "commit": {"oid": SHA_B}},
            {"author": {"login": RABBIT}, "commit": {"oid": SHA_A}},
        ]
        result = self.tally(commits=[commit(SHA_A), commit(SHA_B)], reviews=reviews)
        self.assertEqual(result.commits, 2)
        self.assertEqual(result.bots["copilot"].rounds, [2])
        self.assertEqual(result.bots["coderabbit"].rounds, [1])
        self.assertEqual(result.bots["qodo"].rounds, [])

    def test_promotion_prs_are_excluded(self) -> None:
        result = bm.ReviewMetrics(name="alpha")
        promo = pr_payload(head="develop", commits=[commit(SHA_A)])
        promo["baseRefName"] = "main"
        bm.review_pr(promo, result, "main")
        bm.review_pr(pr_payload(head="develop", commits=[commit(SHA_A)]), result, "main")
        self.assertEqual((result.prs, result.promotions_skipped, result.commits), (1, 1, 1))

    def test_requests_split_by_actor_type_and_follow_the_gates_ask_rule(self) -> None:
        comments = [
            comment(f"@{RABBIT} review"),
            comment(f"@{RABBIT} full review", login="b", kind="Bot"),
            comment(f"@{RABBIT} review", association="NONE"),
            comment(f"@{RABBIT} review", minimized=True),
            comment(f"text\n@{RABBIT} review"),
            comment(f"@{RABBIT} reviewing"),
            comment("/review", kind="Bot"),
            comment("hello\n/review"),
            comment(" /review\n"),
        ]
        events = [requested("Bot", COPILOT), requested("User", COPILOT)]
        result = self.tally(comments=comments, timelineItems=events)
        self.assertEqual(result.bots["copilot"].requests_by_automation, 1)
        self.assertEqual(result.bots["copilot"].requests_by_user, 1)
        self.assertEqual(result.bots["coderabbit"].requests_by_user, 2)
        self.assertEqual(result.bots["coderabbit"].requests_by_automation, 0)
        self.assertEqual(result.bots["qodo"].requests_by_user, 2)
        self.assertEqual(result.bots["qodo"].requests_by_automation, 0)

    def test_each_reviewed_head_is_explicit_automatic_or_unclassified(self) -> None:
        def review(login: str, head: str, at: str) -> dict[str, object]:
            return {"author": {"login": login}, "commit": {"oid": head}, "submittedAt": at}

        result = self.tally(
            commits=[
                commit(SHA_A, at="2026-01-01T01:00:00Z"),
                commit(SHA_B, at="2026-01-01T04:00:00Z"),
                commit(SHA_C),
            ],
            reviews=[
                review(RABBIT, SHA_A, "2026-01-01T03:00:00Z"),
                review(RABBIT, SHA_B, "2026-01-01T05:00:00Z"),
                review(RABBIT, SHA_C, "2026-01-01T06:00:00Z"),
                review(QODO, SHA_A, "2026-01-01T03:00:00Z"),
            ],
            comments=[
                comment(f"@{RABBIT} review", at="2026-01-01T02:00:00Z"),
                comment("/review", at="2026-01-01T02:00:00Z"),
            ],
        )
        rabbit = result.bots["coderabbit"]
        self.assertEqual(
            (rabbit.explicit_rounds, rabbit.automatic_rounds, rabbit.unclassified_rounds), (1, 1, 1)
        )
        qodo = result.bots["qodo"]
        self.assertEqual(
            (qodo.explicit_rounds, qodo.automatic_rounds, qodo.unclassified_rounds), (0, 0, 0)
        )

    def test_a_refused_review_is_no_round_and_no_reviewed_head(self) -> None:
        def review(head: str, body: str, at: str) -> dict[str, object]:
            return {
                "author": {"login": RABBIT},
                "commit": {"oid": head},
                "body": body,
                "submittedAt": at,
            }

        result = self.tally(
            commits=[commit(SHA_A, at="2026-01-01T01:00:00Z"), commit(SHA_B)],
            reviews=[
                review(SHA_A, "Looks fine.", "2026-01-01T03:00:00Z"),
                review(SHA_B, REFUSED, "2026-01-01T04:00:00Z"),
                {**review(SHA_C, "Looks fine.", "2026-01-01T04:00:00Z"), "commit": None},
            ],
        )
        rabbit = result.bots["coderabbit"]
        self.assertEqual(rabbit.rounds, [1])
        self.assertEqual(
            (rabbit.explicit_rounds, rabbit.automatic_rounds, rabbit.unclassified_rounds), (0, 1, 0)
        )

    def test_a_review_request_event_times_a_round_as_explicit(self) -> None:
        review = {
            "author": {"login": RABBIT},
            "commit": {"oid": SHA_A},
            "submittedAt": "2026-01-01T03:00:00Z",
        }
        event = {**requested("User", RABBIT), "createdAt": "2026-01-01T02:00:00Z"}
        result = self.tally(
            commits=[commit(SHA_A, at="2026-01-01T01:00:00Z")],
            reviews=[review],
            timelineItems=[event],
        )
        rabbit = result.bots["coderabbit"]
        self.assertEqual((rabbit.explicit_rounds, rabbit.automatic_rounds), (1, 0))
        self.assertEqual(rabbit.requests_by_user, 1)

    def test_times_with_an_offset_are_ordered_in_utc(self) -> None:
        def review(at: str) -> dict[str, object]:
            return {"author": {"login": RABBIT}, "commit": {"oid": SHA_A}, "submittedAt": at}

        early_ask = {**comment(f"@{RABBIT} review"), "createdAt": "2026-01-01T12:00:00Z"}
        result = self.tally(
            commits=[commit(SHA_A, at="2026-01-01T10:00:00-07:00")],
            reviews=[review("2026-01-01T17:05:00Z")],
            comments=[early_ask],
        )
        rabbit = result.bots["coderabbit"]
        self.assertEqual((rabbit.explicit_rounds, rabbit.automatic_rounds), (0, 1))

    def test_a_refusal_before_an_ask_does_not_time_the_round(self) -> None:
        def review(body: str, at: str) -> dict[str, object]:
            return {
                "author": {"login": RABBIT},
                "commit": {"oid": SHA_A},
                "body": body,
                "submittedAt": at,
            }

        result = self.tally(
            commits=[commit(SHA_A, at="2026-01-01T01:00:00Z")],
            reviews=[
                review(REFUSED, "2026-01-01T02:00:00Z"),
                review("Looks fine.", "2026-01-01T04:00:00Z"),
            ],
            comments=[{**comment(f"@{RABBIT} review"), "createdAt": "2026-01-01T03:00:00Z"}],
        )
        rabbit = result.bots["coderabbit"]
        self.assertEqual((rabbit.explicit_rounds, rabbit.automatic_rounds), (1, 0))

    def test_qodo_rounds_follow_its_standing_comment_and_asks_with_no_classes(self) -> None:
        standing = comment(QODO_COMMENT, association="NONE", login=QODO, kind="Bot")
        both = self.tally(comments=[standing, comment("/review"), comment("/review")])
        self.assertEqual(both.bots["qodo"].rounds, [2])
        alone = self.tally(comments=[standing])
        self.assertEqual(alone.bots["qodo"].rounds, [1])
        none = self.tally(comments=[])
        self.assertEqual(none.bots["qodo"].rounds, [])
        heads_only = self.tally(
            reviews=[{"author": {"login": QODO}, "commit": {"oid": SHA_A}, "body": "x"}] * 3
        )
        self.assertEqual(heads_only.bots["qodo"].rounds, [])
        row = bm.review_bot_rows([both])[2]
        start = bm.REVIEW_BOT_HEADERS.index("Automatic rounds (count)")
        self.assertEqual(row[1], "qodo")
        self.assertEqual(row[start : start + 3], ["n/a", "n/a", "n/a"])

    def test_the_review_query_fetches_what_the_gate_readers_read(self) -> None:
        reviews = bm.REVIEW_QUERY.split("reviews(")[1].split("reviewThreads(")[0]
        comments = bm.REVIEW_QUERY.split("comments(first: 100)")[1].split("timelineItems")[0]
        self.assertIn("body", reviews)
        for field in ("authorAssociation", "isMinimized", "body", "createdAt", "__typename"):
            self.assertIn(field, comments)

    def test_local_findings_sum_and_unknown_is_counted_apart(self) -> None:
        comments = [attest(SHA_A, "2"), attest(SHA_B, "unknown"), attest(SHA_C, "0")]
        heads = [commit(SHA_A), commit(SHA_B), commit(SHA_C)]
        result = self.tally(commits=heads, comments=comments)
        self.assertEqual(result.local_findings, 2)
        self.assertEqual(result.local_unknown, 1)

    def test_an_attestation_is_trusted_only_under_every_condition(self) -> None:
        fenced = f"```\n{marker(SHA_A, '9')}\n```"
        comments = [
            comment(marker(SHA_A, "1"), association="NONE"),
            comment(marker(SHA_A, "2"), minimized=True),
            comment(marker(SHA_A, "3", base="other")),
            comment(fenced),
            comment(f"prose {marker(SHA_A, '4')}"),
            comment(f"text\n{marker(SHA_B, '5')}\nmore"),
        ]
        heads = [commit(SHA_A), commit(SHA_B)]
        self.assertEqual(
            bm.attestations(pr_payload(commits=heads, comments=comments)), {SHA_B: "5"}
        )

    def test_the_first_attestation_of_a_head_wins_as_in_the_gate(self) -> None:
        comments = [attest(SHA_A, "2"), attest(SHA_A, "0"), attest(SHA_B, "unknown")]
        heads = [commit(SHA_A), commit(SHA_B), commit(SHA_C)]
        pr = pr_payload(commits=heads, comments=comments)
        self.assertEqual(bm.attestations(pr), {SHA_A: "2", SHA_B: "unknown"})
        gate = pr_review_rounds.attestation({**pr, "headRefOid": SHA_A})
        self.assertEqual(gate, "2")
        result = self.tally(commits=heads, comments=comments)
        self.assertEqual((result.local_findings, result.local_unknown), (2, 1))

    def test_an_attestation_counts_only_for_a_read_commit(self) -> None:
        pr = pr_payload(commits=[commit(SHA_A)], comments=[attest(SHA_A, "1"), attest(SHA_B, "4")])
        self.assertEqual(bm.attestations(pr), {SHA_A: "1"})

    def test_attestations_use_the_gates_fence_rule_and_no_local_copy(self) -> None:
        tilde = f"~~~\n{marker(SHA_A, '7')}\n~~~"
        heads = [commit(SHA_A), commit(SHA_B)]
        pr = pr_payload(commits=heads, comments=[comment(tilde), attest(SHA_B, "1")])
        self.assertEqual(bm.attestations(pr), {SHA_B: "1"})
        self.assertIsNone(pr_review_rounds.attestation({**pr, "headRefOid": SHA_A}))

    def test_bot_findings_missed_by_clean_local_review_and_on_green_heads(self) -> None:
        threads = [
            thread(RABBIT, SHA_A),
            thread(RABBIT, SHA_B),
            thread(QODO, SHA_A),
            thread("someone", SHA_A),
        ]
        result = self.tally(
            commits=[commit(SHA_A, "SUCCESS"), commit(SHA_B, "FAILURE")],
            comments=[attest(SHA_A, "0"), attest(SHA_B, "3")],
            reviewThreads=threads,
        )
        rabbit = result.bots["coderabbit"]
        self.assertEqual(rabbit.findings, 2)
        self.assertEqual(rabbit.missed_by_local, 1)
        self.assertEqual(rabbit.findings_on_green_heads, 1)
        self.assertEqual(result.bots["qodo"].findings, 1)
        self.assertEqual(result.bots["copilot"].findings, 0)

    def test_failing_heads_and_those_after_a_clean_attestation(self) -> None:
        result = self.tally(
            commits=[commit(SHA_A, "FAILURE"), commit(SHA_B, "ERROR"), commit(SHA_C, "SUCCESS")],
            comments=[attest(SHA_A, "0"), attest(SHA_B, "1")],
        )
        self.assertEqual(result.failing_heads, 2)
        self.assertEqual(result.failing_after_clean_local, 1)

    def test_head_state_reads_the_status_rollup(self) -> None:
        def state(value: str | None) -> str:
            return bm.head_state(commit(SHA_A, value)["commit"])  # type: ignore[arg-type]

        self.assertEqual(state("SUCCESS"), "green")
        self.assertEqual([state("FAILURE"), state("ERROR")], ["failed", "failed"])
        self.assertEqual([state("PENDING"), state("EXPECTED"), state(None)], ["other"] * 3)

    def test_a_cut_connection_is_recorded_as_truncated(self) -> None:
        pr = pr_payload()
        pr["commits"] = {"pageInfo": {"hasNextPage": True}, "nodes": []}
        result = bm.ReviewMetrics(name="alpha")
        bm.review_pr(pr, result, "main")
        self.assertEqual(result.truncated, [f"PR 1 commits past {bm.COMMIT_CAP}"])
        note = bm.review_notes([result])[0]
        self.assertIn(f"PR 1 commits past {bm.COMMIT_CAP}", note)
        self.assertNotIn(str(bm.PR_CAP), note)


class PolicyTests(unittest.TestCase):
    @staticmethod
    def payload(text: str) -> dict[str, str]:
        return {"content": base64.b64encode(text.encode()).decode()}

    def fetch(
        self, *, side_effect: Exception | None = None, return_value: object = None
    ) -> tuple[dict[str, str], str]:
        patch = unittest.mock.patch.object(
            bm, "run_gh", side_effect=side_effect, return_value=return_value
        )
        with patch:
            return bm.fetch_policy("o", "alpha", "develop")

    def test_a_404_reads_the_hub_default_marked_as_such(self) -> None:
        triggers, error = self.fetch(side_effect=bm.GhError("gh: Not Found (HTTP 404)"))
        self.assertEqual(error, "")
        default = json.loads(DEFAULT_POLICY.read_text(encoding="utf-8"))["bots"]
        self.assertEqual(
            triggers, {bot: f"{default[bot]['trigger']} (default)" for bot in bm.BOT_LOGINS}
        )

    def test_a_422_is_absent_as_the_gate_reads_it(self) -> None:
        triggers, error = self.fetch(side_effect=bm.GhError("gh: No commit found (HTTP 422)"))
        self.assertEqual(error, "")
        self.assertTrue(all(t.endswith(" (default)") for t in triggers.values()))

    def test_a_read_carrying_no_content_is_an_error(self) -> None:
        payloads: list[object] = [{"content": None}, {"content": ""}, []]
        for payload in payloads:
            triggers, error = self.fetch(return_value=payload)
            self.assertEqual(triggers, {})
            self.assertIn("carried no content", error)

    def test_any_other_error_is_an_error_not_absent(self) -> None:
        triggers, error = self.fetch(side_effect=bm.GhError("gh: Forbidden (HTTP 403)"))
        self.assertEqual(triggers, {})
        self.assertIn("403", error)

    def test_triggers_are_read_per_bot(self) -> None:
        text = policy_text(qodo="explicit", coderabbit="off")
        triggers, error = self.fetch(return_value=self.payload(text))
        self.assertEqual(error, "")
        self.assertEqual(triggers, {"copilot": "auto", "coderabbit": "off", "qodo": "explicit"})

    def test_a_policy_the_gate_refuses_is_an_error(self) -> None:
        data = json.loads(policy_text(copilot="explicit"))
        data["bots"]["copilot"]["gate"] = False
        triggers, error = self.fetch(return_value=self.payload(json.dumps(data)))
        self.assertEqual(triggers, {})
        self.assertIn("gate false", error)

    def test_a_policy_omitting_a_bot_is_an_error(self) -> None:
        data = json.loads(policy_text())
        del data["bots"]["qodo"]
        triggers, error = self.fetch(return_value=self.payload(json.dumps(data)))
        self.assertEqual(triggers, {})
        self.assertIn("missing qodo", error)

    def test_invalid_json_is_an_error(self) -> None:
        _triggers, error = self.fetch(return_value=self.payload("{nope"))
        self.assertIn("unreadable", error)


def page(
    numbers: list[int], more: bool, total: int, branch: str | None = "main"
) -> dict[str, object]:
    nodes = [pr_payload() | {"number": n} for n in numbers]
    return {
        "repository": {"defaultBranchRef": {"name": branch} if branch else None},
        "search": {
            "issueCount": total,
            "pageInfo": {"hasNextPage": more, "endCursor": "c" if more else None},
            "nodes": nodes,
        },
    }


def quiet_rest(args: list[str]) -> object:
    if "contents" in args[1] or "branches" in args[1]:
        raise bm.GhError("gh: Not Found (HTTP 404)")
    return []


class ReviewCollectTests(unittest.TestCase):
    def collect(self, pages: list[dict[str, object]]) -> bm.ReviewMetrics:
        with (
            unittest.mock.patch.object(bm, "graphql", side_effect=pages) as gql,
            unittest.mock.patch.object(bm, "run_gh", side_effect=quiet_rest),
        ):
            result = bm.collect_reviews("o", "alpha", datetime.date(2026, 1, 1))
        self.gql = gql
        return result

    def test_the_search_is_ordered_and_scoped_to_the_window(self) -> None:
        self.collect([page([], False, 0)])
        variables = self.gql.call_args_list[0].args[1]
        self.assertIn("merged:>=2026-01-01", variables["search"])
        self.assertIn("sort:created-desc", variables["search"])
        self.assertNotIn("after", variables)

    def test_pages_are_followed_with_the_cursor(self) -> None:
        result = self.collect([page([1, 2], True, 3), page([3], False, 3)])
        self.assertEqual(result.prs, 3)
        self.assertEqual(self.gql.call_args_list[1].args[1]["after"], "c")
        self.assertEqual(result.truncated, [])

    def test_the_hard_cap_stops_reading_and_is_reported(self) -> None:
        with unittest.mock.patch.object(bm, "PR_CAP", 3):
            result = self.collect([page([1, 2], True, 9), page([3, 4], True, 9)])
        self.assertEqual(result.prs, 3)
        self.assertEqual(self.gql.call_count, 2)
        self.assertEqual(result.truncated, ["9 merged PRs, 3 read"])

    def test_exactly_the_cap_is_not_truncated(self) -> None:
        with unittest.mock.patch.object(bm, "PR_CAP", 2):
            result = self.collect([page([1, 2], False, 2)])
        self.assertEqual(result.truncated, [])

    def test_a_null_node_is_a_reported_error(self) -> None:
        bad = {"data": {"search": {"nodes": [None]}}}
        started: list[list[str]] = []

        def fake_run(argv: list[str]) -> tuple[int, str, str]:
            started.append(argv)
            return 0, json.dumps(bad), ""

        def routed(query: str, variables: dict[str, object], **_: object) -> gh_graphql.Response:
            return gh_graphql.request(query, variables, run=fake_run)

        with (
            unittest.mock.patch.object(bm, "request", routed),
            self.assertRaises(bm.GhError),
        ):
            bm.collect_reviews("o", "alpha", datetime.date(2026, 1, 1))
        self.assertEqual(len(started), 1)

    def test_a_graphql_error_becomes_a_gh_error(self) -> None:
        with (
            unittest.mock.patch.object(
                bm, "request", side_effect=bm.GraphQLError("boom", "detail")
            ),
            self.assertRaises(bm.GhError) as raised,
        ):
            bm.collect_reviews("o", "alpha", datetime.date(2026, 1, 1))
        self.assertIn("detail", str(raised.exception))

    def test_a_walk_error_becomes_a_gh_error_and_main_names_it(self) -> None:
        stuck = page([1], True, 2)
        stuck["search"]["pageInfo"]["endCursor"] = None  # type: ignore[index]
        with (
            unittest.mock.patch.object(bm, "graphql", return_value=stuck),
            self.assertRaises(bm.GhError) as raised,
        ):
            bm.collect_reviews("o", "alpha", datetime.date(2026, 1, 1))
        self.assertIn("without a cursor", str(raised.exception))
        out, err = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(bm, "graphql", return_value=stuck),
            unittest.mock.patch.object(
                bm,
                "run_gh",
                side_effect=lambda args: {"usageItems": []} if args[0] == "api" else [],
            ),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = bm.main(["--owner", "o", "--repo", "alpha"])
        self.assertEqual(code, 1)
        self.assertIn("backlog_metrics.py: reading the review figures failed", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_a_repeated_pr_is_counted_once_and_noted(self) -> None:
        result = self.collect([page([1, 2], True, 3), page([2, 3], False, 3)])
        self.assertEqual(result.prs, 3)
        self.assertEqual(result.duplicates_skipped, 1)
        self.assertIn("counted once", bm.review_notes([result])[0])

    def test_the_policy_is_read_at_develop_where_it_exists(self) -> None:
        seen: list[str] = []

        def fake(args: list[str]) -> object:
            seen.append(args[1])
            if "contents" in args[1]:
                raise bm.GhError("gh: Not Found (HTTP 404)")
            return []

        with (
            unittest.mock.patch.object(bm, "graphql", return_value=page([], False, 0)),
            unittest.mock.patch.object(bm, "run_gh", side_effect=fake),
        ):
            result = bm.collect_reviews("o", "alpha", datetime.date(2026, 1, 1))
        self.assertEqual(result.policy_branch, "develop")
        self.assertTrue(any("contents" in u and "ref=develop" in u for u in seen))
        self.assertTrue(any("commits" in u and "sha=develop" in u for u in seen))

    def test_without_develop_the_default_branch_is_read(self) -> None:
        for status in ("404", "422"):
            absent = bm.GhError(f"gh: Not Found (HTTP {status})")
            with unittest.mock.patch.object(bm, "run_gh", side_effect=absent):
                self.assertEqual(bm.policy_branch("o", "a", "main"), ("main", ""))
        with unittest.mock.patch.object(bm, "run_gh", side_effect=bm.GhError("gh: x (HTTP 500)")):
            branch, error = bm.policy_branch("o", "a", "main")
        self.assertEqual(branch, "")
        self.assertIn("500", error)

    def test_a_failed_develop_probe_prints_no_branch_and_exits_one(self) -> None:
        def fake(args: list[str]) -> object:
            if "branches" in args[1]:
                raise bm.GhError("gh: Server Error (HTTP 500)")
            return {}

        out, err = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(bm, "graphql", return_value=page([], False, 0)),
            unittest.mock.patch.object(bm, "run_gh", side_effect=fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = bm.main(["--owner", "o", "--repo", "alpha"])
        self.assertEqual(code, 1)
        self.assertIn("| copilot | error | n/a |", out.getvalue())
        self.assertEqual(
            err.getvalue(), "backlog_metrics.py: alpha: reading the develop branch failed\n"
        )
        self.assertIn("alpha: reading the develop branch failed: gh: Server Error", out.getvalue())

    def test_a_failed_history_keeps_the_trigger_and_exits_one(self) -> None:
        text = policy_text(qodo="explicit")
        body = {"content": base64.b64encode(text.encode()).decode()}

        def fake(args: list[str]) -> object:
            if "contents" in args[1]:
                return body
            if "commits" in args[1]:
                raise bm.GhError("gh: Server Error (HTTP 500)")
            return {}

        out, err = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(bm, "graphql", return_value=page([], False, 0)),
            unittest.mock.patch.object(bm, "run_gh", side_effect=fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = bm.main(["--owner", "o", "--repo", "alpha"])
        self.assertEqual(code, 1)
        self.assertIn("| qodo | explicit | develop |", out.getvalue())
        self.assertIn("history failed", out.getvalue())
        self.assertEqual(
            err.getvalue(), "backlog_metrics.py: alpha: reading the policy history failed\n"
        )

    def test_history_reports_more_only_past_the_cap(self) -> None:
        def commits(count: int) -> list[dict[str, object]]:
            return [{"commit": {"committer": {"date": "2026-01-03T00:00:00Z"}}}] * count

        since = datetime.date(2026, 1, 1)
        with unittest.mock.patch.object(bm, "run_gh", return_value=commits(bm.HISTORY_CAP)) as run:
            dates, _ = bm.fetch_policy_changes("o", "a", "main", since)
        self.assertEqual(len(dates), bm.HISTORY_CAP)
        self.assertIn(f"per_page={bm.HISTORY_CAP + 1}", run.call_args.args[0][1])
        with unittest.mock.patch.object(bm, "run_gh", return_value=commits(bm.HISTORY_CAP + 1)):
            dates, _ = bm.fetch_policy_changes("o", "a", "main", since)
        self.assertEqual(dates[-1], f"more than {bm.HISTORY_CAP} commits")
        self.assertEqual(len(dates), bm.HISTORY_CAP + 1)

    def test_a_missing_default_branch_is_an_error_not_an_empty_history(self) -> None:
        result = self.collect([page([], False, 0, branch=None)])
        self.assertEqual(result.policy_read, "default branch")
        self.assertIn("reading the default branch failed", bm.review_notes([result])[0])

    def test_the_history_read_names_the_branch_path_and_window(self) -> None:
        with unittest.mock.patch.object(bm, "run_gh", return_value=[]) as run:
            bm.fetch_policy_changes("o", "alpha", "main", datetime.date(2026, 1, 1))
        url = run.call_args.args[0][1]
        for part in (
            "path=.github%2Fpr-review.json",
            "sha=main",
            "since=2026-01-01T",
            "per_page=11",
        ):
            self.assertIn(part, url)

    def test_history_dates_and_notes(self) -> None:
        commits = [{"commit": {"committer": {"date": "2026-01-03T10:00:00Z"}}}]
        with unittest.mock.patch.object(bm, "run_gh", return_value=commits):
            dates, error = bm.fetch_policy_changes("o", "alpha", "main", datetime.date(2026, 1, 1))
        self.assertEqual((dates, error), (["2026-01-03"], ""))
        result = bm.ReviewMetrics(name="alpha", policy_changes=dates)
        self.assertIn("2026-01-03", bm.review_notes([result])[0])

    def test_any_history_error_including_a_404_is_an_error(self) -> None:
        since = datetime.date(2026, 1, 1)
        for status in ("404", "500"):
            with unittest.mock.patch.object(
                bm, "run_gh", side_effect=bm.GhError(f"gh: x (HTTP {status})")
            ):
                dates, error = bm.fetch_policy_changes("o", "a", "main", since)
            self.assertEqual(dates, [])
            self.assertIn(status, error)

    def test_a_policy_error_still_writes_the_report_and_exits_one(self) -> None:
        def fake(args: list[str]) -> object:
            if args[0] == "api" and "contents" in args[1]:
                raise bm.GhError("gh: Forbidden (HTTP 403)")
            if args[0] == "api" and "commits" in args[1]:
                return []
            if args[0] == "api":
                return {"usageItems": []}
            return []

        out, err = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(bm, "graphql", return_value=page([], False, 0)),
            unittest.mock.patch.object(bm, "run_gh", side_effect=fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = bm.main(["--owner", "o", "--repo", "alpha"])
        self.assertEqual(code, 1)
        self.assertIn("Review return on cost, per bot", out.getvalue())
        self.assertIn("| error |", out.getvalue())
        self.assertEqual(
            err.getvalue(), "backlog_metrics.py: alpha: reading the policy file failed\n"
        )

    def test_a_failed_review_query_still_writes_the_backlog_report_and_exits_one(self) -> None:
        def fake(args: list[str]) -> object:
            if args[0] == "api":
                return {"usageItems": []}
            return [issue("bug")] if args[0] == "issue" else [{"number": 1}]

        out, err = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(bm, "graphql", side_effect=bm.GhError("null node")),
            unittest.mock.patch.object(bm, "run_gh", side_effect=fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = bm.main(["--owner", "o", "--repo", "alpha"])
        self.assertEqual(code, 1)
        report = out.getvalue()
        self.assertIn("| alpha |", report)
        self.assertIn("Actions minutes", report)
        self.assertIn("Review figures were unavailable: null node", report)
        self.assertNotIn("Review return on cost", report)
        self.assertEqual(
            err.getvalue(), "backlog_metrics.py: reading the review figures failed: null node\n"
        )

    def test_both_formats_carry_the_review_tables(self) -> None:
        result = bm.ReviewMetrics(name="alpha", bots={"copilot": bm.BotStats(rounds=[1, 3])})
        start, end = datetime.date(2026, 1, 1), datetime.date(2026, 1, 8)
        md = bm.render_markdown(start, end, [metrics()], [], [result])
        page_html = bm.render_html(start, end, [metrics()], [], [result])
        self.assertIn("| alpha | copilot | n/a | n/a | 0 | 0 | n/a | n/a | n/a | 2.0 | 3 |", md)
        self.assertIn("Threaded findings (count)", md)
        self.assertEqual(page_html.count("<table"), 3)

    def test_a_side_reviewer_row_prints_each_round_class(self) -> None:
        stats = bm.BotStats(automatic_rounds=4, explicit_rounds=2, unclassified_rounds=1)
        result = bm.ReviewMetrics(name="alpha", bots={"coderabbit": stats})
        row = bm.review_bot_rows([result])[0]
        start = bm.REVIEW_BOT_HEADERS.index("Automatic rounds (count)")
        self.assertEqual(
            bm.REVIEW_BOT_HEADERS[start : start + 3],
            ("Automatic rounds (count)", "Explicit rounds (count)", "Unclassified rounds (count)"),
        )
        self.assertEqual(row[start : start + 3], ["4", "2", "1"])
        self.assertEqual(len(row), len(bm.REVIEW_BOT_HEADERS))


class MainTests(unittest.TestCase):
    def test_main_writes_the_report_to_stdout(self) -> None:
        def fake(args: list[str]) -> object:
            if args[0] == "api" and "contents" in args[1]:
                raise bm.GhError("gh api exited 1: gh: Not Found (HTTP 404)")
            if args[0] == "api" and "commits" in args[1]:
                return []
            if args[0] == "api":
                return {"usageItems": []}
            if args[0] == "issue":
                return [issue("bug")]
            if args[0] == "run":
                return [{"name": "Running Copilot Code Review"}]
            return [{"number": 1}]

        out = io.StringIO()
        with (
            unittest.mock.patch.object(bm, "graphql", return_value=page([], False, 0)),
            unittest.mock.patch.object(bm, "run_gh", side_effect=fake),
            contextlib.redirect_stdout(out),
        ):
            code = bm.main(["--owner", "o", "--repo", "alpha", "--repo", "beta", "--days", "3"])
        self.assertEqual(code, 0)
        self.assertIn("| alpha |", out.getvalue())
        self.assertIn("| beta |", out.getvalue())
        self.assertIn("| Total |", out.getvalue())

    def test_a_failed_count_exits_one_without_a_report(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(bm, "run_gh", side_effect=bm.GhError("nope")),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = bm.main(["--owner", "o", "--repo", "alpha"])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("nope", err.getvalue())

    def test_unknown_argument_is_refused(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            bm.main(["--bogus-flag"])
        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("--bogus-flag", err.getvalue())

    def test_help_exits_zero_with_usage(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            bm.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertTrue(out.getvalue().startswith("usage: backlog_metrics.py"))

    def test_registry_default_lists_only_cataloged_repositories(self) -> None:
        data = json.loads(bm.REGISTRY.read_text(encoding="utf-8"))
        owner, names = bm.registry_defaults()
        self.assertEqual(owner, data["owner"])
        self.assertTrue(names)
        self.assertEqual(
            names, [r["name"] for r in data["repos"] if r.get("status") == "cataloged"]
        )


if __name__ == "__main__":
    unittest.main()
