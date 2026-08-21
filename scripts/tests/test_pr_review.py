#!/usr/bin/env python3
"""Drive pr_review's digest and wait loop against responses they must read correctly.

The script exists to collapse a poll cycle into one invocation, so its failure mode is a wrong
answer rather than a crash: a review attributed to the wrong login, a review counted against a
stale head, or a wait that returns success while nothing landed. Each case below feeds a crafted
GraphQL payload and asserts the reading, with `gql` replaced so no case reaches the network.

Run as `python3 scripts/tests/test_pr_review.py`, or under `python3 -m unittest discover -s scripts/tests`.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pr_review

REPO = Path(__file__).resolve().parent.parent.parent
RUNBOOK = REPO / ".github" / "copilot-instructions.md"
GOVERNANCE = REPO / "GOVERNANCE.md"

HEAD = "a" * 40
OLD = "b" * 40


EARLY = "2026-08-02T10:00:00Z"
LATE = "2026-08-02T11:00:00Z"


# The shape 28 of the 332 measured bodies carry: an overview, and no count of what was read.
# A body of no text at all is not one of the shapes, and the reader now says so, correctly.
OVERVIEW = "## Pull request overview\n\nThe change is narrow.\n"


def review(
    login: str = pr_review.REVIEWER,
    oid: str = HEAD,
    body: str = OVERVIEW + "\n<!-- fleet-review: reviewed=1 changed=1 findings=0 -->",
    at: str = EARLY,
) -> dict:
    return {
        "author": {"login": login},
        "state": "COMMENTED",
        "commit": {"oid": oid},
        "body": body,
        "submittedAt": at,
    }


def comment(
    login: str = pr_review.REVIEWER,
    at: str = LATE,
    body: str = "I have reached my quota limit and cannot review this now.",
) -> dict:
    return {"author": {"login": login}, "createdAt": at, "body": body}


COVERED = (
    "Copilot reviewed 3 out of 3 changed files in this pull request and generated no new comments."
)


def collapsed(
    heading: str = "Comments suppressed due to low confidence (1)",
    finding: str = "a.py:12 The retry count is off by one.",
    covers: str = COVERED,
) -> str:
    """The section as its own `<details>` wrapper, under the round's own coverage line.

    The coverage line is the reviewer's, quoted from the corpus rather than invented: it sat here
    as filler that nothing asserted on, which is one of the two places the shape was already in
    this file while no case read it.
    """
    return (
        f"{OVERVIEW}\n{covers}\n\n<details>\n<summary>{heading}</summary>\n\n"
        f"{finding}\n\n</details>\n"
    )


def nested(
    heading: str = "### Suppressed comments (2)",
    finding: str = "**a.py:12**\n* The retry count is off by one.",
    covers: str = "- **Files reviewed:** 1/1 changed files",
    effort: str = "Lite",
) -> str:
    """The section as a Markdown heading nested inside the `Review details` wrapper.

    The live shape as of 2026-08-05: the section is no longer its own `<details>` wrapper with a
    matching `<summary>`, it is a Markdown heading inside the wrapper that also carries the
    round's file and effort metadata, which trails the findings rather than preceding them.

    That metadata is where this shape states its coverage, and it is the second spelling of the
    line rather than a second wrapper. It sat here as filler that nothing asserted on too.
    """
    return (
        "### Ready to approve\n\nThe change is narrow.\n\n"
        "<details>\n<summary>File summaries</summary>\n\n"
        "| File | Description |\n\n</details>\n\n"
        f"<details>\n<summary>Review details</summary>\n\n{heading}\n\n{finding}\n\n"
        f"{covers}\n"
        f"- **Review effort level:** {effort}\n</details>\n"
    )


def summarized(paths: list[str], covers: str = COVERED) -> str:
    """A round carrying its coverage line and the file table naming `paths`.

    The table is quoted from the corpus in shape: a header, an alignment row, and one row per
    file whose second cell is prose about the change.
    """
    rows = "\n".join(f"| {p} | Prose about the change. |" for p in paths)
    return (
        f"{OVERVIEW}\n{covers}\n\n<details>\n<summary>Show a summary per file</summary>\n\n"
        f"| File | Description |\n| ---- | ----------- |\n{rows}\n\n</details>\n"
    )


REFUSED = (
    "Copilot wasn't able to review this pull request because it exceeds the maximum "
    "number of files (300). Try reducing the number of changed files and requesting a "
    "review from Copilot again."
)


def thread(
    tid: str,
    resolved: bool = False,
    login: str = pr_review.REVIEWER,
    body: str = "A finding.",
    path: str = "a.py",
    line: int = 1,
) -> dict:
    return {
        "id": tid,
        "isResolved": resolved,
        "comments": {
            "nodes": [{"author": {"login": login}, "path": path, "line": line, "body": body}]
        },
    }


# A fixed clock, so a case holds a check at a known age instead of at whatever the suite runs at.
NOW = datetime(2026, 8, 6, 17, 0, 0, tzinfo=UTC)


def ago(seconds: int) -> str:
    """A timestamp `seconds` before NOW, in the spelling GraphQL returns.

    For the cases that pass NOW in as the clock. A `wait` case cannot use this, since `main` reads
    the real clock, and an age measured from a fixed point against a moving one is not the age the
    case means: it grows on every later run and goes negative on any machine whose clock sits
    before NOW. Those cases take `real_ago` instead.
    """
    return (NOW - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def real_ago(seconds: int) -> str:
    """A timestamp `seconds` before the real clock, for the `wait` path, which reads that clock."""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def check(
    name: str = "Check pull request workflow status job",
    status: str = "COMPLETED",
    conclusion: str = "SUCCESS",
    started: str | None = None,
) -> dict:
    """One CheckRun rollup node, in the shape the live rollup returns.

    The starved case this exists for was read off run 31118675530 in this repository: an aggregator
    job dispatched the instant its dependency finished, left `QUEUED` with an empty runner name and
    a `startedAt` stamped equal to its creation, which sat fifteen minutes and never ran. Its
    healthy sibling on the next run wore `IN_PROGRESS` with a runner inside two minutes.
    """
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": ago(60) if started is None else started,
    }


def status_context(
    context: str = "ci/external", state: str = "SUCCESS", created: str | None = None
) -> dict:
    """One StatusContext rollup node, which spells every field differently from a CheckRun."""
    return {
        "__typename": "StatusContext",
        "context": context,
        "state": state,
        "createdAt": ago(60) if created is None else created,
    }


def payload(
    reviews: list[dict],
    threads: list[dict] | None = None,
    merge: str = "CLEAN",
    comments: list[dict] | None = None,
    older: bool = False,
    older_reviews: bool = False,
    pending: bool = False,
    checks: list[dict] | None = None,
    rollup_oid: str | None = None,
    files: list[str] | None = None,
    more_files: bool = False,
) -> dict:
    requested = (
        [{"requestedReviewer": {"__typename": "Bot", "login": pr_review.REVIEWER}}]
        if pending
        else []
    )
    return {
        "id": "PR_test",
        "headRefOid": HEAD,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": merge,
        # The diff's own file list, which the round's file table is compared against.
        # A case naming none gets the empty connection a pull request of no files carries.
        "files": {
            "nodes": [{"path": p} for p in files or []],
            "pageInfo": {"hasNextPage": more_files},
        },
        "reviews": {"nodes": reviews, "pageInfo": {"hasPreviousPage": older_reviews}},
        "reviewThreads": {"nodes": threads or []},
        "comments": {"nodes": comments or [], "pageInfo": {"hasPreviousPage": older}},
        "reviewRequests": {"nodes": requested},
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": rollup_oid or HEAD,
                        # Compared against None, so an existing rollup carrying nothing is expressible.
                        # Reading `if checks` collapsed that onto a null rollup, and the two differ.
                        # An empty rollup is a pull request whose checks have not registered yet.
                        # A null one is a pull request that has none at all.
                        "statusCheckRollup": {"state": "PENDING", "contexts": {"nodes": checks}}
                        if checks is not None
                        else None,
                    }
                }
            ]
        },
    }


class GqlCase(unittest.TestCase):
    """Base that answers every `gql` call from a queue, so no case reaches the network."""

    def answer(self, *responses: dict) -> mock._patch:
        """Patch `gql` to return each response in turn, repeating the last one.

        Also patches `gh_graphql` directly with a default reporting no Copilot review anywhere
        to read a bot id from, so `wait`'s auto-request finds nothing to request and falls
        straight through to polling, same as a repository with no Copilot history. `gql` itself
        never reaches the real `gh_graphql` since the mock above replaces it wholesale, so the
        two patches govern disjoint call sites and neither can shadow the other. A case
        exercising the auto-request itself re-patches `gh_graphql` after calling this.
        """
        queue = list(responses)

        def fake(_query, _owner, _repo, _num):
            return queue.pop(0) if len(queue) > 1 else queue[0]

        patched = self.enterContext(mock.patch.object(pr_review, "gql", side_effect=fake))
        self.enterContext(
            mock.patch.object(
                pr_review,
                "gh_graphql",
                return_value={"repository": {"pullRequests": {"nodes": []}}},
            )
        )
        return patched


class TestLiveState(GqlCase):
    def test_the_review_must_be_the_reviewer_and_on_the_current_head(self) -> None:
        """A stale review is the failure the whole wait exists to avoid reporting as done."""
        for label, reviews, want in (
            ("reviewer on head", [review()], True),
            ("reviewer on an older commit", [review(oid=OLD)], False),
            ("a human on head", [review(login="ptr727")], False),
            ("no reviews at all", [], False),
            ("stale round plus a current one", [review(oid=OLD), review()], True),
        ):
            with self.subTest(case=label):
                self.answer(payload(reviews))
                self.assertEqual((HEAD, want, None), pr_review.live_state("o", "r", 1))

    def test_a_null_author_or_commit_does_not_raise(self) -> None:
        """GraphQL returns null for a deleted account, and a crash there stalls the whole wait."""
        self.answer(payload([{"author": None, "state": "COMMENTED", "commit": None}]))
        self.assertEqual((HEAD, False, None), pr_review.live_state("o", "r", 1))


class TestAnsweredOutsideReview(unittest.TestCase):
    """A refusal answers the request without covering the head, so a wait cannot read it as pending."""

    def test_a_reviewer_comment_newer_than_every_review_is_the_answer(self) -> None:
        answer = pr_review.answered_outside_review(
            payload([review(oid=OLD, at=EARLY)], comments=[comment(at=LATE)])
        )
        self.assertIsNotNone(answer)
        self.assertEqual(LATE, (answer or {}).get("createdAt"))

    def test_an_answer_the_reviewer_then_superseded_is_spent(self) -> None:
        """The review it preceded did land, so the comment is history rather than a stop signal."""
        self.assertIsNone(
            pr_review.answered_outside_review(
                payload([review(at=LATE)], comments=[comment(at=EARLY)])
            )
        )

    def test_another_account_s_comment_is_not_the_reviewer_answering(self) -> None:
        """A maintainer note and a codecov post both postdate the review and mean nothing here."""
        for login in ("ptr727", "codecov[bot]", "copilot-swe-agent"):
            with self.subTest(login=login):
                self.assertIsNone(
                    pr_review.answered_outside_review(
                        payload([review(oid=OLD)], comments=[comment(login=login)])
                    )
                )

    def test_no_comments_at_all_reads_as_no_answer(self) -> None:
        self.assertIsNone(pr_review.answered_outside_review(payload([review(oid=OLD)])))

    def test_ordinary_discussion_does_not_push_the_answer_out_of_the_window(self) -> None:
        """The window reads the newest comments, not the reviewer's, so others crowd it."""
        chatter = [comment(login="ptr727", at=LATE) for _ in range(pr_review.WINDOW - 1)]
        found = pr_review.answered_outside_review(
            payload([review(oid=OLD, at=EARLY)], comments=[comment(at=LATE)] + chatter)
        )
        self.assertIsNotNone(found)

    def test_comments_behind_the_window_are_unknown_rather_than_no_answer(self) -> None:
        """Finding nothing and having nothing to find are one reading once an answer can hide."""
        full = [comment(login="ptr727") for _ in range(pr_review.WINDOW)]
        self.assertTrue(
            pr_review.window_blind(payload([review()], comments=full, older=True), "comments")
        )

    def test_a_window_holding_every_comment_is_not_a_gap(self) -> None:
        """A full window and a window holding the lot are the same length, so length cannot say."""
        full = [comment(login="ptr727") for _ in range(pr_review.WINDOW)]
        self.assertFalse(
            pr_review.window_blind(payload([review()], comments=full, older=False), "comments")
        )

    def test_reviews_behind_the_window_report_nothing_rather_than_a_false_answer(self) -> None:
        """No reviewer review in view dates every comment as newer, so each reads as an answer.

        Reporting nothing keeps the wait polling, where a wrong answer ends it outright on a
        pull request whose review landed and simply sits behind a busier review history.
        """
        pr = payload(
            [review(login="ptr727") for _ in range(pr_review.WINDOW)],
            comments=[comment(at=LATE)],
            older_reviews=True,
        )
        self.assertTrue(pr_review.window_blind(pr, "reviews"))
        self.assertIsNone(pr_review.answered_outside_review(pr))

    def test_one_reviewer_review_in_view_is_a_baseline_the_answer_can_be_dated_against(
        self,
    ) -> None:
        """Reviews arrive in creation order too, so a hidden one is older than the one in view."""
        pr = payload(
            [review(at=EARLY, oid=OLD)]
            + [review(login="ptr727") for _ in range(pr_review.WINDOW - 1)],
            comments=[comment(at=LATE)],
            older_reviews=True,
        )
        self.assertFalse(pr_review.window_blind(pr, "reviews"))
        self.assertIsNotNone(pr_review.answered_outside_review(pr))

    def test_one_spent_reviewer_comment_in_view_settles_the_question(self) -> None:
        """Comments arrive in creation order, so a hidden one is older than the spent one in view."""
        full = [comment(at=EARLY)] + [comment(login="ptr727") for _ in range(pr_review.WINDOW - 1)]
        pr = payload([review(at=LATE)], comments=full, older=True)
        self.assertIsNone(pr_review.answered_outside_review(pr))
        self.assertFalse(pr_review.window_blind(pr, "comments"))


class TestReviewEffort(unittest.TestCase):
    """Review effort is observed from completed review metadata and never selected here."""

    def test_inherited_effort_reports_the_effective_level_and_default_source(self) -> None:
        for level in ("Lite", "Balanced", "Max"):
            with self.subTest(level=level):
                pr = payload([review(body=nested(effort=f"Default ({level})"))])
                self.assertEqual((level.lower(), "default"), pr_review.review_effort(pr))

    def test_explicit_effort_reports_the_level_and_explicit_source(self) -> None:
        for level in ("Lite", "Balanced", "Max"):
            with self.subTest(level=level):
                pr = payload([review(body=nested(effort=level))])
                self.assertEqual((level.lower(), "explicit"), pr_review.review_effort(pr))

    def test_absent_effort_metadata_is_unknown(self) -> None:
        self.assertEqual(("unknown", "unknown"), pr_review.review_effort(payload([review()])))

    def test_newest_head_review_does_not_borrow_older_effort_metadata(self) -> None:
        older = review(at=EARLY, body=nested(effort="Balanced"))
        newer = review(at=LATE)
        self.assertEqual(("unknown", "unknown"), pr_review.review_effort(payload([older, newer])))

    def test_the_pending_set_is_read_where_a_bot_reviewer_is_visible(self) -> None:
        """`gh pr view --json reviewRequests` omits a Bot outright and reports an empty set."""
        pending = {
            "reviewRequests": {
                "nodes": [{"requestedReviewer": {"__typename": "Bot", "login": pr_review.REVIEWER}}]
            }
        }
        self.assertTrue(pr_review.reviewer_requested(pending))
        human = {
            "reviewRequests": {
                "nodes": [{"requestedReviewer": {"__typename": "User", "login": "ptr727"}}]
            }
        }
        self.assertFalse(pr_review.reviewer_requested(human))
        self.assertFalse(pr_review.reviewer_requested({"reviewRequests": {"nodes": []}}))
        # A null reviewer is what a deleted account leaves behind, and it must not raise.
        self.assertFalse(
            pr_review.reviewer_requested(
                {"reviewRequests": {"nodes": [{"requestedReviewer": None}]}}
            )
        )


