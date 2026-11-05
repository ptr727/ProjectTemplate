#!/usr/bin/env python3
"""Exercise backlog_metrics.py's counting, rendering, and refusals on constructed fixtures.

No case starts `gh`. The counting and rendering functions take parsed lists, and the cases that
reach `run_gh` replace the subprocess call. The fixtures are invented names and counts.

Run as `python3 tests/test_backlog_metrics.py`, or under
`python3 -m unittest discover -s tests`.
"""

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


def issue(*labels: str) -> dict[str, object]:
    return {"number": 1, "labels": [{"name": label} for label in labels]}


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
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def attest(head: str, findings: str) -> dict[str, object]:
    body = f"<!-- fleet-local-review: head={head} base=develop findings={findings} -->"
    return {"body": body, "author": {"login": "someone", "__typename": "User"}}


def commit(oid: str, *conclusions: str) -> dict[str, object]:
    suites = [{"conclusion": c} for c in conclusions]
    return {"commit": {"oid": oid, "checkSuites": {"nodes": suites}}}


def thread(login: str, head: str) -> dict[str, object]:
    first = {"author": {"login": login}, "originalCommit": {"oid": head}}
    return {"comments": {"nodes": [first]}}


def pr_payload(**parts: object) -> dict[str, object]:
    base: dict[str, object] = {"number": 1}
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
        bm.review_pr(pr_payload(**parts), result)
        return result

    def test_logins_come_from_the_adapter_files(self) -> None:
        self.assertEqual(
            set(bm.BOT_LOGINS.values()),
            {"copilot-pull-request-reviewer", "coderabbitai", "qodo-free-for-open-source-projects"},
        )

    def test_rounds_count_distinct_heads_per_bot_and_pushes_count_commits(self) -> None:
        reviews = [
            {"author": {"login": COPILOT}, "commit": {"oid": SHA_A}},
            {"author": {"login": COPILOT}, "commit": {"oid": SHA_A}},
            {"author": {"login": COPILOT}, "commit": {"oid": SHA_B}},
            {"author": {"login": RABBIT}, "commit": {"oid": SHA_A}},
        ]
        result = self.tally(commits=[commit(SHA_A), commit(SHA_B)], reviews=reviews)
        self.assertEqual(result.pushes, 2)
        self.assertEqual(result.bots["copilot"].rounds, [2])
        self.assertEqual(result.bots["coderabbit"].rounds, [1])
        self.assertEqual(result.bots["qodo"].rounds, [])

    def test_requests_split_by_actor_type_and_explicit_comments_count_as_user(self) -> None:
        comments = [
            {"body": f"@{RABBIT} review", "author": {"login": "p", "__typename": "User"}},
            {"body": f"@{RABBIT} review", "author": {"login": RABBIT, "__typename": "Bot"}},
            {"body": "/review", "author": {"login": "p", "__typename": "User"}},
            {"body": f"@{RABBIT} reviewing", "author": {"login": "p", "__typename": "User"}},
        ]
        events = [requested("Bot", COPILOT), requested("User", COPILOT)]
        result = self.tally(comments=comments, timelineItems=events)
        self.assertEqual(result.bots["copilot"].requests_by_automation, 1)
        self.assertEqual(result.bots["copilot"].requests_by_user, 1)
        self.assertEqual(result.bots["coderabbit"].requests_by_user, 1)
        self.assertEqual(result.bots["qodo"].requests_by_user, 1)

    def test_local_findings_sum_and_unknown_is_counted_apart(self) -> None:
        comments = [attest(SHA_A, "2"), attest(SHA_B, "unknown"), attest(SHA_C, "0")]
        result = self.tally(comments=comments)
        self.assertEqual(result.local_findings, 2)
        self.assertEqual(result.local_unknown, 1)

    def test_bot_findings_missed_by_clean_local_review_and_on_green_heads(self) -> None:
        threads = [
            thread(RABBIT, SHA_A),
            thread(RABBIT, SHA_B),
            thread(QODO, SHA_A),
            thread("someone", SHA_A),
        ]
        result = self.tally(
            commits=[commit(SHA_A, "SUCCESS", "SKIPPED"), commit(SHA_B, "SUCCESS", "FAILURE")],
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
            commits=[
                commit(SHA_A, "FAILURE"),
                commit(SHA_B, "TIMED_OUT"),
                commit(SHA_C, "SUCCESS"),
            ],
            comments=[attest(SHA_A, "0"), attest(SHA_B, "1")],
        )
        self.assertEqual(result.failing_heads, 2)
        self.assertEqual(result.failing_after_clean_local, 1)

    def test_a_pending_head_is_neither_failed_nor_green(self) -> None:
        self.assertEqual(bm.head_state(commit(SHA_A, "SUCCESS", "")["commit"]), "other")  # type: ignore[arg-type]
        self.assertEqual(bm.head_state(commit(SHA_A)["commit"]), "other")  # type: ignore[arg-type]

    def test_a_cut_connection_is_recorded_as_truncated(self) -> None:
        pr = pr_payload()
        pr["commits"] = {"pageInfo": {"hasNextPage": True}, "nodes": []}
        result = bm.ReviewMetrics(name="alpha")
        bm.review_pr(pr, result)
        self.assertEqual(result.truncated, ["PR 1 commits"])
        self.assertIn("truncated", bm.review_notes([result])[0])


