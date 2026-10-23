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
        self.assertEqual(set(result.open_by_label), set(bm.LABELS))

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

    def test_markdown_has_dates_once_units_and_label_lines(self) -> None:
        text = bm.render_markdown(self.START, self.END, self.fixtures(), ["Billing line"])
        self.assertEqual(text.count("2026-01-01"), 1)
        self.assertIn("2026-01-08", text)
        self.assertIn("Issues opened per merged PR (ratio)", text)
        self.assertIn("| beta | 3 | 2 | 1 | 6 | 0 | n/a | 5 | 1 |", text)
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


class MainTests(unittest.TestCase):
    def test_main_writes_the_report_to_stdout(self) -> None:
        def fake(args: list[str]) -> object:
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
