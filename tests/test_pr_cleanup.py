#!/usr/bin/env python3
"""Drive pr_cleanup's verify-then-delete and its sweep against responses and checkouts it must read.

Every GraphQL read is answered from a fake, and the local half runs against throwaway clones, so
no case reaches the network. `pr_review.py cleanup` forwards here, and the cases calling
`pr_review.main` hold that forward.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_cleanup
import pr_review

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "spec"))


CLEANUP_HEAD = "a" * 40
CLEANUP_BRANCH = "feature/example-topic"


def fork_head(number: int) -> dict:
    """An open pull request headed on a same-named branch in a fork, not in `o/r`."""
    return {"number": number, "headRepository": {"nameWithOwner": f"fork{number}/r"}}


def cleanup_pr(**overrides: object) -> dict:
    """A merged pull request into `develop` from a head in o/r, every field overridable."""
    pr: dict[str, object] = {
        "merged": True,
        "baseRefName": "develop",
        "headRefName": CLEANUP_BRANCH,
        "headRefOid": CLEANUP_HEAD,
        "headRepository": {"nameWithOwner": "o/r"},
    }
    pr.update(overrides)
    return pr


class CleanupCase(unittest.TestCase):
    """Drive `cleanup` against crafted reads, so no case reaches GitHub or deletes anything."""

    def setUp(self) -> None:
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(mock.patch.object(pr_review, "origin_owner", return_value="o"))
        self.enterContext(mock.patch.object(pr_review, "READ_RETRY_DELAYS", ()))
        self.enterContext(mock.patch.object(pr_cleanup, "DELETE_SETTLE_DELAYS", ()))
        self.enterContext(
            mock.patch.object(pr_cleanup.handoff, "registry", return_value=("o", {"r"}))
        )
        self.docs: list[tuple[str, dict]] = []
        self.rest: list[str] = []

    def wire(
        self,
        pr: dict | None = None,
        default: str = "main",
        refs: list[object] | None = None,
        rules: object = (),
        protected: object = False,
        delete_fails: bool = False,
        heads: list[int] | None = None,
        bases: list[int] | None = None,
        head_pages: list[list[dict]] | None = None,
        head_answer: object = None,
        base_answer: object = None,
    ) -> None:
        """Answer each document in turn, `refs` holding one answer per ref read in order.

        A ref answer is a tip, None for no such branch, or an exception the read raises.
        `head_pages` pages the heads connection, each page its nodes, where `heads` is one page
        of this repository's own. An answer given whole replaces that read's repository, or is
        raised where it is an exception.
        """
        pending = list(refs if refs is not None else [CLEANUP_HEAD, None])
        local = [{"number": n, "headRepository": {"nameWithOwner": "o/r"}} for n in heads or []]
        pages = head_pages if head_pages is not None else [local]

        def fake(query: str, variables: dict, **_: object) -> pr_cleanup.shared.Response:
            self.docs.append((query, variables))
            if "pullRequest(number" in query:
                repository = {"defaultBranchRef": {"name": default}, "pullRequest": pr}
                return pr_cleanup.shared.Response({"repository": repository}, [])
            if "heads: pullRequests" in query:
                self.assertEqual(CLEANUP_BRANCH, variables["b"])
                if isinstance(head_answer, Exception):
                    raise head_answer
                if head_answer is not None:
                    return pr_cleanup.shared.Response({"repository": head_answer}, [])
                at = int(variables.get("after", "c0")[1:])
                more = at + 1 < len(pages)
                page = {"hasNextPage": more, "endCursor": f"c{at + 1}" if more else None}
                found = {"heads": {"pageInfo": page, "nodes": pages[at]}}
                return pr_cleanup.shared.Response({"repository": found}, [])
            if "bases: pullRequests" in query:
                self.assertEqual(CLEANUP_BRANCH, variables["b"])
                if isinstance(base_answer, Exception):
                    raise base_answer
                if base_answer is not None:
                    return pr_cleanup.shared.Response({"repository": base_answer}, [])
                listed = [{"number": n} for n in bases or []]
                based = {"bases": {"totalCount": len(listed), "nodes": listed}}
                return pr_cleanup.shared.Response({"repository": based}, [])
            if "deleteRef" in query:
                if delete_fails:
                    raise pr_cleanup.shared.GraphQLError("gh graphql reported errors")
                return pr_cleanup.shared.Response({"deleteRef": {"clientMutationId": None}}, [])
            if "qualifiedName" in query:
                answer = pending.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                ref = None if answer is None else {"id": "REF_read", "target": {"oid": answer}}
                return pr_cleanup.shared.Response({"repository": {"ref": ref}}, [])
            raise AssertionError(f"unexpected document: {query[:60]}")

        def rest(
            path: str, jq: str | None = None, raw: bool = False
        ) -> subprocess.CompletedProcess:
            self.rest.append(path)
            if "/rules/branches/" in path:
                body = rules
            else:
                body = {"name": CLEANUP_BRANCH, "protected": protected}
            if isinstance(body, Exception):
                return subprocess.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)")
            text = json.dumps(list(body) if isinstance(body, tuple) else body)
            return subprocess.CompletedProcess([], 0, text, "")

        self.enterContext(mock.patch.object(pr_cleanup.shared, "request", side_effect=fake))
        self.enterContext(mock.patch.object(pr_review, "gh_rest", side_effect=rest))

    def run_cleanup(self, repo: str = "o/r") -> int:
        return pr_review.main(["cleanup", "7", "--repo", repo])

    def deleted(self) -> bool:
        return any("deleteRef" in q for q, _ in self.docs)


class TestCleanupDeletesOnlyAVerifiedMergedBranch(CleanupCase):
    def test_a_matching_tip_is_deleted_by_the_id_just_read_and_confirmed_gone(self) -> None:
        """Two exact reads by qualified name, the tip before the delete and the read back after."""
        self.wire(cleanup_pr())
        self.assertEqual(0, self.run_cleanup())
        delete = next(v for q, v in self.docs if "deleteRef" in q)
        self.assertEqual({"refId": "REF_read"}, delete)
        reads = [v for q, v in self.docs if "qualifiedName" in q]
        self.assertEqual([f"refs/heads/{CLEANUP_BRANCH}"] * 2, [v["q"] for v in reads])
        self.assertIn("status=DELETED", self.out.getvalue())

    def test_a_branch_already_gone_is_done_and_deletes_nothing(self) -> None:
        self.wire(cleanup_pr(), refs=[None])
        self.assertEqual(0, self.run_cleanup())
        self.assertFalse(self.deleted())
        self.assertIn("status=ALREADY_GONE", self.out.getvalue())

    def test_a_moved_tip_refuses_and_names_both_commits(self) -> None:
        moved = "b" * 40
        self.wire(cleanup_pr(), refs=[moved])
        self.assertEqual(1, self.run_cleanup())
        self.assertFalse(self.deleted())
        out = self.out.getvalue()
        self.assertIn("status=TIP_MOVED", out)
        self.assertIn(moved, out)
        self.assertIn(CLEANUP_HEAD, out)

    def test_a_ref_still_there_after_the_delete_is_not_reported_deleted(self) -> None:
        self.wire(cleanup_pr(), refs=[CLEANUP_HEAD, CLEANUP_HEAD])
        self.assertEqual(2, self.run_cleanup())
        self.assertTrue(self.deleted())
        self.assertIn("status=DELETE_NOT_CONFIRMED", self.out.getvalue())
        self.assertNotIn("status=DELETED", self.out.getvalue())

    def test_a_read_back_lagging_the_delete_settles_to_deleted(self) -> None:
        self.wire(cleanup_pr(), refs=[CLEANUP_HEAD, CLEANUP_HEAD, None])
        with mock.patch.object(pr_cleanup, "DELETE_SETTLE_DELAYS", (0, 0)):
            self.assertEqual(0, self.run_cleanup())
        self.assertIn("status=DELETED", self.out.getvalue())
        self.assertNotIn("status=DELETE_NOT_CONFIRMED", self.out.getvalue())

    def test_a_ref_present_on_every_settle_read_still_exits_2(self) -> None:
        self.wire(cleanup_pr(), refs=[CLEANUP_HEAD] * 4)
        with mock.patch.object(pr_cleanup, "DELETE_SETTLE_DELAYS", (0, 0)):
            self.assertEqual(2, self.run_cleanup())
        self.assertIn("status=DELETE_NOT_CONFIRMED", self.out.getvalue())
        reads = [v for q, v in self.docs if "qualifiedName" in q]
        self.assertEqual(4, len(reads))

    def test_a_read_back_that_does_not_answer_is_not_reported_deleted(self) -> None:
        unread = pr_cleanup.shared.GraphQLError("gh graphql failed rc=1")
        self.wire(cleanup_pr(), refs=[CLEANUP_HEAD, unread])
        self.assertEqual(2, self.run_cleanup())
        self.assertTrue(self.deleted())
        self.assertIn("status=DELETE_NOT_CONFIRMED", self.out.getvalue())

    def test_a_delete_reporting_failure_is_decided_by_the_read_back(self) -> None:
        """A failed-looking delete may have landed, so the ref read decides rather than the error."""
        self.wire(cleanup_pr(), refs=[CLEANUP_HEAD, None], delete_fails=True)
        self.assertEqual(0, self.run_cleanup())
        out = self.out.getvalue()
        self.assertIn("the delete reported a failure", out)
        self.assertIn("status=DELETED", out)

    def test_a_delete_reporting_failure_with_the_ref_still_there_exits_2(self) -> None:
        self.wire(cleanup_pr(), refs=[CLEANUP_HEAD, CLEANUP_HEAD], delete_fails=True)
        self.assertEqual(2, self.run_cleanup())
        self.assertIn("status=DELETE_NOT_CONFIRMED", self.out.getvalue())


class TestCleanupRefusesBeforeAnyDelete(CleanupCase):
    def assert_refused(self, code: int = 1, status: str = "status=REFUSED") -> None:
        self.assertEqual(code, self.run_cleanup())
        self.assertFalse(self.deleted())
        self.assertIn(status, self.out.getvalue())

    def test_an_unmerged_pull_request_is_refused(self) -> None:
        self.wire(cleanup_pr(merged=False))
        self.assert_refused()

    def test_a_base_other_than_develop_is_refused(self) -> None:
        self.wire(cleanup_pr(baseRefName="main"))
        self.assert_refused()

    def test_a_head_in_another_repository_is_refused(self) -> None:
        self.wire(cleanup_pr(headRepository={"nameWithOwner": "o/r-fork"}))
        self.assert_refused()

    def test_a_head_repository_gone_is_refused(self) -> None:
        self.wire(cleanup_pr(headRepository=None))
        self.assert_refused()

    def test_a_develop_head_is_refused(self) -> None:
        self.wire(cleanup_pr(headRefName="develop"))
        self.assert_refused()

    def test_a_main_head_is_refused_where_main_is_not_the_default(self) -> None:
        self.wire(cleanup_pr(headRefName="main"), default="trunk")
        self.assert_refused()

    def test_the_default_branch_read_live_is_refused(self) -> None:
        self.wire(cleanup_pr(headRefName="trunk"), default="trunk")
        self.assert_refused()

    def test_a_ruleset_on_the_branch_is_refused(self) -> None:
        self.wire(cleanup_pr(), rules=[{"type": "deletion"}])
        self.assert_refused()
        self.assertIn("deletion", self.out.getvalue())

    def test_classic_branch_protection_is_refused(self) -> None:
        self.wire(cleanup_pr(), protected=True)
        self.assert_refused()

    def test_an_unread_rule_set_stops_at_2_rather_than_reading_as_none(self) -> None:
        self.wire(cleanup_pr(), rules=RuntimeError("unread"))
        self.assert_refused(2, "status=UNREAD")
        self.assertIn("HTTP 404", self.err.getvalue())
        self.assertIn("rc=1", self.out.getvalue())

    def test_an_open_pull_request_headed_on_the_branch_is_refused_and_named(self) -> None:
        self.wire(cleanup_pr(), heads=[41])
        self.assert_refused()
        self.assertIn("#41 (head)", self.out.getvalue())

    def test_an_open_pull_request_based_on_the_branch_is_refused_and_named(self) -> None:
        self.wire(cleanup_pr(), bases=[42, 43])
        self.assert_refused()
        self.assertIn("#42 (base), #43 (base)", self.out.getvalue())

    def test_an_open_pull_request_read_that_does_not_answer_stops_at_2(self) -> None:
        bad = pr_cleanup.shared.GraphQLError("gh graphql failed rc=1")
        page = {"hasNextPage": False, "endCursor": None}
        malformed: list[dict[str, object]] = [
            {"head_answer": bad},
            {"head_answer": {"heads": {"nodes": []}}},
            {"head_answer": {"heads": {"pageInfo": page, "nodes": None}}},
            {"head_answer": {"heads": {"pageInfo": page, "nodes": ["#9"]}}},
            {"head_answer": {"heads": {"pageInfo": {"hasNextPage": True}, "nodes": []}}},
            {"base_answer": bad},
            {"base_answer": {"bases": {"totalCount": "0", "nodes": []}}},
        ]
        for answer in malformed:
            with self.subTest(answer=answer):
                self.docs.clear()
                self.wire(
                    cleanup_pr(),
                    head_answer=answer.get("head_answer"),
                    base_answer=answer.get("base_answer"),
                )
                self.assert_refused(2, "status=UNREAD")

    def test_an_unread_heads_page_past_the_first_stops_at_2(self) -> None:
        """A page that does not answer is no pull request missing, however clean the first was."""
        self.wire(cleanup_pr(), head_pages=[[fork_head(9)], [fork_head(10)]])
        request = pr_cleanup.shared.request
        assert isinstance(request, mock.Mock)
        answer = request.side_effect

        def second_page_fails(query: str, variables: dict, **kw: object) -> object:
            if "heads: pullRequests" in query and variables.get("after"):
                raise pr_cleanup.shared.GraphQLError("gh graphql failed rc=1")
            return answer(query, variables, **kw)

        request.side_effect = second_page_fails
        self.assert_refused(2, "status=UNREAD")

    def test_more_open_base_pull_requests_than_the_page_holds_are_refused(self) -> None:
        full = {"bases": {"totalCount": 25, "nodes": []}}
        self.wire(cleanup_pr(), base_answer=full)
        self.assert_refused()
        self.assertIn("25 more with the branch as base", self.out.getvalue())

    def test_a_forks_pull_request_on_a_same_named_branch_is_not_a_user(self) -> None:
        self.wire(cleanup_pr(), head_pages=[[fork_head(9)]])
        self.assertEqual(0, self.run_cleanup())
        self.assertTrue(self.deleted())

    def test_fork_pull_requests_filling_every_page_are_not_users(self) -> None:
        """Past the first page, forks alone sharing the branch name leave nothing to refuse."""
        pages = [[fork_head(n) for n in range(k, k + 100)] for k in (100, 200, 300)]
        self.wire(cleanup_pr(), head_pages=pages)
        self.assertEqual(0, self.run_cleanup())
        self.assertTrue(self.deleted())
        reads = [v.get("after") for q, v in self.docs if "heads: pullRequests" in q]
        self.assertEqual([None, "c1", "c2"], reads)
        self.assertNotIn("more with the branch", self.out.getvalue())

    def test_a_local_pull_request_past_the_first_page_is_refused_and_named(self) -> None:
        local = {"number": 512, "headRepository": {"nameWithOwner": "O/R"}}
        pages = [[fork_head(n) for n in range(100, 200)], [fork_head(200), local]]
        self.wire(cleanup_pr(), head_pages=pages)
        self.assert_refused()
        self.assertIn("#512 (head)", self.out.getvalue())
        self.assertNotIn("#200", self.out.getvalue())

    def test_a_rule_read_that_is_not_a_list_stops_at_2(self) -> None:
        self.wire(cleanup_pr(), rules={"message": "Upgrade to GitHub Pro"})
        self.assert_refused(2, "status=UNREAD")

    def test_an_unread_protection_flag_stops_at_2(self) -> None:
        self.wire(cleanup_pr(), protected=None)
        self.assert_refused(2, "status=UNREAD")

    def test_a_branch_name_that_changes_the_rest_path_stops_at_2(self) -> None:
        self.wire(cleanup_pr(headRefName="feature/a?b"), refs=[CLEANUP_HEAD])
        self.assert_refused(2, "status=UNREAD")
        self.assertEqual([], self.rest)

    def test_an_unread_pull_request_stops_at_2(self) -> None:
        self.wire(None)
        self.assert_refused(2, "status=UNREAD")

    def test_an_unread_tip_stops_at_2(self) -> None:
        self.wire(cleanup_pr(), refs=[pr_cleanup.shared.GraphQLError("gh graphql failed rc=1")])
        self.assert_refused(2, "status=UNREAD")

    def test_an_owner_other_than_the_checkout_is_out_of_scope_before_any_read(self) -> None:
        self.wire(cleanup_pr())
        self.assertEqual(64, self.run_cleanup(repo="x/r"))
        self.assertEqual([], self.docs)
        self.assertIn("status=OUT_OF_SCOPE", self.out.getvalue())

    def test_an_owner_other_than_the_registry_is_refused_before_any_read(self) -> None:
        self.wire(cleanup_pr())
        with mock.patch.object(pr_cleanup.handoff, "registry", return_value=("p", {"r"})):
            self.assertEqual(1, self.run_cleanup())
        self.assertEqual([], self.docs)
        self.assertIn("status=REFUSED", self.out.getvalue())

    def test_an_unregistered_repository_that_is_not_a_fork_is_refused(self) -> None:
        self.wire(cleanup_pr())
        flags = {"hasIssuesEnabled": True, "isFork": False}
        with mock.patch.object(pr_cleanup.handoff, "gh_json", return_value=flags):
            self.assertEqual(1, self.run_cleanup(repo="o/unlisted"))
        self.assertEqual([], self.docs)

    def test_an_unregistered_fork_under_the_owner_is_admitted(self) -> None:
        self.wire(cleanup_pr(headRepository={"nameWithOwner": "o/forked"}))
        flags = {"hasIssuesEnabled": True, "isFork": True}
        with mock.patch.object(pr_cleanup.handoff, "gh_json", return_value=flags):
            self.assertEqual(0, self.run_cleanup(repo="o/forked"))
        self.assertTrue(self.deleted())

    def test_an_unreadable_registry_stops_at_2(self) -> None:
        self.wire(cleanup_pr())
        failure = pr_cleanup.handoff.Execution("could not read the fleet registry")
        with mock.patch.object(pr_cleanup.handoff, "registry", side_effect=failure):
            self.assertEqual(2, self.run_cleanup())
        self.assertEqual([], self.docs)


EDIT_PICKS = (
    r"import sys,re,pathlib; p=pathlib.Path(sys.argv[1]); "
    r"p.write_text(re.sub(r\"^pick\", \"edit\", p.read_text(), flags=re.M))"
)
SEQUENCE_EDITOR = shlex.quote(sys.executable) + ' -c "' + EDIT_PICKS + '"'


def run_git(cwd: str, *args: str) -> str:
    """One git call in a throwaway repository, failing the test on a nonzero exit."""
    done = subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return done.stdout.strip()


class LocalCleanupCase(CleanupCase):
    """A bare origin, a primary clone naming o/r, and linked worktrees, all real git in a temp dir.

    Only the GitHub reads and writes are mocked, so every local removal below is git's own.
    """

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.enterContext(
            mock.patch.dict(
                os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
            )
        )
        self.bare = os.path.join(self.root, "origin.git")
        self.primary = os.path.join(self.root, "primary")
        self.seed = os.path.join(self.root, "seed")
        subprocess.run(
            ["git", "init", "--bare", "-b", "develop", self.bare], check=True, capture_output=True
        )
        subprocess.run(["git", "init", "-b", "develop", self.seed], check=True, capture_output=True)
        self.configure(self.seed)
        self.commit(self.seed, "seed")
        run_git(self.seed, "push", self.bare, "develop:develop", "develop:main")
        subprocess.run(["git", "clone", self.bare, self.primary], check=True, capture_output=True)
        self.configure(self.primary)
        run_git(self.primary, "branch", "main", "origin/main")
        run_git(self.primary, "config", f"url.{self.bare}.insteadOf", "https://github.com/o/r.git")
        run_git(self.primary, "remote", "set-url", "origin", "https://github.com/o/r.git")
        self.count = 0

    def configure(self, repo: str) -> None:
        run_git(repo, "config", "user.name", "Test User")
        run_git(repo, "config", "user.email", "test@example.com")
        run_git(repo, "config", "commit.gpgsign", "false")

    def commit(self, repo: str, message: str) -> str:
        run_git(repo, "commit", "--allow-empty", "-m", message)
        return run_git(repo, "rev-parse", "HEAD")

    def worktree(self, branch: str, commits: int = 1) -> tuple[str, str]:
        """A linked worktree on a new local branch with `commits` unpushed commits."""
        path = os.path.join(self.root, "wt", branch.replace("/", "_"))
        run_git(self.primary, "worktree", "add", "-b", branch, path, "origin/develop")
        for _ in range(commits):
            self.count += 1
            self.commit(path, f"work {self.count}")
        return path, run_git(path, "rev-parse", "HEAD")

    def advance_origin(self) -> str:
        """A new commit on origin's develop, as a merge into it would leave."""
        run_git(self.seed, "pull", "--ff-only", self.bare, "develop")
        self.count += 1
        tip = self.commit(self.seed, f"merge {self.count}")
        run_git(self.seed, "push", self.bare, "develop:develop")
        return tip

    def branches(self) -> list[str]:
        out = run_git(self.primary, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        return out.splitlines()

    def listed(self) -> list[str]:
        out = run_git(self.primary, "worktree", "list", "--porcelain")
        return [
            os.path.realpath(ln.split(" ", 1)[1])
            for ln in out.splitlines()
            if ln.startswith("worktree ")
        ]

    def run_local(self, checkout: str | None = None) -> int:
        argv = ["cleanup", "7", "--repo", "o/r", "--checkout", checkout or self.primary]
        return pr_review.main(argv)

    def pause_rebase(self, repo: str) -> None:
        env = {**os.environ, "GIT_SEQUENCE_EDITOR": SEQUENCE_EDITOR}
        argv = ["git", "-C", repo, "rebase", "-i", "HEAD~2"]
        subprocess.run(argv, env=env, capture_output=True, check=True)

    def wire_merged(self, tip: str) -> None:
        self.wire(cleanup_pr(headRefOid=tip), refs=[tip, tip, None])


class TestCleanupLocalHalf(LocalCleanupCase):
    def test_the_worktree_and_branch_go_and_the_base_clone_catches_up(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        merged = self.advance_origin()
        self.wire_merged(tip)
        self.assertEqual(0, self.run_local())
        self.assertFalse(os.path.exists(path))
        self.assertNotIn(CLEANUP_BRANCH, self.branches())
        self.assertEqual([self.primary], self.listed())
        self.assertEqual(merged, run_git(self.primary, "rev-parse", "HEAD"))
        out = self.out.getvalue()
        self.assertIn("status=DELETED", out)
        self.assertIn("status=CLEANED", out)
        self.assertIn("## develop...origin/develop", out)

    def test_without_a_checkout_nothing_local_is_touched(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        self.wire(cleanup_pr(headRefOid=tip), refs=[tip, None])
        self.assertEqual(0, pr_review.main(["cleanup", "7", "--repo", "o/r"]))
        self.assertTrue(os.path.isdir(path))
        self.assertIn(CLEANUP_BRANCH, self.branches())
        self.assertNotIn("status=CLEANED", self.out.getvalue())

    def test_a_branch_already_gone_locally_still_returns_the_base_clone(self) -> None:
        merged = self.advance_origin()
        self.wire(cleanup_pr(headRefOid=merged), refs=[merged, merged, None])
        self.assertEqual(0, self.run_local())
        self.assertIn("is already gone", self.out.getvalue())
        self.assertEqual(merged, run_git(self.primary, "rev-parse", "HEAD"))

    def test_a_moved_local_tip_refuses_and_removes_nothing(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        self.count += 1
        later = self.commit(path, f"unpushed {self.count}")
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        out = self.out.getvalue()
        self.assertIn("status=LOCAL_TIP_MOVED", out)
        self.assertIn(tip, out)
        self.assertIn(later, out)
        self.assertFalse(self.deleted())
        self.assertIn("remote branch included", self.out.getvalue())
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(later, run_git(self.primary, "rev-parse", CLEANUP_BRANCH))

    def test_a_dirty_worktree_refuses_and_removes_nothing(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        Path(path, "scratch.txt").write_text("x", encoding="utf-8")
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=WORKTREE_DIRTY", self.out.getvalue())
        self.assertFalse(self.deleted())
        self.assertIn("remote branch included", self.out.getvalue())
        self.assertTrue(os.path.isdir(path))
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_locked_worktree_refuses_and_removes_nothing(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        run_git(self.primary, "worktree", "lock", path)
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
        self.assertFalse(self.deleted())
        self.assertIn("remote branch included", self.out.getvalue())
        self.assertTrue(os.path.isdir(path))
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_checkout_whose_origin_is_another_repository_is_refused_before_the_remote_delete(
        self,
    ) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        run_git(self.primary, "remote", "set-url", "origin", "git@github.com:other/repo.git")
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=BAD_CHECKOUT", self.out.getvalue())
        self.assertFalse(self.deleted())
        self.assertTrue(os.path.isdir(path))

    def test_a_linked_worktree_is_not_a_primary_checkout(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local(path))
        self.assertIn("status=BAD_CHECKOUT", self.out.getvalue())
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_remote_refusal_skips_the_local_half(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        self.wire(cleanup_pr(headRefOid=tip, merged=False))
        self.assertEqual(1, self.run_local())
        self.assertNotIn("status=CLEANED", self.out.getvalue())
        self.assertTrue(os.path.isdir(path))
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_primary_holding_the_branch_moves_to_develop_before_the_delete(self) -> None:
        run_git(self.primary, "checkout", "-b", CLEANUP_BRANCH)
        tip = self.commit(self.primary, "primary work")
        self.advance_origin()
        self.wire_merged(tip)
        self.assertEqual(0, self.run_local())
        self.assertNotIn(CLEANUP_BRANCH, self.branches())
        self.assertEqual("develop", run_git(self.primary, "symbolic-ref", "--short", "HEAD"))
        out = self.out.getvalue()
        self.assertLess(out.index("switched"), out.index("deleting:"))

    def test_a_rebase_paused_in_the_primary_on_the_branch_refuses(self) -> None:
        run_git(self.primary, "checkout", "-b", CLEANUP_BRANCH)
        for n in range(3):
            self.commit(self.primary, f"primary work {n}")
        tip = run_git(self.primary, "rev-parse", "HEAD")
        self.pause_rebase(self.primary)
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
        self.assertFalse(self.deleted())
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_the_closing_informational_reads_never_fail_a_landed_cleanup(self) -> None:
        _, tip = self.worktree(CLEANUP_BRANCH)
        self.wire_merged(tip)
        real = pr_cleanup.cleanup_git

        def flaky(root: str, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
            if args[:1] == ("status",) and "--branch" in args:
                raise pr_cleanup.CleanupUnread("status read failed")
            return real(root, *args, **kw)

        listing = pr_cleanup.cleanup_worktrees

        def failing_list(primary: str) -> list[pr_cleanup.Worktree]:
            if "status unavailable" in self.out.getvalue():
                raise pr_cleanup.CleanupUnread("list read failed")
            return listing(primary)

        with (
            mock.patch.object(pr_cleanup, "cleanup_git", side_effect=flaky),
            mock.patch.object(pr_cleanup, "cleanup_worktrees", side_effect=failing_list),
        ):
            self.assertEqual(0, self.run_local())
        out = self.out.getvalue()
        self.assertIn("status unavailable", out)
        self.assertIn("worktrees listed: unavailable", out)
        self.assertIn("status=CLEANED", out)

    def test_a_primary_stopped_mid_cherry_pick_on_the_branch_refuses(self) -> None:
        run_git(self.primary, "checkout", "-b", CLEANUP_BRANCH)
        tip = self.commit(self.primary, "primary work")
        admin = Path(run_git(self.primary, "rev-parse", "--absolute-git-dir"))
        (admin / "CHERRY_PICK_HEAD").write_text(tip + "\n", encoding="utf-8")
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
        self.assertFalse(self.deleted())
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_remote_branch_that_moved_before_the_delete_is_not_deleted(self) -> None:
        _, tip = self.worktree(CLEANUP_BRANCH)
        self.wire(cleanup_pr(headRefOid=tip), refs=[tip, "c" * 40])
        self.assertEqual(1, self.run_local())
        self.assertIn("status=TIP_MOVED", self.out.getvalue())
        self.assertFalse(self.deleted())

    def test_an_origin_branch_that_differs_from_the_merged_head_refuses(self) -> None:
        path, _ = self.worktree(CLEANUP_BRANCH)
        run_git(path, "push", "origin", CLEANUP_BRANCH)
        self.count += 1
        tip = self.commit(path, f"after the push {self.count}")
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=TIP_MOVED", self.out.getvalue())
        self.assertFalse(self.deleted())

    def test_a_primary_holding_the_branch_with_tracked_changes_refuses(self) -> None:
        run_git(self.primary, "checkout", "-b", CLEANUP_BRANCH)
        Path(self.primary, "tracked.txt").write_text("a", encoding="utf-8")
        run_git(self.primary, "add", "tracked.txt")
        tip = run_git(self.primary, "rev-parse", "HEAD")
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=BASE_DIRTY", self.out.getvalue())
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_diverged_develop_is_reported_without_failing_the_removals(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        self.advance_origin()
        run_git(self.primary, "fetch", "origin")
        self.count += 1
        self.commit(self.primary, f"local only {self.count}")
        self.wire_merged(tip)
        self.assertEqual(0, self.run_local())
        out = self.out.getvalue()
        self.assertLess(out.index("removed worktree"), out.index("status=BASE_NOT_FF"))
        self.assertIn("base=kept", out)
        self.assertFalse(os.path.exists(path))
        self.assertNotIn(CLEANUP_BRANCH, self.branches())

    def test_the_shell_directory_inside_a_removed_worktree_is_handled_per_platform(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        self.wire_merged(tip)
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(path)
        if pr_cleanup.ON_WINDOWS:
            self.assertEqual(1, self.run_local())
            self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
            self.assertTrue(os.path.isdir(path))
        else:
            self.assertEqual(0, self.run_local())
            self.assertIn("note: the shell's directory", self.out.getvalue())
            self.assertFalse(os.path.exists(path))

    def test_a_shell_inside_the_worktree_refuses_on_windows(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        self.wire_merged(tip)
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(path)
        with mock.patch.object(pr_cleanup, "ON_WINDOWS", True):
            self.assertEqual(1, self.run_local())
        self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
        self.assertTrue(os.path.isdir(path))
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_worktree_with_submodules_refuses_and_removes_nothing(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        Path(path, ".gitmodules").write_text("", encoding="utf-8")
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
        self.assertTrue(os.path.isdir(path))

    def test_a_rerun_with_the_primary_elsewhere_is_done_with_the_base_kept(self) -> None:
        run_git(self.primary, "checkout", "-b", "elsewhere")
        self.wire(cleanup_pr(headRefOid=CLEANUP_HEAD), refs=[CLEANUP_HEAD, CLEANUP_HEAD, None])
        self.assertEqual(0, self.run_local())
        out = self.out.getvalue()
        self.assertIn("is already gone", out)
        self.assertIn("base=kept", out)

    def test_a_tip_that_moves_before_the_delete_is_a_refusal_after_the_removals(self) -> None:
        _, tip = self.worktree(CLEANUP_BRANCH)
        self.wire_merged(tip)
        real = pr_cleanup.cleanup_tip
        heads = []

        def moving(primary: str, ref: str) -> str | None:
            if not ref.startswith("refs/heads/"):
                return real(primary, ref)
            heads.append(ref)
            return "b" * 40 if len(heads) == 3 else real(primary, ref)

        with mock.patch.object(pr_cleanup, "cleanup_tip", side_effect=moving):
            self.assertEqual(1, self.run_local())
        out = self.out.getvalue()
        self.assertLess(out.index("removed worktree"), out.index("status=LOCAL_TIP_MOVED"))
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_an_unconfirmed_branch_delete_is_exit_2(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        self.wire_merged(tip)
        with mock.patch.object(pr_cleanup, "cleanup_tip", side_effect=itertools.repeat(tip)):
            self.assertEqual(2, self.run_local())
        self.assertIn("status=LOCAL_DELETE_NOT_CONFIRMED", self.out.getvalue())
        self.assertFalse(os.path.exists(path))

    def test_a_primary_on_another_branch_is_left_there(self) -> None:
        _, tip = self.worktree(CLEANUP_BRANCH)
        run_git(self.primary, "checkout", "-b", "elsewhere")
        self.wire_merged(tip)
        self.assertEqual(0, self.run_local())
        self.assertEqual("elsewhere", run_git(self.primary, "symbolic-ref", "--short", "HEAD"))
        out = self.out.getvalue()
        self.assertIn("keep base-not-on-develop", out)
        self.assertIn("base=kept", out)

    def test_a_branch_being_bisected_in_another_worktree_is_kept(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH, commits=3)
        run_git(path, "bisect", "start")
        run_git(path, "bisect", "bad", "HEAD")
        run_git(path, "bisect", "good", "HEAD~2")
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
        self.assertFalse(self.deleted())
        self.assertIn(CLEANUP_BRANCH, self.branches())
        self.assertTrue(os.path.isdir(path))

    def test_a_branch_being_rebased_in_another_worktree_is_kept(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH, commits=3)
        self.pause_rebase(path)
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
        self.assertFalse(self.deleted())
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_prunable_entry_holding_the_branch_is_removed_without_force(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        shutil.rmtree(path)
        self.wire_merged(tip)
        self.assertEqual(0, self.run_local())
        self.assertEqual([self.primary], self.listed())
        self.assertNotIn(CLEANUP_BRANCH, self.branches())

    def test_a_local_tip_behind_the_merged_head_is_removed(self) -> None:
        path, _ = self.worktree(CLEANUP_BRANCH)
        self.count += 1
        ahead = self.commit(path, f"merged tip {self.count}")
        run_git(self.primary, "branch", "ahead", ahead)
        run_git(path, "reset", "--hard", "HEAD~1")
        self.wire_merged(ahead)
        self.assertEqual(0, self.run_local())
        self.assertNotIn(CLEANUP_BRANCH, self.branches())

    def test_a_partly_removed_worktree_is_an_unconfirmed_removal(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        self.wire_merged(tip)
        real = pr_cleanup.cleanup_git

        def partial(root: str, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("worktree", "remove"):
                real(root, *args)
                os.makedirs(path)
                raise pr_cleanup.CleanupUnread("could not delete every file")
            return real(root, *args, **kw)

        with mock.patch.object(pr_cleanup, "cleanup_git", side_effect=partial):
            self.assertEqual(2, self.run_local())
        self.assertIn(path, self.out.getvalue())
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_branch_delete_git_refuses_is_a_kept_branch(self) -> None:
        _, tip = self.worktree(CLEANUP_BRANCH)
        self.wire_merged(tip)
        real = pr_cleanup.cleanup_git

        def refusing(root: str, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("branch", "-D"):
                return subprocess.CompletedProcess([], 1, "", "error: used by worktree")
            return real(root, *args, **kw)

        with mock.patch.object(pr_cleanup, "cleanup_git", side_effect=refusing):
            self.assertEqual(1, self.run_local())
        self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_worktree_mid_operation_refuses_and_removes_nothing(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        admin = Path(run_git(path, "rev-parse", "--absolute-git-dir"))
        (admin / "MERGE_HEAD").write_text(tip + "\n", encoding="utf-8")
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
        self.assertTrue(os.path.isdir(path))
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_worktree_removed_by_someone_else_mid_run_is_done(self) -> None:
        path, tip = self.worktree(CLEANUP_BRANCH)
        self.wire_merged(tip)
        real = pr_cleanup.cleanup_git

        def racing(root: str, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("worktree", "remove"):
                real(root, *args)
                raise pr_cleanup.CleanupUnread("another process got there first")
            return real(root, *args, **kw)

        with mock.patch.object(pr_cleanup, "cleanup_git", side_effect=racing):
            self.assertEqual(0, self.run_local())
        self.assertIn("already gone", self.out.getvalue())
        self.assertFalse(os.path.exists(path))
        self.assertNotIn(CLEANUP_BRANCH, self.branches())

    def test_a_branch_deleted_by_someone_else_before_the_delete_is_already_gone(self) -> None:
        _, tip = self.worktree(CLEANUP_BRANCH)
        self.wire_merged(tip)
        real = pr_cleanup.cleanup_tip
        calls = []

        def vanishing(primary: str, ref: str) -> str | None:
            if ref.startswith("refs/heads/"):
                calls.append(ref)
            if len(calls) == 3 and ref.startswith("refs/heads/"):
                run_git(primary, "branch", "-D", "--", CLEANUP_BRANCH)
                return None
            return real(primary, ref)

        with mock.patch.object(pr_cleanup, "cleanup_tip", side_effect=vanishing):
            self.assertEqual(0, self.run_local())
        self.assertIn("status=CLEANED", self.out.getvalue())

    def test_a_primary_that_cannot_leave_the_branch_keeps_it_and_exits_1(self) -> None:
        Path(self.seed, "clash.txt").write_text("x", encoding="utf-8")
        run_git(self.seed, "add", "clash.txt")
        self.commit(self.seed, "add clash")
        run_git(self.seed, "push", self.bare, "develop:develop")
        run_git(self.primary, "pull", "--ff-only", "origin", "develop")
        run_git(self.primary, "checkout", "-b", CLEANUP_BRANCH, "develop~1")
        Path(self.primary, "clash.txt").write_text("y", encoding="utf-8")
        tip = run_git(self.primary, "rev-parse", "HEAD")
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("could not leave", self.out.getvalue())
        self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
        self.assertIn("nothing was deleted, remote branch included", self.out.getvalue())
        self.assertFalse(self.deleted())
        self.assertIn(CLEANUP_BRANCH, self.branches())

    def test_a_develop_held_by_another_worktree_stops_the_primary_leaving(self) -> None:
        run_git(self.primary, "checkout", "-b", CLEANUP_BRANCH)
        tip = self.commit(self.primary, "primary work")
        other = os.path.join(self.root, "wt", "develop-holder")
        run_git(self.primary, "worktree", "add", other, "develop")
        self.wire_merged(tip)
        self.assertEqual(1, self.run_local())
        self.assertIn("status=WORKTREE_KEPT", self.out.getvalue())
        self.assertFalse(self.deleted())
        self.assertIn(CLEANUP_BRANCH, self.branches())


class TestCleanupOriginParsing(unittest.TestCase):
    def test_https_and_ssh_forms_name_the_repository_case_insensitively(self) -> None:
        for url in (
            "https://github.com/O/R.git",
            "https://github.com/o/r",
            "https://user@github.com/o/r.git/",
            "git@github.com:o/R.git",
            "ssh://git@github.com/O/r",
        ):
            found = pr_cleanup.GITHUB_REMOTE.match(url)
            self.assertIsNotNone(found, url)
            assert found is not None
            self.assertEqual("o/r", f"{found[1]}/{found[2]}".lower())

    def test_other_hosts_and_shapes_do_not_match(self) -> None:
        for url in ("https://example.com/o/r.git", "/srv/o/r.git", "https://github.com/o", ""):
            self.assertIsNone(pr_cleanup.GITHUB_REMOTE.match(url), url)


class TestCleanupSweepArguments(CleanupCase):
    def usage(self, *argv: str) -> None:
        with self.assertRaises(SystemExit) as caught:
            pr_review.main(list(argv))
        self.assertEqual(2, caught.exception.code)

    def test_a_missing_number_is_a_usage_error_outside_the_sweep(self) -> None:
        self.usage("status", "--repo", "o/r")
        self.usage("cleanup", "--repo", "o/r")

    def test_a_required_number_still_parses_ahead_of_the_options(self) -> None:
        class Reached(Exception):
            pass

        with (
            mock.patch.object(pr_review, "full_read", side_effect=Reached),
            self.assertRaises(Reached),
        ):
            pr_review.main(["status", "--repo", "o/r", "7"])
        with mock.patch.object(pr_cleanup, "cleanup", return_value=0) as cleaned:
            self.assertEqual(
                0, pr_review.main(["cleanup", "--repo", "o/r", "7", "--checkout", "x"])
            )
        cleaned.assert_called_once_with("o", "r", 7, "x")

    def test_the_forward_finds_cleanup_behind_a_leading_flag(self) -> None:
        with mock.patch.object(pr_cleanup, "cleanup", return_value=0) as cleaned:
            self.assertEqual(0, pr_review.main(["--repo", "o/r", "cleanup", "7"]))
        cleaned.assert_called_once_with("o", "r", 7, None)

    def test_the_sweep_flags_are_checked_against_each_other(self) -> None:
        self.usage("cleanup", "7", "--sweep", "--repo", "o/r", "--checkout", "/x")
        self.usage("cleanup", "--sweep", "--repo", "o/r")
        self.usage("cleanup", "7", "--repo", "o/r", "--apply")
        self.usage("cleanup", "7", "--repo", "o/r", "--min-age-hours", "1")
        self.usage("status", "7", "--repo", "o/r", "--sweep")


class TestCleanupSweep(LocalCleanupCase):
    def setUp(self) -> None:
        super().setUp()
        self.merged_prs: dict[str, list[dict]] = {}
        self.failing: set[str] = set()

    def age(self, path: str, hours: float) -> None:
        """Set every file the sweep reads the age from to `hours` ago."""
        admin = Path(path, ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        stamp = time.time() - hours * 3600
        for p in (path, *(os.path.join(admin, n) for n in ("HEAD", "index", "logs/HEAD"))):
            if os.path.exists(p):
                os.utime(p, (stamp, stamp))

    def age_branch(self, branch: str, hours: float) -> None:
        log = os.path.join(self.primary, ".git", "logs", "refs", "heads", branch)
        stamp = time.time() - hours * 3600
        os.utime(log, (stamp, stamp))

    def merged_pr(self, branch: str, tip: str, base: str = "develop") -> None:
        node = {
            "number": 7,
            "baseRefName": base,
            "headRefOid": tip,
            "headRepository": {"nameWithOwner": "o/r"},
        }
        self.merged_prs[branch] = [node]

    def wire_sweep(
        self, remote_tip: str | None = None, remote_gone: bool = False, **pr: object
    ) -> None:
        refs: list[object] = [remote_tip, None] if remote_tip else []
        if remote_gone:
            refs = [None]
        self.wire(cleanup_pr(headRefOid=remote_tip or CLEANUP_HEAD, **pr), refs=refs)
        inner = pr_cleanup.shared.request

        def fake(query: str, variables: dict, **kw: Any) -> pr_cleanup.shared.Response:
            if query == pr_cleanup.Q_SWEEP_DEFAULT:
                head = {"defaultBranchRef": {"name": "main"}}
                return pr_cleanup.shared.Response({"repository": head}, [])
            if query == pr_cleanup.Q_SWEEP_MERGED:
                if variables["b"] in self.failing:
                    raise pr_cleanup.shared.GraphQLError("gh graphql reported errors")
                nodes = self.merged_prs.get(variables["b"], [])
                page = {"merged": {"totalCount": len(nodes), "nodes": nodes}}
                return pr_cleanup.shared.Response({"repository": page}, [])
            return inner(query, variables, **kw)

        self.enterContext(mock.patch.object(pr_cleanup.shared, "request", side_effect=fake))

    def sweep(self, *extra: str) -> int:
        argv = ["cleanup", "--sweep", "--repo", "o/r", "--checkout", self.primary, *extra]
        return pr_review.main(argv)

    def scenario(self) -> dict[str, str]:
        """Old and finished items beside every kind of item that must stay."""
        made: dict[str, str] = {}
        path, tip = self.worktree(CLEANUP_BRANCH)
        self.merged_pr(CLEANUP_BRANCH, tip)
        self.age(path, 48)
        made["merged"] = path
        detached = os.path.join(self.root, "wt", "detached")
        run_git(self.primary, "worktree", "add", "--detach", detached, "origin/develop")
        self.age(detached, 48)
        made["detached"] = detached
        gone = os.path.join(self.root, "wt", "gone")
        run_git(self.primary, "worktree", "add", "--detach", gone, "origin/develop")
        admin = Path(gone, ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        shutil.rmtree(gone)
        old = time.time() - 48 * 3600
        os.utime(os.path.join(admin, "index"), (old, old))
        made["gone"] = gone
        fresh = os.path.join(self.root, "wt", "fresh-gone")
        run_git(self.primary, "worktree", "add", "--detach", fresh, "origin/develop")
        shutil.rmtree(fresh)
        made["fresh-gone"] = fresh
        recent, recent_tip = self.worktree("recent-work")
        self.merged_pr("recent-work", recent_tip)
        made["recent"] = recent
        unmerged, _ = self.worktree("unmerged-work")
        self.age(unmerged, 48)
        made["unmerged"] = unmerged
        dirty, dirty_tip = self.worktree("dirty-work")
        self.merged_pr("dirty-work", dirty_tip)
        Path(dirty, "scratch.txt").write_text("x", encoding="utf-8")
        self.age(dirty, 48)
        made["dirty"] = dirty
        locked, locked_tip = self.worktree("locked-work")
        self.merged_pr("locked-work", locked_tip)
        run_git(self.primary, "worktree", "lock", locked)
        self.age(locked, 48)
        made["locked"] = locked
        run_git(self.primary, "branch", "loose-done", "origin/develop")
        self.age_branch("loose-done", 48)
        run_git(self.primary, "branch", "loose-recent", "origin/develop")
        self.advance_origin()
        return made

    def test_a_dry_run_reports_every_item_and_removes_nothing(self) -> None:
        made = self.scenario()
        before = (sorted(self.branches()), sorted(self.listed()))
        self.wire_sweep(remote_tip=run_git(self.primary, "rev-parse", CLEANUP_BRANCH))
        self.assertEqual(0, self.sweep())
        self.assertEqual(before, (sorted(self.branches()), sorted(self.listed())))
        self.assertFalse(self.deleted())
        out = self.out.getvalue()
        self.assertIn(f"would-remove worktree {made['merged']}", out)
        self.assertIn(f"would-remove worktree {made['detached']} detached", out)
        self.assertIn(f"would-remove prunable worktree entry {made['gone']}", out)
        self.assertIn("would-remove branch loose-done", out)
        self.assertIn(f"would-remove remote branch {CLEANUP_BRANCH} via #7", out)
        self.assertIn(f"would-fast-forward develop in {self.primary}", out)
        self.assertNotIn("removed ", out.replace("would-remove", ""))

    def test_apply_removes_what_is_finished_and_keeps_the_rest(self) -> None:
        made = self.scenario()
        tip = run_git(self.primary, "rev-parse", CLEANUP_BRANCH)
        self.wire_sweep(remote_tip=tip)
        self.assertEqual(0, self.sweep("--apply"))
        self.assertIn(made["fresh-gone"], self.listed())
        for gone in ("merged", "detached", "gone"):
            self.assertNotIn(made[gone], self.listed(), gone)
        self.assertFalse(os.path.exists(made["merged"]))
        for kept in ("recent", "unmerged", "dirty", "locked"):
            self.assertTrue(os.path.isdir(made[kept]), kept)
            self.assertIn(made[kept], self.listed(), kept)
        branches = self.branches()
        self.assertNotIn(CLEANUP_BRANCH, branches)
        self.assertNotIn("loose-done", branches)
        for kept in ("develop", "main", "loose-recent", "recent-work", "unmerged-work"):
            self.assertIn(kept, branches)
        self.assertTrue(self.deleted())
        out = self.out.getvalue()
        self.assertIn("keep too-recent", out)
        self.assertIn("keep unmerged-commits", out)
        self.assertIn("keep dirty", out)
        self.assertIn("keep locked", out)
        self.assertEqual(
            run_git(self.primary, "rev-parse", "origin/develop"),
            run_git(self.primary, "rev-parse", "develop"),
        )

    def test_a_read_failure_continues_and_exits_2(self) -> None:
        made = self.scenario()
        early, _ = self.worktree("aa-failing")
        self.age(early, 48)
        self.failing.add("aa-failing")
        tip = run_git(self.primary, "rev-parse", CLEANUP_BRANCH)
        self.wire_sweep(remote_tip=tip)
        self.assertEqual(2, self.sweep("--apply"))
        out = self.out.getvalue()
        self.assertLess(out.index("unread worktree"), out.index("removed worktree"))
        self.assertNotIn(made["merged"], self.listed())
        self.assertIn(early, self.listed())

    def test_the_remote_check_in_a_report_names_a_refusal(self) -> None:
        self.scenario()
        self.wire_sweep(remote_tip=run_git(self.primary, "rev-parse", CLEANUP_BRANCH), merged=False)
        self.assertEqual(0, self.sweep())
        out = self.out.getvalue()
        self.assertIn("keep-remote via #7", out)
        self.assertNotIn("would-remove remote", out)
        self.assertFalse(self.deleted())

    def test_the_remote_check_in_a_report_names_a_branch_already_gone(self) -> None:
        self.scenario()
        self.wire_sweep(remote_gone=True)
        self.assertEqual(0, self.sweep())
        self.assertIn(f"remote already gone {CLEANUP_BRANCH} via #7", self.out.getvalue())

    def test_a_loose_branch_whose_tip_moved_is_kept_as_tip_moved(self) -> None:
        run_git(self.primary, "branch", "loose-done", "origin/develop")
        self.age_branch("loose-done", 48)
        self.wire_sweep()
        moved = pr_cleanup.CleanupRefused("LOCAL_TIP_MOVED", "moved")
        with mock.patch.object(pr_cleanup, "cleanup_delete_branch", side_effect=moved):
            self.assertEqual(0, self.sweep("--apply"))
        self.assertIn("keep tip-moved branch loose-done", self.out.getvalue())

    def test_main_and_develop_survive_a_sweep_with_no_age_floor(self) -> None:
        run_git(self.primary, "checkout", "-b", "primary-work")
        self.wire_sweep()
        self.assertEqual(0, self.sweep("--apply", "--min-age-hours", "0"))
        for kept in ("main", "develop", "primary-work"):
            self.assertIn(kept, self.branches())

    def test_the_primary_checkout_branch_is_never_touched(self) -> None:
        run_git(self.primary, "checkout", "-b", "primary-work")
        self.age_branch("primary-work", 48)
        self.wire_sweep()
        self.assertEqual(0, self.sweep("--apply"))
        self.assertIn("primary-work", self.branches())
        for line in self.out.getvalue().splitlines():
            if "primary-work" in line:
                self.assertTrue(line.startswith("keep base-not-on-develop"), line)

    def test_a_branch_without_a_reflog_is_kept_as_age_unreadable(self) -> None:
        run_git(self.primary, "branch", "loose-done", "origin/develop")
        os.remove(os.path.join(self.primary, ".git", "logs", "refs", "heads", "loose-done"))
        self.wire_sweep()
        self.assertEqual(0, self.sweep("--apply"))
        self.assertIn("keep age-unreadable branch loose-done", self.out.getvalue())
        self.assertIn("loose-done", self.branches())

    def gone_on_branch(self, branch: str, entry_hours: float = 48) -> str:
        """A finished, idle branch whose worktree directory is gone, its record `entry_hours` old."""
        gone = os.path.join(self.root, "wt", branch)
        run_git(self.primary, "worktree", "add", "-b", branch, gone, "origin/develop")
        admin = Path(gone, ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        shutil.rmtree(gone)
        old = time.time() - entry_hours * 3600
        os.utime(os.path.join(admin, "index"), (old, old))
        self.age_branch(branch, 48)
        return gone

    def test_a_pruned_entry_s_branch_is_swept_in_the_same_run(self) -> None:
        gone = self.gone_on_branch("gone-done")
        self.advance_origin()
        self.wire_sweep(remote_tip=None)
        self.assertEqual(0, self.sweep("--apply"))
        self.assertNotIn(gone, self.listed())
        self.assertNotIn("gone-done", self.branches())

    def test_a_dry_run_reports_a_prunable_entry_s_branch(self) -> None:
        self.gone_on_branch("gone-done")
        self.advance_origin()
        self.wire_sweep(remote_tip=None)
        self.assertEqual(0, self.sweep())
        self.assertIn("gone-done", self.branches())
        self.assertIn("would-remove branch gone-done", self.out.getvalue())

    def test_a_too_recent_prunable_entry_still_holds_its_branch(self) -> None:
        gone = self.gone_on_branch("gone-recent", entry_hours=0)
        self.advance_origin()
        self.wire_sweep(remote_tip=None)
        self.assertEqual(0, self.sweep())
        self.assertNotIn("would-remove branch gone-recent", self.out.getvalue())
        self.assertEqual(0, self.sweep("--apply"))
        self.assertIn(gone, self.listed())
        self.assertIn("gone-recent", self.branches())

    def test_a_failed_prune_is_counted_and_the_other_items_still_run(self) -> None:
        made = self.scenario()
        self.gone_on_branch("gone-failed")
        tip = run_git(self.primary, "rev-parse", CLEANUP_BRANCH)
        self.wire_sweep(remote_tip=tip)
        real = pr_cleanup.cleanup_git

        def failing(root: str, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("worktree", "prune"):
                raise pr_cleanup.CleanupUnread("prune failed")
            return real(root, *args, **kw)

        with mock.patch.object(pr_cleanup, "cleanup_git", side_effect=failing):
            self.assertEqual(2, self.sweep("--apply"))
        self.assertIn("unread prunable worktree entries", self.out.getvalue())
        self.assertNotIn("branch gone-failed", self.out.getvalue())
        self.assertIn("gone-failed", self.branches())
        self.assertNotIn(made["merged"], self.listed())

    def test_a_stale_remote_ref_for_a_deleted_branch_is_not_containment(self) -> None:
        stale = os.path.join(self.root, "wt", "stale-detached")
        run_git(self.primary, "worktree", "add", "--detach", stale, "origin/develop")
        self.count += 1
        self.commit(stale, f"only on a deleted branch {self.count}")
        run_git(stale, "push", "origin", "HEAD:refs/heads/gone-branch")
        run_git(self.primary, "fetch", "origin")
        run_git(self.bare, "branch", "-D", "gone-branch")
        self.age(stale, 48)
        sha = run_git(stale, "rev-parse", "HEAD")
        self.wire_sweep()
        for extra in ((), ("--apply",)):
            run_git(self.primary, "update-ref", "refs/remotes/origin/gone-branch", sha)
            self.assertEqual(0, self.sweep(*extra))
            out = self.out.getvalue()
            self.assertIn(f"keep unpushed-commits worktree {stale} detached", out)
            self.assertNotIn(f"would-remove worktree {stale}", out)
            self.assertIn(stale, self.listed())

    def test_a_primary_paused_mid_rebase_keeps_the_branch_in_both_modes(self) -> None:
        run_git(self.primary, "checkout", "-b", "rebasing-work")
        for n in range(3):
            self.commit(self.primary, f"primary work {n}")
        tip = run_git(self.primary, "rev-parse", "HEAD")
        self.pause_rebase(self.primary)
        self.merged_pr("rebasing-work", tip)
        self.age_branch("rebasing-work", 48)
        self.wire_sweep()
        for extra in ((), ("--apply",)):
            self.assertEqual(0, self.sweep(*extra))
            self.assertIn("keep in-progress-operation branch rebasing-work", self.out.getvalue())
            self.assertIn("rebasing-work", self.branches())

    def test_a_worktree_with_submodules_is_kept(self) -> None:
        path, tip = self.worktree("modular-work")
        self.merged_pr("modular-work", tip)
        Path(path, ".gitmodules").write_text("", encoding="utf-8")
        self.age(path, 48)
        self.wire_sweep()
        self.assertEqual(0, self.sweep("--apply"))
        self.assertIn("keep submodules", self.out.getvalue())
        self.assertIn(path, self.listed())

    def test_a_shell_inside_a_worktree_keeps_it_on_windows(self) -> None:
        path, tip = self.worktree("held-work")
        self.merged_pr("held-work", tip)
        self.age(path, 48)
        self.wire_sweep()
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(path)
        with mock.patch.object(pr_cleanup, "ON_WINDOWS", True):
            self.assertEqual(0, self.sweep("--apply"))
        self.assertIn("keep cwd-inside", self.out.getvalue())
        self.assertIn(path, self.listed())

    def test_a_worktree_mid_operation_is_kept(self) -> None:
        path, tip = self.worktree("paused-work")
        self.merged_pr("paused-work", tip)
        admin = Path(run_git(path, "rev-parse", "--absolute-git-dir"))
        (admin / "BISECT_LOG").write_text("\n", encoding="utf-8")
        self.age(path, 48)
        self.wire_sweep()
        self.assertEqual(0, self.sweep("--apply"))
        self.assertIn("keep in-progress-operation", self.out.getvalue())
        self.assertIn(path, self.listed())
        self.assertIn("paused-work", self.branches())

    def test_a_branch_being_bisected_elsewhere_is_kept_as_in_progress(self) -> None:
        path, tip = self.worktree("bisected-work", commits=3)
        self.merged_pr("bisected-work", tip)
        run_git(path, "bisect", "start")
        run_git(path, "bisect", "bad", "HEAD")
        run_git(path, "bisect", "good", "HEAD~2")
        self.age(path, 48)
        self.age_branch("bisected-work", 48)
        self.wire_sweep()
        self.assertEqual(0, self.sweep("--apply"))
        self.assertIn("keep in-progress-operation branch bisected-work", self.out.getvalue())
        self.assertIn("bisected-work", self.branches())
        self.assertIn(path, self.listed())

    def test_a_branch_delete_git_refuses_is_kept_as_checked_out_elsewhere(self) -> None:
        run_git(self.primary, "branch", "loose-done", "origin/develop")
        self.age_branch("loose-done", 48)
        self.wire_sweep()
        real = pr_cleanup.cleanup_git

        def refusing(root: str, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("branch", "-D"):
                return subprocess.CompletedProcess([], 1, "", "error: used by worktree")
            return real(root, *args, **kw)

        with mock.patch.object(pr_cleanup, "cleanup_git", side_effect=refusing):
            self.assertEqual(0, self.sweep("--apply"))
        self.assertIn("keep checked-out-elsewhere branch loose-done", self.out.getvalue())

    def test_a_branch_behind_a_merged_head_counts_as_finished(self) -> None:
        path, _ = self.worktree("behind-work")
        self.count += 1
        ahead = self.commit(path, f"merged tip {self.count}")
        run_git(self.primary, "branch", "ahead", ahead)
        run_git(path, "reset", "--hard", "HEAD~1")
        run_git(self.primary, "worktree", "remove", path)
        self.merged_pr("behind-work", ahead)
        self.age_branch("behind-work", 48)
        self.wire_sweep(remote_tip=ahead)
        self.assertEqual(0, self.sweep())
        self.assertIn("would-remove branch behind-work", self.out.getvalue())

    def test_a_checkout_naming_another_repository_is_refused(self) -> None:
        run_git(self.primary, "remote", "set-url", "origin", "https://github.com/other/repo.git")
        self.wire_sweep()
        self.assertEqual(1, self.sweep("--apply"))
        self.assertIn("status=BAD_CHECKOUT", self.out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