class PolicyTests(unittest.TestCase):
    @staticmethod
    def payload(text: str) -> dict[str, str]:
        import base64

        return {"content": base64.b64encode(text.encode()).decode()}

    def fetch(
        self, *, side_effect: Exception | None = None, return_value: object = None
    ) -> tuple[dict[str, str], str]:
        patch = unittest.mock.patch.object(
            bm, "run_gh", side_effect=side_effect, return_value=return_value
        )
        with patch:
            return bm.fetch_policy("o", "alpha")

    def test_a_404_means_absent(self) -> None:
        triggers, error = self.fetch(side_effect=bm.GhError("gh: Not Found (HTTP 404)"))
        self.assertEqual(error, "")
        self.assertEqual(set(triggers.values()), {"default"})

    def test_any_other_error_is_an_error_not_absent(self) -> None:
        triggers, error = self.fetch(side_effect=bm.GhError("gh: Forbidden (HTTP 403)"))
        self.assertEqual(triggers, {})
        self.assertIn("403", error)

    def test_triggers_are_read_per_bot(self) -> None:
        text = json.dumps(
            {"bots": {"copilot": {"trigger": "auto"}, "qodo": {"trigger": "explicit"}}}
        )
        triggers, error = self.fetch(return_value=self.payload(text))
        self.assertEqual(error, "")
        self.assertEqual(
            triggers, {"copilot": "auto", "coderabbit": "unspecified", "qodo": "explicit"}
        )

    def test_invalid_json_is_an_error(self) -> None:
        _triggers, error = self.fetch(return_value=self.payload("{nope"))
        self.assertIn("unreadable", error)


class ReviewCollectTests(unittest.TestCase):
    def test_the_query_is_capped_and_search_scoped_to_the_window(self) -> None:
        reply = {"data": {"search": {"issueCount": 0, "nodes": []}}}
        with unittest.mock.patch.object(
            bm, "run_gh", side_effect=[reply, bm.GhError("HTTP 404")]
        ) as run:
            result = bm.collect_reviews("o", "alpha", datetime.date(2026, 1, 1))
        first = run.call_args_list[0].args[0]
        self.assertIn("repo:o/alpha is:pr is:merged merged:>=2026-01-01", " ".join(first))
        self.assertIn(f"first: {bm.PR_CAP}", bm.REVIEW_QUERY)
        self.assertEqual(result.prs, 0)

    def test_more_merged_prs_than_the_cap_is_marked_truncated(self) -> None:
        reply = {"data": {"search": {"issueCount": bm.PR_CAP + 1, "nodes": []}}}
        with unittest.mock.patch.object(bm, "run_gh", side_effect=[reply, bm.GhError("HTTP 404")]):
            result = bm.collect_reviews("o", "alpha", datetime.date(2026, 1, 1))
        self.assertTrue(result.truncated)

    def test_graphql_errors_raise(self) -> None:
        with (
            unittest.mock.patch.object(bm, "run_gh", return_value={"errors": [{"m": "x"}]}),
            self.assertRaises(bm.GhError),
        ):
            bm.collect_reviews("o", "alpha", datetime.date(2026, 1, 1))

    def test_a_policy_error_still_writes_the_report_and_exits_one(self) -> None:
        def fake(args: list[str]) -> object:
            if args[:2] == ["api", "graphql"]:
                return {"data": {"search": {"issueCount": 0, "nodes": []}}}
            if args[0] == "api" and "contents" in args[1]:
                raise bm.GhError("gh: Forbidden (HTTP 403)")
            if args[0] == "api":
                return {"usageItems": []}
            return []

        out, err = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.object(bm, "run_gh", side_effect=fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = bm.main(["--owner", "o", "--repo", "alpha"])
        self.assertEqual(code, 1)
        self.assertIn("Review return on cost, per bot", out.getvalue())
        self.assertIn("| error |", out.getvalue())
        self.assertIn("alpha", err.getvalue())

    def test_both_formats_carry_the_review_tables(self) -> None:
        result = bm.ReviewMetrics(name="alpha", bots={"copilot": bm.BotStats(rounds=[1, 3])})
        start, end = datetime.date(2026, 1, 1), datetime.date(2026, 1, 8)
        md = bm.render_markdown(start, end, [metrics()], [], [result])
        page = bm.render_html(start, end, [metrics()], [], [result])
        self.assertIn("| alpha | copilot | default | 0 | 0 | 2.0 | 3 |", md)
        self.assertEqual(page.count("<table"), 3)


class MainTests(unittest.TestCase):
    def test_main_writes_the_report_to_stdout(self) -> None:
        def fake(args: list[str]) -> object:
            if args[:2] == ["api", "graphql"]:
                return {"data": {"search": {"issueCount": 0, "nodes": []}}}
            if args[0] == "api" and "contents" in args[1]:
                raise bm.GhError("gh api exited 1: gh: Not Found (HTTP 404)")
            if args[0] == "api":
                return {"usageItems": []}
            if args[0] == "issue":
                return [issue("bug")]
            if args[0] == "run":
                return [{"name": "Running Copilot Code Review"}]
            return [{"number": 1}]

        out = io.StringIO()
        with (
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
