#!/usr/bin/env python3
"""Drive pr_merge's gate-then-merge against crafted reads, so no case reaches GitHub or merges.

Every GraphQL read is answered from a queue, and the gate is `wait`'s own exit stubbed per case.
The merge call is recorded rather than run. `pr_review.py merge` forwards here, and the cases
calling `pr_review.main` hold that forward.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_merge
import pr_review
import test_pr_review as fixtures

HEAD = fixtures.HEAD
MOVED = "b" * 40
SQUASH = "c" * 40
REAL_GATE = pr_merge.gate
REAL_UNMET = pr_merge.unmet_items


def target(**overrides: object) -> dict:
    """An open pull request into `develop` from a head in example/repo, every field overridable."""
    pr: dict[str, object] = {
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "develop",
        "headRefName": "feature/example",
        "headRefOid": HEAD,
        "mergeCommit": None,
        "headRepository": {"nameWithOwner": "example/repo"},
    }
    pr.update(overrides)
    return pr


MERGED = target(state="MERGED", mergeCommit={"oid": SQUASH})


class MergeCase(unittest.TestCase):
    """Run `merge` with each read, the gate, the merge call, and the cleanup answered here."""

    def setUp(self) -> None:
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(mock.patch.object(pr_review, "origin_owner", return_value="example"))
        cleanup = pr_merge.pr_cleanup
        self.registry = self.enterContext(
            mock.patch.object(cleanup.handoff, "registry", return_value=("example", {"repo"}))
        )
        self.enterContext(mock.patch.object(cleanup, "DELETE_SETTLE_DELAYS", ()))
        self.rules = self.enterContext(mock.patch.object(cleanup, "branch_rules", return_value=[]))
        self.reads: list[object] = []
        self.calls: list[list[str]] = []
        self.default = "main"
        self.gate = self.enterContext(mock.patch.object(pr_merge, "gate", return_value=0))
        self.unmet = self.enterContext(
            mock.patch.object(pr_merge, "unmet_items", return_value=(HEAD, []))
        )
        self.cleanup = self.enterContext(
            mock.patch.object(pr_merge.pr_cleanup, "cleanup", return_value=0)
        )
        self.merge_rc = 0
        self.merge_err = "merge answer"
        shared = pr_merge.pr_cleanup.shared

        def fake_read(query: str, variables: dict, **_: object) -> shared.Response:
            self.assertIs(pr_merge.pr_cleanup.Q_CLEANUP_TARGET, query)
            answer = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
            if isinstance(answer, Exception):
                raise answer
            repository = {"defaultBranchRef": {"name": self.default}, "pullRequest": answer}
            return shared.Response({"repository": repository}, [])

        def fake_run(argv: list[str], *_: object, **__: object) -> subprocess.CompletedProcess:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, self.merge_rc, "", self.merge_err)

        self.request = self.enterContext(
            mock.patch.object(shared, "request", side_effect=fake_read)
        )
        self.enterContext(mock.patch.object(pr_review, "_run", side_effect=fake_run))

    def run_merge(self, *reads: object, extra: tuple[str, ...] = ()) -> int:
        self.reads = list(reads)
        return pr_review.main(["merge", "123", "--repo", "example/repo", *extra])

    def assert_not_merged(self) -> None:
        self.assertEqual([], self.calls)
        self.cleanup.assert_not_called()


class TestRefusals(MergeCase):
    def test_a_main_base_is_refused_and_named_as_the_promotion_skill(self) -> None:
        self.assertEqual(1, self.run_merge(target(baseRefName="main")))
        self.assertIn("status=REFUSED", self.out.getvalue())
        self.assertIn("`merge-and-release`", self.out.getvalue())
        self.gate.assert_not_called()
        self.assert_not_merged()

    def test_another_base_is_refused(self) -> None:
        self.assertEqual(1, self.run_merge(target(baseRefName="release")))
        self.assertIn("its base is 'release', not 'develop'", self.out.getvalue())
        self.assert_not_merged()

    def test_a_head_in_a_fork_is_refused(self) -> None:
        fork = target(headRepository={"nameWithOwner": "someone/repo"})
        self.assertEqual(1, self.run_merge(fork))
        self.assertIn("its head lives in someone/repo", self.out.getvalue())
        self.gate.assert_not_called()
        self.assert_not_merged()

    def test_a_deleted_head_repository_is_refused(self) -> None:
        self.assertEqual(1, self.run_merge(target(headRepository=None)))
        self.assertIn("its head lives in no repository", self.out.getvalue())
        self.assert_not_merged()

    def test_a_draft_is_refused(self) -> None:
        self.assertEqual(1, self.run_merge(target(isDraft=True)))
        self.assertIn("it is a draft", self.out.getvalue())
        self.gate.assert_not_called()
        self.assert_not_merged()

    def test_a_closed_pull_request_is_refused(self) -> None:
        self.assertEqual(1, self.run_merge(target(state="CLOSED")))
        self.assertIn("it reads CLOSED, not OPEN", self.out.getvalue())
        self.gate.assert_not_called()
        self.assert_not_merged()

    def test_a_foreign_owner_is_refused_before_any_read(self) -> None:
        with mock.patch.object(pr_review, "origin_owner", return_value="acme"):
            self.assertEqual(64, self.run_merge(target()))
        self.assertIn("status=OUT_OF_SCOPE nothing was merged", self.out.getvalue())
        self.request.assert_not_called()
        self.gate.assert_not_called()
        self.assert_not_merged()

    def test_a_repository_outside_the_registry_owner_is_refused_before_any_read(self) -> None:
        """The scope `cleanup` checks, so a `--cleanup` after a landed merge cannot refuse on it."""
        self.registry.return_value = ("someone", {"repo"})
        self.assertEqual(1, self.run_merge(target(), extra=("--cleanup",)))
        self.assertIn(
            "status=REFUSED nothing was merged, refused by handoff.py", self.out.getvalue()
        )
        self.request.assert_not_called()
        self.assert_not_merged()

    def test_an_unreadable_registry_is_unread_before_any_read(self) -> None:
        self.registry.side_effect = pr_merge.pr_cleanup.handoff.Execution("no registry")
        self.assertEqual(2, self.run_merge(target()))
        self.assertIn("status=UNREAD nothing was merged: no registry", self.out.getvalue())
        self.request.assert_not_called()
        self.assert_not_merged()

    def test_a_permanent_head_branch_is_refused(self) -> None:
        """A `main -> develop` pull request is a sync, never a squash into develop."""
        self.default = "trunk"
        for branch in ("main", "develop", "trunk"):
            with self.subTest(branch=branch):
                self.assertEqual(1, self.run_merge(target(headRefName=branch)))
                self.assertIn(
                    f"its head branch {branch!r} is a permanent branch", self.out.getvalue()
                )
                self.gate.assert_not_called()
                self.assert_not_merged()

    def test_a_protected_head_branch_is_refused(self) -> None:
        self.rules.return_value = ["branch protection"]
        self.assertEqual(1, self.run_merge(target()))
        self.assertIn("'feature/example' is covered by branch protection", self.out.getvalue())
        self.rules.assert_called_once_with("example", "repo", "feature/example")
        self.gate.assert_not_called()
        self.assert_not_merged()

    def test_an_unread_branch_rule_is_unread_rather_than_refused(self) -> None:
        self.rules.side_effect = pr_merge.pr_cleanup.CleanupUnread("the branch rules read failed")
        self.assertEqual(2, self.run_merge(target()))
        self.assertIn("status=UNREAD nothing was merged", self.out.getvalue())
        self.gate.assert_not_called()
        self.assert_not_merged()

    def test_an_unanswered_first_read_is_unread_rather_than_refused(self) -> None:
        unread = pr_merge.pr_cleanup.shared.GraphQLError("gh graphql failed rc=1")
        self.assertEqual(2, self.run_merge(unread))
        self.assertIn("status=UNREAD nothing was merged", self.out.getvalue())
        self.assert_not_merged()


class TestGate(MergeCase):
    def test_a_gate_not_met_returns_its_code_and_never_merges(self) -> None:
        for code in (30, 41, 46, 49, 50):
            with self.subTest(code=code):
                self.gate.return_value = code
                self.assertEqual(code, self.run_merge(target()))
                self.assertIn(f"Merge Gate read above exited {code}", self.out.getvalue())
                self.assert_not_merged()

    def test_a_gate_read_that_ends_the_run_is_unread(self) -> None:
        self.gate.side_effect = SystemExit("gh graphql failed rc=1")
        self.assertEqual(2, self.run_merge(target()))
        self.assertIn("a read for the gate did not answer", self.out.getvalue())
        self.assert_not_merged()

    def test_a_head_moved_while_the_gate_read_refuses(self) -> None:
        self.assertEqual(1, self.run_merge(target(), target(headRefOid=MOVED)))
        self.assertIn(
            f"status=HEAD_MOVED nothing was merged: #123 on example/repo is at {MOVED}",
            self.out.getvalue(),
        )
        self.assert_not_merged()

    def test_a_pull_request_merged_while_the_gate_read_refuses(self) -> None:
        """`wait` exits as `status` does on a merged pull request, so a 0 there is no gate met."""
        self.assertEqual(1, self.run_merge(target(), MERGED))
        self.assertIn("it reads MERGED, not OPEN", self.out.getvalue())
        self.assert_not_merged()

    def test_the_gate_is_waits_own_and_requests_nothing(self) -> None:
        seen: list[tuple[argparse.Namespace, bool]] = []

        def fake_wait(owner: str, repo: str, a: argparse.Namespace, may_request: bool = True):
            seen.append((a, may_request))
            return 0

        with mock.patch.object(pr_review, "run_wait", fake_wait):
            self.assertEqual(0, REAL_GATE("example", "repo", 123))
        ((a, may_request),) = seen
        self.assertFalse(may_request)
        self.assertFalse(a.request)
        self.assertEqual(pr_merge.GATE_TIMEOUT, a.timeout)
        self.assertLess(a.timeout, 2700)
        self.assertEqual((0, False), (a.min_rounds, a.ignore_quota_signal))


class TestMerge(MergeCase):
    def test_a_met_gate_merges_pinned_to_the_head_read_before_it(self) -> None:
        self.assertEqual(0, self.run_merge(target(), target(), MERGED))
        want = ["gh", "pr", "merge", "123", "--squash", "--match-head-commit", HEAD]
        self.assertEqual([[*want, "--repo", "example/repo"]], self.calls)
        self.assertIn(
            f"status=MERGED pr=123 head={HEAD} merge_commit={SQUASH}", self.out.getvalue()
        )
        self.cleanup.assert_not_called()

    def test_the_merge_call_never_deletes_the_branch_or_overrides_rules(self) -> None:
        self.run_merge(target(), target(), MERGED)
        (argv,) = self.calls
        self.assertNotIn("--admin", argv)
        self.assertNotIn("--delete-branch", argv)
        self.assertNotIn("--auto", argv)

    def test_a_failed_merge_call_read_back_merged_is_done(self) -> None:
        self.merge_rc = 1
        self.assertEqual(0, self.run_merge(target(), target(), MERGED))
        self.assertIn("gh pr merge exited 1", self.out.getvalue())
        self.assertIn("  merge answer", self.out.getvalue())
        self.assertIn("status=MERGED", self.out.getvalue())

    def test_a_merge_gh_refused_that_reads_back_open_is_refused(self) -> None:
        self.merge_rc = 1
        self.assertEqual(1, self.run_merge(target(), target(), target()))
        self.assertIn("status=MERGE_REFUSED gh refused the merge of #123", self.out.getvalue())
        self.assertIn("so nothing was merged", self.out.getvalue())
        self.cleanup.assert_not_called()

    def test_a_merge_gh_accepted_that_reads_back_open_is_unconfirmed(self) -> None:
        self.assertEqual(2, self.run_merge(target(), target(), target()))
        self.assertIn(
            "status=MERGE_NOT_CONFIRMED #123 on example/repo reads OPEN", self.out.getvalue()
        )
        self.cleanup.assert_not_called()

    def test_a_merge_call_that_never_finished_is_unconfirmed_rather_than_refused(self) -> None:
        self.merge_rc, self.merge_err = 1, pr_merge.UNRUN
        self.assertEqual(2, self.run_merge(target(), target(), target()))
        self.assertIn("status=MERGE_NOT_CONFIRMED", self.out.getvalue())
        self.cleanup.assert_not_called()

    def test_a_read_back_that_does_not_answer_says_the_merge_may_have_happened(self) -> None:
        unread = pr_merge.pr_cleanup.shared.GraphQLError("gh graphql failed rc=1")
        self.assertEqual(2, self.run_merge(target(), target(), unread))
        self.assertIn("may have happened on the server", self.out.getvalue())
        self.assertEqual(1, len(self.calls))
        self.cleanup.assert_not_called()


class TestUnmetItems(MergeCase):
    """`wait`'s 0 leaves these undecided, so each is read again off a fresh payload."""

    CLEAN = fixtures.overview_v2(
        open_count="0 open findings",
        covers="<!-- fleet-review: reviewed=1 changed=1 findings=0 -->",
        entries=0,
        verdict="### No changes recommended",
    )

    def unmet_on(self, pr: dict) -> list[str]:
        with mock.patch.object(pr_review, "full_read", return_value=pr):
            head, unmet = REAL_UNMET("example", "repo", 123)
        self.assertEqual(HEAD, head)
        return [f"{status} {why}" for status, why in unmet]

    def clean(self, **overrides: Any) -> dict:
        reviews = [fixtures.review(body=self.CLEAN)]
        return fixtures.payload(reviews, files=["a.py"], **overrides)

    def test_a_clean_payload_leaves_nothing_unmet(self) -> None:
        self.assertEqual([], self.unmet_on(self.clean()))

    def test_each_item_unmet_is_named(self) -> None:
        coderabbit = fixtures.review(
            login=fixtures.pr_review_rounds.CODERABBIT_LOGIN,
            body=fixtures.cr_outside_diff_body(),
            rid="PRR_two",
        )
        qodo = fixtures.comment(
            login=fixtures.pr_review_rounds.QODO_LOGIN, body=fixtures.qodo_review_body()
        )
        for label, pr, want in (
            ("open thread", self.clean(threads=[fixtures.thread("T1")]), "THREADS_UNRESOLVED 1"),
            (
                "blocked",
                self.clean(merge="BLOCKED"),
                "MERGE_NOT_CLEAN the merge state reads BLOCKED",
            ),
            (
                "no coverage",
                fixtures.payload([fixtures.review(body=fixtures.OVERVIEW)], files=["a.py"]),
                "COVERAGE_NOT_SHOWN the digest reads coverage=none",
            ),
            (
                "outside diff",
                fixtures.payload([fixtures.review(body=self.CLEAN), coderabbit], files=["a.py"]),
                "UNTHREADED_FINDINGS the digest reads cr_outside_diff=1",
            ),
            (
                "qodo",
                self.clean(comments=[qodo]),
                "UNTHREADED_FINDINGS the digest reads qodo_open=1",
            ),
            (
                "unthreaded",
                fixtures.payload([fixtures.review(body=fixtures.overview_v2())], files=["a.py"]),
                "FINDINGS WITH NO THREAD",
            ),
            (
                "no stated count",
                fixtures.payload([fixtures.review()], files=["a.py"]),
                "COUNT_UNREAD the digest holds no readable count for overview=?/0",
            ),
        ):
            with self.subTest(label):
                unmet = self.unmet_on(pr)
                self.assertTrue(any(want in item for item in unmet), unmet)

    def test_a_finding_on_an_earlier_round_is_unmet_whatever_answered_it(self) -> None:
        for label, kind in (
            ("Comments suppressed due to low confidence (1)", "suppressed"),
            ("1 previously missed issue", "previously_missed"),
        ):
            section = (
                f"{fixtures.OVERVIEW}\n<details>\n<summary><strong>{label}</strong></summary>\n\n"
                "In code that has not changed since last review\n\n"
                "[P2] Guard the write.\n</details>"
            )
            earlier = fixtures.review(oid=fixtures.OLD, body=section, rid="PRR_old")
            pr = fixtures.payload([earlier, fixtures.review(body=self.CLEAN)], files=["a.py"])
            with self.subTest(kind):
                self.assertIn(
                    f"UNTHREADED_FINDINGS the digest reads {kind}=1 (on_head=0 earlier=1)",
                    self.unmet_on(pr),
                )

    def test_a_count_the_digest_did_not_build_is_unmet_rather_than_zero(self) -> None:
        real = pr_review.digest

        def without_qodo(owner: str, repo: str, num: int, **kwargs: Any) -> tuple[str, int]:
            answer = real(owner, repo, num, **kwargs)
            built = kwargs["fields_out"]
            built[:] = [field for field in built if field[0] != "qodo_open"]
            return answer

        with mock.patch.object(pr_review, "digest", side_effect=without_qodo):
            unmet = self.unmet_on(self.clean())
        self.assertEqual(
            ["COUNT_UNREAD the digest holds no readable count for qodo_open (not built)"], unmet
        )

    def test_a_met_wait_with_an_open_thread_merges_nothing(self) -> None:
        """`wait` returns 0 on a Copilot-covered head without reading its threads."""
        self.unmet.side_effect = REAL_UNMET
        pr = self.clean(threads=[fixtures.thread("T1")])
        with mock.patch.object(pr_review, "full_read", return_value=pr):
            self.assertEqual(1, self.run_merge(target(), target(), MERGED))
        self.assertIn("status=THREADS_UNRESOLVED nothing was merged", self.out.getvalue())
        self.assert_not_merged()

    def test_a_head_moved_before_the_fresh_read_refuses(self) -> None:
        self.unmet.return_value = (MOVED, [])
        self.assertEqual(1, self.run_merge(target(), target(), MERGED))
        self.assertIn(
            f"status=HEAD_MOVED nothing was merged: #123 on example/repo is at {MOVED}",
            self.out.getvalue(),
        )
        self.assert_not_merged()


