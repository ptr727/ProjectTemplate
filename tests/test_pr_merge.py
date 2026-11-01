#!/usr/bin/env python3
"""Drive pr_merge's gate-then-merge against crafted reads, so no case reaches GitHub or merges.

Every GraphQL read is answered from a queue, and the gate is `wait`'s own exit stubbed per case.
The merge call is recorded rather than run. `pr_review.py merge` forwards here, and the cases
calling `pr_review.main` hold that forward.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_merge
import pr_review
import pr_review_answers as answers
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
        self.enterContext(
            mock.patch.object(pr_merge.pr_cleanup, "cleanup_primary", return_value="/primary")
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

    def test_an_unusable_checkout_is_refused_before_any_merge(self) -> None:
        refused = pr_merge.pr_cleanup.CleanupRefused("BAD_CHECKOUT", "not a git checkout")
        with mock.patch.object(pr_merge.pr_cleanup, "cleanup_primary", side_effect=refused):
            code = self.run_merge(target(), extra=("--cleanup", "--checkout", "/nowhere"))
        self.assertEqual(1, code)
        self.assertIn("status=BAD_CHECKOUT nothing was merged", self.out.getvalue())
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

    def test_no_branch_rule_is_read_before_the_merge(self) -> None:
        """The fleet's rulesets cover only the permanent branches, which are refused by name."""
        self.assertEqual(0, self.run_merge(target(), target(), MERGED))
        self.rules.assert_not_called()

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

    def test_a_merged_pull_request_is_named_before_any_unmet_item(self) -> None:
        """A peer merge during the gate reads as merged rather than as items a dead head lacks."""
        self.unmet.return_value = (HEAD, [("MERGE_NOT_CLEAN", "the merge state reads UNKNOWN")])
        self.assertEqual(1, self.run_merge(target(), MERGED))
        self.assertIn("it reads MERGED, not OPEN", self.out.getvalue())
        self.assertNotIn("MERGE_NOT_CLEAN", self.out.getvalue())
        self.unmet.assert_not_called()
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
    def test_a_peer_merge_of_a_newer_head_is_no_merge_of_this_run(self) -> None:
        self.merge_rc = 1
        code = self.run_merge(
            target(), target(), {**MERGED, "headRefOid": MOVED}, extra=("--cleanup",)
        )
        self.assertEqual(1, code)
        self.assertIn(
            f"status=PEER_MERGED this run merged nothing: #123 merged at {MOVED}",
            self.out.getvalue(),
        )
        self.assertNotIn("status=MERGED pr=", self.out.getvalue())
        self.cleanup.assert_not_called()

    def test_a_merge_reading_open_for_a_moment_is_read_again(self) -> None:
        cleanup = pr_merge.pr_cleanup
        with (
            mock.patch.object(cleanup, "DELETE_SETTLE_DELAYS", (1,)),
            mock.patch.object(pr_review.time, "sleep") as slept,
        ):
            self.assertEqual(0, self.run_merge(target(), target(), target(), MERGED))
        slept.assert_called_once_with(1)
        self.assertIn("status=MERGED", self.out.getvalue())

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
        self.assertIn(
            f"status=MERGE_REFUSED #123 on example/repo reads OPEN at {HEAD} after gh pr merge answered 1",
            self.out.getvalue(),
        )
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

    def test_a_read_back_closed_is_a_refusal_rather_than_unconfirmed(self) -> None:
        for rc, err, said in ((0, "", "0"), (1, "", "1"), (1, pr_merge.UNRUN, "nothing")):
            with self.subTest(rc=rc, err=err):
                self.calls.clear()
                self.out.seek(0)
                self.out.truncate()
                self.merge_rc, self.merge_err = rc, err
                self.assertEqual(1, self.run_merge(target(), target(), target(state="CLOSED")))
                self.assertIn(
                    f"status=REFUSED #123 on example/repo reads CLOSED at {HEAD} after gh pr "
                    f"merge answered {said}, so nothing was merged",
                    self.out.getvalue(),
                )
                self.assertNotIn("MERGE_NOT_CONFIRMED", self.out.getvalue())
                self.cleanup.assert_not_called()

    def test_a_read_back_that_does_not_answer_says_the_merge_may_have_happened(self) -> None:
        unread = pr_merge.pr_cleanup.shared.GraphQLError("gh graphql failed rc=1")
        self.assertEqual(2, self.run_merge(target(), target(), unread))
        self.assertIn("may have happened on the server", self.out.getvalue())
        self.assertEqual(1, len(self.calls))
        self.cleanup.assert_not_called()