class TestDigest(GqlCase):
    def test_the_summary_line_counts_what_it_names(self) -> None:
        self.answer(
            payload([review(), review(oid=OLD)], [thread("T1"), thread("T2", resolved=True)])
        )
        out, unresolved = pr_review.digest("o", "r", 7)
        self.assertEqual(1, unresolved)
        self.assertIn("pr=7", out)
        self.assertIn(f"head={HEAD[:8]}", out)
        self.assertIn("rounds=2", out)
        self.assertIn("review_on_head=yes", out)
        self.assertIn("threads=2", out)
        self.assertIn("unresolved=1", out)
        self.assertIn("merge=CLEAN", out)

    def test_review_on_head_reports_no_when_every_round_is_stale(self) -> None:
        """`NO` is upper-case on purpose, so the one state that blocks a merge is not skimmed past."""
        self.answer(payload([review(oid=OLD)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("review_on_head=NO", out)

    def test_digest_reports_effective_effort_without_changing_the_verdict(self) -> None:
        self.answer(payload([review(body=nested(effort="Default (Balanced)"))]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("effort=balanced effort_source=default", out)
        self.assertIn("coverage=full", out)

    def test_a_thread_from_a_deleted_account_does_not_crash_the_digest(self) -> None:
        """GraphQL sends `author` present and null, which a defaulted lookup returns as None."""
        orphan = thread("T1")
        orphan["comments"]["nodes"][0]["author"] = None
        self.answer(payload([review()], [orphan, thread("T2")]))
        out, unresolved = pr_review.digest("o", "r", 7)
        self.assertEqual(1, unresolved)
        self.assertIn("T2", out)

    def test_only_the_reviewer_s_own_unresolved_threads_are_listed(self) -> None:
        """A maintainer's own open thread is not a review finding to answer."""
        self.answer(payload([review()], [thread("T1", login="ptr727"), thread("T2")]))
        out, unresolved = pr_review.digest("o", "r", 7)
        self.assertEqual(1, unresolved)
        self.assertIn("T2", out)
        self.assertNotIn("T1", out)

    def test_a_seen_set_marks_each_thread_new_exactly_once(self) -> None:
        """The seen set is what tells a second round's findings from the ones already answered."""
        seen: set[str] = set()
        self.answer(
            payload([review()], [thread("T1")]), payload([review()], [thread("T1"), thread("T2")])
        )
        first, _ = pr_review.digest("o", "r", 7, seen)
        self.assertIn("NEW T1", first)
        self.assertIn("new=1", first)
        second, unresolved = pr_review.digest("o", "r", 7, seen)
        self.assertNotIn("T1", second)
        self.assertIn("NEW T2", second)
        self.assertIn("new=1", second)
        # The count still reports every open thread, not only the newly seen ones.
        self.assertEqual(2, unresolved)

    def test_a_body_is_flattened_and_bounded(self) -> None:
        """A multi-line finding on one digest line keeps the digest a few hundred bytes."""
        self.answer(payload([review()], [thread("T1", body="one\n  two\t\tthree " + "x" * 400)]))
        out, _ = pr_review.digest("o", "r", 7)
        body = out.split("\n")[1]
        self.assertIn("one two three", body)
        self.assertLessEqual(len(body.split("a.py:1 ")[1]), 160)


class TestSuppressed(GqlCase):
    """The findings collapsed in a review body, which reach no thread and so no thread poll."""

    def test_either_documented_heading_counts_and_a_clean_body_does_not(self) -> None:
        """One phrasing alone reports zero on a review that has them, the false clean once more."""
        for label, body, want in (
            ("the current heading", collapsed(), 1),
            ("the earlier heading", collapsed(heading="Suppressed comments (2)"), 2),
            ("a heading with no count", collapsed(heading="Suppressed comments"), 1),
            ("a clean pass", "Reviewed 3 of 3 changed files and generated no comments.", 0),
            ("no body at all", "", 0),
        ):
            with self.subTest(case=label):
                self.answer(payload([review(body=body)]))
                out, _ = pr_review.digest("o", "r", 7)
                self.assertIn(f"suppressed={want}", out)

    def test_a_block_on_a_review_with_no_commit_names_that_rather_than_an_empty_sha(self) -> None:
        """GraphQL returns a null commit for a pending review, and the sha is what traces it.

        Rendered from an empty string it read "raised on , earlier round", which loses the round
        and reads as a formatting glitch rather than as a finding that still needs an answer.
        """
        self.answer(payload([review(body=collapsed()) | {"commit": None}]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("commit unknown, treat as outstanding", out)
        self.assertNotIn("raised on ,", out)
        # It still counts, since an unknown round is not a reason to drop a finding.
        self.assertIn("suppressed=1", out)

    def test_a_block_on_an_earlier_round_is_still_reported(self) -> None:
        """A suppressed finding has no resolved state, so a push must not retire it.

        Head-scoping read "superseded by a push" as "answered", and the two are not the same:
        a finding nobody replied to left the digest the moment the branch moved, and the run
        then reported zero. Four rounds of findings went unanswered across three pull requests
        that way in a single day, each one discovered by the maintainer rather than the gate.

        The round is marked instead, since a finding on an older round may be moot and deciding
        that is the reader's judgment rather than something the count should make for them.
        """
        self.answer(payload([review(oid=OLD, body=collapsed()), review()]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("suppressed=1", out)
        self.assertIn("earlier=1", out)
        self.assertIn("earlier round", out)

    def test_the_finding_prints_whole_under_a_marker_naming_the_answer(self) -> None:
        """A thread can be re-read at its id and truncates for that reason, and this cannot."""
        finding = "a.py:12 " + ("the same clause repeated. " * 20).strip()
        self.answer(payload([review(body=collapsed(finding=finding))]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("no thread to resolve, answer it in the PR conversation", out)
        self.assertIn(finding, out)
        # The `<details>` wrapper is markup around the finding, not part of it.
        self.assertNotIn("<summary>", out)
        self.assertNotIn("<details>", out)

    def test_a_body_naming_the_block_outside_a_details_wrapper_still_reports(self) -> None:
        """Reporting zero because the markup moved is the failure the whole case guards."""
        self.answer(
            payload([review(body=OVERVIEW + "\nSuppressed comments (1)\n\na.py:12 Off by one.")])
        )
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("suppressed=1", out)
        self.assertIn("a.py:12 Off by one.", out)

    def test_the_per_file_summary_block_beside_it_is_not_a_finding(self) -> None:
        """Every real body collapses a file table too, and reporting that is noise, not a finding."""
        body = (
            OVERVIEW + "\n<details>\n<summary>Show a summary per file</summary>\n\n"
            "| File | Description |\n\n</details>\n" + collapsed()
        )
        self.answer(payload([review(body=body)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("suppressed=1", out)
        self.assertNotIn("Show a summary per file", out)
        self.assertIn("The retry count is off by one.", out)

    def test_the_count_is_findings_rather_than_blocks(self) -> None:
        """A body holds one block per round, so counting blocks reports two findings as one."""
        self.answer(
            payload(
                [
                    review(body=collapsed(heading="Suppressed comments (3)")),
                    review(body=collapsed(heading="Suppressed comments (2)")),
                ]
            )
        )
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("suppressed=5", out)

    def test_prose_that_merely_discusses_the_phrase_is_not_a_block(self) -> None:
        """This PR's own review body was the false positive, discussing the phrase and carrying none."""
        body = (
            "## Pull request overview\n\nThis PR reports the suppressed and low confidence "
            "findings that reach no thread (and are therefore invisible to a thread poll).\n\n"
            "<details>\n<summary>Show a summary per file</summary>\n\n| File |\n\n</details>\n"
        )
        self.answer(payload([review(body=body)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("suppressed=0", out)
        self.assertNotIn("SUPPRESSED", out)

    def test_a_heading_nested_inside_the_review_details_wrapper_reports(self) -> None:
        """The shape that reported `suppressed=0` over a body carrying two findings.

        The reviewer moved the section inside the `Review details` wrapper as a Markdown heading,
        so the wrapper's summary reads `Review details` and matches nothing, and the fallback that
        exists for a moved wrapper scans the body with every wrapper deleted, which deletes the
        region the heading now sits in. The two misses compound into a clean round over findings
        no thread will ever carry, which is the one failure this whole digest exists to prevent.
        """
        self.answer(payload([review(body=nested())]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("suppressed=2", out)
        self.assertIn("The retry count is off by one.", out)

    def test_the_nested_count_is_the_heading_s_own_rather_than_the_wrapper_s(self) -> None:
        """The wrapper's summary carries no count, so reading it floors two findings to one."""
        self.answer(payload([review(body=nested(heading="### Suppressed comments (3)"))]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("suppressed=3", out)
        # The block starts at its own heading, so the wrapper's summary is not the finding's header.
        self.assertNotIn("Review details", out)

    def test_both_the_wrapper_shape_and_the_nested_shape_report_in_one_run(self) -> None:
        """Both appear across the rounds of a single pull request, so neither replaces the other.

        Retargeting the parse from the old shape to the new one would report the same false clean
        one round later, on whichever shape the reviewer happened not to emit that time.
        """
        self.answer(
            payload(
                [
                    review(oid=OLD, body=collapsed(heading="Suppressed comments (2)")),
                    review(body=nested()),
                ]
            )
        )
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("suppressed=4", out)
        self.assertIn("(on_head=2 earlier=2)", out)

    def test_the_file_summary_wrapper_beside_a_nested_section_is_still_not_a_finding(self) -> None:
        """A body carries several wrappers, and scanning them all must not read the table as one."""
        self.answer(payload([review(body=nested())]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertNotIn("File summaries", out)
        self.assertNotIn("| File | Description |", out)

    def test_a_human_review_carrying_the_phrase_is_not_a_copilot_finding(self) -> None:
        self.answer(payload([review(login="ptr727", body=collapsed()), review()]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("suppressed=0", out)


class TestRefusal(GqlCase):
    """The review that says it did not review, which carries the head and covers nothing.

    It is a formal review with the correct commit and no threads, so every check a clean pass
    satisfies it satisfies too, and the digest it renders is the clean pass byte for byte. The
    pull request it was observed on had 301 changed files, one over the reviewer's limit, and was
    one command from merging on a round that never ran.
    """

    def test_a_refusal_on_the_head_is_not_coverage(self) -> None:
        self.answer(payload([review(body=REFUSED)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("review_on_head=NO", out)
        self.assertIn("refusal=YES", out)
        # It happened, so it is still a round.
        # What it is not is a review of anything.
        self.assertIn("rounds=1", out)
        self.assertFalse(pr_review.reviewed_head(payload([review(body=REFUSED)])))

    def test_the_body_prints_whole_under_a_marker_naming_the_remedy(self) -> None:
        """Its wording is what separates a file-count refusal from a quota one, so it is not cut."""
        self.answer(payload([review(body=REFUSED)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("COPILOT REFUSED THIS ROUND", out)
        self.assertIn(REFUSED, out)

    def test_each_documented_phrasing_counts_including_the_typographic_apostrophe(self) -> None:
        """One phrasing alone is one rewording away from reporting a refusal as a review."""
        # The typographic apostrophe is an escape, since the charset rule governs this file too.
        # The case is about the byte the reviewer sends rather than the character on screen.
        for body in (
            "Copilot wasn't able to review this pull request because it is too large.",
            "Copilot wasn\u2019t able to review this pull request.",
            "Copilot was not able to review this pull request.",
            "Copilot is unable to review this pull request right now.",
        ):
            with self.subTest(body=body):
                self.assertEqual(body, pr_review.refusal_of({"body": body}))

    def test_a_clean_pass_and_an_ordinary_review_are_not_refusals(self) -> None:
        for body in (
            "Reviewed 3 of 3 changed files and generated no comments.",
            collapsed(),
            nested(),
            "",
        ):
            with self.subTest(body=body[:40]):
                self.assertEqual("", pr_review.refusal_of({"body": body}))

    def test_a_review_quoting_the_wording_below_its_overview_is_not_a_refusal(self) -> None:
        """This pull request's own review body is that quotation, and the shape has bitten once.

        The suppressed matcher read the whole body and reported the review that discussed
        suppressed findings as carrying them. The opening is the unit for that reason: a refusal
        is the whole body, so a match further down is a review describing the wording.

        This is also what fixes the unit at one line. The overview prose is the second line of
        every review body, and reading two lines reports this review as a refusal of itself.
        """
        body = (
            "## Pull request overview\n\nThis PR treats a review that says Copilot "
            "wasn't able to review a pull request as a terminal state rather than "
            "as coverage.\n\n- The digest now reports `unable to review` separately.\n"
        )
        self.assertEqual("", pr_review.refusal_of({"body": body}))
        self.answer(payload([review(body=body)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("review_on_head=yes", out)
        self.assertIn("refusal=no", out)

    def test_a_refusal_from_an_earlier_round_is_spent(self) -> None:
        """A refusal is a statement about one commit, so the push that changed it retires it."""
        self.answer(payload([review(oid=OLD, body=REFUSED), review()]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("review_on_head=yes", out)
        self.assertIn("refusal=no", out)

    def test_a_genuine_review_of_the_same_head_outranks_a_refusal_of_it(self) -> None:
        """Coverage that landed is coverage, whatever an earlier round of the same head said.

        The digest has to agree with the exit codes here, which return 0 on that coverage and
        never reach 41. Reported unconditionally the field read `review_on_head=yes refusal=YES`
        on one line, which tells a reader to split a pull request that has just been reviewed,
        and tells an automated one that a state it treats as actionable is outstanding.
        """
        pr = payload([review(body=REFUSED, at=EARLY), review(at=LATE)])
        self.assertTrue(pr_review.reviewed_head(pr))
        self.answer(pr)
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("review_on_head=yes", out)
        self.assertIn("refusal=no", out)
        self.assertNotIn("COPILOT REFUSED THIS ROUND", out)
        self.assertNotIn(REFUSED, out)

    def test_the_wait_returns_zero_where_coverage_followed_a_refusal_of_the_same_head(self) -> None:
        """The digest and the exit code read one payload, so neither may spend it differently."""
        self.answer(payload([review(body=REFUSED, at=EARLY), review(at=LATE)]))
        with (
            contextlib.redirect_stdout(io.StringIO()) as out,
            mock.patch.object(pr_review.time, "sleep"),
        ):
            self.assertEqual(0, pr_review.main(["wait", "7", "--repo", "o/r"]))
        self.assertNotIn("status=REVIEW_IS_A_REFUSAL", out.getvalue())

    def test_a_human_review_carrying_the_wording_is_not_the_reviewer_refusing(self) -> None:
        self.answer(payload([review(login="ptr727", body=REFUSED), review(oid=OLD)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("refusal=no", out)


class TestCoverage(GqlCase):
    """The round that covered the head and read part of the diff, which is a clean pass elsewhere.

    Measured over 332 Copilot review bodies on this repository: five rounds across three pull
    requests reported reading fewer files than the pull request changed, and all three merged.
    One of them changed three files, left one unread across both its rounds, and reported
    "generated no comments" each time.
    """

    def digest_for(self, *reviews: dict) -> str:
        self.answer(payload(list(reviews)))
        out, _ = pr_review.digest("o", "r", 7)
        return out

    def test_each_vetted_spelling_of_a_full_round_reads_as_full(self) -> None:
        """The census: four tails on the first spelling, and the `Review details` bullet."""
        for covers in (
            (
                "Copilot reviewed 7 out of 7 changed files in this pull request and generated "
                "no new comments."
            ),
            (
                "Copilot reviewed 7 out of 7 changed files in this pull request and generated "
                "no comments."
            ),
            (
                "Copilot reviewed 7 out of 7 changed files in this pull request and generated "
                "1 comment."
            ),
            (
                "Copilot reviewed 7 out of 7 changed files in this pull request and generated "
                "4 comments."
            ),
            "- **Files reviewed:** 7/7 changed files",
        ):
            with self.subTest(covers=covers[:44]):
                self.assertEqual((pr_review.FULL, covers), pr_review.coverage_of({"body": covers}))

    def test_a_round_that_read_part_of_the_diff_is_a_failure_the_digest_names(self) -> None:
        """PR 592's shape: three changed files, one never read, and it merged."""
        line = (
            "Copilot reviewed 2 out of 3 changed files in this pull request and generated "
            "no comments."
        )
        out = self.digest_for(review(body=OVERVIEW + "\n" + line))
        self.assertIn("coverage=PARTIAL", out)
        self.assertIn("COVERAGE IS PARTIAL", out)
        # The counts print, since they are what say how much went unread and no thread carries it.
        self.assertIn(line, out)
        # The round did cover the head.
        # That reading was correct, and it was not the whole reading.
        self.assertIn("review_on_head=yes", out)

    def test_the_second_spelling_reports_a_partial_round_too(self) -> None:
        """Both spellings appear in the corpus to this day, so neither replaces the other."""
        out = self.digest_for(review(body=nested(covers="- **Files reviewed:** 2/3 changed files")))
        self.assertIn("coverage=PARTIAL", out)

    def test_the_bullet_spelling_is_a_coverage_line_on_its_label_alone(self) -> None:
        """Its label is the marker, so dropping the words after the counts is not dropping it.

        Requiring them read a bullet that had lost them as no statement at all, which is a
        silent `unstated` over a round that stated its coverage plainly.
        """
        # Detected and read, which are two assertions rather than one.
        # Asserting only the first left the bare bullet blocking on a line it could read.
        for line in (
            "- **Files reviewed:** 4/4",
            "- **Files reviewed:** 4/4 changed file",
            "- **Files reviewed:** 4/4 changed files",
        ):
            with self.subTest(line=line):
                self.assertEqual([line], pr_review.coverage_statements(line))
                self.assertEqual((4, 4), pr_review.read_coverage(line))
                self.assertEqual(pr_review.FULL, pr_review.coverage_of({"body": line})[0])

    def test_a_bullet_carrying_no_counts_still_blocks(self) -> None:
        """What is left after the words are optional is a line that states no coverage at all."""
        self.assertIsNone(pr_review.read_coverage("- **Files reviewed:** all of them"))
        self.assertEqual(
            pr_review.UNVETTED,
            pr_review.coverage_of({"body": "- **Files reviewed:** all of them"})[0],
        )

    def test_the_reviewer_s_name_alone_is_not_a_coverage_line(self) -> None:
        """The other opener keeps its text requirement, prose opening lines with that name too."""
        self.assertFalse(pr_review.is_coverage_line("Copilot answers a request with a comment."))

    def test_a_singular_changed_file_reads_rather_than_blocks(self) -> None:
        """A one-file round means what it says, and stopping the fleet over an -s is crying wolf."""
        self.assertEqual(
            (1, 1),
            pr_review.read_coverage(
                "Copilot reviewed 1 out of 1 changed file in this pull request and generated "
                "no comments."
            ),
        )

    def test_counts_that_cannot_both_be_true_block_rather_than_read_as_full(self) -> None:
        """A round claiming it read more files than were changed is one this is parsing wrongly.

        Read as full coverage it fails open on the very statement saying something is off, which
        is the shape of every other failure here.
        """
        line = (
            "Copilot reviewed 8 out of 7 changed files in this pull request and generated "
            "no comments."
        )
        self.assertIsNone(pr_review.read_coverage(line))
        self.assertEqual(pr_review.UNVETTED, pr_review.coverage_of({"body": line})[0])
        self.assertIn(f"coverage line: {line}", pr_review.unrecognized_in(OVERVIEW + "\n" + line))

    def test_a_coverage_line_that_parses_to_nothing_names_this_script(self) -> None:
        """The wording has drifted once for each of the two patterns beside this one.

        Passing a shape it does not recognize is how a gate stops gating as the wording moves,
        so the unrecognized shape is reported and its remedy is stated as fixing this script.
        """
        line = "Copilot reviewed most of the changed files in this pull request."
        out = self.digest_for(review(body=OVERVIEW + "\n" + line))
        self.assertIn("coverage=UNVETTED", out)
        # It is one of the unrecognized shapes rather than a report of its own.
        # Both say the reader needs fixing, and one of them saying it once is the whole message.
        self.assertIn("UNRECOGNIZED REVIEWER OUTPUT (1)", out)
        self.assertIn(f"coverage line: {line}", out)

    def test_a_round_stating_no_coverage_at_all_is_unstated(self) -> None:
        """28 of the 332 bodies are an overview and a change list, and that shape is current.

        It is a recognized shape rather than unvetted reviewer output, but it cannot prove that
        the reviewer covered the full diff and therefore blocks the status gate.
        """
        body = (
            "## Pull request overview\n\nThis PR updates the backlog.\n\n"
            "**Changes:**\n- Delete the shipped cluster from `TODO.md`.\n"
        )
        out = self.digest_for(review(body=body))
        self.assertIn("coverage=unstated", out)
        self.assertNotIn("COVERAGE IS PARTIAL", out)
        self.assertIn("shapes=ok", out)

    def test_machine_readable_marker_reports_full_coverage(self) -> None:
        """The instructed marker is stable input while older prose remains supported."""
        marker = "<!-- fleet-review: reviewed=3 changed=3 findings=0 -->"
        self.assertEqual((3, 3), pr_review.read_coverage(marker))
        self.assertEqual(pr_review.FULL, pr_review.coverage_of({"body": marker})[0])

    def test_machine_readable_marker_reports_partial_coverage(self) -> None:
        """The marker's measured counts use the same fail-closed comparison as prose."""
        marker = "<!-- fleet-review: reviewed=2 changed=3 findings=1 -->"
        self.assertEqual((2, 3), pr_review.read_coverage(marker))
        self.assertEqual(pr_review.PARTIAL, pr_review.coverage_of({"body": marker})[0])

    def test_a_refusal_is_exempt_rather_than_an_unrecognized_shape(self) -> None:
        """It states no coverage by design, and `head_reviews` has already dropped it.

        Read as a round, every refusal becomes a spurious unvetted-shape failure sitting on top
        of the `refusal=YES` that already names the state and its remedy.
        """
        out = self.digest_for(review(body=REFUSED))
        self.assertIn("refusal=YES", out)
        self.assertIn("coverage=unstated", out)
        self.assertIn("shapes=ok", out)

    def test_coverage_is_read_from_the_head_rather_than_from_a_superseded_round(self) -> None:
        """A partial round describes one commit's diff, and the push that changes it is answered
        by a round reading the whole of the new one."""
        old = (
            OVERVIEW + "\nCopilot reviewed 2 out of 3 changed files in this pull request "
            "and generated no comments."
        )
        out = self.digest_for(review(oid=OLD, body=old), review(body=OVERVIEW + "\n" + COVERED))
        self.assertIn("coverage=full", out)
        self.assertNotIn("COVERAGE IS PARTIAL", out)

    def test_the_worst_of_two_rounds_on_one_head_is_what_reports(self) -> None:
        """A head carries two rounds through a re-request, and both read the same diff."""
        partial = (
            "Copilot reviewed 2 out of 3 changed files in this pull request and "
            "generated no comments."
        )
        self.assertEqual(
            pr_review.PARTIAL,
            pr_review.head_coverage(
                payload([review(body=COVERED, at=EARLY), review(body=partial, at=LATE)])
            )[0],
        )

    def test_a_round_that_states_full_coverage_settles_it_over_one_stating_none(self) -> None:
        """Unstated is the absence of a statement rather than a bad one, so it loses to a count."""
        self.assertEqual(
            pr_review.FULL,
            pr_review.head_coverage(
                payload([review(body="## Overview\n\nNarrow."), review(body=COVERED)])
            )[0],
        )

    def test_prose_mentioning_changed_files_is_not_this_round_stating_its_coverage(self) -> None:
        """The false positive the suppressed matcher and the refusal matcher have each had once.

        A review of the pull request that adds this check discusses the wording it adds, and a
        body-wide match reads that discussion as the round's own count.
        """
        body = (
            "## Pull request overview\n\nThis PR reads the line saying Copilot reviewed 2 "
            "out of 3 changed files, so a partial round stops reporting as a clean pass.\n\n"
            "- The digest now carries a `coverage` field.\n"
        )
        out = self.digest_for(review(body=body))
        self.assertIn("coverage=unstated", out)

    def test_a_quoted_line_in_a_fenced_block_is_not_this_round_stating_its_coverage(self) -> None:
        """131 of the 332 bodies carry a fence, and this change puts both spellings in the diff."""
        body = (
            "### Ready to approve\n\nThe vetted spellings read:\n\n```\n"
            "Copilot reviewed 2 out of 3 changed files in this pull request and generated "
            "no comments.\n- **Files reviewed:** 4/9 changed files\n```\n\n" + COVERED + "\n"
        )
        body = "### Ready to approve\n\n" + body
        out = self.digest_for(review(body=body))
        self.assertIn("coverage=full", out)
        self.assertNotIn("COVERAGE IS PARTIAL", out)

    def test_a_human_review_carrying_a_coverage_line_is_not_the_reviewer_s_round(self) -> None:
        partial = (
            "Copilot reviewed 2 out of 3 changed files in this pull request and "
            "generated no comments."
        )
        out = self.digest_for(
            review(login="ptr727", body=partial), review(body=OVERVIEW + "\n" + COVERED)
        )
        self.assertIn("coverage=full", out)


class TestUnrecognizedShapes(GqlCase):
    """A shape this script has no reader for blocks, rather than being read past.

    Every reader here keys on a structural marker, so a marker that changes spelling is a section
    the reader stops finding and reports as absent. All three failures on record have that shape:
    a suppressed heading reworded, a suppressed section moved inside another wrapper, and a
    coverage line nothing parsed. Each was caught after it had already reported a clean pass.

    The inventory is measured rather than imagined. Over the same 333 review bodies, with fenced
    blocks removed and text reduced to ASCII, the whole corpus is 8 headings, 6 summaries and 3
    metadata labels, and every body carries at least one of them.
    """

    def digest_for(self, *reviews: dict) -> str:
        self.answer(payload(list(reviews)))
        out, _ = pr_review.digest("o", "r", 7)
        return out

    def test_every_vetted_marker_together_reads_as_recognized(self) -> None:
        """The corpus shape in one body, so the lists are held against what they were built from."""
        self.assertEqual([], pr_review.unrecognized_in(nested()))
        self.assertEqual([], pr_review.unrecognized_in(collapsed()))
        self.assertEqual([], pr_review.unrecognized_in(OVERVIEW))

    def test_a_heading_that_is_not_in_the_inventory_blocks(self) -> None:
        """A renamed section is one the reader stops finding, which it reports as nothing there."""
        out = self.digest_for(review(body=OVERVIEW + "\n### Confidence assessment\n\nHigh.\n"))
        self.assertIn("shapes=UNRECOGNIZED", out)
        self.assertIn("heading: ### Confidence assessment", out)

    def test_approval_recommended_heading_is_vetted(self) -> None:
        """The no-findings verdict introduced by the current Copilot review stays readable."""
        body = "### \U0001f7e2 Approval recommended\n\nDocumentation updates are consistent.\n"
        self.assertEqual([], pr_review.unrecognized_in(body))

    def test_a_details_summary_that_is_not_in_the_inventory_blocks(self) -> None:
        """The suppressed section has already moved between wrappers once."""
        body = OVERVIEW + "\n<details>\n<summary>Withheld findings</summary>\n\nx\n</details>\n"
        out = self.digest_for(review(body=body))
        self.assertIn("summary: Withheld findings", out)

    def test_a_metadata_label_that_is_not_in_the_inventory_blocks(self) -> None:
        """The coverage line arrived as one of these bullets, so the next reading may too."""
        out = self.digest_for(review(body=nested() + "\n- **Confidence:** high\n"))
        self.assertIn("metadata label: Confidence", out)

    def test_a_body_carrying_no_heading_at_all_blocks(self) -> None:
        """Every measured body opens on a heading, so one with none is a format never seen.

        This is the arm that catches a rewrite wholesale rather than marker by marker, and it is
        also what catches the refusal wording drifting, since a refusal stops being exempt.
        """
        self.assertIn(
            "body carrying no heading at all",
            " ".join(pr_review.unrecognized_in("Looks good to me.")),
        )
        self.assertIn("body carrying no heading at all", " ".join(pr_review.unrecognized_in("")))

    def test_a_refusal_is_exempt_because_it_is_already_a_vetted_shape(self) -> None:
        """It is a bare paragraph by design, and `refusal=YES` already names its remedy."""
        self.assertEqual([], pr_review.unrecognized_in(REFUSED))
        out = self.digest_for(review(body=REFUSED))
        self.assertIn("shapes=ok", out)
        self.assertIn("refusal=YES", out)

    def test_a_refusal_whose_wording_drifted_stops_being_exempt_and_blocks(self) -> None:
        """The exemption is the pattern, so losing the pattern loses the exemption, not the check.

        This is the failure the refusal check was built for arriving one rewording later: the
        body would read as an ordinary review carrying the head and raising nothing.
        """
        drifted = "Copilot has declined to review this pull request because it is too large."
        self.assertEqual("", pr_review.refusal_of({"body": drifted}))
        out = self.digest_for(review(body=drifted))
        self.assertIn("shapes=UNRECOGNIZED", out)
        self.assertIn("refusal=no", out)

    def test_the_emoji_and_the_finding_count_are_normalized_rather_than_vetted(self) -> None:
        """Both change without the section changing, so comparing them raw blocks every review."""
        for heading in (
            "### \U0001f7e2 Ready to approve",
            "### \U0001f7e1 Changes recommended",
            "### Suppressed comments (4)",
            "### Suppressed comments (11)",
        ):
            with self.subTest(heading=heading):
                self.assertEqual([], pr_review.unrecognized_in(f"{heading}\n\nText.\n"))

    def test_a_marker_quoted_in_a_fenced_block_is_not_one_the_review_carries(self) -> None:
        """This change publishes the inventory, so a review of it quotes the lot back."""
        body = (
            OVERVIEW + "\nThe vetted headings are:\n\n```\n### Confidence assessment\n"
            "- **Confidence:** high\n```\n"
        )
        self.assertEqual([], pr_review.unrecognized_in(body))

    def test_a_reviewer_login_that_is_not_the_one_every_query_filters_on_blocks(self) -> None:
        """A rename leaves every filter here matching nothing, which reads as no review at all.

        That is the quietest drift of the lot: the digest reports `rounds=0 review_on_head=NO`
        over a review that landed, and a wait polls out its whole timeout against it.
        """
        pr = payload([review(login="copilot-code-review-agent")])
        found = " ".join(pr_review.unrecognized_shapes(pr))
        self.assertIn("reviewer login: copilot-code-review-agent", found)

    def test_the_coding_agent_and_a_human_are_not_the_reviewer_renamed(self) -> None:
        """`copilot-swe-agent` edits code and is not this reviewer under another name."""
        for login in ("copilot-swe-agent", "ptr727", "codecov[bot]", "dependabot[bot]"):
            with self.subTest(login=login):
                self.assertEqual([], pr_review.unrecognized_shapes(payload([review(login=login)])))

    def test_the_block_names_the_hub_and_leaves_the_merge_to_the_maintainer(self) -> None:
        """The remedy is an issue where the reader lives, and the merge is not this script's call."""
        out = self.digest_for(review(body=OVERVIEW + "\n### Confidence assessment\n"))
        self.assertIn("UNRECOGNIZED REVIEWER OUTPUT (1)", out)
        self.assertIn(f"File an issue on {pr_review.HUB}", out)
        self.assertIn("maintainer's call rather than the agent's", out)

    def test_every_round_is_read_rather_than_the_head_s(self) -> None:
        """This asks whether the reader still understands the reviewer, which is not head-scoped.

        A shape that arrived one round ago is one every later round carries, so waiting for it to
        reach the head is waiting through the rounds it is already misreading.
        """
        out = self.digest_for(
            review(oid=OLD, body=OVERVIEW + "\n### Confidence assessment\n"), review(body=OVERVIEW)
        )
        self.assertIn("shapes=UNRECOGNIZED", out)
        self.assertIn(f"(round {OLD[:8]})", out)


class TestCoverageExitCodes(GqlCase):
    """Coverage that is partial or unstated blocks a covered-head verdict."""

    def setUp(self) -> None:
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def partial(self) -> dict:
        return review(
            body=OVERVIEW + "\nCopilot reviewed 2 out of 3 changed files in this "
            "pull request and generated no comments."
        )

    def test_status_exits_forty_two_on_a_round_that_read_part_of_the_diff(self) -> None:
        self.answer(payload([self.partial()]))
        self.assertEqual(42, pr_review.main(["status", "7", "--repo", "o/r"]))
        self.assertIn("status=COVERAGE_IS_PARTIAL", self.out.getvalue())

    def test_status_exits_forty_five_when_coverage_is_unstated(self) -> None:
        """A current-head review is not proof that the reviewer read the full diff."""
        self.answer(payload([review(body=OVERVIEW)]))
        self.assertEqual(45, pr_review.main(["status", "7", "--repo", "o/r"]))
        self.assertIn("status=COVERAGE_IS_UNSTATED", self.out.getvalue())

    def test_status_has_no_coverage_verdict_before_a_review_lands(self) -> None:
        """A missing round is incomplete work, not an unstated statement by a reviewer."""
        self.answer(payload([]))
        self.assertEqual(0, pr_review.main(["status", "7", "--repo", "o/r"]))
        self.assertNotIn("status=COVERAGE_IS_UNSTATED", self.out.getvalue())

    def test_the_partial_message_counts_the_unread_files_rather_than_assuming_one(self) -> None:
        """Every partial on record skipped exactly one file, which is a measurement of seven
        rounds rather than a property the state carries, so the line reads the run it prints."""
        for reviewed, changed, phrase in (
            (2, 3, "1 of the 3 changed files has"),
            (5, 9, "4 of the 9 changed files have"),
        ):
            with self.subTest(reviewed=reviewed, changed=changed):
                out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
                self.answer(
                    payload(
                        [
                            review(
                                body=OVERVIEW
                                + f"\nCopilot reviewed {reviewed} out of {changed} changed "
                                f"files in this pull request and generated no comments."
                            )
                        ]
                    )
                )
                self.assertEqual(42, pr_review.main(["status", "7", "--repo", "o/r"]))
                self.assertIn(phrase, out.getvalue())

    def test_the_partial_message_still_blocks_where_the_counts_cannot_be_re_read(self) -> None:
        """Unreachable by construction, since PARTIAL is set only where that line parsed, so the
        counts are withheld from `report_verdict` directly. A crash on the blocking path would
        read to a caller as this script being broken rather than as a round that read part."""
        pr = payload([self.partial()])
        unparseable = "a coverage line carrying no counts at all"
        with mock.patch.object(
            pr_review, "head_coverage", return_value=(pr_review.PARTIAL, unparseable)
        ):
            self.assertEqual(42, pr_review.report_verdict(pr))
        out = self.out.getvalue()
        self.assertIn("status=COVERAGE_IS_PARTIAL", out)
        self.assertIn("could not be re-read", out)
        self.assertNotIn("changed files", out)

    def test_status_exits_forty_three_on_a_wording_it_does_not_read(self) -> None:
        self.answer(
            payload([review(body=OVERVIEW + "\nCopilot reviewed some of the changed files.")])
        )
        self.assertEqual(43, pr_review.main(["status", "7", "--repo", "o/r"]))
        out = self.out.getvalue()
        self.assertIn("status=UNRECOGNIZED_REVIEWER_OUTPUT", out)
        self.assertIn("coverage=UNVETTED", out)

    def test_status_still_exits_zero_on_a_full_round(self) -> None:
        """Both the legacy coverage prose and the stable marker close the coverage gate."""
        marker = OVERVIEW + "\n<!-- fleet-review: reviewed=1 changed=1 findings=0 -->"
        for body in (OVERVIEW + "\n" + COVERED, nested(), marker):
            with self.subTest(body=body[:40]):
                self.answer(payload([review(body=body)]))
                self.assertEqual(0, pr_review.main(["status", "7", "--repo", "o/r"]))

    def test_wait_carries_the_same_codes_rather_than_ending_on_a_partial_round(self) -> None:
        """`reviewed_head` is what decided its zero, and coverage of the head is not coverage
        of the diff."""
        self.answer(payload([self.partial()]))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(42, pr_review.main(["wait", "7", "--repo", "o/r"]))
        self.assertIn("coverage=PARTIAL", self.out.getvalue())

    def test_status_and_wait_both_exit_forty_three_on_an_unrecognized_shape(self) -> None:
        """Neither may report a clean pass over output the reader does not understand."""
        self.answer(payload([review(body=OVERVIEW + "\n### Confidence assessment\n")]))
        self.assertEqual(43, pr_review.main(["status", "7", "--repo", "o/r"]))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(43, pr_review.main(["wait", "7", "--repo", "o/r"]))
        self.assertIn("status=UNRECOGNIZED_REVIEWER_OUTPUT", self.out.getvalue())

    def test_an_unrecognized_shape_outranks_the_partial_coverage_code(self) -> None:
        """A reader that does not understand the output cannot be believed about the diff either."""
        body = (
            OVERVIEW + "\n### Confidence assessment\n\nCopilot reviewed 2 out of 3 changed "
            "files in this pull request and generated no comments.\n"
        )
        self.answer(payload([review(body=body)]))
        self.assertEqual(43, pr_review.main(["status", "7", "--repo", "o/r"]))

    def test_a_drifted_login_reaches_the_code_rather_than_timing_out_as_pending(self) -> None:
        """The gate could not fire for the one drift it was written to catch.

        `reviewed_head` filters on the login, so a renamed reviewer leaves it false, and gating
        the verdict behind it meant the login check never reached an exit code. The digest
        printed `shapes=UNRECOGNIZED` and the wait returned 30, which is the digest disagreeing
        with the code, and an automated reader settles that by believing the code.
        """
        pr = payload([review(login="copilot-code-review-agent")])
        self.assertFalse(pr_review.reviewed_head(pr))
        self.answer(pr)
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(43, pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "600"]))
        # Terminal, so it stops rather than polling out the timeout against a landed review.
        slept.assert_not_called()
        out = self.out.getvalue()
        self.assertIn("status=UNRECOGNIZED_REVIEWER_OUTPUT", out)
        self.assertIn("shapes=UNRECOGNIZED", out)

    def test_a_shape_on_a_round_that_covers_no_head_still_reaches_the_code(self) -> None:
        """The same gap with the body reader rather than the login, since both sit behind it."""
        self.answer(payload([review(oid=OLD, body=OVERVIEW + "\n### Confidence assessment\n")]))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(43, pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"]))

    def test_a_pending_round_with_nothing_unrecognized_still_reports_pending(self) -> None:
        """The gate is not allowed to swallow the ordinary wait, which is most of them."""
        self.answer(payload([review(oid=OLD)]))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"]))

    def test_a_refusal_still_exits_forty_one_rather_than_on_a_coverage_code(self) -> None:
        """The refusal names the round that declined, where a coverage code names a round."""
        self.answer(payload([review(body=REFUSED)]))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(41, pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"]))


class TestTheRoundsOwnFileTable(GqlCase):
    """The file table a partial round carries, read against the diff it claims to describe.

    The reading reports and decides nothing, because the measurement says it cannot decide
    anything. Over 348 review bodies here and 121 on ptr727/Blog, the table names the whole
    changed set on partial and fully covered rounds alike, and every case below is one of the
    shapes that corpus carries rather than one invented for the reader.
    """

    PART = (
        "Copilot reviewed 2 out of 3 changed files in this pull request and generated no comments."
    )

    def reading(self, body: str, files: list[str], more: bool = False) -> str:
        counts = pr_review.read_coverage(self.PART) if self.PART in body else None
        return pr_review.table_against_diff(
            payload([review(body=body)], files=files, more_files=more), counts
        )

    def test_the_table_reads_as_its_paths_rather_than_as_its_rows(self) -> None:
        """The header and the alignment row are punctuation, and the second cell is prose."""
        self.assertEqual(
            ["a.py", "docs/b.md"], pr_review.file_table(summarized(["a.py", "docs/b.md"]))
        )

    def test_a_quoted_table_is_not_this_rounds_own(self) -> None:
        """The reason a quoted coverage line is not: this change puts a table in the diff.

        A review of it quotes one, and a quoted table read as the round's own names files
        nobody reviewed.
        """
        quoted = f"{OVERVIEW}\n```\n| File | Description |\n| ---- | ---- |\n| a.py | Prose. |\n```"
        self.assertEqual([], pr_review.file_table(quoted))

    def test_a_table_naming_every_changed_file_corroborates_nothing(self) -> None:
        """The reporter's case, and the one the measurement answers.

        All seven partial rounds on ptr727/Blog name every changed file, as do #476 and #592
        here. So does a round stating full coverage, which is why a full table cannot separate a
        miscount from a file that went unread.
        """
        body = summarized(["a.py", "b.md", "c.yml"], covers=self.PART)
        self.assertIn("corroborates nothing", self.reading(body, ["a.py", "b.md", "c.yml"]))

    def test_a_table_short_by_what_the_counts_leave_unread_names_the_file(self) -> None:
        """#479 states 16 of 17 and names 16, omitting `GOVERNANCE.md`.

        It is the only round on record whose table locates the unread file, and it is reported
        as a lead rather than as a verdict, the table being prose the reviewer writes.
        """
        out = self.reading(
            summarized(["a.py", "b.md"], covers=self.PART), ["a.py", "b.md", "c.yml"]
        )
        self.assertIn("omits exactly the 1 file", out)
        self.assertIn("c.yml", out)
        self.assertIn("lead to check rather than a verdict", out)

    def test_a_path_the_diff_does_not_carry_disqualifies_the_naming(self) -> None:
        """#606 names `GOVENANCE.md`, which no diff here carries.

        A misspelled path drops the real file into the omissions, where the arm above would read
        it as the one nobody reviewed, so a table naming anything outside the diff names nothing.
        """
        out = self.reading(
            summarized(["a.py", "b.mb"], covers=self.PART), ["a.py", "b.md", "c.yml"]
        )
        self.assertIn("b.mb", out)
        self.assertIn("names no unread file", out)
        self.assertNotIn("omits exactly", out)

    def test_a_table_short_by_more_than_the_counts_tracks_neither(self) -> None:
        """#609 states 61 of 62 and names 50, so the shortfalls disagree by eleven files."""
        out = self.reading(summarized(["a.py"], covers=self.PART), ["a.py", "b.md", "c.yml"])
        self.assertIn("names 1 of the 3 changed files", out)
        self.assertIn("names no unread file", out)

    def test_a_changed_file_list_the_window_cut_short_is_not_compared(self) -> None:
        """A path outside the window reads exactly like a path the reviewer left out.

        The record holds a pull request of 301 changed files, so this is reachable rather than
        theoretical, and reading it would name every file past the window as unreviewed.
        """
        out = self.reading(summarized(["a.py"], covers=self.PART), ["a.py"], more=True)
        self.assertIn("longer than the window this reads", out)
        self.assertNotIn("corroborates nothing", out)

    def test_a_head_carrying_no_table_at_all_says_so_rather_than_staying_silent(self) -> None:
        """A silent field reads as a comparison that ran and found nothing to report."""
        self.assertIn(
            "no round covering this head carries a file table",
            self.reading(OVERVIEW + "\n" + self.PART, ["a.py", "b.md", "c.yml"]),
        )

    def test_the_table_is_read_from_any_round_on_the_head_rather_than_the_deciding_one(
        self,
    ) -> None:
        """A re-request restates the counts and carries no table, and #474 is that pair.

        Thirteen commits here carry more than one round, and on one of them a round with a table
        sits beside a round without, so which of the two the verdict reads must not decide
        whether a table is found. Both reviewed the same commit, so both describe the same diff.
        """
        again = (
            "Copilot reviewed 2 out of 3 changed files in this pull request and generated "
            "no new comments."
        )
        pr = payload(
            [
                review(body=summarized(["a.py", "b.md", "c.yml"], covers=self.PART), at=EARLY),
                review(body=OVERVIEW + "\n" + again, at=LATE),
            ],
            files=["a.py", "b.md", "c.yml"],
        )
        self.assertIn(
            "corroborates nothing", pr_review.table_against_diff(pr, pr_review.read_coverage(again))
        )

    def test_a_table_from_before_a_push_describes_another_diff_and_is_not_read(self) -> None:
        """The tighter half, and the shape three of this repository's four partials carry.

        The round naming files sits before a push and describes the diff that push replaced, so
        comparing it against the current changed files names a file as unreviewed on a stale
        list. Reporting no table is the honest answer where the only table is that one.
        """
        again = (
            "Copilot reviewed 2 out of 3 changed files in this pull request and generated "
            "no new comments."
        )
        pr = payload(
            [
                review(oid=OLD, body=summarized(["a.py"], covers=self.PART)),
                review(body=OVERVIEW + "\n" + again),
            ],
            files=["a.py", "b.md", "c.yml"],
        )
        self.assertIn(
            "no round covering this head carries a file table",
            pr_review.table_against_diff(pr, pr_review.read_coverage(again)),
        )

    def test_the_table_moves_no_exit_code_and_prints_under_the_partial_marker(self) -> None:
        """The counts decide the state, and the table is what the maintainer decides beside it."""
        self.answer(
            payload(
                [review(body=summarized(["a.py", "b.md", "c.yml"], covers=self.PART))],
                files=["a.py", "b.md", "c.yml"],
            )
        )
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(42, pr_review.main(["status", "7", "--repo", "o/r"]))
        printed = out.getvalue()
        self.assertIn("COVERAGE IS PARTIAL", printed)
        self.assertIn("corroborates nothing", printed)
        self.assertIn("status=COVERAGE_IS_PARTIAL", printed)


class TestDigestReportsTheAnswer(GqlCase):
    def test_the_comment_prints_whole_under_a_marker_naming_it_terminal(self) -> None:
        """Its wording is what separates a refusal from a remark, so it is not truncated."""
        text = "Copilot has reached its quota limit.\nTry again after the window resets."
        self.answer(payload([review(oid=OLD)], comments=[comment(body=text)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("answered_outside_review=yes", out)
        self.assertIn("COPILOT COMMENT", out)
        for line in text.splitlines():
            self.assertIn(line, out)

    def test_a_pull_request_with_no_such_answer_says_so_rather_than_staying_silent(self) -> None:
        self.answer(payload([review()]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("answered_outside_review=no", out)

    def test_an_unreadable_window_reports_unknown_and_names_why(self) -> None:
        """Reporting `no` off a window an answer can hide behind is the false clean to avoid."""
        self.answer(
            payload(
                [review()],
                older=True,
                comments=[comment(login="ptr727") for _ in range(pr_review.WINDOW)],
            )
        )
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("answered_outside_review=unknown", out)
        self.assertIn("BEHIND THE WINDOW (comments)", out)


class TestCheckShapes(unittest.TestCase):
    """`mergeStateStatus` folds every one of these into `BLOCKED`, and each wants its own answer.

    The run this exists for polled `BLOCKED` for twenty-five minutes on a pull request whose only
    unfinished check was a rollup job no runner ever took, and learned the cause from the
    maintainer rather than from the digest.
    """

    def shape(self, node: dict, grace: float = 300, stall: float = 1800) -> str:
        """Judged through the normalizer, since that is the only shape the digest ever feeds it.

        Handing `check_shape` a raw rollup node instead reads every field as absent, which scores
        a queued job as a failure. That was this helper's first version, and it failed eight cases
        at once rather than one, which is what said the fault was in the helper.
        """
        (normalized,) = pr_review.check_nodes(payload([review()], checks=[node]))
        return pr_review.check_shape(normalized, NOW, grace, stall)

    def test_a_queued_check_is_starting_inside_the_grace_and_starved_past_it(self) -> None:
        """The two are the same state, and only the clock separates them, so the grace decides."""
        self.assertEqual("", self.shape(check(status="QUEUED", conclusion="", started=ago(90))))
        self.assertEqual(
            "NOT_PICKED_UP", self.shape(check(status="QUEUED", conclusion="", started=ago(900)))
        )

    def test_every_unstarted_spelling_counts_not_only_queued(self) -> None:
        """Reading QUEUED alone reports a check held at a gate as one that is running."""
        for state in ("QUEUED", "WAITING", "PENDING", "REQUESTED"):
            with self.subTest(state=state):
                self.assertEqual(
                    "NOT_PICKED_UP",
                    self.shape(check(status=state, conclusion="", started=ago(900))),
                )

    def test_an_expected_status_is_its_own_shape_not_a_starved_job(self) -> None:
        """`EXPECTED` is a StatusContext state meaning nothing has posted the status yet.

        Left out of every set it reaches the conclusion branch, matches no pass, and reports a
        required status nobody has reported as a red check. Folded in with the queued states it
        gets the starved remedy instead, which is wrong in both directions: no runner is owed a
        status nothing has posted, so re-running a workflow clears nothing, and a reader is sent
        at the runner pool over a missing poster. Both readings were raised in review here, the
        second on the fix for the first.
        """
        self.assertEqual(
            "NOT_POSTED", self.shape(status_context(state="EXPECTED", created=ago(900)))
        )
        self.assertEqual("", self.shape(status_context(state="EXPECTED", created=ago(60))))

    def test_the_unposted_message_does_not_borrow_the_starved_remedy(self) -> None:
        """The remedy is the reason the shapes are told apart, so the wording has to differ too."""
        out, _ = pr_review.digest(
            "o",
            "r",
            7,
            now=NOW,
            stalled="",
            pr=payload(
                [review()],
                merge="BLOCKED",
                checks=[status_context(context="ci/external", state="EXPECTED", created=ago(900))],
            ),
        )
        self.assertIn("stuck=NOT_POSTED", out)
        self.assertIn("CHECK NEVER POSTED ('ci/external'", out)
        self.assertIn("no runner is owed it", out)
        self.assertNotIn("CHECK NOT PICKED UP", out)

    def test_an_unknown_rollup_member_is_neither_forced_nor_dropped_quietly(self) -> None:
        """Forcing a third union member into the StatusContext shape invents a verdict for it.

        It would render nameless, since the node spells its label something else, and report as a
        red check, since the state read off it is not there. So skipping is the right half of the
        answer, and skipping *quietly* is the wrong half: an unread check absent from the tally
        renders as a clean pass over something never seen, which is this script's core failure.
        Both halves were raised in review here, the second against the fix for the first.

        So it is carried as a marker rather than dropped, counted by neither reader, and named.
        """
        pr = payload(
            [review()],
            checks=[
                check(name="lint"),
                {"__typename": "SomethingNew", "label": "future-gate", "verdict": "WHO_KNOWS"},
            ],
        )
        nodes = pr_review.check_nodes(pr)
        self.assertEqual(["SomethingNew"], pr_review.checks_unread(nodes))
        # Neither reader speaks for it, so the tally counts one check and no shape is judged.
        self.assertEqual((1, 1), pr_review.checks_tally(nodes))
        self.assertEqual(
            [], pr_review.checks_stuck([n for n in nodes if n.get("unreadable")], NOW, 300, 1800)
        )

    def test_the_digest_names_the_context_type_it_could_not_read(self) -> None:
        """A skip nobody is told about is the silent narrowing every other guard here prevents."""
        out, _ = pr_review.digest(
            "o",
            "r",
            7,
            now=NOW,
            stalled="",
            pr=payload(
                [review()],
                merge="BLOCKED",
                checks=[check(name="lint"), {"__typename": "SomethingNew", "label": "x"}],
            ),
        )
        self.assertIn("CHECKS PARTIALLY UNREAD (SomethingNew)", out)
        self.assertIn("neither speaks for it", out)

    def test_a_rollup_of_only_readable_members_says_nothing_about_unread_ones(self) -> None:
        """Otherwise the line fires on every pull request and stops being read."""
        out, _ = pr_review.digest(
            "o",
            "r",
            7,
            now=NOW,
            stalled="",
            pr=payload([review()], checks=[check(name="lint"), status_context()]),
        )
        self.assertNotIn("CHECKS PARTIALLY UNREAD", out)

    def test_a_running_check_is_reported_only_past_the_stall_threshold(self) -> None:
        """A lint job here legitimately runs eleven minutes, so the default must not flag it."""
        self.assertEqual(
            "", self.shape(check(status="IN_PROGRESS", conclusion="", started=ago(11 * 60)))
        )
        self.assertEqual(
            "RUNNING_LONG",
            self.shape(check(status="IN_PROGRESS", conclusion="", started=ago(40 * 60))),
        )

    def test_a_skipped_or_neutral_check_is_a_pass_not_a_blocker(self) -> None:
        """This fleet's aggregator pattern skips the conditional jobs on every green run.

        Four of the six checks on a passing pull request here are skips, so scoring a skip as
        unfinished reports four blockers on a pull request with none.
        """
        for conclusion in ("SUCCESS", "SKIPPED", "NEUTRAL"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual("", self.shape(check(conclusion=conclusion)))

    def test_a_finished_check_that_did_not_pass_is_a_failure_including_an_unknown_verdict(
        self,
    ) -> None:
        """An unrecognized conclusion is reported rather than passed over.

        A new enum member read as a pass is a red check rendering as a green digest, which is the
        false clean this whole script exists to prevent, one level down in the reading.
        """
        for conclusion in (
            "FAILURE",
            "CANCELLED",
            "TIMED_OUT",
            "STARTUP_FAILURE",
            "ACTION_REQUIRED",
            "STALE",
            "SOMETHING_NEW",
        ):
            with self.subTest(conclusion=conclusion):
                self.assertEqual("FAILED", self.shape(check(conclusion=conclusion)))

    def test_an_unreadable_timestamp_reports_nothing_rather_than_an_age_of_zero(self) -> None:
        """Zero would read as a check that just started, which is how one stuck for hours passes.

        The zone-less stamp is the one that has to be listed separately, since it *parses*. It
        yields a naive datetime that will not subtract from an aware `now`, so catching only
        `ValueError` lets a `TypeError` out of a reporting call and takes the whole digest with it.
        A crash is the worst outcome here, because saying what the state is is the entire job.
        Raised in review on this change.
        """
        for started in ("", "not-a-timestamp", "2026-08-06T17:00:00", "Thu Aug 6 2026"):
            with self.subTest(started=started):
                self.assertEqual(
                    "", self.shape(check(status="QUEUED", conclusion="", started=started))
                )

    def test_a_stamp_ahead_of_the_clock_renders_zero_rather_than_a_negative_age(self) -> None:
        """`queued -3m` reads as nonsense and hides how long the thing has actually waited.

        Raised in review here, and the reachability is worth stating rather than assuming. Through
        the CLI it is unreachable: a negative age cannot exceed a non-negative threshold, and the
        parser rejects a negative one, so no shape is ever judged and `mins` never renders. The
        library API takes the thresholds directly, which is the path this drives and the reason
        the clamp is worth having. Display only, so the comparison still reads the raw value.
        """
        ahead = (NOW + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pr = payload(
            [review()],
            merge="BLOCKED",
            checks=[check(name="gate", status="QUEUED", conclusion="", started=ahead)],
        )
        out, _ = pr_review.digest("o", "r", 7, pr=pr, stalled="", now=NOW, grace=-200)
        self.assertIn("queued 0m", out)
        self.assertNotIn("-2m", out)
        # The CLI cannot reach it, since the parser refuses the threshold that would.
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pr_review.main(["status", "7", "--repo", "o/r", "--check-grace", "-200"])

    def test_a_caller_handing_the_digest_an_odd_node_costs_a_field_not_the_digest(self) -> None:
        """The rendering path reads with `.get` for the reason `age` catches two exceptions.

        A raw GraphQL node reaching it through `checks` would `KeyError` and take down the one
        call whose whole job is to report the state. Raised in review here as mandatory.
        """
        raw = {"state": "QUEUED", "since": ago(900)}
        out, _ = pr_review.digest(
            "o", "r", 7, stalled="", now=NOW, checks=[raw], pr=payload([review()], merge="BLOCKED")
        )
        self.assertIn("CHECK NOT PICKED UP ('unnamed'", out)

    def test_a_zone_less_stamp_returns_none_rather_than_raising(self) -> None:
        """The same guard at the function, since a digest that raises reports nothing at all."""
        self.assertIsNone(pr_review.age("2026-08-06T17:00:00", NOW))
        self.assertIsNone(pr_review.age("nonsense", NOW))
        self.assertEqual(900.0, pr_review.age(ago(900), NOW))

    def test_a_finished_check_carrying_no_conclusion_yet_is_settling_not_failing(self) -> None:
        """Reading an absent verdict as a failure invents a red check out of a race in the API.

        Caught by this suite rather than in review: the first implementation fell straight through
        to the unknown-conclusion branch, so a completed check whose conclusion had not been
        written yet reported as FAILED.
        """
        self.assertEqual("", self.shape(check(conclusion="")))

    def test_both_rollup_node_shapes_normalize_rather_than_one_reading_as_stateless(self) -> None:
        """A StatusContext folds status and conclusion into one field and renames the label.

        Reading a CheckRun's keys off it yields None for every one, which scores an external
        status as a check in no state at all, so neither pending nor failed.
        """
        pr = payload([review()], checks=[check(name="job"), status_context(state="FAILURE")])
        nodes = pr_review.check_nodes(pr)
        self.assertEqual(["job", "ci/external"], [n["name"] for n in nodes])
        # Judged directly rather than through `self.shape`, which normalizes a second time.
        # A normalized node carries no typename, so that pass would skip it as unknown.
        self.assertEqual("FAILED", pr_review.check_shape(nodes[1], NOW, 300, 1800))

    def test_a_pending_status_context_is_running_rather_than_unstarted(self) -> None:
        """The same string is two states, and only the node shape says which.

        A CheckRun's PENDING means dispatched and not begun, while a StatusContext's means the
        posting system reported the run as under way. Read as unstarted, a long external build
        reports as queued with no runner assigned, which names a cause it does not have on a
        system that did pick it up. Raised in review on this change.
        """
        self.assertEqual("", self.shape(status_context(state="PENDING", created=ago(900))))
        self.assertEqual(
            "RUNNING_LONG", self.shape(status_context(state="PENDING", created=ago(40 * 60)))
        )
        # The CheckRun spelling keeps the opposite reading, which is what makes the shape decide.
        self.assertEqual(
            "NOT_PICKED_UP", self.shape(check(status="PENDING", conclusion="", started=ago(900)))
        )

    def test_a_pull_request_with_no_rollup_reads_as_no_checks_not_as_a_failure(self) -> None:
        """A null rollup is a pull request nothing has run on yet, which blocks nothing here."""
        self.assertEqual([], pr_review.check_nodes(payload([review()])))
        # Both readers take the normalized list, since the digest parses the rollup once.
        self.assertEqual((0, 0), pr_review.checks_tally(pr_review.check_nodes(payload([review()]))))
        self.assertEqual(
            [], pr_review.checks_stuck(pr_review.check_nodes(payload([review()])), NOW, 300, 1800)
        )


class TestDigestReportsChecks(GqlCase):
    def digest(self, pr: dict) -> str:
        out, _ = pr_review.digest("o", "r", 7, pr=pr, stalled="", now=NOW)
        return out

    def test_the_tally_says_how_far_the_head_got_beside_the_merge_word(self) -> None:
        """`merge=BLOCKED` names no cause, and this is what says which half is unfinished."""
        out = self.digest(
            payload(
                [review()],
                merge="BLOCKED",
                checks=[
                    check(name="lint"),
                    check(name="gate", status="QUEUED", conclusion="", started=ago(900)),
                ],
            )
        )
        self.assertIn("merge=BLOCKED checks=1/2", out)

    def test_a_starved_check_is_named_with_its_queue_time_and_its_remedy(self) -> None:
        """The remedy is the point: nothing agent-side starts a job no hosted runner took."""
        out = self.digest(
            payload(
                [review()],
                merge="BLOCKED",
                checks=[check(name="gate", status="QUEUED", conclusion="", started=ago(15 * 60))],
            )
        )
        self.assertIn("stuck=NOT_PICKED_UP", out)
        self.assertIn("CHECK NOT PICKED UP ('gate', queued 15m", out)
        self.assertIn("re-run the workflow", out)

    def test_a_long_running_check_is_reported_without_asserting_a_fault(self) -> None:
        """Duration alone cannot separate hung from slow, so the wording must not claim it has."""
        out = self.digest(
            payload(
                [review()],
                merge="BLOCKED",
                checks=[
                    check(name="build", status="IN_PROGRESS", conclusion="", started=ago(45 * 60))
                ],
            )
        )
        self.assertIn("stuck=RUNNING_LONG", out)
        self.assertIn("it has a runner, so it is not starved", out)
        self.assertIn("judgment against what this job normally costs", out)

    def test_a_failed_check_is_named_as_a_verdict_rather_than_left_to_be_deduced(self) -> None:
        out = self.digest(
            payload([review()], merge="BLOCKED", checks=[check(name="lint", conclusion="FAILURE")])
        )
        self.assertIn("stuck=FAILED", out)
        self.assertIn("CHECK FAILED ('lint', COMPLETED/FAILURE)", out)

    def test_a_green_pull_request_carries_no_stuck_field_at_all(self) -> None:
        """A field reading `none` on every green run is one a reader skips on the run it matters."""
        out = self.digest(payload([review()], checks=[check(), check(conclusion="SKIPPED")]))
        self.assertIn("checks=2/2", out)
        self.assertNotIn("stuck=", out)

    def test_a_check_still_running_normally_is_not_stuck(self) -> None:
        """Otherwise the field fires on every pull request mid-CI and so carries nothing."""
        out = self.digest(
            payload(
                [review()],
                merge="BLOCKED",
                checks=[check(name="lint", status="IN_PROGRESS", conclusion="", started=ago(120))],
            )
        )
        self.assertIn("checks=0/1", out)
        self.assertNotIn("stuck=", out)

    def test_the_rollup_is_selected_by_the_head_rather_than_by_position(self) -> None:
        """A rollup off any other commit describes a push ago and renders every field.

        The first version of this case asserted the *fixture's* commit equalled the head, which
        tests the payload rather than the code, and the code was reading `commits[0]` regardless.
        Raised in review on this change, and it is the sharper reading: position is not identity,
        and trusting it is the same false-clean shape as counting a review without reading it.
        This asserts the selection by giving the connection a rollup on a commit that is not the
        head, which must be ignored rather than reported.
        """
        pr = payload(
            [review()], checks=[check(name="stale-run", conclusion="FAILURE")], rollup_oid=OLD
        )
        self.assertEqual([], pr_review.check_nodes(pr))
        self.assertEqual((0, 0), pr_review.checks_tally(pr_review.check_nodes(pr)))
        # No fallback to another commit's rollup, which is the same stale read by another route.
        # The absence is named rather than left to render as a fact about the head.
        self.assertTrue(pr_review.checks_unreadable(pr))
        self.assertIn("CHECKS UNREADABLE", self.digest(pr))

    def test_a_rollup_past_the_window_says_so_rather_than_reporting_what_it_saw(self) -> None:
        """The `window_blind` guard one connection along, and the same false clean it prevents.

        A rollup past a hundred contexts drops the rest silently, so a required check among them
        is missing from the tally and the stuck reading alike and the digest renders a clean pass
        over a check it never saw. A fleet repository with a matrix build reaches a hundred long
        before this one does. Raised in review here.
        """
        pr = payload([review()], merge="BLOCKED", checks=[check(name="lint")])
        rollup = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
        rollup["contexts"]["pageInfo"] = {"hasNextPage": True}
        self.assertTrue(pr_review.checks_truncated(pr))
        self.assertIn("CHECKS TRUNCATED", self.digest(pr))

    def test_a_rollup_inside_the_window_is_not_reported_as_truncated(self) -> None:
        """Otherwise the loudest line in the digest fires on every pull request that has checks."""
        self.assertFalse(pr_review.checks_truncated(payload([review()], checks=[check()])))
        self.assertNotIn("CHECKS TRUNCATED", self.digest(payload([review()], checks=[check()])))

    def test_the_rollup_is_normalized_once_for_both_readers(self) -> None:
        """Two calls parsed the same rollup twice a digest, and the parse is what this script pays.

        Raised in review here. Held by counting the calls rather than by reading the output, since
        the output is identical either way, which is what let the second call go unnoticed.
        """
        pr = payload([review()], merge="BLOCKED", checks=[check(name="lint"), check(name="gate")])
        with mock.patch.object(
            pr_review, "check_nodes", side_effect=pr_review.check_nodes
        ) as parsed:
            pr_review.digest("o", "r", 7, pr=pr, stalled="", now=NOW)
        self.assertEqual(1, parsed.call_count)

    def test_a_caller_that_parsed_the_rollup_is_not_made_to_parse_it_again(self) -> None:
        """The first fix left `wait` parsing once to print and once to decide, which is the same
        doubling one caller up. Raised in review here, on the commit that halved it."""
        pr = payload([review()], merge="BLOCKED", checks=[check(name="lint")])
        with mock.patch.object(
            pr_review, "check_nodes", side_effect=pr_review.check_nodes
        ) as parsed:
            pr_review.digest(
                "o", "r", 7, pr=pr, stalled="", now=NOW, checks=pr_review.check_nodes(pr)
            )
        # The one call is the case's own, so the digest made none of its own.
        self.assertEqual(1, parsed.call_count)

    def test_the_truncation_line_quotes_the_window_the_query_actually_asks_for(self) -> None:
        """The message borrowed WINDOW, which is the reviews and comments window and not this one.

        It read correctly only while the two numbers happened to agree, so a change to either
        would have made the line state a limit the query does not use. Raised in review here.
        """
        self.assertIn(f"contexts(first:{pr_review.CHECKS_WINDOW})", pr_review.Q_FULL)
        pr = payload([review()], merge="BLOCKED", checks=[check()])
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["pageInfo"] = {
            "hasNextPage": True
        }
        self.assertIn(f"more than the {pr_review.CHECKS_WINDOW} contexts", self.digest(pr))

    def test_one_reader_finds_the_head_commit_for_all_three_callers(self) -> None:
        """Three traversals of one shape drift apart, and a payload change must be caught once.

        Raised in review here. Each caller is held to the shared reader by giving the payload a
        head that matches nothing, where all three must agree that there is no rollup to read.
        """
        pr = payload([review()], checks=[check()], rollup_oid=OLD)
        self.assertEqual({}, pr_review.head_commit(pr))
        self.assertEqual([], pr_review.check_nodes(pr))
        self.assertFalse(pr_review.checks_truncated(pr))
        self.assertTrue(pr_review.checks_unreadable(pr))

    def test_a_pull_request_with_no_commits_at_all_is_not_reported_as_unreadable(self) -> None:
        """Nothing to match against is not a failed match, or every payload without one says so."""
        self.assertFalse(pr_review.checks_unreadable(payload([review()], checks=[check()])))
        bare = payload([review()])
        bare["commits"] = {"nodes": []}
        self.assertFalse(pr_review.checks_unreadable(bare))
        self.assertNotIn("CHECKS UNREADABLE", self.digest(bare))

    def test_the_head_rollup_is_found_wherever_the_connection_puts_it(self) -> None:
        """Selection is by oid, so an extra node ahead of the head's does not shadow it."""
        pr = payload([review()], checks=[check(name="lint")])
        head_node = pr["commits"]["nodes"][0]
        pr["commits"]["nodes"] = [{"commit": {"oid": OLD, "statusCheckRollup": None}}, head_node]
        self.assertEqual(["lint"], [n["name"] for n in pr_review.check_nodes(pr)])


class TestGqlTransport(unittest.TestCase):
    def test_a_failed_call_raises_rather_than_returning_an_empty_reading(self) -> None:
        """Returning nothing on failure would read as a PR with no reviews and no threads."""
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        with (
            mock.patch.object(pr_review.subprocess, "run", return_value=failed),
            contextlib.redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit),
        ):
            pr_review.gql("query", "o", "r", 1)
        self.assertIn("boom", err.getvalue())

    def test_a_successful_call_unwraps_to_the_pull_request(self) -> None:
        body = json.dumps({"data": {"repository": {"pullRequest": {"headRefOid": HEAD}}}})
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=body, stderr="")
        with mock.patch.object(pr_review.subprocess, "run", return_value=done) as run:
            self.assertEqual({"headRefOid": HEAD}, pr_review.gql("query", "o", "r", 1))
        # Read-only: the transport shells out to `gh api graphql` and nothing else.
        self.assertEqual(["gh", "api", "graphql"], run.call_args.args[0][:3])


class TestCli(GqlCase):
    def setUp(self) -> None:
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def cli(self, argv: list[str]) -> int:
        """Every run names its repository, since the parser supplies no default for one."""
        return pr_review.main([*argv, "--repo", "o/r"])

    def test_status_prints_the_digest_and_exits_zero(self) -> None:
        self.answer(payload([review()]))
        self.assertEqual(0, self.cli(["status", "7"]))
        self.assertIn("pr=7", self.out.getvalue())

    def test_wait_returns_zero_once_the_review_lands_on_the_head(self) -> None:
        """The first poll already sees it, so the loop body never runs."""
        self.answer(payload([review()]))
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        slept.assert_not_called()
        self.assertIn("waited=", self.out.getvalue())

    def test_wait_exits_forty_four_where_the_review_closed_but_a_check_is_starved(self) -> None:
        """Exit 0 was saying the review loop closing is the merge gate, and it is not.

        `main` reads the real clock, so the timestamp comes from that clock rather than from the
        suite's fixed NOW. Measured from NOW instead, the age grows on every later run and turns
        negative on a machine whose clock sits before it, so the case would stop meaning what it
        says. Raised in review on this change.
        """
        self.answer(
            payload(
                [review()],
                merge="BLOCKED",
                checks=[check(name="gate", status="QUEUED", conclusion="", started=real_ago(900))],
            )
        )
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(44, self.cli(["wait", "7"]))
        out = self.out.getvalue()
        self.assertIn("status=CHECKS_NOT_MERGEABLE", out)
        self.assertIn("CHECK NOT PICKED UP", out)

    def test_a_stuck_check_nothing_requires_does_not_take_forty_four(self) -> None:
        """A rollup carries checks the ruleset does not require, four of six on a green run here.

        So the code borrows GitHub's own reading of which checks gate a merge, and `CLEAN` proves
        no required gate is outstanding whatever else the rollup is doing. Without that, a stuck
        check nothing requires returns 44 on a mergeable pull request. Raised in review on this
        change. The digest still names the check, so the narrower code costs the reader nothing.
        """
        self.answer(
            payload(
                [review()],
                merge="CLEAN",
                checks=[
                    check(
                        name="optional-scan", status="QUEUED", conclusion="", started=real_ago(900)
                    )
                ],
            )
        )
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7"]))
        out = self.out.getvalue()
        self.assertIn("stuck=NOT_PICKED_UP", out)
        self.assertNotIn("status=CHECKS_NOT_MERGEABLE", out)

    def test_wait_exits_zero_where_a_check_is_merely_still_running(self) -> None:
        """A code that fires on every pull request mid-CI carries nothing, so this must be 0.

        The wait returns the moment coverage lands, which on almost every pull request is while
        the checks are still going, so a pending check taking 44 would make 44 the usual outcome.
        Inside the default grace on the real clock, which is what this reads, so no flag is needed.
        """
        self.answer(
            payload(
                [review()],
                merge="BLOCKED",
                checks=[check(name="lint", status="QUEUED", conclusion="", started=real_ago(30))],
            )
        )
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertNotIn("stuck=", self.out.getvalue())

    def test_a_starved_check_does_not_pre_empt_a_review_that_never_landed(self) -> None:
        """The review codes outrank it, since a missing review is the older and larger failure."""
        self.answer(
            payload(
                [review(oid=OLD)],
                merge="BLOCKED",
                checks=[check(name="gate", status="QUEUED", conclusion="", started=real_ago(900))],
            )
        )
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        out = self.out.getvalue()
        self.assertIn("status=PENDING", out)
        # Reported in the digest regardless, since the reader still needs to know it is there.
        self.assertIn("CHECK NOT PICKED UP", out)

    def test_wait_polls_again_after_a_pending_round(self) -> None:
        """Each iteration re-reads the head, since a push during the wait moves it."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertEqual(1, slept.call_count)

    def test_wait_exits_thirty_at_the_timeout_rather_than_reporting_success(self) -> None:
        """Pending is not failure and not success, so it takes a code of its own."""
        self.answer(payload([review(oid=OLD)]))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertIn("status=PENDING", self.out.getvalue())

    def test_the_timeout_carries_the_digest_rather_than_a_bare_pending_line(self) -> None:
        """A wait that ends with no evidence reports a slow reviewer and a broken poll alike."""
        self.answer(payload([review(oid=OLD)], [thread("T1")]))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        out = self.out.getvalue()
        self.assertIn("review_on_head=NO", out)
        self.assertIn("unresolved=1", out)

    def test_wait_ends_on_an_answer_outside_a_review_instead_of_waiting_it_out(self) -> None:
        """A refusal covers no head, so polling on for the timeout waits for nothing.

        The zero timeout is what this case fails on rather than hangs on: an answer read as
        pending spins the loop for the whole default wait, and a case that hangs gates nothing.
        """
        self.answer(payload([review(oid=OLD)], comments=[comment()]))
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(40, self.cli(["wait", "7", "--timeout", "0"]))
        slept.assert_not_called()
        out = self.out.getvalue()
        self.assertIn("status=ANSWERED_OUTSIDE_REVIEW", out)
        self.assertIn("quota", out)

    def test_wait_exits_forty_one_on_a_review_that_says_it_did_not_review(self) -> None:
        """Returning zero here is the failure: the digest is the clean pass byte for byte."""
        self.answer(payload([review(body=REFUSED)]))
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(41, self.cli(["wait", "7", "--timeout", "0"]))
        # Terminal, so it ends the wait rather than polling out the timeout against it.
        slept.assert_not_called()
        out = self.out.getvalue()
        self.assertIn("status=REVIEW_IS_A_REFUSAL", out)
        self.assertIn("review_on_head=NO", out)
        self.assertIn(REFUSED, out)

    def test_the_liveness_reading_ends_the_wait_and_the_full_read_refuses_it(self) -> None:
        """The liveness query carries no bodies, so a refusal reads there as ordinary coverage.

        That is what ends the loop, and nothing decides on it: the full read that follows every
        wait carries the body and is where the exit code comes from.
        """
        bodyless = {k: v for k, v in review().items() if k != "body"}
        self.answer(payload([bodyless]), payload([review(body=REFUSED)]))
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(41, self.cli(["wait", "7", "--timeout", "600"]))
        slept.assert_not_called()
        self.assertIn("status=REVIEW_IS_A_REFUSAL", self.out.getvalue())

    def test_a_landed_review_wins_over_an_older_answer(self) -> None:
        """Coverage is the success case, and a spent comment does not downgrade it to 40."""
        self.answer(payload([review(at=LATE)], comments=[comment(at=EARLY)]))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7"]))

    def test_a_pending_effort_labeled_request_reaches_the_timeout(self) -> None:
        """Missing pickup telemetry cannot prove that an effort-labeled request is abandoned."""
        self.answer(payload([review(oid=OLD)], pending=True))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        out = self.out.getvalue()
        self.assertIn("status=PENDING", out)
        self.assertNotIn("REQUEST NOT PICKED UP", out)

    def test_a_pending_request_can_complete_without_pickup_telemetry(self) -> None:
        """The PR #873 lifecycle reaches a review without `copilot_work_started`."""
        self.answer(payload([review(oid=OLD)], pending=True), payload([review()], pending=True))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7", "--timeout", "600"]))

    def test_a_pending_review_landing_during_the_final_read_wins_over_timeout(self) -> None:
        """The digest and exit code come from one payload when the review lands at timeout."""
        self.answer(payload([review(oid=OLD)], pending=True), payload([review()], pending=True))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7", "--timeout", "0"]))
        out = self.out.getvalue()
        self.assertIn("review_on_head=yes", out)

    def test_a_review_landing_during_the_last_read_wins_over_the_timeout(self) -> None:
        """Same disagreement at the other exit: printing coverage and returning PENDING."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7", "--timeout", "0"]))
        out = self.out.getvalue()
        self.assertIn("review_on_head=yes", out)
        self.assertNotIn("status=PENDING", out)

    def test_pickup_grace_remains_an_ignored_compatibility_option(self) -> None:
        """Existing callers keep parsing while the option makes no liveness claim."""
        self.answer(payload([review(oid=OLD)], pending=True))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--pickup-grace", "0", "--timeout", "0"]))
        out = self.out.getvalue()
        self.assertIn("status=PENDING", out)

    def test_an_answer_ends_a_pending_request(self) -> None:
        """An answer remains terminal regardless of absent pickup telemetry."""
        self.answer(payload([review(oid=OLD)], comments=[comment()], pending=True))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(40, self.cli(["wait", "7", "--timeout", "0"]))

    def test_the_repo_argument_splits_into_owner_and_name(self) -> None:
        self.answer(payload([review()]))
        with mock.patch.object(pr_review, "digest", return_value=("x", 0)) as dig:
            self.assertEqual(0, pr_review.main(["status", "7", "--repo", "owner/name"]))
        self.assertEqual(("owner", "name", 7), dig.call_args.args)

    def test_a_run_naming_no_repo_is_rejected_rather_than_sent_somewhere(self) -> None:
        """A default would send it to one repository, and every number resolves there.

        That is the failure this had twice: the digest rendered, nothing in it disagreed, and
        the run was reading a pull request in a repository nobody had named.
        """
        self.answer(payload([review()]))
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            pr_review.main(["status", "7"])
        self.assertIn("--repo", err.getvalue())

    def test_a_repo_that_is_not_owner_slash_name_is_rejected_by_name(self) -> None:
        """The near-miss a required argument still admits, and unpacking it is a bare traceback."""
        for bad in ("ProjectTemplate", "ptr727/", "/ProjectTemplate", "a/b/c", ""):
            with self.subTest(repo=bad):
                with (
                    contextlib.redirect_stderr(io.StringIO()) as err,
                    self.assertRaises(SystemExit),
                ):
                    pr_review.main(["status", "7", "--repo", bad])
                self.assertIn("OWNER/NAME", err.getvalue())

    def test_the_digest_names_the_repository_it_read(self) -> None:
        """A digest of the wrong pull request is well-formed, so the line has to say which one."""
        self.answer(payload([review()]))
        self.assertEqual(0, self.cli(["status", "7"]))
        self.assertIn("repo=o/r pr=7", self.out.getvalue())

    def wire_bot(self, bot_id: str | None) -> list[tuple[str, dict]]:
        """Route `gh_graphql` calls after `self.answer(...)`, recording each one made.

        `self.answer(...)` already installs a default returning no bot id, which is what every
        other wait test relies on to skip the auto-request path untouched. This overrides that
        default for the handful of tests below that exercise the auto-request itself.
        """
        calls: list[tuple[str, dict]] = []

        def fake(query: str, **variables: object) -> dict:
            calls.append((query, variables))
            if "pullRequests(first:20" in query:
                if bot_id is None:
                    return {"repository": {"pullRequests": {"nodes": []}}}
                return {
                    "repository": {
                        "pullRequests": {
                            "nodes": [
                                {
                                    "reviews": {
                                        "nodes": [
                                            {
                                                "author": {
                                                    "__typename": "Bot",
                                                    "login": pr_review.REVIEWER,
                                                    "id": bot_id,
                                                }
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                }
            if "requestReviews" in query:
                return {"requestReviews": {"pullRequest": {"id": variables.get("pr")}}}
            raise AssertionError(f"unexpected document: {query[:60]}")

        self.enterContext(mock.patch.object(pr_review, "gh_graphql", side_effect=fake))
        return calls

    def test_a_resolved_bot_id_issues_the_request_before_the_first_poll(self) -> None:
        """The gap this closed: nothing outstanding and nothing ever asked for, twice measured."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        calls = self.wire_bot("BOT_123")
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertEqual(1, slept.call_count)
        mutations = [(q, v) for q, v in calls if "requestReviews" in q]
        self.assertEqual(1, len(mutations))
        self.assertEqual("BOT_123", mutations[0][1].get("bot"))
        self.assertEqual("PR_test", mutations[0][1].get("pr"))
        self.assertIn("auto-request: requested a Copilot review", self.out.getvalue())

    def test_no_bot_id_anywhere_skips_the_request_and_still_polls(self) -> None:
        """A repository with no Copilot history falls back to plain polling, not a crash."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        calls = self.wire_bot(None)
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertEqual(1, slept.call_count)
        self.assertFalse([c for c in calls if "requestReviews" in c[0]])
        self.assertIn("no Copilot review found", self.out.getvalue())

    def test_an_already_requested_review_is_not_re_requested(self) -> None:
        """Calling `wait` twice on the same pending request never double-requests it."""
        self.answer(payload([review()], pending=True))
        calls = self.wire_bot("BOT_123")
        self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertFalse(calls)
        self.assertNotIn("auto-request:", self.out.getvalue())


def rthread(
    tid: str,
    body: str = "The retry count is off by one.",
    path: str = "a.py",
    line: int = 12,
    resolved: bool = False,
    login: str = pr_review.REVIEWER,
) -> dict:
    """A thread as the reply query reads it, with `path` and `line` on the thread itself."""
    return {
        "id": tid,
        "isResolved": resolved,
        "path": path,
        "line": line,
        "comments": {"nodes": [{"author": {"login": login}, "body": body}]},
    }


def page(threads: list[dict], more: bool = False, cursor: str | None = None) -> dict:
    return {"nodes": threads, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


COMMENT_BODY = "Disproven: the bound is checked before the write."
COMMENT = {
    "id": "c1",
    "url": "https://github.com/o/r/pull/7#issuecomment-1",
    "body": COMMENT_BODY,
}


class CommentCase(unittest.TestCase):
    """Drive `comment` against a live PR target and a crafted mutation response."""

    def setUp(self) -> None:
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(mock.patch.object(pr_review, "origin_owner", return_value="o"))
        self.calls: list[tuple[str, dict[str, object]]] = []

    def wire(self, target: dict | None = None, comment: dict | None = COMMENT) -> None:
        def fake(query: str, **variables: object) -> dict:
            self.calls.append((query, variables))
            if "pullRequest(number" in query:
                return {"repository": {"pullRequest": target}}
            if "addComment" in query:
                return {"addComment": {"commentEdge": {"node": comment}}}
            raise AssertionError(f"unexpected document: {query[:60]}")

        self.enterContext(mock.patch.object(pr_review, "gh_graphql", side_effect=fake))

    def run_comment(self, repo: str = "o/r", body: str = COMMENT_BODY) -> int:
        return pr_review.main(["comment", "7", "--repo", repo, "--body", body])


class TestCommentConfirmsItsTargetAndResult(CommentCase):
    def test_the_pr_id_is_read_live_and_the_returned_comment_is_confirmed(self) -> None:
        self.wire({"id": "PR_from_query", "url": "https://github.com/o/r/pull/7"})
        self.assertEqual(0, self.run_comment())
        mutation = next((v for q, v in self.calls if "addComment" in q))
        self.assertEqual("PR_from_query", mutation["subjectId"])
        self.assertEqual(COMMENT["body"], mutation["body"])
        self.assertIn(COMMENT["url"], self.out.getvalue())
        self.assertIn("status=COMMENTED", self.out.getvalue())

    def test_an_unreadable_pr_stops_before_the_mutation(self) -> None:
        self.wire(None)
        self.assertEqual(65, self.run_comment())
        self.assertFalse(any("addComment" in query for query, _ in self.calls))
        self.assertIn("TARGET_NOT_READ", self.out.getvalue())

    def test_a_response_without_a_url_is_not_reported_as_posted(self) -> None:
        self.wire(
            {"id": "PR_from_query", "url": "https://github.com/o/r/pull/7"},
            {"id": "c1", "url": None, "body": COMMENT["body"]},
        )
        self.assertEqual(66, self.run_comment())
        self.assertIn("COMMENT_NOT_CONFIRMED", self.out.getvalue())

    def test_a_response_with_a_different_body_is_not_reported_as_posted(self) -> None:
        self.wire(
            {"id": "PR_from_query", "url": "https://github.com/o/r/pull/7"},
            {"id": "c1", "url": COMMENT["url"], "body": ""},
        )
        self.assertEqual(66, self.run_comment())

    def test_newlines_are_normalized_before_the_comment_is_sent_and_confirmed(self) -> None:
        body = "Suppressed finding:\r\n\r\nDisproven.\rOne boundary applies."
        normalized = "Suppressed finding:\n\nDisproven.\nOne boundary applies."
        response = {"id": "c1", "url": COMMENT["url"], "body": normalized}
        self.wire({"id": "PR_from_query", "url": "https://github.com/o/r/pull/7"}, response)
        self.assertEqual(0, self.run_comment(body=body))
        mutation = next((v for q, v in self.calls if "addComment" in q))
        self.assertEqual(normalized, mutation["body"])

    def test_a_target_under_another_owner_is_refused_before_the_pr_read(self) -> None:
        self.wire({"id": "PR_wrong_owner", "url": "https://github.com/x/r/pull/7"})
        self.assertEqual(64, self.run_comment(repo="x/r"))
        self.assertEqual([], self.calls)
        self.assertIn("OUT_OF_SCOPE", self.out.getvalue())

    def test_an_unreadable_origin_refuses_before_the_pr_read(self) -> None:
        self.wire({"id": "PR_from_query", "url": "https://github.com/o/r/pull/7"})
        with mock.patch.object(pr_review, "origin_owner", return_value=None):
            self.assertEqual(64, self.run_comment())
        self.assertEqual([], self.calls)
        self.assertIn("OUT_OF_SCOPE", self.out.getvalue())


LANDED = {"id": "c1", "url": "https://github.com/o/r/pull/7#discussion_r1", "body": "Fixed in abc."}


class ReplyCase(unittest.TestCase):
    """Base driving `reply` against crafted responses, so no case reaches the network."""

    def setUp(self) -> None:
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(mock.patch.object(pr_review, "origin_owner", return_value="o"))
        self.docs: list[str] = []

    def wire(self, *pages: dict, reply: dict | None = LANDED, resolved: bool = True) -> None:
        """Answer the thread reads from `pages`, and each mutation from the given shape."""
        queue = list(pages) or [page([])]

        def fake(query: str, **variables: object) -> dict:
            self.docs.append(query)
            if "reviewThreads" in query:
                return {"repository": {"pullRequest": {"reviewThreads": queue.pop(0)}}}
            if "addPullRequestReviewThreadReply" in query:
                return {"addPullRequestReviewThreadReply": {"comment": reply}}
            if "resolveReviewThread" in query:
                return {"resolveReviewThread": {"thread": {"isResolved": resolved}}}
            raise AssertionError(f"unexpected document: {query[:60]}")

        self.enterContext(mock.patch.object(pr_review, "gh_graphql", side_effect=fake))

    def run_reply(self, *extra: str) -> int:
        return pr_review.main(
            [
                "reply",
                "7",
                "--repo",
                "o/r",
                "--match",
                "retry count",
                "--body",
                "Fixed in abc.",
                *extra,
            ]
        )

    def wrote(self) -> bool:
        return any("mutation" in d for d in self.docs)

    def resolved_a_thread(self) -> bool:
        return any("resolveReviewThread" in d for d in self.docs)


class TestReplySelectsWithoutAnId(ReplyCase):
    def test_the_matching_thread_is_answered_and_resolved(self) -> None:
        self.wire(page([rthread("t1")]))
        self.assertEqual(0, self.run_reply("--resolve"))
        self.assertIn("REPLIED_AND_RESOLVED", self.out.getvalue())
        self.assertIn(LANDED["url"], self.out.getvalue())

    def test_the_id_comes_from_the_query_rather_than_the_caller(self) -> None:
        """The whole point: the id each mutation carries is one this same run just read."""
        ids = []

        def capture(query: str, **variables: object) -> dict:
            if "reviewThreads" in query:
                return {
                    "repository": {
                        "pullRequest": {"reviewThreads": page([rthread("t-from-the-query")])}
                    }
                }
            ids.append(variables.get("threadId"))
            if "addPullRequestReviewThreadReply" in query:
                return {"addPullRequestReviewThreadReply": {"comment": LANDED}}
            return {"resolveReviewThread": {"thread": {"isResolved": True}}}

        with mock.patch.object(pr_review, "gh_graphql", side_effect=capture):
            self.assertEqual(0, self.run_reply("--resolve"))
        self.assertEqual(["t-from-the-query", "t-from-the-query"], ids)

    def test_no_match_writes_nothing_and_lists_what_is_open(self) -> None:
        """A no-match reads the same as an already-answered thread, so it stops rather than guesses."""
        self.wire(page([rthread("t1", body="An unrelated finding about naming.")]))
        self.assertEqual(60, self.run_reply("--resolve"))
        self.assertFalse(self.wrote())
        self.assertIn("NO_MATCH", self.out.getvalue())
        # The open threads print, or the reader's next move is to go hunting for an id.
        self.assertIn("unrelated finding", self.out.getvalue())

    def test_two_matches_refuse_rather_than_take_the_first(self) -> None:
        """`head -n 1` on an ambiguous match is how a reply lands on the wrong finding."""
        self.wire(page([rthread("t1", path="a.py"), rthread("t2", path="b.py")]))
        self.assertEqual(61, self.run_reply("--resolve"))
        self.assertFalse(self.wrote())
        self.assertIn("AMBIGUOUS", self.out.getvalue())
        for path in ("a.py", "b.py"):
            self.assertIn(path, self.out.getvalue())

    def test_path_narrows_an_otherwise_ambiguous_match(self) -> None:
        self.wire(page([rthread("t1", path="a.py"), rthread("t2", path="b.py")]))
        self.assertEqual(0, self.run_reply("--resolve", "--path", "b.py"))
        self.assertIn("b.py:12", self.out.getvalue())

    def test_a_resolved_thread_is_not_a_candidate(self) -> None:
        """It is answered, and replying again reopens a conversation nobody is reading."""
        self.wire(page([rthread("t1", resolved=True)]))
        self.assertEqual(60, self.run_reply("--resolve"))
        self.assertFalse(self.wrote())

    def test_the_match_follows_the_cursor_to_the_last_page(self) -> None:
        """A first page read as the whole set reports no match on a thread further along."""
        self.wire(
            page([rthread("t1", body="Something else.")], more=True, cursor="c1"),
            page([rthread("t2")]),
        )
        self.assertEqual(0, self.run_reply("--resolve"))
        self.assertIn("REPLIED_AND_RESOLVED", self.out.getvalue())

    def test_the_match_reads_the_finding_text_rather_than_a_line_number(self) -> None:
        """A fix push moves the line, and every lookup keyed to one then misses."""
        self.wire(page([rthread("t1", line=999, body="The RETRY COUNT is off by one.")]))
        self.assertEqual(0, self.run_reply("--resolve"))
        self.assertIn("REPLIED_AND_RESOLVED", self.out.getvalue())


class TestReplyConfirmsBeforeResolving(ReplyCase):
    def test_a_reply_returning_no_url_leaves_the_thread_open(self) -> None:
        """Three replies posted empty and the resolves still succeeded, closing them unanswered."""
        self.wire(page([rthread("t1")]), reply={"id": "c1", "url": None, "body": ""})
        self.assertEqual(62, self.run_reply("--resolve"))
        self.assertFalse(self.resolved_a_thread())
        self.assertIn("REPLY_NOT_CONFIRMED", self.out.getvalue())

    def test_a_reply_whose_body_came_back_empty_leaves_the_thread_open(self) -> None:
        """A url alone says a comment exists, not that it carries the answer."""
        self.wire(page([rthread("t1")]), reply={"id": "c1", "url": LANDED["url"], "body": "   "})
        self.assertEqual(62, self.run_reply("--resolve"))
        self.assertFalse(self.resolved_a_thread())

    def test_a_resolve_that_does_not_confirm_is_reported_rather_than_assumed(self) -> None:
        """The reply is already posted, so silence here leaves a thread open behind an answer."""
        self.wire(page([rthread("t1")]), resolved=False)
        self.assertEqual(63, self.run_reply("--resolve"))
        self.assertIn("RESOLVE_NOT_CONFIRMED", self.out.getvalue())

    def test_without_resolve_the_thread_is_answered_and_left_open(self) -> None:
        """A decline is resolved once its evidence is in the thread, which is the reader's call."""
        self.wire(page([rthread("t1")]))
        self.assertEqual(0, self.run_reply())
        self.assertFalse(self.resolved_a_thread())
        self.assertIn("status=REPLIED", self.out.getvalue())


class TestReplyStaysInScope(ReplyCase):
    def test_a_target_under_another_owner_is_refused_before_anything_is_read(self) -> None:
        """The incident shape: a write that lands on a stranger's repository."""
        self.wire(page([rthread("t1")]))
        code = pr_review.main(
            [
                "reply",
                "7",
                "--repo",
                "someone-else/r",
                "--match",
                "retry",
                "--body",
                "Fixed.",
                "--resolve",
            ]
        )
        self.assertEqual(64, code)
        self.assertEqual([], self.docs)
        self.assertIn("OUT_OF_SCOPE", self.out.getvalue())

    def test_an_unreadable_origin_refuses_rather_than_assuming_scope(self) -> None:
        """An unverified scope is not a scope, and a check that cannot run reports itself."""
        self.wire(page([rthread("t1")]))
        with mock.patch.object(pr_review, "origin_owner", return_value=None):
            self.assertEqual(64, self.run_reply("--resolve"))
        self.assertEqual([], self.docs)

    def test_a_sibling_repository_under_the_same_owner_is_in_scope(self) -> None:
        """The fleet is one owner, and that is the case the maintainer works in daily."""
        self.wire(page([rthread("t1")]))
        code = pr_review.main(
            [
                "reply",
                "7",
                "--repo",
                "o/some-other-repo",
                "--match",
                "retry count",
                "--body",
                "Fixed in abc.",
                "--resolve",
            ]
        )
        self.assertEqual(0, code)


class TestReplyArguments(unittest.TestCase):
    def err(self, argv: list[str]) -> str:
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            pr_review.main(argv)
        return err.getvalue()

    def test_an_empty_body_is_rejected_rather_than_posted(self) -> None:
        """A thread resolved on an empty answer reads as addressed while carrying nothing."""
        for body in ("", "   "):
            with self.subTest(body=body):
                self.assertIn(
                    "--body",
                    self.err(["reply", "7", "--repo", "o/r", "--match", "x", "--body", body]),
                )

    def test_a_missing_match_is_rejected_rather_than_matching_everything(self) -> None:
        self.assertIn("--match", self.err(["reply", "7", "--repo", "o/r", "--body", "Fixed."]))

    def test_a_comment_requires_a_non_empty_body(self) -> None:
        for body in ("", "   "):
            with self.subTest(body=body):
                self.assertIn("--body", self.err(["comment", "7", "--repo", "o/r", "--body", body]))

    def test_reply_only_options_are_rejected_on_comment(self) -> None:
        for flag in (["--match", "x"], ["--resolve"], ["--path", "a.py"]):
            with self.subTest(flag=flag[0]):
                self.assertIn(
                    flag[0],
                    self.err(["comment", "7", "--repo", "o/r", "--body", "Fixed.", *flag]),
                )

    def test_a_writing_option_on_a_reading_command_is_an_error(self) -> None:
        """Silently ignored, it reads as an option that took effect on a run that wrote nothing."""
        for flag in (["--body", "Fixed."], ["--match", "x"], ["--resolve"], ["--path", "a.py"]):
            with self.subTest(flag=flag[0]):
                self.assertIn(flag[0], self.err(["status", "7", "--repo", "o/r", *flag]))


PIN = "actions/checkout@" + "9" * 40


def refs(body: str) -> tuple[list[str], list[str]]:
    return pr_review.body_references(body)


class TestBodyReferences(unittest.TestCase):
    """What a description is read as claiming, before anything is fetched.

    Over-reading is the failure that matters here, and the free SHA scan this replaced is the
    proof: over 25 merged pull requests it raised four findings and every one was correct prose.
    The cases below are those four shapes, each held to yielding nothing.
    """

    def test_an_action_pin_is_a_uses_ref_and_not_a_commit_of_this_repository(self) -> None:
        """The pin's SHA belongs to the action's repository, so reading it here reports it stale."""
        uses, shas = refs(f"Bumped to `uses: {PIN}` this round.")
        self.assertEqual([PIN], uses)
        self.assertEqual([], shas)

    def test_a_commit_a_verb_claims_this_branch_carries_is_read(self) -> None:
        for phrase in (
            "Fixed in `69688ec`",
            "landed in 69688ec",
            "Corrected by `69688ec`",
            "shipped as `69688ec`",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(["69688ec"], refs(f"{phrase} on this branch.")[1])

    def test_a_commit_stated_as_history_is_not_a_claim_about_this_branch(self) -> None:
        """PR 592's shape: a `develop` commit named as history, correct and not on this head."""
        self.assertEqual(
            [],
            refs(
                '`9d85941` merged "Reaching the Hub" whole, so the cluster '
                "is deleted rather than annotated."
            )[1],
        )

    def test_a_sha_inside_quoted_tool_output_is_not_a_claim(self) -> None:
        """PR 584's shape: a digest pasted to show what the tool prints."""
        self.assertEqual(
            [], refs("pr=108 head=9f56a472 rounds=1 review_on_head=yes threads=0 merge=CLEAN")[1]
        )

    def test_a_commit_in_another_repository_is_not_a_claim(self) -> None:
        """PR 571 and 568's shape, and neither carries a URL that would mark it as elsewhere."""
        for body in (
            "Read at Blog `main@2b132e4`. Verdict operational.",
            "Both are on Blog's ground-truth `main` (`2b132e4`), verified by reading it.",
            "`themes/README.md` records the upstream repository, commit `154d006e`.",
        ):
            with self.subTest(body=body):
                self.assertEqual([], refs(body)[1])

    def test_an_all_hex_english_word_is_not_read_as_a_commit(self) -> None:
        """The digit backstop, so a verb this list gains later cannot start reading prose."""
        for word in ("accede", "acceded", "defaced", "effaced"):
            with self.subTest(word=word):
                self.assertEqual([], refs(f"The clause was corrected as {word} above.")[1])

    def test_a_hex_run_shorter_than_gits_own_floor_is_not_a_commit(self) -> None:
        """Six is short enough to collide with an identifier, and git abbreviates to seven."""
        self.assertEqual([], refs("Fixed in `abc12` which is not a commit.")[1])

    def test_each_reference_is_reported_once_however_often_it_is_quoted(self) -> None:
        uses, shas = refs(
            f"Fixed in `69688ec`, again fixed in `69688ec`, and `uses: {PIN}` twice: `uses: {PIN}`."
        )
        self.assertEqual(["69688ec"], shas)
        self.assertEqual([PIN], uses)

    def test_a_description_naming_nothing_yields_nothing(self) -> None:
        self.assertEqual(([], []), refs("This widens a rule and adds a case for it."))


class ClaimsCase(unittest.TestCase):
    """Base for the `claims` path, with the pull request and every read replaced."""

    def setUp(self) -> None:
        self.compare: dict[str, tuple[int, str, str]] = {}
        self.carried: set[str] | None = set()

    def run_claims(self, body: str, head: str = HEAD) -> tuple[int, str]:
        def rest(path: str, jq: str | None = None) -> subprocess.CompletedProcess:
            rc, out, err = self.compare.get(path, (1, "", "gh: Not Found (HTTP 404)"))
            return subprocess.CompletedProcess([path], rc, out, err)

        with (
            mock.patch.object(pr_review, "gql", return_value={"headRefOid": head, "body": body}),
            mock.patch.object(pr_review, "gh_rest", side_effect=rest),
            mock.patch.object(pr_review, "head_carries", return_value=self.carried),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            code = pr_review.check_claims("ptr727", "ProjectTemplate", 7)
        return code, out.getvalue()

    def answer(self, sha: str, status: str, head: str = HEAD) -> None:
        self.compare[f"repos/ptr727/ProjectTemplate/compare/{sha}...{head}"] = (0, status, "")

    def unread(self, sha: str, head: str = HEAD) -> None:
        self.compare[f"repos/ptr727/ProjectTemplate/compare/{sha}...{head}"] = (
            1,
            "",
            "error connecting to api.github.com",
        )


class TestClaimsReadsCommits(ClaimsCase):
    def test_a_commit_the_head_descends_from_is_clean(self) -> None:
        """The count is asserted beside the verdict, since `stale=0` over nothing read is also 0."""
        for status in ("ahead", "identical"):
            with self.subTest(status=status):
                self.answer("69688ec", status)
                code, out = self.run_claims("Fixed in `69688ec`.")
                self.assertEqual(0, code)
                self.assertIn("commits=1 uses=0 stale=0 unread=0", out)

    def test_a_commit_the_repository_does_not_carry_is_stale(self) -> None:
        """The amended-away SHA: the body still names the commit the branch was rewritten off."""
        code, out = self.run_claims("Fixed in `deadbee1`.")
        self.assertEqual(70, code)
        self.assertIn("STALE COMMIT `deadbee1`", out)
        self.assertIn("carries no such commit", out)

    def test_a_commit_this_head_does_not_descend_from_is_stale_and_names_the_status(self) -> None:
        """A commit that exists and is not on this branch is the rebase case, not a missing one."""
        self.answer("dbd1cdc", "diverged")
        code, out = self.run_claims("Landed in `dbd1cdc`.")
        self.assertEqual(70, code)
        self.assertIn("does not descend from it (diverged)", out)

    def test_a_commit_github_did_not_answer_for_is_undecided_rather_than_stale(self) -> None:
        """A network failure reported as a stale description sends a reader to fix correct prose."""
        self.unread("69688ec")
        self.answer("a6d7a4b", "ahead")
        code, out = self.run_claims("Fixed in `69688ec`, then corrected in `a6d7a4b`.")
        self.assertEqual(0, code)
        self.assertIn("unread=1", out)
        self.assertIn("left undecided", out)

    def test_every_reference_undecided_reports_no_verdict_rather_than_a_clean_pass(self) -> None:
        """`stale=0` from a check that read nothing renders exactly like `stale=0` from one that did."""
        self.unread("69688ec")
        code, out = self.run_claims("Fixed in `69688ec`.")
        self.assertEqual(71, code)
        self.assertIn("NOTHING_WAS_READ", out)


class TestClaimsReadsUsesRefs(ClaimsCase):
    def test_a_ref_the_head_tree_carries_is_clean(self) -> None:
        self.carried = {PIN}
        code, out = self.run_claims(f"Pinned to `uses: {PIN}`.")
        self.assertEqual(0, code)
        self.assertIn("uses=1 stale=0", out)

    def test_a_ref_no_file_at_head_carries_is_stale(self) -> None:
        """The bump the branch reverted, with the description still quoting the pin it named."""
        self.carried = set()
        code, out = self.run_claims(f"Pinned to `uses: {PIN}`.")
        self.assertEqual(70, code)
        self.assertIn(f"STALE USES `{PIN}`", out)

    def test_an_unreadable_head_tree_is_undecided_rather_than_stale(self) -> None:
        self.carried = None
        code, out = self.run_claims(f"Pinned to `uses: {PIN}`.")
        self.assertEqual(71, code)
        self.assertIn("NOTHING_WAS_READ", out)

    def test_a_description_quoting_no_ref_reads_no_tree(self) -> None:
        """The archive is one request, and a body naming nothing has no reason to spend it."""
        with (
            mock.patch.object(
                pr_review, "gql", return_value={"headRefOid": HEAD, "body": "Prose only."}
            ),
            mock.patch.object(pr_review, "head_carries") as tree,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, pr_review.check_claims("ptr727", "ProjectTemplate", 7))
        tree.assert_not_called()

    def test_a_stale_ref_and_a_stale_commit_are_both_reported(self) -> None:
        """One failing reference does not end the read, since a body drifts in more than one place."""
        self.carried = set()
        code, out = self.run_claims(f"Fixed in `deadbee1`, pinned to `uses: {PIN}`.")
        self.assertEqual(70, code)
        self.assertIn("STALE COMMIT", out)
        self.assertIn("STALE USES", out)
        self.assertIn("stale=2", out)


class TestClaimsIsReadOnly(unittest.TestCase):
    def test_the_head_tree_read_asks_for_an_archive_and_nothing_else(self) -> None:
        source = (REPO / "scripts" / "pr_review.py").read_text(encoding="utf-8")
        self.assertIn("tarball/{head}", source)

    def test_an_unreadable_archive_reads_as_undecided_rather_than_as_an_empty_tree(self) -> None:
        """An empty tree carries no ref, so reading a failed download as one reports every ref stale."""
        for outcome in (subprocess.CompletedProcess([], 1, b"", b""),):
            with mock.patch.object(pr_review.subprocess, "run", return_value=outcome):
                self.assertIsNone(pr_review.head_carries("ptr727", "ProjectTemplate", HEAD, [PIN]))

    def test_gh_being_absent_leaves_the_tree_undecided_rather_than_aborting_claims(self) -> None:
        """This read cannot go through `gh_rest`, so it carries that helper's guards itself.

        Raising here would abort a run mid-way, where every other unreadable answer in this
        subcommand reports undecided and lets the caller see what was decided.
        """
        with mock.patch.object(pr_review.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(pr_review.head_carries("ptr727", "ProjectTemplate", HEAD, [PIN]))

    def test_a_hung_download_times_out_rather_than_hanging_the_run(self) -> None:
        with mock.patch.object(
            pr_review.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 1)
        ):
            self.assertIsNone(pr_review.head_carries("ptr727", "ProjectTemplate", HEAD, [PIN]))

    def test_the_archive_read_is_bounded_by_a_timeout(self) -> None:
        """An unbounded read of a whole repository is a wait with no end and no message."""
        with mock.patch.object(pr_review.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, b"", b"")
            pr_review.head_carries("ptr727", "ProjectTemplate", HEAD, [PIN])
        self.assertEqual(pr_review.TARBALL_TIMEOUT, run.call_args.kwargs["timeout"])

    def test_an_archive_that_is_not_a_readable_tarball_is_undecided(self) -> None:
        proc = subprocess.CompletedProcess([], 0, b"not a gzip stream", b"")
        with mock.patch.object(pr_review.subprocess, "run", return_value=proc):
            self.assertIsNone(pr_review.head_carries("ptr727", "ProjectTemplate", HEAD, [PIN]))

    def test_a_ref_is_matched_as_bytes_so_an_undecodable_file_is_still_searched(self) -> None:
        """Skipping a file this cannot decode is how a present ref reads as absent."""
        source = (REPO / "scripts" / "pr_review.py").read_text(encoding="utf-8")
        self.assertIn("n in blob", source)

    def test_an_absent_status_is_absence_and_a_rate_limit_is_not(self) -> None:
        """Reading a 403 as a missing commit reports a correct description as contradicting itself."""
        for status, absent in (
            ("404", True),
            ("422", True),
            ("403", False),
            ("401", False),
            ("500", False),
        ):
            with self.subTest(status=status):
                proc = subprocess.CompletedProcess([], 1, "", f"gh: (HTTP {status})")
                self.assertEqual(absent, pr_review.answered_absent(proc))

    def test_a_network_error_carrying_no_status_is_not_absence(self) -> None:
        proc = subprocess.CompletedProcess([], 1, "", "error connecting to api.github.com")
        self.assertFalse(pr_review.answered_absent(proc))

    def test_gh_being_absent_does_not_raise(self) -> None:
        with mock.patch.object(pr_review.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(1, pr_review.gh_rest("repos/o/r").returncode)


class TestContract(unittest.TestCase):
    def test_status_documents_its_no_review_success_case(self) -> None:
        self.assertIn("Exit 0 = no review covers the head yet", pr_review.__doc__ or "")

    def test_the_runbook_bootstraps_the_review_skill(self) -> None:
        """Copilot reaches the provider-independent review contract from its always-on file."""
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn(".github/skills/code-review/SKILL.md", text)

    def test_the_runbook_forbids_suppressed_findings(self) -> None:
        """A finding without a thread cannot participate in the ordinary reply loop."""
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("Never suppress a finding", text)

    def test_the_runbook_publishes_the_machine_readable_coverage_marker(self) -> None:
        """The instructed shape is stable while the parser retains legacy prose readers."""
        text = (REPO / ".github" / "skills" / "code-review" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        marker = "<!-- fleet-review: reviewed=N changed=N findings=N -->"
        self.assertIn(marker, text)
        self.assertIsNotNone(pr_review.read_coverage(marker.replace("N", "1")))

    def test_the_runbook_names_partial_coverage_as_a_state_that_blocks_a_merge(self) -> None:
        """A verify step reading `commit.oid` alone is what let five partial rounds merge."""
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("partial or absent coverage statement", text)

    def test_the_only_writes_are_the_four_named_here(self) -> None:
        """Every write this script makes is one of four, and each arrived as a reviewed change.

        The read subcommands are the bulk of it and a mutation reaching them is a digest that
        writes, so the whole-source guard stays and is narrowed to the documents the script
        actually needs rather than dropped when the first of them arrived. `requestReviews`
        closes the gap where `wait` only polled and never asked. `addComment` owns body-only
        finding responses. The reply and resolve pair owns inline threads. `union:true` only
        adds to the request set, so it cannot drop a requested human reviewer. The runbook keeps
        the `union:false` clear-and-recover form as a manual operation.
        """
        source = (REPO / "scripts" / "pr_review.py").read_text(encoding="utf-8")
        for verb in (
            "-X POST",
            "-X PATCH",
            "-X PUT",
            "-X DELETE",
            "--method",
            "gh pr merge",
            "gh pr review",
            "gh pr edit",
        ):
            with self.subTest(verb=verb):
                self.assertNotIn(
                    verb,
                    source,
                    f"{verb!r} is a state-changing call this script has no reason to make",
                )
        self.assertNotIn(
            "union:false",
            "".join(source.split()),
            "the additive form is the only one this script issues, since dropping "
            "a pending human reviewer is the runbook's manual recovery path, never "
            "an automatic one",
        )
        # `mutation(` opens a document, so the count is the number of documents.
        # A fifth arriving is a write nobody reviewed as one rather than a style drift.
        self.assertEqual(4, source.count("mutation("))
        self.assertIn("addComment", source)
        self.assertIn("addPullRequestReviewThreadReply", source)
        self.assertIn("resolveReviewThread", source)
        self.assertIn("requestReviews", source)

    def test_the_runbook_routes_mutations_to_the_script(self) -> None:
        """Provider mechanics have one executable owner instead of copied query snippets."""
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("scripts/pr_review.py", text)
        self.assertIn("`comment`", text)
        self.assertNotIn("mutation(", text)

    def test_the_fleet_routes_provider_writes_through_portable_tooling(self) -> None:
        """A connector-first write recreates the provider-specific 403 this change removes."""
        text = GOVERNANCE.read_text(encoding="utf-8")
        self.assertIn("Provider connectors are read-only for fleet work", text)
        self.assertIn("documented hub tool", text)
        self.assertIn("authenticated `gh`", text)

    def test_outward_facing_links_require_a_live_url(self) -> None:
        """A plausible review ID produced a valid-looking link to no review during this change."""
        text = GOVERNANCE.read_text(encoding="utf-8")
        self.assertIn("identifier embedded in outward-facing text", text)
        self.assertIn("complete URL from the live object", text)

    def test_no_argument_accepts_a_thread_id(self) -> None:
        """The failure is an id typed into a mutation, so the fix is having nowhere to type one.

        A parser that takes an id restores the failing shape however plainly the docs discourage
        it, which is the lesson the two prior instances taught: the rule was known and read.
        """
        source = (REPO / "scripts" / "pr_review.py").read_text(encoding="utf-8")
        # A node id literal anywhere in the source is an example a hand copies out of it.
        self.assertNotIn("PRRT_", source)
        parser_options = re.findall(r"add_argument\(\s*[\"'](--[a-z-]+)[\"']", source)
        # A floor, since a quoting change once emptied this scan and zero options read as a pass.
        self.assertGreaterEqual(len(parser_options), 5)
        for opt in parser_options:
            with self.subTest(option=opt):
                self.assertNotIn("id", opt.replace("--", "").split("-"))
        # The selector is the finding's text, and it is required rather than defaulted.
        self.assertIn('"--match"', source)

    def test_no_write_suppresses_or_forces_its_own_result(self) -> None:
        """A mutation whose output is discarded is a write nobody can say landed."""
        source = (REPO / "scripts" / "pr_review.py").read_text(encoding="utf-8")
        for tail in (">/dev/null", "2>/dev/null", "&>/dev/null", "|| true", "|| :", "shell=True"):
            with self.subTest(tail=tail):
                self.assertNotIn(tail, source)

    def test_the_guard_tests_the_window_the_queries_actually_read(self) -> None:
        """A guard measuring one number while the query fetches another reads clean on drift."""
        source = (REPO / "scripts" / "pr_review.py").read_text(encoding="utf-8")
        windows = set(re.findall(r"(?:comments|reviews)\(last:(\d+)\)", source))
        self.assertEqual({str(pr_review.WINDOW)}, windows)
        # The guard reads `hasPreviousPage`, so a connection that stops asking reports no.
        # That is the silent narrowing this holds every window against.
        # Four: reviews and comments, in each of the two queries.
        self.assertEqual(4, source.count("pageInfo{ hasPreviousPage }"))
        self.assertEqual(4, len(re.findall(r"(?:comments|reviews)\(last:\d+\)", source)))

    def test_a_negative_pickup_grace_is_rejected_rather_than_read_as_every_poll(self) -> None:
        """Compatibility accepts old callers without accepting a nonsensical negative value."""
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            pr_review.main(["wait", "7", "--repo", "o/r", "--pickup-grace", "-1"])
        # The repository is named, or this exits on the missing argument and proves nothing.
        self.assertIn("pickup-grace", err.getvalue())

    def test_a_negative_check_threshold_is_rejected_rather_than_firing_on_every_check(self) -> None:
        """Below zero, every check in that state reports stuck from the first read.

        A field that fires always is one a reader learns to skip, which costs the case it exists
        for, so the parser refuses it by name rather than rendering a digest nobody trusts.
        """
        for flag in ("--check-grace", "--check-stall"):
            with self.subTest(flag=flag):
                with (
                    contextlib.redirect_stderr(io.StringIO()) as err,
                    self.assertRaises(SystemExit),
                ):
                    pr_review.main(["wait", "7", "--repo", "o/r", flag, "-1"])
                self.assertIn(flag.lstrip("-"), err.getvalue())

    def test_the_documented_exit_codes_are_the_ones_the_code_returns(self) -> None:
        """The 44 docstring claimed a failed check without naming the BLOCKED the code requires.

        A failed *required* check does read BLOCKED, so the code was right and the doc was short
        of it, which is the direction that costs a reader trust in the field. Raised in review
        here. This holds the docstring to naming the condition rather than only the shapes.
        """
        doc = pr_review.__doc__ or ""
        self.assertIn("44 =", doc)
        self.assertIn("BLOCKED", doc)
        # Every shape the digest can print is named where the code can return 44 for it.
        for shape in ("queued", "never posted", "far past", "failed"):
            with self.subTest(shape=shape):
                self.assertIn(shape, doc)

    def test_the_check_thresholds_are_ordered_so_a_queue_reads_before_a_run(self) -> None:
        """A stall threshold under the grace would report a running job before a starved one.

        The two measure different things, and the grace is short because a pickup is fast while
        the stall is long because a build is not, so an inversion here inverts both readings.
        """
        self.assertLess(pr_review.CHECK_GRACE, pr_review.CHECK_STALL)

    def test_an_inverted_pair_of_check_thresholds_is_rejected_at_the_flags_too(self) -> None:
        """The case above held the constants ordered while the flags could still invert them.

        Asserting the defaults and leaving the inputs open is the gap between a rule and its
        check, one level down. Raised in review on this change.
        """
        for grace, stall in (("1800", "300"), ("600", "600")):
            with self.subTest(grace=grace, stall=stall):
                with (
                    contextlib.redirect_stderr(io.StringIO()) as err,
                    self.assertRaises(SystemExit),
                ):
                    pr_review.main(
                        [
                            "wait",
                            "7",
                            "--repo",
                            "o/r",
                            "--check-grace",
                            grace,
                            "--check-stall",
                            stall,
                        ]
                    )
                self.assertIn("check-grace", err.getvalue())

    def test_the_backoff_is_bounded_and_non_decreasing(self) -> None:
        """A wait that sleeps zero seconds is a busy loop, and one that shrinks polls harder later."""
        source = (REPO / "scripts" / "pr_review.py").read_text(encoding="utf-8")
        delays = [
            int(n) for n in source.split("delays = [")[1].split("]")[0].replace(" ", "").split(",")
        ]
        self.assertGreaterEqual(len(delays), 3)
        self.assertTrue(all(d > 0 for d in delays))
        self.assertEqual(delays, sorted(delays))


class TestHarness(unittest.TestCase):
    def test_this_module_collects_a_plausible_number_of_cases(self) -> None:
        """A module whose cases fail to load still reports OK, which is a pass proving nothing."""
        loaded = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertGreaterEqual(loaded.countTestCases(), 48)


if __name__ == "__main__":
    unittest.main(verbosity=2)