class TestCleanup(MergeCase):
    def test_cleanup_runs_after_a_confirmed_merge_and_returns_its_code(self) -> None:
        self.cleanup.return_value = 1
        extra = ("--cleanup", "--checkout", "/example/clone")
        self.assertEqual(1, self.run_merge(target(), target(), MERGED, extra=extra))
        self.cleanup.assert_called_once_with("example", "repo", 123, "/example/clone")
        out = self.out.getvalue()
        self.assertIn("status=MERGED", out)

    def test_cleanup_without_a_checkout_cleans_the_remote_half(self) -> None:
        self.assertEqual(0, self.run_merge(target(), target(), MERGED, extra=("--cleanup",)))
        self.cleanup.assert_called_once_with("example", "repo", 123, None)

    def test_cleanup_never_runs_without_a_confirmed_merge(self) -> None:
        extra = ("--cleanup", "--checkout", "/example/clone")
        self.assertEqual(2, self.run_merge(target(), target(), target(), extra=extra))
        self.gate.return_value = 46
        self.assertEqual(46, self.run_merge(target(), extra=extra))
        self.cleanup.assert_not_called()


class TestArguments(MergeCase):
    def error(self, argv: list[str]) -> str:
        with self.assertRaises(SystemExit):
            pr_review.main(argv)
        return self.err.getvalue()

    def test_a_checkout_without_cleanup_is_refused(self) -> None:
        err = self.error(["merge", "123", "--repo", "example/repo", "--checkout", "."])
        self.assertIn("--checkout belongs to --cleanup", err)
        self.assert_not_merged()

    def test_a_flag_merge_does_not_own_is_refused(self) -> None:
        err = self.error(["merge", "123", "--repo", "example/repo", "--timeout", "5"])
        self.assertIn("unrecognized arguments: --timeout", err)
        self.assert_not_merged()

    def test_cleanup_is_refused_off_merge(self) -> None:
        err = self.error(["status", "123", "--repo", "example/repo", "--cleanup"])
        self.assertTrue(err.strip().endswith("error: --cleanup belongs to `merge`, not `status`"))

    def test_merge_after_a_leading_flag_names_where_its_arguments_go(self) -> None:
        err = self.error(["--timeout", "5", "merge", "123", "--repo", "example/repo"])
        self.assertIn("`merge` takes its arguments after it", err)
        self.assert_not_merged()


if __name__ == "__main__":
    unittest.main()
