#!/usr/bin/env python3
"""Drive the promotion's read of its constituent pull requests against constructed responses.

A wrong answer here counts a change as covered that nobody read, or sends a promotion that met
Merge Gate item 2 to the maintainer. So each case pins one decision of the Dependabot clause.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_review
import pr_review_answers
import pr_review_promotion as promotion
import test_pr_review as fixtures

HEAD = fixtures.HEAD
BUMP = "c" * 40
FEATURE = "d" * 40


def landed(
    oid: str,
    paths: list[str],
    login: str = "dependabot",
    typename: str = "Bot",
    state: str = "SUCCESS",
    base: str = "develop",
    more: bool = False,
    number: int = 11,
) -> dict:
    """One commit on the promotion and the pull request that landed it."""
    return {
        "commit": {
            "oid": oid,
            "associatedPullRequests": {
                "nodes": [
                    {
                        "number": number,
                        "baseRefName": base,
                        "mergeCommit": {"oid": oid},
                        "author": {"__typename": typename, "login": login},
                        "files": {
                            "pageInfo": {"hasNextPage": more},
                            "nodes": [{"path": p} for p in paths],
                        },
                        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": state}}}]},
                    }
                ]
            },
        }
    }


def page(*nodes: dict, head: str = HEAD, total: int | None = None) -> dict:
    return {
        "headRefOid": head,
        "commits": {
            "totalCount": len(nodes) if total is None else total,
            "pageInfo": {"hasNextPage": False},
            "nodes": list(nodes),
        },
    }


def promotion_pr(paths: list[str], table: list[str] | None = None) -> dict:
    """A develop -> main promotion whose round on the head states nothing, with one table or none."""
    body = fixtures.summarized(table, covers="") if table is not None else fixtures.OVERVIEW
    pr = fixtures.payload([fixtures.review(body=body)], files=paths)
    return {
        **pr,
        "headRefName": "develop",
        "baseRefName": "main",
        "baseRepository": {"defaultBranchRef": {"name": "main"}},
    }


class TestReadBumps(unittest.TestCase):
    def test_a_green_dependabot_lockfile_bump_is_counted(self) -> None:
        got = promotion.read_bumps([landed(BUMP, ["pkg/uv.lock", "pkg/pyproject.toml"])])
        self.assertEqual(frozenset({"pkg/uv.lock", "pkg/pyproject.toml"}), got.paths)
        self.assertEqual((11,), got.numbers)
        self.assertEqual("", got.why)

    def test_each_named_manifest_is_a_manifest(self) -> None:
        for path in (
            "pyproject.toml",
            "a/uv.lock",
            "a/requirements-dev.txt",
            "requirements.txt",
            "Directory.Packages.props",
            "a/packages.lock.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(promotion.is_manifest(path))

    def test_a_workflow_or_other_source_is_no_manifest(self) -> None:
        for path in (".github/workflows/ci.yml", "Dockerfile", "a/uv.lock.txt", "src/main.py"):
            with self.subTest(path=path):
                self.assertFalse(promotion.is_manifest(path))

    def test_a_path_another_pull_request_also_changed_is_not_counted(self) -> None:
        got = promotion.read_bumps(
            [
                landed(BUMP, ["uv.lock"]),
                landed(FEATURE, ["uv.lock", "a.py"], login="maintainer", typename="User"),
            ]
        )
        self.assertEqual(frozenset(), got.paths)

    def test_a_bump_touching_anything_else_is_not_counted(self) -> None:
        got = promotion.read_bumps([landed(BUMP, ["uv.lock", "a.py"])])
        self.assertEqual(frozenset(), got.paths)

    def test_a_user_named_dependabot_is_not_counted(self) -> None:
        got = promotion.read_bumps([landed(BUMP, ["uv.lock"], typename="User")])
        self.assertEqual(frozenset(), got.paths)

    def test_another_bot_is_not_counted(self) -> None:
        got = promotion.read_bumps([landed(BUMP, ["uv.lock"], login="renovate")])
        self.assertEqual(frozenset(), got.paths)

    def test_a_bump_not_green_is_not_counted(self) -> None:
        for state in ("FAILURE", "PENDING", "ERROR"):
            with self.subTest(state=state):
                got = promotion.read_bumps([landed(BUMP, ["uv.lock"], state=state)])
                self.assertEqual(frozenset(), got.paths)

    def test_a_commit_no_pull_request_landed_withdraws_the_reading(self) -> None:
        got = promotion.read_bumps(
            [landed(BUMP, ["uv.lock"]), {"commit": {"oid": FEATURE, "associatedPullRequests": {}}}]
        )
        self.assertEqual(frozenset(), got.paths)
        self.assertIn(f"commit {FEATURE[:8]} landed through no pull request", got.why)

    def test_a_pull_request_into_another_base_does_not_land_the_commit(self) -> None:
        got = promotion.read_bumps([landed(BUMP, ["uv.lock"], base="main")])
        self.assertEqual(frozenset(), got.paths)
        self.assertIn("landed through no pull request", got.why)

    def test_a_file_list_past_its_page_withdraws_the_reading(self) -> None:
        got = promotion.read_bumps(
            [landed(BUMP, ["uv.lock"]), landed(FEATURE, ["a.py"], more=True, login="m", number=12)]
        )
        self.assertEqual(frozenset(), got.paths)
        self.assertIn("#12's file list runs past a page", got.why)


class TestAttach(unittest.TestCase):
    def follow(self, *pages: dict) -> mock.MagicMock:
        return mock.MagicMock(return_value=iter(pages))

    def test_a_promotion_reading_none_is_walked(self) -> None:
        pr = promotion_pr(["uv.lock"])
        follow = self.follow(page(landed(BUMP, ["uv.lock"])))
        self.assertIs(pr, promotion.attach(pr, "o", "r", 7, follow))
        follow.assert_called_once_with(promotion.Q_CONSTITUENTS, "o", "r", 7, "commits")
        self.assertEqual(frozenset({"uv.lock"}), pr[promotion.BUMPS_KEY].paths)

    def test_nothing_is_walked_off_a_promotion(self) -> None:
        for change in ({"headRefName": "feature/x"}, {"baseRefName": "develop"}):
            with self.subTest(change=change):
                pr = {**promotion_pr(["uv.lock"]), **change}
                follow = self.follow()
                promotion.attach(pr, "o", "r", 7, follow)
                follow.assert_not_called()
                self.assertNotIn(promotion.BUMPS_KEY, pr)

    def test_a_covered_head_is_not_walked(self) -> None:
        pr = promotion_pr(["uv.lock"], table=["uv.lock"])
        follow = self.follow()
        promotion.attach(pr, "o", "r", 7, follow)
        follow.assert_not_called()

    def test_a_head_with_no_round_is_not_walked(self) -> None:
        pr = {**promotion_pr(["uv.lock"]), "reviews": {"nodes": [], "pageInfo": {}}}
        follow = self.follow()
        promotion.attach(pr, "o", "r", 7, follow)
        follow.assert_not_called()

    def test_a_cut_file_list_is_not_walked(self) -> None:
        pr = promotion_pr(["uv.lock"])
        pr["files"]["pageInfo"]["hasNextPage"] = True
        follow = self.follow()
        promotion.attach(pr, "o", "r", 7, follow)
        follow.assert_not_called()

    def test_a_head_that_moved_while_paging_counts_nothing(self) -> None:
        pr = promotion_pr(["uv.lock"])
        promotion.attach(
            pr, "o", "r", 7, self.follow(page(landed(BUMP, ["uv.lock"]), head="f" * 40))
        )
        bumps = pr[promotion.BUMPS_KEY]
        self.assertEqual(frozenset(), bumps.paths)
        self.assertIn("the head moved", bumps.why)

    def test_a_commit_list_github_cut_short_counts_nothing(self) -> None:
        pr = promotion_pr(["uv.lock"])
        promotion.attach(pr, "o", "r", 7, self.follow(page(landed(BUMP, ["uv.lock"]), total=251)))
        self.assertIn("cut short", pr[promotion.BUMPS_KEY].why)


class TestDigestReading(fixtures.GqlCase):
    """Through `status`, so the wiring from the full read to the digest and the gate is held too."""

    def status(self, pr: dict, *pages: dict) -> int:
        self.answer(pr)
        with mock.patch.object(pr_review, "follow", return_value=iter(pages)):
            return pr_review.main(["status", "7", "--repo", "o/r"])

    def setUp(self) -> None:
        super().setUp()
        self.out = self.enterContext(mock.patch("sys.stdout", new_callable=fixtures.io.StringIO))

    def test_a_promotion_of_only_a_green_bump_reads_dependabot(self) -> None:
        self.assertEqual(0, self.status(promotion_pr(["uv.lock"]), page(landed(BUMP, ["uv.lock"]))))
        out = self.out.getvalue()
        self.assertIn("coverage=dependabot ", out)
        self.assertIn("COVERAGE IS DEPENDABOT: every changed file is one of 1 changed", out)
        self.assertIn("(#11)", out)
        self.assertNotIn("Merge Gate item 2 is unmet", out)

    def test_a_table_naming_all_but_the_bump_reads_dependabot(self) -> None:
        pr = promotion_pr(["a.py", "uv.lock"], table=["a.py"])
        pages = page(
            landed(BUMP, ["uv.lock"]), landed(FEATURE, ["a.py"], login="m", typename="User")
        )
        self.assertEqual(0, self.status(pr, pages))
        out = self.out.getvalue()
        self.assertIn("coverage=dependabot ", out)
        self.assertIn("with 1 changed only by green Dependabot bumps (#11) left out", out)

    def test_a_table_missing_a_file_beside_the_bump_reads_none(self) -> None:
        pr = promotion_pr(["a.py", "b.py", "uv.lock"], table=["a.py"])
        pages = page(
            landed(BUMP, ["uv.lock"]),
            landed(FEATURE, ["a.py", "b.py"], login="m", typename="User"),
        )
        self.assertEqual(0, self.status(pr, pages))
        out = self.out.getvalue()
        self.assertIn("coverage=none ", out)
        self.assertIn("names 1 path(s) where the pull request changes 2 file(s)", out)

    def test_a_direct_push_says_why_nothing_is_counted(self) -> None:
        pr = promotion_pr(["uv.lock"])
        pages = page(
            landed(BUMP, ["uv.lock"]), {"commit": {"oid": FEATURE, "associatedPullRequests": {}}}
        )
        self.assertEqual(0, self.status(pr, pages))
        out = self.out.getvalue()
        self.assertIn("coverage=none ", out)
        self.assertIn("no Dependabot bump can be counted", out)

    def test_the_merge_gate_reads_a_dependabot_reading_as_coverage_shown(self) -> None:
        pr = promotion_pr(["uv.lock"])
        codes = [code for code, _ in pr_review_answers.unmet(pr, {"coverage": "dependabot"}, [])]
        self.assertNotIn("COVERAGE_NOT_SHOWN", codes)

    def test_a_promotion_with_no_bump_reads_as_it_did(self) -> None:
        pr = promotion_pr(["a.py"])
        pages = page(landed(FEATURE, ["a.py"], login="m", typename="User"))
        self.assertEqual(0, self.status(pr, pages))
        out = self.out.getvalue()
        self.assertIn("coverage=none ", out)
        self.assertNotIn("Dependabot", out)


if __name__ == "__main__":
    unittest.main()