class TestGateNamespace(unittest.TestCase):
    """`gate` builds `wait`'s arguments by hand, so each one `run_wait` reads has to be among them."""

    def test_every_argument_run_wait_reads_is_built(self) -> None:
        source = "".join(inspect.getsource(f) for f in (pr_review.run_wait, pr_review.run_status))
        read = set(re.findall(r"\ba\.([a-z_]+)", source))
        built: dict[str, argparse.Namespace] = {}

        def capture(owner: str, repo: str, a: argparse.Namespace, may_request: bool) -> int:
            built["a"] = a
            return 0

        with mock.patch.object(pr_review, "run_wait", side_effect=capture):
            pr_merge.gate("example", "repo", 123)
        self.assertLessEqual(read, set(vars(built["a"])))


class TestUnmetItems(MergeCase):
    """`wait`'s 0 leaves these undecided, so each is read again off a fresh payload."""

    CLEAN = fixtures.overview_v2(
        open_count="0 open findings",
        covers="<!-- fleet-review: reviewed=1 changed=1 findings=0 -->",
        entries=0,
        verdict="### Approval recommended",
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

    def test_an_attested_head_with_no_round_leaves_nothing_unmet(self) -> None:
        """The ordinary develop pull request, read from real digest fields rather than rewritten ones."""
        marker = {
            "author": {"login": "maintainer"},
            "authorAssociation": "OWNER",
            "createdAt": fixtures.LATE,
            "body": f"Attested.\n\n<!-- fleet-local-review: head={fixtures.HEAD} base=develop findings=0 -->",
        }
        pr = fixtures.payload([], files=["a.py"], comments=[marker])
        pr = {
            **pr,
            "baseRefName": "develop",
            "baseRepository": {"defaultBranchRef": {"name": "main"}},
        }
        self.assertEqual([], self.unmet_on(pr))

    def test_a_finished_state_beside_clean_leaves_nothing_unmet(self) -> None:
        for state in ("UNSTABLE", "HAS_HOOKS"):
            with self.subTest(state):
                self.assertEqual([], self.unmet_on(self.clean(merge=state)))

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
                "MERGE_NOT_CLEAN the merge state reads BLOCKED, not a finished one",
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

    def earlier(self, label: str, rid: str = "PRR_old", finding: str = "Guard the write.") -> dict:
        """A Copilot round on an earlier commit collapsing one finding under `label`."""
        section = (
            f"{fixtures.OVERVIEW}\n<details>\n<summary><strong>{label}</strong></summary>\n\n"
            f"In code that has not changed since last review\n\n[P2] {finding}\n</details>"
        )
        return fixtures.review(oid=fixtures.OLD, body=section, rid=rid)

    def answer(
        self, pr: dict, kinds: tuple[str, ...], outcome: str = "declined", **overrides: Any
    ) -> dict:
        """A trusted comment acknowledging every item under `kinds` with `outcome`."""
        marks = [
            answers.FINDING_ACK.format(item.key, outcome)
            for item in answers.items(pr)
            if item.kind in kinds
        ]
        self.assertTrue(marks, kinds)
        body = "Declined: the guard sits upstream of the write.\n\n" + "\n".join(marks)
        return {**fixtures.ack("0" * 12, "OWNER", text=body), **overrides}

    def with_comments(self, pr: dict, *comments: dict) -> dict:
        nodes = [*((pr.get("comments") or {}).get("nodes") or []), *comments]
        return {**pr, "comments": {**(pr.get("comments") or {}), "nodes": nodes}}

    def test_a_finding_on_an_earlier_round_is_unmet_until_acknowledged(self) -> None:
        for label, kind in (
            ("Comments suppressed due to low confidence (1)", "suppressed"),
            ("1 previously missed issue", "previously_missed"),
        ):
            reviews = [self.earlier(label), fixtures.review(body=self.CLEAN)]
            pr = fixtures.payload(reviews, files=["a.py"])
            unmet = f"UNTHREADED_FINDINGS the digest reads {kind}=1 (on_head=0 earlier=1)"
            with self.subTest(kind):
                self.assertIn(unmet, self.unmet_on(pr)[0])
                self.assertEqual(
                    [], self.unmet_on(self.with_comments(pr, self.answer(pr, (kind,))))
                )

    def test_an_acknowledgment_answers_only_where_it_is_trusted_written_and_known(self) -> None:
        reviews = [self.earlier("1 previously missed issue"), fixtures.review(body=self.CLEAN)]
        pr = fixtures.payload(reviews, files=["a.py"])
        good = self.answer(pr, ("previously_missed",))
        marker_only = "\n".join(ln for ln in good["body"].splitlines() if ln.startswith("<!--"))
        for label, comment in (
            ("untrusted", {**good, "authorAssociation": "NONE"}),
            ("hidden", {**good, "isMinimized": True}),
            ("no written outcome", {**good, "body": marker_only}),
            ("unknown outcome", self.answer(pr, ("previously_missed",), outcome="ignored")),
            ("quoted in code", {**good, "body": good["body"].replace("<!--", "`<!--")}),
            ("outcome only in a fence", {**good, "body": f"```\nDeclined.\n```\n\n{marker_only}"}),
        ):
            with self.subTest(label):
                unmet = self.unmet_on(self.with_comments(pr, comment))
                self.assertTrue(any(i.startswith("UNTHREADED_FINDINGS") for i in unmet), unmet)

    def test_one_answered_item_leaves_another_under_the_same_count_unmet(self) -> None:
        label = "Comments suppressed due to low confidence (1)"
        first = self.earlier(label)
        second = self.earlier(label, rid="PRR_older", finding="Bound the retry.")
        pr = fixtures.payload([first, fixtures.review(body=self.CLEAN)], files=["a.py"])
        answered = self.answer(pr, ("suppressed",))
        both = fixtures.payload(
            [second, first, fixtures.review(body=self.CLEAN)], files=["a.py"], comments=[answered]
        )
        self.assertTrue(any("suppressed=2" in item for item in self.unmet_on(both)))

    def test_a_new_round_raising_the_same_text_owes_a_new_answer(self) -> None:
        label = "1 previously missed issue"
        keys = {
            answers.items(fixtures.payload([self.earlier(label, rid=rid)]))[0].key
            for rid in ("PRR_a", "PRR_b")
        }
        self.assertEqual(2, len(keys))

    def test_each_count_no_thread_carries_is_met_once_every_item_is_acknowledged(self) -> None:
        coderabbit = fixtures.review(
            login=fixtures.pr_review_rounds.CODERABBIT_LOGIN,
            body=fixtures.cr_outside_diff_body(),
            rid="PRR_two",
        )
        qodo = {
            **fixtures.comment(
                login=fixtures.pr_review_rounds.QODO_LOGIN, body=fixtures.qodo_review_body()
            ),
            "id": "IC_qodo",
        }
        odd = "## Pull request overview\n\nThe change is narrow.\n"
        noted = fixtures.review(oid=fixtures.OLD, body=odd, rid="PRR_odd")
        unthreaded = fixtures.review(body=fixtures.overview_v2())
        for label, reviews, comments, kinds in (
            (
                "outside diff",
                [fixtures.review(body=self.CLEAN), coderabbit],
                [],
                ("cr_outside_diff",),
            ),
            ("qodo", [fixtures.review(body=self.CLEAN)], [qodo], ("qodo_open",)),
            ("noted", [noted, fixtures.review(body=self.CLEAN)], [], (answers.NOTED,)),
            ("unthreaded", [unthreaded], [], (answers.NO_THREAD,)),
        ):
            with self.subTest(label):
                pr = fixtures.payload(reviews, files=["a.py"], comments=comments)
                self.assertTrue(self.unmet_on(pr))
                answered = self.with_comments(pr, self.answer(pr, kinds))
                left = [i for i in self.unmet_on(answered) if not i.startswith("COUNT_UNREAD")]
                self.assertEqual([], left)

    def test_a_key_moves_with_the_round_and_the_text_it_names(self) -> None:
        """Two rounds on one commit, or a Qodo entry edited in place, never share an answer."""
        odd = "## Pull request overview\n\nThe change is narrow.\n"
        rounds = [
            fixtures.review(oid=fixtures.OLD, body=odd, rid=rid) for rid in ("PRR_a", "PRR_b")
        ]
        notes = [i.key for i in answers.items(fixtures.payload(rounds)) if i.kind == answers.NOTED]
        self.assertEqual(2, len(set(notes)))
        login = fixtures.pr_review_rounds.QODO_LOGIN
        qodo = [
            answers.items(
                fixtures.payload(
                    [], comments=[{**fixtures.comment(login=login, body=body), "id": "IC_q"}]
                )
            )
            for body in (
                fixtures.qodo_review_body(),
                fixtures.qodo_review_body().replace("does not describe", "hides"),
            )
        ]
        self.assertTrue(qodo[0])
        self.assertNotEqual([i.key for i in qodo[0]], [i.key for i in qodo[1]])

    def test_a_no_thread_block_s_key_moves_with_what_it_counts(self) -> None:
        def key(text: str) -> str:
            pr = fixtures.payload([fixtures.review(body=text)], files=["a.py"])
            return next(i.key for i in answers.items(pr) if i.kind == answers.NO_THREAD)

        self.assertNotEqual(
            key(fixtures.overview_v2()), key(fixtures.overview_v2(open_count="3 open findings"))
        )

    def test_a_login_drift_takes_no_answer(self) -> None:
        drifted = fixtures.review(login="copilot-pull-request-reviewer-v2", rid="PRR_d")
        pr = fixtures.payload([drifted, fixtures.review(body=self.CLEAN)], files=["a.py"])
        (drift,) = [i for i in answers.items(pr) if i.where == answers.DRIFT]
        self.assertEqual(
            ([], f"'declined' does not answer {drift.key}, which takes none while it stands"),
            answers.ack_lines(pr, [f"{drift.key}=declined"]),
        )
        mark = answers.FINDING_ACK.format(drift.key, "declined")
        written = fixtures.ack("0" * 12, "OWNER", text=f"Declined.\n\n{mark}")
        unmet = self.unmet_on(self.with_comments(pr, written))
        self.assertTrue(any(i.startswith("REVIEWER_LOGIN_DRIFT") for i in unmet), unmet)
        self.assertFalse(any(i.startswith("OUTPUT_NOTED") for i in unmet), unmet)
        self.assertEqual("", answers.mark(drift, {}))

    def test_a_noted_part_its_own_round_acknowledged_on_its_head_is_answered(self) -> None:
        """A fix push turns an acknowledged head hold into noted output, already answered."""
        earlier = fixtures.review(oid=fixtures.OLD, body=fixtures.unplaced("Earlier."), rid="PRR_o")
        pr = fixtures.payload([earlier, fixtures.review(body=self.CLEAN)], files=["a.py"])
        hold = fixtures.pr_review_rounds.shape_hold(pr, fixtures.OLD)
        assert hold is not None
        self.assertTrue(any(i.startswith("OUTPUT_NOTED") for i in self.unmet_on(pr)))
        self.assertEqual([], self.unmet_on(self.with_comments(pr, fixtures.ack(hold[0], "OWNER"))))

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

    def fields_on(self, pr: dict, **fields: object) -> list[str]:
        """`unmet_on`, each named digest field rebuilt to a value no crafted payload reaches."""
        real = pr_review.digest

        def rebuilt(owner: str, repo: str, num: int, **kwargs: Any) -> tuple[str, int]:
            answer = real(owner, repo, num, **kwargs)
            kwargs["fields_out"][:] = [(k, fields.get(k, v)) for k, v in kwargs["fields_out"]]
            return answer

        with mock.patch.object(pr_review, "digest", side_effect=rebuilt):
            return self.unmet_on(pr)

    def test_reviewer_output_held_or_noted_is_unmet_until_acknowledged(self) -> None:
        held = fixtures.review(body=fixtures.unplaced())
        fp = fixtures.pr_review_rounds.shape_hold(fixtures.payload([held]))
        assert fp is not None
        acked = fixtures.ack(fp[0], "OWNER")
        earlier = fixtures.review(oid=fixtures.OLD, body=fixtures.unplaced("Earlier."), rid="PRR_o")
        for label, reviews, comments, want, unwanted in (
            ("held", [held], [], "UNPLACED_PARTS_UNACKNOWLEDGED", "OUTPUT_NOTED"),
            ("acknowledged", [held], [acked], None, "UNPLACED_PARTS"),
            ("noted", [earlier, fixtures.review(body=self.CLEAN)], [], "OUTPUT_NOTED", "UNPLACED"),
            ("noted under an acknowledgment", [earlier, held], [acked], "OUTPUT_NOTED", "UNPLACED"),
        ):
            with self.subTest(label):
                pr = fixtures.payload(reviews, comments=comments, files=["a.py"])
                unmet = self.unmet_on(pr)
                self.assertFalse(any(item.startswith(unwanted) for item in unmet), unmet)
                if want is not None:
                    self.assertTrue(any(item.startswith(want) for item in unmet), unmet)

    def test_a_pending_review_request_is_unmet(self) -> None:
        self.assertEqual(
            ["REVIEW_PENDING the digest reads requested=yes"],
            self.unmet_on(self.clean(pending=True)),
        )
        self.assertEqual(
            ["REVIEW_PENDING the digest reads requested=None"],
            self.fields_on(self.clean(), requested=None),
        )

    def test_a_count_built_as_none_is_zero_only_where_its_source_is_absent(self) -> None:
        unread = "COUNT_UNREAD the digest holds no readable count for"
        for label, fields, want in (
            ("no head round", {"overview": None, "review_on_head": "local"}, []),
            ("head round", {"overview": None}, [f"{unread} overview=None"]),
            ("suppressed", {"suppressed": None}, [f"{unread} suppressed=None"]),
            ("no qodo", {"qodo_open": None}, []),
            ("no outside diff", {"cr_outside_diff": None, "previously_missed": None}, []),
        ):
            with self.subTest(label):
                self.assertEqual(want, self.fields_on(self.clean(), **fields))
        unmarked = fixtures.payload([fixtures.review(body="Looks narrow.")], files=["a.py"])
        self.assertIn(f"{unread} overview=None", self.unmet_on(unmarked))

    def test_a_count_a_cut_reviews_window_leaves_a_floor_is_unread(self) -> None:
        cut = "0+ (on_head=0 earlier=0)"
        self.assertEqual(
            [f"COUNT_UNREAD the digest holds no readable count for suppressed={cut}"],
            self.fields_on(self.clean(), suppressed=cut),
        )
        unmet = self.unmet_on(self.clean(older_reviews=True))
        self.assertTrue(any(item.startswith("COUNT_UNREAD") for item in unmet), unmet)
        self.assertFalse(any(item.startswith("UNTHREADED_FINDINGS") for item in unmet), unmet)

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

    def test_a_head_moved_is_reported_before_any_unmet_item(self) -> None:
        self.unmet.return_value = (MOVED, [("COVERAGE_NOT_SHOWN", "coverage=none")])
        self.assertEqual(1, self.run_merge(target(), target(), MERGED))
        self.assertIn(
            f"status=HEAD_MOVED nothing was merged: #123 on example/repo is at {MOVED}",
            self.out.getvalue(),
        )
        self.assertNotIn("COVERAGE_NOT_SHOWN", self.out.getvalue())
        self.assert_not_merged()

    def test_a_merge_gh_refused_on_a_moved_head_reads_head_moved(self) -> None:
        self.merge_rc = 1
        self.assertEqual(1, self.run_merge(target(), target(), target(headRefOid=MOVED)))
        self.assertIn("status=HEAD_MOVED #123 on example/repo reads OPEN", self.out.getvalue())
        self.assertIn(f"reads OPEN at {MOVED}", self.out.getvalue())
        self.cleanup.assert_not_called()


# Every status line `merge` or `wait`'s gate prints, pinned to the route `next=` names for it.
ROUTED = {
    **dict.fromkeys(("MERGE_NOT_CONFIRMED",), "readback"),
    **dict.fromkeys(("PEER_MERGED",), "cleanup"),
    **dict.fromkeys(("UNREAD", "MERGE_REFUSED"), "retry"),
    **dict.fromkeys(
        (
            "HEAD_MOVED",
            "BAD_CHECKOUT",
            "THREADS_UNRESOLVED",
            "UNPLACED_PARTS_UNACKNOWLEDGED",
            "REVIEW_PENDING",
            "UNTHREADED_FINDINGS",
            "OUTPUT_NOTED",
            "MERGE_NOT_CLEAN",
            "VERDICT_NAMES_UNCOUNTED_FINDINGS",
            "AWAITING_LOCAL_PASS",
            "PENDING",
            "CHECKS_PENDING",
            "CHECKS_NOT_MERGEABLE",
            "CHECKS_MISSING",
            "COPILOT_ERROR_TRANSIENT",
        ),
        "redrive",
    ),
    **dict.fromkeys(
        (
            "REFUSED",
            "REVIEW_IS_A_REFUSAL",
            "OUT_OF_SCOPE",
            "COVERAGE_NOT_SHOWN",
            "COUNT_UNREAD",
            "COVERAGE_IS_PARTIAL",
            "COPILOT_QUOTA_EXHAUSTED",
            "COPILOT_QUOTA_EXHAUSTED_REPO_WIDE",
            "COPILOT_ERROR_POSSIBLE_QUOTA",
            "REVIEW_EXCLUDED_FILES_ONLY",
            "ANSWERED_OUTSIDE_REVIEW",
            "REQUEST_NOT_RECORDED",
            "REVIEWER_LOGIN_DRIFT",
        ),
        "maintainer",
    ),
}
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class TestRoutes(MergeCase):
    """`merge` ends on one `next=` line, decided by the table beside its status lines."""

    def printed(self, name: str) -> set[str]:
        """Every literal status name `name` prints, where the gate and `merge` both print them."""
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        return set(re.findall(r"status=([A-Z][A-Z_]+)", source)) - {"MERGED"}

    def test_every_status_line_is_pinned_to_its_route(self) -> None:
        names = set().union(
            *(self.printed(n) for n in ("pr_merge.py", "pr_review.py", "pr_review_answers.py"))
        )
        unmet = {status for status, _ in answers.unmet(fixtures.payload([]), {}, [{}])}
        names |= unmet | {"BAD_CHECKOUT", "OUT_OF_SCOPE"}
        names -= {
            "COMMENTED",
            "REPLIED",
            "REPLIED_AND_RESOLVED",
            "DESCRIPTION_CONTRADICTS_ITS_BRANCH",
        }
        names -= {"NOTHING_WAS_READ"}
        self.assertLessEqual(names, set(ROUTED), names - set(ROUTED))
        for status, want in ROUTED.items():
            with self.subTest(status):
                self.assertEqual(want, answers.route(1, False, f"status={status} why\n"))

    def test_a_run_naming_several_takes_what_this_seat_clears_first(self) -> None:
        text = "status=COUNT_UNREAD a\nstatus=THREADS_UNRESOLVED b\nstatus=UNREAD c\n"
        self.assertEqual("redrive", answers.route(1, False, text))
        self.assertEqual("retry", answers.route(1, False, "status=COUNT_UNREAD a\nstatus=UNREAD c"))

    def test_a_run_printing_no_status_line_goes_to_the_maintainer(self) -> None:
        self.assertEqual(
            "maintainer", answers.route(50, False, "repo=o/r pr=1\n  status=PENDING\n")
        )

    def test_a_confirmed_merge_routes_on_whether_cleanup_ran_clean(self) -> None:
        merged = "status=MERGED pr=1 head=a merge_commit=b, confirmed on read back\n"
        for code, cleanup, want in (
            (0, True, "done"),
            (0, False, "cleanup"),
            (1, True, "maintainer"),
        ):
            with self.subTest(code=code, cleanup=cleanup):
                self.assertEqual(want, answers.route(code, cleanup, merged + "status=X y\n"))

    def test_a_refusal_naming_only_an_earlier_merge_owes_cleanup(self) -> None:
        self.assertEqual(1, self.run_merge(MERGED))
        self.assertEqual("next=cleanup", self.out.getvalue().splitlines()[-1])
        self.out.seek(0)
        self.out.truncate()
        self.assertEqual(1, self.run_merge({**MERGED, "isDraft": True}))
        self.assertEqual("next=maintainer", self.out.getvalue().splitlines()[-1])

    def test_the_route_follows_the_gate_s_own_status_line(self) -> None:
        def verdict(*_: object) -> int:
            print("status=VERDICT_NAMES_UNCOUNTED_FINDINGS the round covering the head opens on")
            return 50

        self.gate.side_effect = verdict
        self.assertEqual(50, self.run_merge(target()))
        self.assertEqual("next=redrive", self.out.getvalue().splitlines()[-1])
        self.assert_not_merged()

    def test_a_confirmed_merge_without_cleanup_names_cleanup(self) -> None:
        self.assertEqual(0, self.run_merge(target(), target(), MERGED))
        self.assertEqual("next=cleanup", self.out.getvalue().splitlines()[-1])
        self.out.seek(0)
        self.out.truncate()
        self.assertEqual(0, self.run_merge(target(), target(), MERGED, extra=("--cleanup",)))
        self.assertEqual("next=done", self.out.getvalue().splitlines()[-1])


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
