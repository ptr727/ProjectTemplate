#!/usr/bin/env python3
"""Drive pr_review's digest and wait loop against responses they must read correctly.

The script exists to collapse a poll cycle into one invocation, so its failure mode is a wrong
answer rather than a crash: a review attributed to the wrong login, a review counted against a
stale head, or a wait that returns success while nothing landed. Each case below feeds a crafted
GraphQL payload and asserts the reading, with `gql` replaced so no case reaches the network.

Run under `uv run --no-project --with-requirements tests/requirements.txt`, as
`python tests/test_pr_review.py` or `python -m unittest discover -s tests`.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import inspect
import io
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_review
import pr_review_checks
import pr_review_rounds

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "spec"))
from git_env import without_git_location
from import_probe import import_delta

REPO = Path(__file__).resolve().parent.parent
# The gate and the reader modules it imports, which every whole-source guard below reads together.
GATE_SOURCES = ("pr_review.py", "pr_review_checks.py", "pr_review_rounds.py", "pr_review_digest.py")
# What a reader may import: the stdlib it parses with, the reviewer adapter, and the other readers.
READER_IMPORTS = frozenset(
    {
        "__future__",
        "collections",
        "datetime",
        "re",
        "pr_review_copilot",
        "pr_review_checks",
        "pr_review_rounds",
    }
)
# One import each reader module is known to make, the floor under the allowlist above.
READER_FLOORS = {
    "pr_review_checks": "datetime",
    "pr_review_rounds": "pr_review_copilot",
    "pr_review_digest": "pr_review_rounds",
}
# What a reader must never load, directly or through the adapter.
REACHING_OUT = ("pr_review", "gh_graphql", "subprocess")
RUNBOOK = REPO / ".github" / "copilot-instructions.md"
GOVERNANCE = REPO / "GOVERNANCE.md"
CODE_REVIEW_SKILL = REPO / ".github" / "skills" / "fleet-code-review" / "SKILL.md"
REVIEWER_REFERENCE = REPO / "docs" / "pr-reviewer-reference.md"


def flowed_text(text: str) -> str:
    """Collapse every whitespace run in `text` to one space, stripping the ends.

    A hard-wrapped body rewraps when a word is added earlier in the paragraph, so a sentence
    pinned against its raw text fails on an edit making no claim about it. Collapsing pins the
    words rather than the line breaks. An assertion whose subject is the layout itself reads
    raw instead, since collapsing would let a differently wrapped or indented line match.
    """
    return " ".join(text.split())


def comment_flowed_text(text: str) -> str:
    """Strip each line's leading `#` marker, then collapse as `flowed_text` does.

    A `#` comment that rewraps gains a marker on its new line as well as a newline, so
    collapsing whitespace alone leaves the marker between two words of a pinned phrase.
    """
    return flowed_text(" ".join(line.lstrip().removeprefix("#") for line in text.splitlines()))


HEAD = "a" * 40
OLD = "b" * 40


EARLY = "2026-08-02T10:00:00Z"
LATE = "2026-08-02T11:00:00Z"


# The version marker Copilot's current overview format opens its body with, which identifies it.
CCR_MARKER = "<!-- ccr-overview-v2 -->"
# A round in that format with no verdict, no count, and no statement of what it read.
OVERVIEW = f"{CCR_MARKER}\n\nThe change is narrow.\n"
# The machine-readable statement of a round that read every changed file.
COVERED = "<!-- fleet-review: reviewed=3 changed=3 findings=0 -->"
# The wall-clock ceiling the backtracking cases read, stated once so they cannot drift apart.
# Each case sizes its own input so that a regression finishes and trips this rather than running long enough to hang the suite.
# Three seconds against readings of microseconds and milliseconds, which is the margin that makes a loaded runner a non-issue rather than the clock being trusted.
BACKTRACK_BOUND = 3.0


def review(
    login: str = pr_review_rounds.REVIEWER,
    oid: str | None = HEAD,
    body: str = OVERVIEW + "\n<!-- fleet-review: reviewed=1 changed=1 findings=0 -->",
    at: str = EARLY,
    rid: str = "PRR_one",
) -> dict:
    return {
        "id": rid,
        "author": {"login": login},
        "state": "COMMENTED",
        "commit": {"oid": oid},
        "body": body,
        "submittedAt": at,
    }


def request_state(pending: bool = False, events: tuple[str, ...] = ()) -> dict:
    """What a review request leaves on a pull request, in the shape `REQUEST_STATE` reads it.

    `events` are the ids of review-request events naming the reviewer, oldest first. The request
    set carries the reviewer where `pending` is true.
    """
    bot = {"__typename": "Bot", "login": pr_review_rounds.REVIEWER}
    return {
        "reviewRequests": {"nodes": [{"requestedReviewer": bot}] if pending else []},
        "timelineItems": {"nodes": [{"id": e, "requestedReviewer": bot} for e in events]},
    }


RECORDED = (request_state(events=("RRE_1",)), request_state(True, ("RRE_1", "RRE_2")))


def answer_request(query: str, variables: dict, states: tuple[dict, dict]) -> dict | None:
    """The pre-request read and the mutation, answered from `states`, or None for any other document."""
    if "requestReviews" in query:
        return {"requestReviews": {"pullRequest": {"id": variables.get("pr"), **states[1]}}}
    if "REVIEW_REQUESTED_EVENT" in query:
        return {"repository": {"pullRequest": {"id": "PR_test", **states[0]}}}
    return None


def hist_review(number: int, body: str, at: str = LATE) -> dict:
    """One `pullRequests.nodes` entry in the shape `copilot_history` reads it: a pull request
    number paired with one reviewer review, for the repo-wide reading `wire_history` drives.

    `body` has no default, unlike `review()`'s, since every caller of this one cares which
    wording it carries: the whole point of the repo-wide reading is telling a quota refusal
    from a genuine round.
    """
    return {
        "number": number,
        "reviews": {
            "nodes": [
                {
                    "author": {
                        "__typename": "Bot",
                        "login": pr_review_rounds.REVIEWER,
                        "id": "BOT_1",
                    },
                    "state": "COMMENTED",
                    "body": body,
                    "submittedAt": at,
                }
            ]
        },
        "comments": {"nodes": []},
    }


def comment(
    login: str = pr_review_rounds.REVIEWER,
    at: str = LATE,
    body: str = "I have reached my quota limit and cannot review this now.",
) -> dict:
    return {"author": {"login": login}, "createdAt": at, "body": body}


def unplaced(finding: str = "The retry bound is off by one.") -> str:
    """A marked overview carrying a collapsed section the vocabulary has no spelling for."""
    return (
        f"{OVERVIEW}\n<details>\n<summary>Confidence notes (1)</summary>\n\n{finding}\n"
        f"</details>\n\n{COVERED}"
    )


def ack(fp: str, association: str, text: str | None = None) -> dict:
    """A conversation comment acknowledging the unplaced set under `fp`, by `association`."""
    written = f"Fixed in abc1234: the bound.\n\n{pr_review_rounds.ACK_MARKER.format(fp)}"
    return {
        "author": {"login": "maintainer"},
        "authorAssociation": association,
        "createdAt": LATE,
        "body": written if text is None else text,
    }


def overview_v2(
    open_count: str = "2 open findings",
    covers: str = "<!-- fleet-review: reviewed=1 changed=1 findings=2 -->",
    effort: str = "<sub>Review effort: Lite</sub>",
    entries: int = 2,
    verdict: str = "### Changes recommended",
    paths: tuple[str, ...] = ("a.py",),
) -> str:
    """A body in Copilot's current overview format, built from its own markers.

    Each open entry links its thread's first comment, as `#discussion_r<id>`, the ids running
    `400000000<n>`. The titles and paths are constructed rather than lifted from a live review,
    per GOVERNANCE.md "Representative Data in Agent-Authored Text".
    """
    listed = "\n".join(
        f"- [Finding {i}](#discussion_r400000000{i}) New" for i in range(1, entries + 1)
    )
    rows = "\n".join(f"| `{p}` | Narrows the reader. |" for p in paths)
    return (
        f"{CCR_MARKER}\n\n{verdict}\n\nThe change is narrow.\n\n"
        f"<details open>\n<summary><strong>{open_count}</strong></summary>\n\n"
        f"{listed}\n</details>\n\n"
        "<details>\n<summary><strong>What changed in this PR</strong></summary>\n\n"
        "This pull request narrows one reader.\n\n**Changes:**\n- Narrow the reader.\n\n"
        f"| File | Description |\n| ---- | ----------- |\n{rows}\n\n"
        f"{covers}\n</details>\n\n{effort}\n"
    )


def summarized(paths: list[str], covers: str = COVERED) -> str:
    """A round with no verdict or open section, carrying `covers` and a file table naming `paths`."""
    rows = "\n".join(f"| `{p}` | Prose about the change. |" for p in paths)
    return (
        f"{OVERVIEW}\n{covers}\n\n<details>\n<summary><strong>What changed in this PR</strong>"
        f"</summary>\n\n| File | Description |\n| ---- | ----------- |\n{rows}\n\n</details>\n"
    )


REFUSED = (
    "Copilot wasn't able to review this pull request because it exceeds the maximum "
    "number of files (300). Try reducing the number of changed files and requesting a "
    "review from Copilot again."
)

ERROR_REFUSED = (
    "Copilot encountered an error and was unable to review this pull request. You can try again "
    "by re-requesting a review."
)
RATE_LIMIT_LOG = (
    "2026-08-02T10:59:00.0000000Z Error creating PR review request: SessionModelError: You've "
    "reached your weekly rate limit. Please wait for your limit to reset on August 9, 2026 at "
    "12:00 AM or switch to auto model to continue. Learn More (https://docs.example.test).\n"
    "2026-08-02T10:59:00.0000000Z   errorType: 'rate_limit',\n"
)

TRANSIENT_LOG = (
    "2026-08-02T10:59:00.0000000Z HttpError: The request could not be processed because too "
    "many files changed - https://docs.example.test/pulls: Failed to fetch PR metadata for o/r#7\n"
)
TRANSIENT_LINE = TRANSIENT_LOG.strip()
SENT_AFTER = {
    "reviewRequests": {"nodes": []},
    "timelineItems": {
        "nodes": [
            {
                "id": "RRE_1",
                "createdAt": "2026-08-02T10:30:00Z",
                "requestedReviewer": {"__typename": "Bot", "login": pr_review_rounds.REVIEWER},
            }
        ]
    },
}

# Quoted from the corpus rather than invented: the body a refused review carried, byte for byte.
QUOTA_REFUSED = (
    "Copilot was unable to review this pull request because the user who requested the "
    "review has reached their quota limit."
)

# Quoted from the corpus rather than invented: CodeRabbit's own comment, trimmed to the marker and its opening line.
# The rest is CodeRabbit's ordinary walkthrough prose.
RATE_LIMITED_COMMENT = (
    "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n\n"
    "> [!WARNING]\n> ## Review limit reached\n"
)

RATE_LIMITED_REPLY_COMMENT = (
    "<!-- This is an auto-generated reply by CodeRabbit -->\n"
    "<!-- CodeRabbit review command invocation: v2:0123abcd -->\n"
    "<details>\n<summary>\u26a0\ufe0f Action not completed</summary>\n\n"
    "Review rate limited.\n</details>\n"
)


def thread(
    tid: str,
    resolved: bool = False,
    login: str = pr_review_rounds.REVIEWER,
    body: str = "A finding.",
    path: str = "a.py",
    line: int = 1,
    rid: str | None = None,
    cid: str | None = None,
) -> dict:
    """One `reviewThreads` node. `rid` is the review that opened it, which the manifest counts.

    `cid` is its first comment's database id, which an overview entry's `#discussion_r` anchor
    links, and None leaves the thread carrying none, as every case not reading those entries wants.

    None leaves the thread naming no round, which is the shape the reader has to survive rather
    than a claim about when GitHub returns it, and it is what every case not exercising the
    manifest wants. A thread belonging to a different round is the realistic exclusion, and a case
    covers that one separately.
    """
    return {
        "id": tid,
        "isResolved": resolved,
        "comments": {
            "nodes": [
                {
                    "author": {"login": login},
                    "path": path,
                    "line": line,
                    "body": body,
                    "pullRequestReview": {"id": rid} if rid else None,
                    **({"fullDatabaseId": cid} if cid else {}),
                }
            ]
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
    suite: int = 1,
    workflow: int | None = None,
    slug: str = "github-actions",
    app_id: int = 15368,
    suite_status: str = "COMPLETED",
    run: int | None = None,
) -> dict:
    """One CheckRun rollup node, in the shape the live rollup returns.

    The run's `databaseId` is carried only where a case names one.

    The starved case this exists for was read off run 31118675530 in this repository: an aggregator
    job dispatched the instant its dependency finished, left `QUEUED` with an empty runner name and
    a `startedAt` stamped equal to its creation, which sat fifteen minutes and never ran. Its
    healthy sibling on the next run wore `IN_PROGRESS` with a runner inside two minutes.
    """
    workflow_run: dict | None = None
    if workflow is not None:
        workflow_run = {"workflow": {"databaseId": workflow}}
        if run is not None:
            workflow_run["databaseId"] = run
    check_suite: dict = {
        "databaseId": suite,
        "app": {"slug": slug, "databaseId": app_id},
        "workflowRun": workflow_run,
        "status": suite_status,
    }
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": ago(60) if started is None else started,
        "checkSuite": check_suite,
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
    more_threads: bool = False,
) -> dict:
    requested = (
        [{"requestedReviewer": {"__typename": "Bot", "login": pr_review_rounds.REVIEWER}}]
        if pending
        else []
    )
    return {
        "id": "PR_test",
        "headRefOid": HEAD,
        "baseRefName": "develop",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": merge,
        # The diff's own file list, which the round's file table is compared against.
        # A case naming none gets the empty connection a pull request of no files carries.
        "files": {
            "nodes": [{"path": p} for p in files or []],
            "pageInfo": {"hasNextPage": more_files},
        },
        "reviews": {"nodes": reviews, "pageInfo": {"hasPreviousPage": older_reviews}},
        "reviewThreads": {
            "nodes": threads or [],
            "pageInfo": {"hasNextPage": more_threads},
        },
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

    def setUp(self) -> None:
        """Patch `gh_rest` to a read that did not happen, for every case rather than every caller.

        Patched inside `answer` instead, a case building its own payload and never calling it
        could spawn a real `gh` against a pull request that does not exist, which is what this
        suite exists not to do.

        The re-read schedule is emptied too, so a case answering a failed read is not slept on or
        asked again. A case about the re-read sets its own schedule.
        """
        super().setUp()
        # These readers are memoized for the run, and the cases share their commit constants.
        pr_review.run_cause.cache_clear()
        self.addCleanup(pr_review.run_cause.cache_clear)
        pr_review.required_contexts.cache_clear()
        self.addCleanup(pr_review.required_contexts.cache_clear)
        self.enterContext(
            mock.patch.object(
                pr_review,
                "gh_rest",
                return_value=subprocess.CompletedProcess([], 1, "", "no read in this suite"),
            )
        )
        self.enterContext(mock.patch.object(pr_review, "READ_RETRY_DELAYS", ()))

    def answer(self, *responses: dict) -> mock.MagicMock:
        """Patch `gql` to return each response in turn, repeating the last one.

        Also patches `gh_graphql` directly with a default reporting no Copilot review anywhere
        to read a bot id from, so `wait`'s auto-request finds nothing to request and falls
        straight through to polling, same as a repository with no Copilot history. `gql` itself
        never reaches the real `gh_graphql` since the mock above replaces it wholesale, so the
        two patches govern disjoint call sites and neither can shadow the other. A case
        exercising the auto-request itself re-patches `gh_graphql` after calling this.

        `origin_owner` is patched to this suite's own "o" too, since `wait` now runs the same
        in-scope check `comment` and `reply` already did, and this suite's cases run against a
        real checkout whose actual origin has nothing to do with the "o/r" they exercise.

        `gh_rest` is patched in `setUp` rather than here, so a case that never calls this still
        makes no REST read.

        `page_files` is patched to leave each response as it stands, since a response here is
        the whole read, a list marked cut short included. A case about paging patches
        `gh_graphql` itself rather than calling this.
        """
        queue = list(responses)
        self.enterContext(mock.patch.object(pr_review, "page_files"))

        def fake(_query, _owner, _repo, _num):
            return queue.pop(0) if len(queue) > 1 else queue[0]

        patched = self.enterContext(mock.patch.object(pr_review, "gql", side_effect=fake))
        self.enterContext(mock.patch.object(pr_review, "origin_owner", return_value="o"))
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
                self.assertEqual((want, None, []), pr_review.live_state("o", "r", 1))

    def test_a_null_author_or_commit_does_not_raise(self) -> None:
        """GraphQL returns null for a deleted account, and a crash there stalls the whole wait."""
        self.answer(payload([{"author": None, "state": "COMMENTED", "commit": None}]))
        self.assertEqual((False, None, []), pr_review.live_state("o", "r", 1))

    def test_a_poll_reads_the_same_three_readings_its_payload_gives(self) -> None:
        for label, pr in (
            ("on head", payload([review()])),
            ("stale", payload([review(oid=OLD)])),
            ("answered outside", payload([review(oid=OLD, at=EARLY)], comments=[comment(at=LATE)])),
        ):
            with self.subTest(case=label):
                self.answer(pr)
                self.assertEqual(pr_review.liveness(pr), pr_review.live_state("o", "r", 1))


class TestAnsweredOutsideReview(unittest.TestCase):
    """A refusal answers the request without covering the head, so a wait cannot read it as pending."""

    def test_a_reviewer_comment_newer_than_every_review_is_the_answer(self) -> None:
        answer = pr_review_rounds.answered_outside_review(
            payload([review(oid=OLD, at=EARLY)], comments=[comment(at=LATE)])
        )
        self.assertIsNotNone(answer)
        self.assertEqual(LATE, (answer or {}).get("createdAt"))

    def test_an_answer_the_reviewer_then_superseded_is_spent(self) -> None:
        """The review it preceded did land, so the comment is history rather than a stop signal."""
        self.assertIsNone(
            pr_review_rounds.answered_outside_review(
                payload([review(at=LATE)], comments=[comment(at=EARLY)])
            )
        )

    def test_another_account_s_comment_is_not_the_reviewer_answering(self) -> None:
        """A maintainer note and a codecov post both postdate the review and mean nothing here."""
        for login in ("ptr727", "codecov[bot]", "copilot-swe-agent"):
            with self.subTest(login=login):
                self.assertIsNone(
                    pr_review_rounds.answered_outside_review(
                        payload([review(oid=OLD)], comments=[comment(login=login)])
                    )
                )

    def test_no_comments_at_all_reads_as_no_answer(self) -> None:
        self.assertIsNone(pr_review_rounds.answered_outside_review(payload([review(oid=OLD)])))

    def test_ordinary_discussion_does_not_push_the_answer_out_of_the_window(self) -> None:
        """The window reads the newest comments, not the reviewer's, so others crowd it."""
        chatter = [comment(login="ptr727", at=LATE) for _ in range(pr_review.WINDOW - 1)]
        found = pr_review_rounds.answered_outside_review(
            payload([review(oid=OLD, at=EARLY)], comments=[comment(at=LATE)] + chatter)
        )
        self.assertIsNotNone(found)

    def test_comments_behind_the_window_are_unknown_rather_than_no_answer(self) -> None:
        """Finding nothing and having nothing to find are one reading once an answer can hide."""
        full = [comment(login="ptr727") for _ in range(pr_review.WINDOW)]
        self.assertTrue(
            pr_review_rounds.window_blind(
                payload([review()], comments=full, older=True), "comments"
            )
        )

    def test_a_window_holding_every_comment_is_not_a_gap(self) -> None:
        """A full window and a window holding the lot are the same length, so length cannot say."""
        full = [comment(login="ptr727") for _ in range(pr_review.WINDOW)]
        self.assertFalse(
            pr_review_rounds.window_blind(
                payload([review()], comments=full, older=False), "comments"
            )
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
        self.assertTrue(pr_review_rounds.window_blind(pr, "reviews"))
        self.assertIsNone(pr_review_rounds.answered_outside_review(pr))

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
        self.assertFalse(pr_review_rounds.window_blind(pr, "reviews"))
        self.assertIsNotNone(pr_review_rounds.answered_outside_review(pr))

    def test_one_spent_reviewer_comment_in_view_settles_the_question(self) -> None:
        """Comments arrive in creation order, so a hidden one is older than the spent one in view."""
        full = [comment(at=EARLY)] + [comment(login="ptr727") for _ in range(pr_review.WINDOW - 1)]
        pr = payload([review(at=LATE)], comments=full, older=True)
        self.assertIsNone(pr_review_rounds.answered_outside_review(pr))
        self.assertFalse(pr_review_rounds.window_blind(pr, "comments"))

    def test_threads_truncated_reads_the_review_threads_page_info(self) -> None:
        """Unlike `window_blind`'s `last`-windowed connections, `reviewThreads` reads forward
        from the first page, so `hasNextPage` alone says a page was cut, with nothing in view
        to settle which end."""
        self.assertTrue(pr_review_rounds.threads_truncated(payload([review()], more_threads=True)))
        self.assertFalse(
            pr_review_rounds.threads_truncated(payload([review()], more_threads=False))
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

    def test_a_truncated_thread_page_marks_the_summary_and_names_the_gap(self) -> None:
        """`reviewThreads(first:100)` carries no cursor, so a pull request with more than
        that many threads must say so rather than letting `threads=`/`unresolved=` undercount
        silently. The connection reads oldest-first, so what is cut is the newest threads."""
        self.answer(payload([review()], [thread("T1")], more_threads=True))
        out, unresolved = pr_review.digest("o", "r", 7)
        self.assertEqual(1, unresolved)
        self.assertIn("threads=1+ unresolved=1+", out)
        self.assertIn("THREADS TRUNCATED", out)

    def test_an_untruncated_thread_page_carries_no_marker_or_block(self) -> None:
        self.answer(payload([review()], [thread("T1")], more_threads=False))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("threads=1 unresolved=1", out)
        self.assertNotIn("THREADS TRUNCATED", out)

    def test_review_on_head_reports_no_when_every_round_is_stale(self) -> None:
        """`NO` is upper-case on purpose, so the one state that blocks a merge is not skimmed past."""
        self.answer(payload([review(oid=OLD)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("review_on_head=NO", out)

    def test_digest_reports_effective_effort_without_changing_the_verdict(self) -> None:
        self.answer(
            payload(
                [
                    review(
                        body=f"{OVERVIEW}\n{COVERED}\n\n<sub>Review effort: Default (Balanced)</sub>"
                    )
                ]
            )
        )
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("effort=balanced effort_source=default", out)
        self.assertIn("coverage=full", out)

    def test_a_thread_from_a_deleted_account_does_not_crash_the_digest(self) -> None:
        """GraphQL sends `author` present and null, which a defaulted lookup returns as None.

        The thread still blocks the merge, so it counts, attributed as `other`.
        """
        orphan = thread("T1")
        orphan["comments"]["nodes"][0]["author"] = None
        self.answer(payload([review()], [orphan, thread("T2")]))
        out, unresolved = pr_review.digest("o", "r", 7)
        self.assertEqual(2, unresolved)
        self.assertIn(f"unresolved=2 ({pr_review_rounds.REVIEWER}=1 other=1)", out)
        self.assertIn("T1", out)
        self.assertIn("T2", out)

    def test_every_open_thread_is_listed_whoever_opened_it(self) -> None:
        """An open thread blocks a ruleset-gated merge whatever its author."""
        self.answer(payload([review()], [thread("T1", login="ptr727"), thread("T2")]))
        out, unresolved = pr_review.digest("o", "r", 7)
        self.assertEqual(2, unresolved)
        self.assertIn("T1", out)
        self.assertIn("T2", out)

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


CROWDED_DIGEST = "\n".join(
    (
        (
            "repo=o/r pr=7 head=aaaaaaaa rounds=2 review_on_head=yes effort=unknown "
            "effort_source=unknown coverage=full shapes=ok refusal=no threads=2+ unresolved=2+ "
            f"({pr_review_rounds.REVIEWER}=1 other=1) suppressed=0+ (on_head=0 earlier=0) "
            "previously_missed=0+ (on_head=0 earlier=0) overview=?/0 cr_outside_diff=0+ "
            "(on_head=0 earlier=0) qodo_open=unknown answered_outside_review=unknown requested=no "
            "merge=BLOCKED checks=0/1 stuck=FAILED new=2"
        ),
        (
            "  CHECK FAILED ('Check pull request workflow status job', COMPLETED/FAILURE): a verdict "
            "rather than a stuck check, so read the run and fix what failed, since no wait and no "
            "re-run clears a real failure"
        ),
        (
            "  BEHIND THE WINDOW (comments): the newest 100 carry none from the reviewer and older "
            "ones exist, so this cannot decide"
        ),
        (
            "  THREADS TRUNCATED: this pull request carries more review threads than the 100 read "
            "here, and the connection reads oldest-first, so the ones cut off are the newest rather "
            "than the oldest, exactly the ones most likely to still be open. `threads=` and "
            "`unresolved=` above undercount. Read the rest with `reply`'s own paginated walk before "
            "trusting either number"
        ),
        (
            "  REVIEWS TRUNCATED: this pull request carries more reviews than the 100 read here, and "
            "the connection reads newest-first, so the ones cut off are the oldest, exactly where an "
            "unanswered suppressed or outside-diff-range finding is most likely to still sit "
            "unresolved. `suppressed=` and `cr_outside_diff=` above undercount"
        ),
        "  NEW T1 a.py:1 First finding.",
        "  NEW T2 a.py:1 A finding.",
    )
)
QUIET_DIGEST = (
    "repo=o/r pr=7 head=aaaaaaaa rounds=1 review_on_head=NO effort=unknown effort_source=unknown "
    "coverage=none shapes=ok refusal=no threads=0 unresolved=0 suppressed=0 (on_head=0 earlier=0) "
    "answered_outside_review=no requested=yes merge=CLEAN checks=1/1"
)


class TestDigestRenderIsPinned(GqlCase):
    """The whole digest, byte for byte, as `pr_review_digest.render` lays it out.

    Each expected text was captured before the rendering moved to the leaf module.
    A reorder among the fields and lines these two cases print fails here rather than in a parse.
    """

    def test_a_crowded_digest_keeps_its_field_and_advisory_order(self) -> None:
        self.answer(
            payload(
                [review(), review(oid=OLD, rid="PRR_two")],
                [thread("T1", body="First finding."), thread("T2", login="ptr727")],
                merge="BLOCKED",
                comments=[comment(login="ptr727") for _ in range(pr_review.WINDOW)],
                older=True,
                older_reviews=True,
                checks=[check(conclusion="FAILURE")],
                more_threads=True,
            )
        )
        out, unresolved = pr_review.digest("o", "r", 7, set(), now=NOW)
        self.assertEqual(2, unresolved)
        self.assertEqual(CROWDED_DIGEST, out)

    def test_a_quiet_digest_leaves_out_every_optional_field(self) -> None:
        self.answer(payload([review(oid=OLD)], checks=[check()], pending=True))
        out, unresolved = pr_review.digest("o", "r", 7, now=NOW)
        self.assertEqual(0, unresolved)
        self.assertEqual(QUIET_DIGEST, out)


class TestOtherReviewers(GqlCase):
    """Thread resolution, head-presence, and the rate-limit marker's own structural pattern,
    generalized past Copilot to the other review bots this repository has trialed. Free-text
    prose parsing for CodeRabbit's outside-diff findings and Qodo's comment-only findings lives
    in `TestCodeRabbitOutsideDiff` and `TestQodoOpenFindings` below, not here.

    `status`'s `unresolved=0` used to hide a CodeRabbit/qodo thread that still blocked a
    ruleset-gated merge, first since only Copilot's own threads counted and then since only known
    reviewer logins did, so every open thread now counts and a login only attributes it. Coverage
    and refusal reading stay Copilot-only: `review_on_head` above names Copilot's own coverage
    specifically, the reviewer this script requests and waits for, not "no review of any kind
    covers this head".
    """

    def other_review(self, login: str, oid: str = HEAD, body: str = "") -> dict:
        """A review node for a tracked non-Copilot reviewer, `body` empty unless a case needs it."""
        return {
            "author": {"login": login},
            "state": "COMMENTED",
            "commit": {"oid": oid},
            "body": body,
            "submittedAt": LATE,
        }

    def test_a_coderabbit_and_a_qodo_thread_both_count_toward_unresolved(self) -> None:
        self.answer(
            payload(
                [review()],
                [
                    thread("T1", login="coderabbitai"),
                    thread("T2", login="qodo-free-for-open-source-projects"),
                    thread("T3", resolved=True, login="coderabbitai"),
                ],
            )
        )
        out, unresolved = pr_review.digest("o", "r", 7)
        self.assertEqual(2, unresolved)
        self.assertIn("unresolved=2", out)
        self.assertIn("T1", out)
        self.assertIn("T2", out)
        self.assertNotIn("T3", out)

    def test_a_thread_from_an_unknown_login_counts_and_is_named_other(self) -> None:
        """A reviewer posting under a login missing from the known set once read `unresolved=0`.

        The known logins attribute a thread and never decide whether it counts, and `other` is
        named even where it is the only contributor, since it is the count a reader cannot assume.
        """
        self.answer(payload([review()], [thread("T1", login="example-review-bot")]))
        out, unresolved = pr_review.digest("o", "r", 7)
        self.assertEqual(1, unresolved)
        self.assertIn("unresolved=1 (other=1)", out)
        self.assertIn("T1", out)

    def test_status_and_wait_print_an_unknown_login_s_thread_in_the_digest(self) -> None:
        """`status` and `wait` print the digest a Merge Gate decision is read from."""
        unknown = thread("T1", login="example-review-bot")
        for command in (["status", "7"], ["wait", "7"]):
            with self.subTest(command=command[0]):
                self.answer(payload([review()], [unknown]))
                out = io.StringIO()
                with (
                    contextlib.redirect_stdout(out),
                    mock.patch.object(pr_review.time, "sleep"),
                ):
                    pr_review.main([*command, "--repo", "o/r"])
                self.assertIn("unresolved=1 (other=1)", out.getvalue())
                self.assertIn("T1", out.getvalue())

    def test_one_known_reviewer_alone_prints_no_breakdown_and_two_print_one(self) -> None:
        self.answer(payload([review()], [thread("T1"), thread("T2")]))
        out, unresolved = pr_review.digest("o", "r", 7)
        self.assertEqual(2, unresolved)
        self.assertIn("unresolved=2 ", out)
        self.assertNotIn("unresolved=2 (", out)

        self.answer(payload([review()], [thread("T1"), thread("T2", login="coderabbitai")]))
        out, unresolved = pr_review.digest("o", "r", 7)
        self.assertEqual(2, unresolved)
        self.assertIn(f"unresolved=2 ({pr_review_rounds.REVIEWER}=1 coderabbitai=1)", out)

    def test_other_reviewed_names_only_the_bots_that_posted_on_this_exact_head(self) -> None:
        self.answer(
            payload(
                [
                    review(),
                    self.other_review("coderabbitai"),
                    self.other_review(pr_review_rounds.QODO_LOGIN, oid=OLD),
                ]
            )
        )
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("other_reviewed=coderabbitai", out)
        self.assertNotIn(pr_review_rounds.QODO_LOGIN, out)

    def test_other_reviewed_is_absent_where_neither_has_posted(self) -> None:
        """The common case, a repository not trialing either, stays silent rather than `none`."""
        self.answer(payload([review()]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertNotIn("other_reviewed", out)

    def test_reviewer_nodes_defaults_to_copilot_but_reads_any_login_explicitly(self) -> None:
        pr = payload([review(), self.other_review("coderabbitai")])
        self.assertEqual(1, len(pr_review_rounds.reviewer_nodes(pr, "reviews")))
        self.assertEqual(1, len(pr_review_rounds.reviewer_nodes(pr, "reviews", "coderabbitai")))
        self.assertEqual(
            0, len(pr_review_rounds.reviewer_nodes(pr, "reviews", pr_review_rounds.QODO_LOGIN))
        )

    def test_other_rate_limited_reads_the_structural_marker_on_a_plain_comment(self) -> None:
        """The one observed shape: a rate-limit notice riding a PR comment, not a formal review."""
        self.answer(
            payload([review()], comments=[comment(login="coderabbitai", body=RATE_LIMITED_COMMENT)])
        )
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("other_rate_limited=coderabbitai:coderabbit.ai", out)

    def test_other_rate_limited_clears_once_a_later_comment_supersedes_it(self) -> None:
        self.answer(
            payload(
                [review()],
                comments=[
                    comment(login="coderabbitai", body=RATE_LIMITED_COMMENT, at=EARLY),
                    comment(login="coderabbitai", body="Ordinary walkthrough prose.", at=LATE),
                ],
            )
        )
        out, _ = pr_review.digest("o", "r", 7)
        self.assertNotIn("other_rate_limited", out)

    def test_other_rate_limited_is_absent_where_no_marker_is_present(self) -> None:
        """The common case, neither is rate-limited right now, stays silent rather than `none`."""
        self.answer(payload([review()], comments=[comment(login="coderabbitai", body="Hi.")]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertNotIn("other_rate_limited", out)

    def test_rate_limited_by_reads_either_connection_and_names_no_untracked_login(self) -> None:
        pr = payload(
            [review()], comments=[comment(login="coderabbitai", body=RATE_LIMITED_COMMENT)]
        )
        self.assertEqual("coderabbit.ai", pr_review_rounds.rate_limited_by(pr, "coderabbitai"))
        self.assertIsNone(pr_review_rounds.rate_limited_by(pr, pr_review_rounds.QODO_LOGIN))

    def test_other_rate_limited_reads_the_command_reply_shape(self) -> None:
        """The command reply's marker says CodeRabbit rather than the service, so the login names it."""
        self.answer(
            payload(
                [review()],
                comments=[comment(login="coderabbitai", body=RATE_LIMITED_REPLY_COMMENT)],
            )
        )
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("other_rate_limited=coderabbitai:coderabbit.ai", out)

    def test_rate_limited_by_reads_both_shapes_and_not_an_ordinary_comment(self) -> None:
        """Constructed bodies, the reply shape's invocation id made up and its glyph escaped.

        The last two are near misses. One is the reply marker over another block, and the other
        is the block's own words with no reply marker over them.
        """
        other_reply = (
            "<!-- This is an auto-generated reply by CodeRabbit -->\n"
            "<details>\n<summary>Action not completed</summary>\n\nSomething else.\n"
        )
        unmarked = "<details>\n<summary>Action not completed</summary>\n\nReview rate limited.\n"
        summary = "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
        for body, expected in (
            (RATE_LIMITED_REPLY_COMMENT, "coderabbit.ai"),
            (RATE_LIMITED_COMMENT, "coderabbit.ai"),
            (summary, None),
            (other_reply, None),
            (unmarked, None),
        ):
            with self.subTest(body=body):
                pr = payload([review()], comments=[comment(login="coderabbitai", body=body)])
                self.assertEqual(expected, pr_review_rounds.rate_limited_by(pr, "coderabbitai"))

    def test_rate_limited_by_leaves_the_reply_shape_unread_for_an_unmapped_login(self) -> None:
        pr = payload(
            [review()],
            comments=[comment(login=pr_review_rounds.QODO_LOGIN, body=RATE_LIMITED_REPLY_COMMENT)],
        )
        self.assertIsNone(pr_review_rounds.rate_limited_by(pr, pr_review_rounds.QODO_LOGIN))

    def test_rate_limited_by_reads_the_reviews_connection_too(self) -> None:
        """Both observed shapes ride a plain comment, but the reader checks both connections,
        since a future bot's own marker might ride a formal review."""
        pr = payload(
            [review(), self.other_review("coderabbitai", body=RATE_LIMITED_COMMENT)],
            comments=[],
        )
        self.assertEqual("coderabbit.ai", pr_review_rounds.rate_limited_by(pr, "coderabbitai"))
        self.assertIsNone(pr_review_rounds.rate_limited_by(pr, pr_review_rounds.QODO_LOGIN))

    def test_thread_author_defaults_a_deleted_account_rather_than_crashing(self) -> None:
        orphan = thread("T1")
        orphan["comments"]["nodes"][0]["author"] = None
        self.assertEqual("", pr_review_rounds.thread_author(orphan))
        self.assertEqual(pr_review_rounds.REVIEWER, pr_review_rounds.thread_author(thread("T2")))


# Trimmed from CodeRabbit's own review body, one file and one finding rather than the two files the live round carried.
# Its warning emoji is dropped, since this file's own charset rule keeps its literal source ASCII.
# The leading blockquote (`>` per line) and the file-level `<details>` nesting are both kept, since both are what the reader has to see through.
# CodeRabbit wraps its own outside-diff section in a blockquote, and the section nests one file-level wrapper deep before the finding.
def cr_outside_diff_body(count: int = 1, path: str = "a.py", finding: str = "Off by one.") -> str:
    return (
        f"{OVERVIEW}\n"
        "> [!CAUTION]\n"
        "> Some comments are outside the diff and can't be posted inline due to platform "
        "limitations.\n"
        "> \n"
        "> <details>\n"
        f"> <summary>Outside diff range comments ({count})</summary><blockquote>\n"
        "> \n"
        "> <details>\n"
        f"> <summary>{path} (1)</summary><blockquote>\n"
        "> \n"
        f"> `12-14`: **{finding}**\n"
        "> \n"
        "> </blockquote></details>\n"
        "> \n"
        "> </blockquote></details>\n"
    )


# Trimmed from Qodo's own "Code Review by Qodo" comment, one numbered finding rather than the two the live comment carried.
# Its nested Description/Code/Relevance/Evidence/Agent-prompt sub-summaries are kept, since those are exactly what a naive `<summary>` scan would miscount as findings of their own.
# The badge's check mark is escaped (U+2713) rather than typed literally, keeping this file's own literal source inside the ASCII charset rule that governs the repository.
def qodo_review_body(resolved: bool = False, heading: str = "Bad naming") -> str:
    badge = " <code>\u2713 Resolved</code>" if resolved else ""
    label = f"<s>{heading}</s>" if resolved else heading
    return (
        "<h3>Code Review by Qodo</h3>\n\n"
        "<code>Bugs (1)</code>\n\n"
        "<details>\n"
        f"<summary>  1.  {label}{badge} <code>Bug</code></summary>\n\n"
        "<br/>\n\n"
        "> <details open>\n><summary>Description</summary>\n><br/>\n>\n><pre>\n"
        ">The name does not describe what the function does.\n></pre>\n></details>\n\n"
        "> <details>\n><summary>Code</summary>\n><br/>\n>\n"
        "><code>a.py[12]</code>\n></details>\n\n"
        "> <details>\n><summary>Relevance</summary>\n><br/>\n>\n"
        "><pre>Recent history accepts naming corrections.</pre>\n></details>\n\n"
        "<hr/>\n</details>\n"
    )


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
        self.assertFalse(pr_review_rounds.reviewed_head(payload([review(body=REFUSED)])))

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
                self.assertEqual(body, pr_review_rounds.refusal_of({"body": body}))

    def test_a_clean_pass_and_an_ordinary_review_are_not_refusals(self) -> None:
        for body in (
            "Reviewed 3 of 3 changed files and generated no comments.",
            overview_v2(),
            summarized(["a.py"]),
            "",
        ):
            with self.subTest(body=body[:40]):
                self.assertEqual("", pr_review_rounds.refusal_of({"body": body}))

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
        self.assertEqual("", pr_review_rounds.refusal_of({"body": body}))
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
        self.assertTrue(pr_review_rounds.reviewed_head(pr))
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


class TestQuotaRefusal(GqlCase):
    """The refusal wording naming the account quota, distinct from any other refusal cause.

    A file-count refusal is cleared by splitting the pull request. A quota one is an
    account-level state that neither a re-request nor a further wait touches. Folding the two
    into one field or one exit code sends a reader at a remedy that does nothing for the quota
    case, which is why each gets its own reading below.
    """

    def test_the_field_and_exit_code_are_distinct_from_a_generic_refusal(self) -> None:
        self.answer(payload([review(body=QUOTA_REFUSED)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("refusal=QUOTA", out)
        self.assertNotIn("refusal=YES", out)
        with (
            contextlib.redirect_stdout(io.StringIO()) as captured,
            mock.patch.object(pr_review.time, "sleep"),
        ):
            self.assertEqual(41, pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"]))
        self.assertIn("status=COPILOT_QUOTA_EXHAUSTED", captured.getvalue())
        self.assertNotIn("status=REVIEW_IS_A_REFUSAL", captured.getvalue())

    def test_a_file_count_refusal_still_reads_as_the_generic_shape(self) -> None:
        """A regression guard: the new field must not swallow the cause it already had a code for."""
        self.assertFalse(pr_review_rounds.quota_refusal({"body": REFUSED}))
        self.answer(payload([review(body=REFUSED)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("refusal=YES", out)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(pr_review.time, "sleep"),
        ):
            self.assertEqual(41, pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"]))

    def test_a_quotation_of_the_wording_is_not_itself_a_refusal(self) -> None:
        """The same opening-line anchor that protects `refusal_of` protects this reading too."""
        body = "## Pull request overview\n\nThis PR documents: " + QUOTA_REFUSED + "\n"
        self.assertFalse(pr_review_rounds.quota_refusal({"body": body}))


class TestRawLogRead(unittest.TestCase):
    """A job log read keeps working on a `gh` older than the escape-sequence flag."""

    LOG = "repos/o/r/actions/jobs/1/logs"
    PROBE = ("gh", "api", "--allow-escape-sequences", "--help")

    def setUp(self) -> None:
        """The probe is memoized for the run, so each case starts from an unasked `gh`."""
        super().setUp()
        pr_review.gh_takes_escape_flag.cache_clear()
        self.addCleanup(pr_review.gh_takes_escape_flag.cache_clear)

    def test_an_older_gh_reads_the_log_without_the_flag(self) -> None:
        refused = subprocess.CompletedProcess([], 1, "", "")
        read = subprocess.CompletedProcess([], 0, "the log", "")
        with mock.patch.object(pr_review, "_gh_run", side_effect=[refused, read]) as run:
            proc = pr_review.gh_rest(self.LOG, raw=True)
        self.assertEqual("the log", proc.stdout)
        self.assertEqual(list(self.PROBE), run.call_args_list[0].args[0])
        self.assertNotIn("--allow-escape-sequences", run.call_args_list[1].args[0])

    def test_a_newer_gh_reads_the_log_with_the_flag(self) -> None:
        accepted = subprocess.CompletedProcess([], 0, "usage", "")
        read = subprocess.CompletedProcess([], 0, "the log", "")
        with mock.patch.object(pr_review, "_gh_run", side_effect=[accepted, read]) as run:
            proc = pr_review.gh_rest(self.LOG, raw=True)
        self.assertEqual("the log", proc.stdout)
        self.assertIn("--allow-escape-sequences", run.call_args_list[1].args[0])

    def test_another_failure_is_not_retried(self) -> None:
        """A failed read is returned as it is, whatever its message says, after one attempt."""
        accepted = subprocess.CompletedProcess([], 0, "usage", "")
        for stderr in ("gh: Not Found (HTTP 404)", "unknown flag: --allow-escape-sequences"):
            with self.subTest(stderr=stderr):
                pr_review.gh_takes_escape_flag.cache_clear()
                failed = subprocess.CompletedProcess([], 1, "", stderr)
                with mock.patch.object(pr_review, "_gh_run", side_effect=[accepted, failed]) as run:
                    proc = pr_review.gh_rest(self.LOG, raw=True)
                self.assertIs(failed, proc)
                self.assertEqual(2, run.call_count)

    def test_the_probe_runs_once_per_process(self) -> None:
        accepted = subprocess.CompletedProcess([], 0, "usage", "")
        read = subprocess.CompletedProcess([], 0, "the log", "")
        with mock.patch.object(pr_review, "_gh_run", side_effect=[accepted, read, read]) as run:
            pr_review.gh_rest(self.LOG, raw=True)
            pr_review.gh_rest(self.LOG, raw=True)
        probes = [c for c in run.call_args_list if c.args[0] == list(self.PROBE)]
        self.assertEqual(1, len(probes))

    def test_a_read_that_is_not_a_log_never_asks(self) -> None:
        read = subprocess.CompletedProcess([], 0, "{}", "")
        with mock.patch.object(pr_review, "_gh_run", return_value=read) as run:
            pr_review.gh_rest("repos/o/r")
        self.assertEqual(1, run.call_count)
        self.assertNotIn("--allow-escape-sequences", run.call_args.args[0])


class TestEveryGqlCaseKeepsItsBaseSetUp(unittest.TestCase):
    """A subclass overriding `setUp` without chaining silently drops what the base installs.

    `GqlCase.setUp` patches `gh_rest` for every case rather than every caller, and clears the
    `changed_at` memo so one case does not inherit another's compares. A subclass that overrides
    `setUp` and does not chain gets neither, so it can spawn a real `gh` against a pull request
    that does not exist, which is the thing this suite exists not to do.

    Asserted over the class tree rather than fixed in the two that had drifted, since the next
    subclass to override `setUp` is the one that would reintroduce it.
    """

    def test_a_subclass_that_overrides_setup_chains_to_it(self) -> None:
        for cls in GqlCase.__subclasses__():
            with self.subTest(cls=cls.__name__):
                if "setUp" not in vars(cls):
                    continue
                src = inspect.getsource(cls.setUp)
                self.assertIn("super().setUp()", src)


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
        (normalized,) = pr_review_checks.check_nodes(payload([review()], checks=[node]))
        return pr_review_checks.check_shape(normalized, NOW, grace, stall)

    def test_a_queued_check_is_starting_inside_the_grace_and_starved_past_it(self) -> None:
        """The two are the same state, and only the clock separates them, so the grace decides."""
        self.assertEqual("", self.shape(check(status="QUEUED", conclusion="", started=ago(90))))
        self.assertEqual(
            "NOT_PICKED_UP", self.shape(check(status="QUEUED", conclusion="", started=ago(900)))
        )

    def test_every_not_begun_spelling_reads_as_stuck_not_running(self) -> None:
        """Reading QUEUED alone reports a check that has not begun as one that is running.

        WAITING is held by its environment rather than starved, so it reads as its own shape.
        """
        for state, shape in (
            ("QUEUED", "NOT_PICKED_UP"),
            ("WAITING", "GATED"),
            ("PENDING", "NOT_PICKED_UP"),
            ("REQUESTED", "NOT_PICKED_UP"),
        ):
            with self.subTest(state=state):
                self.assertEqual(
                    shape, self.shape(check(status=state, conclusion="", started=ago(900)))
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
        nodes = pr_review_checks.check_nodes(pr)
        self.assertEqual(["SomethingNew"], pr_review_checks.checks_unread(nodes))
        # Neither reader speaks for it, so the tally counts one check and no shape is judged.
        self.assertEqual((1, 1), pr_review_checks.checks_tally(nodes))
        self.assertEqual(
            [],
            pr_review_checks.checks_stuck(
                [n for n in nodes if n.get("unreadable")], NOW, 300, 1800
            ),
        )

    def test_the_digest_names_the_context_type_it_could_not_read(self) -> None:
        """A skip nobody is told about is the silent narrowing every other guard here prevents."""
        out, _ = pr_review.digest(
            "o",
            "r",
            7,
            now=NOW,
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
        out, _ = pr_review.digest("o", "r", 7, pr=pr, now=NOW, grace=-200)
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
            "o", "r", 7, now=NOW, checks=[raw], pr=payload([review()], merge="BLOCKED")
        )
        self.assertIn("CHECK NOT PICKED UP ('unnamed'", out)

    def test_a_zone_less_stamp_returns_none_rather_than_raising(self) -> None:
        """The same guard at the function, since a digest that raises reports nothing at all."""
        self.assertIsNone(pr_review_checks.age("2026-08-06T17:00:00", NOW))
        self.assertIsNone(pr_review_checks.age("nonsense", NOW))
        self.assertEqual(900.0, pr_review_checks.age(ago(900), NOW))

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
        nodes = pr_review_checks.check_nodes(pr)
        self.assertEqual(["job", "ci/external"], [n["name"] for n in nodes])
        # Judged directly rather than through `self.shape`, which normalizes a second time.
        # A normalized node carries no typename, so that pass would skip it as unknown.
        self.assertEqual("FAILED", pr_review_checks.check_shape(nodes[1], NOW, 300, 1800))

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

    def conclusions(self, *runs: dict) -> list[str]:
        """The conclusions the normalizer keeps for one head's runs, in kept order."""
        nodes = pr_review_checks.check_nodes(payload([review()], checks=list(runs)))
        return [n["conclusion"] for n in nodes]

    def test_a_newer_suite_supersedes_an_older_one_in_either_order(self) -> None:
        """A close and reopen makes a new suite, and its runs are the ones that gate."""
        failed = check(name="lint", conclusion="FAILURE", suite=10, workflow=7)
        passed = check(name="lint", conclusion="SUCCESS", suite=20, workflow=7)
        self.assertEqual(["SUCCESS"], self.conclusions(failed, passed))
        self.assertEqual(["SUCCESS"], self.conclusions(passed, failed))

    def test_the_newest_suite_failing_reads_failed(self) -> None:
        """The older green run is the superseded one here, so the newest failure stands."""
        passed = check(name="lint", conclusion="SUCCESS", suite=10, workflow=7)
        failed = check(name="lint", conclusion="FAILURE", suite=20, workflow=7)
        self.assertEqual(["FAILURE"], self.conclusions(failed, passed))
        self.assertEqual(["FAILURE"], self.conclusions(passed, failed))

    def test_start_time_does_not_decide_which_overlapping_run_is_newer(self) -> None:
        """A job in the older suite that starts later still does not win."""
        old = check(name="lint", conclusion="FAILURE", suite=10, workflow=7, started=ago(58))
        new = check(name="lint", conclusion="SUCCESS", suite=20, workflow=7, started=ago(60))
        self.assertEqual(["SUCCESS"], self.conclusions(old, new))
        self.assertEqual(["SUCCESS"], self.conclusions(new, old))

    def test_same_named_jobs_inside_one_suite_are_all_counted(self) -> None:
        """Two jobs can share a display name, and collapsing them could hide the failing one."""
        ok = check(name="build", conclusion="SUCCESS", suite=10, workflow=7)
        bad = check(name="build", conclusion="FAILURE", suite=10, workflow=7)
        self.assertEqual(["SUCCESS", "FAILURE"], self.conclusions(ok, bad))
        self.assertEqual(["FAILURE", "SUCCESS"], self.conclusions(bad, ok))

    def test_a_name_only_the_older_suite_carries_is_kept(self) -> None:
        """The merge gate still reads a name the newer suite no longer produces."""
        old_only = check(name="leg", conclusion="FAILURE", suite=10, workflow=7)
        shared_old = check(name="lint", conclusion="FAILURE", suite=10, workflow=7)
        shared_new = check(name="lint", conclusion="SUCCESS", suite=20, workflow=7)
        for runs in ((old_only, shared_old, shared_new), (shared_new, shared_old, old_only)):
            nodes = pr_review_checks.check_nodes(payload([review()], checks=list(runs)))
            kept = sorted((n["name"], n["conclusion"]) for n in nodes)
            self.assertEqual([("leg", "FAILURE"), ("lint", "SUCCESS")], kept)

    def superseded_rollup(self, newer: str) -> list[dict]:
        """A cancelled run's failed aggregator, beside the jobs of a newer run on the same head."""
        gate = "Check pull request workflow status job"
        return [
            check(
                name="lint",
                conclusion="CANCELLED",
                suite=10,
                workflow=7,
                suite_status="COMPLETED",
                run=100,
            ),
            check(
                name=gate,
                conclusion="FAILURE",
                suite=10,
                workflow=7,
                suite_status="COMPLETED",
                run=100,
            ),
            check(
                name="lint",
                status="IN_PROGRESS",
                conclusion="",
                suite=20,
                workflow=7,
                suite_status=newer,
                run=200,
            ),
        ]

    def test_an_older_runs_failure_is_superseded_while_the_newer_run_is_in_progress(self) -> None:
        """The newer run posts its aggregator only once its jobs finish, so the old one waits."""
        nodes = pr_review_checks.check_nodes(
            payload([review()], checks=self.superseded_rollup("IN_PROGRESS"))
        )
        gate = next(n for n in nodes if n["conclusion"] == "FAILURE")
        self.assertTrue(gate.get("superseded"))
        self.assertEqual(200, gate.get("newer_run"))
        self.assertEqual([], pr_review_checks.checks_stuck(nodes, NOW, 300, 1800))

    def test_an_older_runs_failure_stands_once_the_newer_run_completes(self) -> None:
        """A name the finished newer run never posted is what the merge gate still reads."""
        rollup = self.superseded_rollup("COMPLETED")
        rollup[2] = check(name="lint", suite=20, workflow=7, suite_status="COMPLETED", run=200)
        nodes = pr_review_checks.check_nodes(payload([review()], checks=rollup))
        self.assertFalse(any(n.get("superseded") for n in nodes))
        self.assertEqual(
            ["FAILED"], [s for _, s in pr_review_checks.checks_stuck(nodes, NOW, 300, 1800)]
        )

    def test_a_held_wait_polls_on_a_superseded_failure_until_the_newer_run_posts(self) -> None:
        """The newer run's gate replaces the superseded one, and only then does the hold release.

        The newer run's job is marked optional, so the superseded gate alone is what holds.
        """
        rollup = self.superseded_rollup("IN_PROGRESS")
        rollup[2]["isRequired"] = False
        held = payload([review()], merge="BLOCKED", checks=rollup)
        nodes = pr_review_checks.check_nodes(held)
        self.assertTrue(pr_review_checks.held_checks_open(held, nodes, NOW, 300, 1800))
        gate = "Check pull request workflow status job"
        done = payload(
            [review()],
            merge="BLOCKED",
            checks=[
                *self.superseded_rollup("COMPLETED")[:2],
                check(name="lint", suite=20, workflow=7, run=200),
                check(name=gate, suite=20, workflow=7, run=200),
            ],
        )
        nodes = pr_review_checks.check_nodes(done)
        self.assertFalse(pr_review_checks.held_checks_open(done, nodes, NOW, 300, 1800))

    def test_a_newer_suite_with_no_check_run_yet_still_supersedes_a_failure(self) -> None:
        """A run waiting on its concurrency slot has a suite and no check run in the rollup."""
        pr = payload([review()], merge="BLOCKED", checks=self.superseded_rollup("QUEUED")[:2])
        waiting = {
            "databaseId": 20,
            "status": "QUEUED",
            "workflowRun": {"databaseId": 200, "workflow": {"databaseId": 7}},
        }
        pr["commits"]["nodes"][0]["commit"]["checkSuites"] = {"nodes": [waiting]}
        nodes = pr_review_checks.check_nodes(pr)
        gate = next(n for n in nodes if n["conclusion"] == "FAILURE")
        self.assertTrue(gate.get("superseded"))
        self.assertEqual(200, gate.get("newer_run"))
        self.assertEqual([], pr_review_checks.checks_stuck(nodes, NOW, 300, 1800))

    def test_an_unfinished_check_in_an_older_run_keeps_its_stuck_reading(self) -> None:
        """A newer run queued behind the older one cannot start, so the older one's shape stands."""
        starved = check(
            name="gate", status="QUEUED", conclusion="", started=ago(900), suite=10, workflow=7
        )
        pr = payload([review()], merge="BLOCKED", checks=[starved])
        waiting = {
            "databaseId": 20,
            "status": "QUEUED",
            "workflowRun": {"databaseId": 200, "workflow": {"databaseId": 7}},
        }
        pr["commits"]["nodes"][0]["commit"]["checkSuites"] = {"nodes": [waiting]}
        nodes = pr_review_checks.check_nodes(pr)
        self.assertFalse(any(n.get("superseded") for n in nodes))
        self.assertEqual(
            ["NOT_PICKED_UP"], [s for _, s in pr_review_checks.checks_stuck(nodes, NOW, 300, 1800)]
        )

    def test_the_rollup_query_asks_for_the_head_commits_own_suites(self) -> None:
        query = " ".join(pr_review.Q_FULL.split())
        self.assertIn(
            "checkSuites(last:50){ nodes{ databaseId status app{ slug } "
            "workflowRun{ databaseId workflow{ databaseId } } } }",
            query,
        )

    def test_a_newer_run_of_another_workflow_does_not_supersede_a_failure(self) -> None:
        rollup = self.superseded_rollup("IN_PROGRESS")
        rollup[2] = check(
            name="lint",
            status="IN_PROGRESS",
            conclusion="",
            suite=20,
            workflow=8,
            suite_status="IN_PROGRESS",
        )
        nodes = pr_review_checks.check_nodes(payload([review()], checks=rollup))
        self.assertFalse(any(n.get("superseded") for n in nodes))

    def test_the_rollup_query_asks_for_the_suite_and_workflow_identity(self) -> None:
        """Q_FULL selects the suite and workflow fields the dedup keys on."""
        query = " ".join(pr_review.Q_FULL.split())
        self.assertIn(
            "checkSuite{ databaseId status app{ slug databaseId } "
            "workflowRun{ databaseId workflow{ databaseId } } }",
            query,
        )

    def test_the_full_query_asks_which_checks_are_required_and_whether_the_pr_is_open(self) -> None:
        """A dropped field reads as required or open, which holds a held wait to its timeout."""
        query = " ".join(pr_review.Q_FULL.split())
        self.assertIn("headRefOid baseRefName state mergeable", query)
        self.assertIn("conclusion startedAt isRequired(pullRequestNumber:$n)", query)
        self.assertIn("context state createdAt isRequired(pullRequestNumber:$n)", query)

    def test_a_status_context_reads_its_own_required_flag(self) -> None:
        optional = {**status_context("ci/external", state="PENDING"), "isRequired": False}
        nodes = pr_review_checks.check_nodes(payload([review()], checks=[optional, check()]))
        self.assertEqual([False, True], [n["required"] for n in nodes])

    def test_a_check_run_with_no_suite_does_not_crash(self) -> None:
        """A node missing its suite reads as suite zero under no workflow and no app."""
        bare = check(name="lint")
        bare.pop("checkSuite")
        self.assertEqual(["SUCCESS"], self.conclusions(bare))

    def test_the_same_name_in_two_workflows_is_two_checks(self) -> None:
        """Two workflows can each have a `build` job under one app, and neither hides the other."""
        nodes = pr_review_checks.check_nodes(
            payload(
                [review()],
                checks=[
                    check(name="build", conclusion="FAILURE", suite=10, workflow=7),
                    check(name="build", conclusion="SUCCESS", suite=20, workflow=8),
                ],
            )
        )
        self.assertEqual((1, 2), pr_review_checks.checks_tally(nodes))

    def test_the_same_name_under_two_apps_without_a_workflow_is_two_checks(self) -> None:
        """A suite with no workflow run falls back to its app slug."""
        nodes = pr_review_checks.check_nodes(
            payload(
                [review()],
                checks=[
                    check(name="cov", conclusion="FAILURE", suite=10, slug="codecov"),
                    check(name="cov", conclusion="SUCCESS", suite=20, slug="other-app"),
                ],
            )
        )
        self.assertEqual((1, 2), pr_review_checks.checks_tally(nodes))

    def test_different_names_are_each_counted(self) -> None:
        """Two names in one suite are two checks."""
        nodes = pr_review_checks.check_nodes(
            payload(
                [review()],
                checks=[
                    check(name="lint", conclusion="FAILURE", suite=10, workflow=7),
                    check(name="build", conclusion="SUCCESS", suite=10, workflow=7),
                ],
            )
        )
        self.assertEqual((1, 2), pr_review_checks.checks_tally(nodes))

    def test_a_pull_request_with_no_rollup_reads_as_no_checks_not_as_a_failure(self) -> None:
        """A null rollup is a pull request nothing has run on yet, which blocks nothing here."""
        self.assertEqual([], pr_review_checks.check_nodes(payload([review()])))
        # Both readers take the normalized list, since the digest parses the rollup once.
        self.assertEqual(
            (0, 0), pr_review_checks.checks_tally(pr_review_checks.check_nodes(payload([review()])))
        )
        self.assertEqual(
            [],
            pr_review_checks.checks_stuck(
                pr_review_checks.check_nodes(payload([review()])), NOW, 300, 1800
            ),
        )


class TestDigestReportsChecks(GqlCase):
    def digest(self, pr: dict) -> str:
        out, _ = pr_review.digest("o", "r", 7, pr=pr, now=NOW)
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
        out = self.digest(
            payload([review()], checks=[check(), check(name="other", conclusion="SKIPPED")])
        )
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
        self.assertEqual([], pr_review_checks.check_nodes(pr))
        self.assertEqual((0, 0), pr_review_checks.checks_tally(pr_review_checks.check_nodes(pr)))
        # No fallback to another commit's rollup, which is the same stale read by another route.
        # The absence is named rather than left to render as a fact about the head.
        self.assertTrue(pr_review_checks.checks_unreadable(pr))
        self.assertIn("CHECKS UNREADABLE", self.digest(pr))

    def test_a_third_party_rerun_still_dedupes_while_workflow_keys_are_unavailable(self) -> None:
        """Only Actions runs lose their key, so another app's superseded failure stays dropped."""
        old = check(name="scan", conclusion="FAILURE", suite=10, workflow=None, slug="other-app")
        new = check(name="scan", conclusion="SUCCESS", suite=20, workflow=None, slug="other-app")
        pr = payload([review()], checks=[old, new])
        pr[pr_review_checks.WORKFLOW_KEYS_UNAVAILABLE] = True
        self.assertEqual((1, 1), pr_review_checks.checks_tally(pr_review_checks.check_nodes(pr)))

    def test_a_tolerated_workflow_run_error_keeps_same_named_jobs_of_two_workflows(self) -> None:
        """With no workflow keys, a failing `build` must not hide behind another workflow's.

        Keying Actions runs by app slug would keep only the highest suite id of two unrelated
        `build` jobs and read a failure as green, so each run keys by its own suite instead.
        """
        failed = check(name="build", conclusion="FAILURE", suite=10, workflow=None)
        passed = check(name="build", conclusion="SUCCESS", suite=20, workflow=None)
        pr_in = payload([review()], checks=[failed, passed])
        errors = [{"message": "forbidden", "path": ["a", "checkSuite", "workflowRun"]}]
        body = {"data": {"repository": {"pullRequest": pr_in}}, "errors": errors}
        done = subprocess.CompletedProcess([], 1, json.dumps(body), "")
        with (
            mock.patch.object(pr_review.subprocess, "run", return_value=done),
            contextlib.redirect_stderr(io.StringIO()) as warned,
        ):
            pr = pr_review.gql(pr_review.Q_FULL, "o", "r", 7)
        self.assertIn("workflowRun unreadable", warned.getvalue())
        self.assertEqual((1, 2), pr_review_checks.checks_tally(pr_review_checks.check_nodes(pr)))
        self.assertIn("CHECKS DEDUPED WITHOUT WORKFLOW KEYS", self.digest(pr))
        self.assertNotIn(
            "CHECKS DEDUPED WITHOUT WORKFLOW KEYS",
            self.digest(payload([review()], checks=[passed])),
        )

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
        self.assertTrue(pr_review_checks.checks_truncated(pr))
        self.assertIn("CHECKS TRUNCATED", self.digest(pr))

    def test_a_rollup_inside_the_window_is_not_reported_as_truncated(self) -> None:
        """Otherwise the loudest line in the digest fires on every pull request that has checks."""
        self.assertFalse(pr_review_checks.checks_truncated(payload([review()], checks=[check()])))
        self.assertNotIn("CHECKS TRUNCATED", self.digest(payload([review()], checks=[check()])))

    @contextlib.contextmanager
    def parse_spies(self) -> Iterator[tuple[mock.MagicMock, mock.MagicMock]]:
        """Count a parse from the gate and from the checks module alike, since each binds its own."""
        parse = pr_review_checks.check_nodes
        with (
            mock.patch.object(pr_review, "check_nodes", side_effect=parse) as gate,
            mock.patch.object(pr_review_checks, "check_nodes", side_effect=parse) as readers,
        ):
            yield gate, readers

    def test_the_rollup_is_normalized_once_for_both_readers(self) -> None:
        """Two calls parsed the same rollup twice a digest, and the parse is what this script pays.

        Raised in review here. Held by counting the calls rather than by reading the output, since
        the output is identical either way, which is what let the second call go unnoticed.
        """
        pr = payload([review()], merge="BLOCKED", checks=[check(name="lint"), check(name="gate")])
        with self.parse_spies() as spies:
            pr_review.digest("o", "r", 7, pr=pr, now=NOW)
        self.assertEqual(1, sum(s.call_count for s in spies))

    def test_a_caller_that_parsed_the_rollup_is_not_made_to_parse_it_again(self) -> None:
        """The first fix left `wait` parsing once to print and once to decide, which is the same
        doubling one caller up. Raised in review here, on the commit that halved it."""
        pr = payload([review()], merge="BLOCKED", checks=[check(name="lint")])
        with self.parse_spies() as spies:
            pr_review.digest("o", "r", 7, pr=pr, now=NOW, checks=pr_review.check_nodes(pr))
        # The one call is the case's own, so the digest made none of its own.
        self.assertEqual(1, sum(s.call_count for s in spies))

    def test_the_truncation_line_quotes_the_window_the_query_actually_asks_for(self) -> None:
        """The message borrowed WINDOW, which is the reviews and comments window and not this one.

        It read correctly only while the two numbers happened to agree, so a change to either
        would have made the line state a limit the query does not use. Raised in review here.
        """
        self.assertIn(f"contexts(first:{pr_review_checks.CHECKS_WINDOW})", pr_review.Q_FULL)
        pr = payload([review()], merge="BLOCKED", checks=[check()])
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["pageInfo"] = {
            "hasNextPage": True
        }
        self.assertIn(f"more than the {pr_review_checks.CHECKS_WINDOW} contexts", self.digest(pr))

    def test_one_reader_finds_the_head_commit_for_all_three_callers(self) -> None:
        """Three traversals of one shape drift apart, and a payload change must be caught once.

        Raised in review here. Each caller is held to the shared reader by giving the payload a
        head that matches nothing, where all three must agree that there is no rollup to read.
        """
        pr = payload([review()], checks=[check()], rollup_oid=OLD)
        self.assertEqual({}, pr_review_checks.head_commit(pr))
        self.assertEqual([], pr_review_checks.check_nodes(pr))
        self.assertFalse(pr_review_checks.checks_truncated(pr))
        self.assertTrue(pr_review_checks.checks_unreadable(pr))

    def test_a_pull_request_with_no_commits_at_all_is_not_reported_as_unreadable(self) -> None:
        """Nothing to match against is not a failed match, or every payload without one says so."""
        self.assertFalse(pr_review_checks.checks_unreadable(payload([review()], checks=[check()])))
        bare = payload([review()])
        bare["commits"] = {"nodes": []}
        self.assertFalse(pr_review_checks.checks_unreadable(bare))
        self.assertNotIn("CHECKS UNREADABLE", self.digest(bare))

    def test_the_head_rollup_is_found_wherever_the_connection_puts_it(self) -> None:
        """Selection is by oid, so an extra node ahead of the head's does not shadow it."""
        pr = payload([review()], checks=[check(name="lint")])
        head_node = pr["commits"]["nodes"][0]
        pr["commits"]["nodes"] = [{"commit": {"oid": OLD, "statusCheckRollup": None}}, head_node]
        self.assertEqual(["lint"], [n["name"] for n in pr_review_checks.check_nodes(pr)])


class TestReadingsThroughTheAdapter(GqlCase):
    """The gate's verdicts read from the adapter's typed result, over constructed bodies."""

    def setUp(self) -> None:
        super().setUp()
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def status(self, pr: dict) -> int:
        self.answer(pr)
        return pr_review.main(["status", "7", "--repo", "o/r"])

    def test_a_head_round_stating_partial_coverage_exits_forty_two(self) -> None:
        part = f"{OVERVIEW}\n<!-- fleet-review: reviewed=1 changed=3 findings=0 -->"
        self.assertEqual(42, self.status(payload([review(body=part)], files=["a.py"])))
        out = self.out.getvalue()
        self.assertIn("coverage=PARTIAL ", out)
        self.assertIn("status=COVERAGE_IS_PARTIAL the review covering the head states it", out)
        self.assertNotIn("may have no review", out)

    def test_an_earlier_partial_carries_to_a_head_stating_none(self) -> None:
        part = f"{OVERVIEW}\n<!-- fleet-review: reviewed=1 changed=3 findings=0 -->"
        rounds = [review(oid=OLD, body=part, rid="A"), review(body=OVERVIEW, rid="B")]
        self.assertEqual(42, self.status(payload(rounds, files=["a.py"])))
        line = next(ln for ln in self.out.getvalue().splitlines() if ln.startswith("status="))
        self.assertIn("because an earlier round states partial coverage", line)
        self.assertNotIn("the review covering the head states", line)
        self.assertIn("A round on the head stating full coverage clears this", line)
        self.assertNotIn("A re-request is not the remedy", line)

    def test_an_earlier_partial_carried_past_an_unbelievable_head_says_so(self) -> None:
        part = f"{OVERVIEW}\n<!-- fleet-review: reviewed=1 changed=3 findings=0 -->"
        odd = f"{OVERVIEW}\n<!-- fleet-review: reviewed=4 changed=3 findings=0 -->"
        rounds = [review(oid=OLD, body=part, rid="A"), review(body=odd, rid="B")]
        self.assertEqual(42, self.status(payload(rounds, files=["a.py"])))
        line = next(ln for ln in self.out.getvalue().splitlines() if ln.startswith("status="))
        self.assertIn("a round on the head states a reading this cannot believe", line)
        self.assertNotIn("the review covering the head states", line)

    def test_a_prose_partial_on_an_earlier_round_keeps_a_table_from_standing_in(self) -> None:
        part = f"{OVERVIEW}\nCopilot reviewed 1 out of 2 changed files.\n"
        rounds = [
            review(oid=OLD, body=part, rid="A"),
            review(body=summarized(["a.py"], covers=""), rid="B"),
        ]
        self.assertEqual(42, self.status(payload(rounds, files=["a.py"])))

    def test_a_table_naming_exactly_the_changed_files_covers_the_head(self) -> None:
        pr = payload([review(body=summarized(["a.py", "b.md"], covers=""))], files=["b.md", "a.py"])
        self.assertEqual(0, self.status(pr))
        out = self.out.getvalue()
        self.assertIn("coverage=table ", out)
        self.assertNotIn("note: no statement", out)

    def test_a_table_naming_other_files_covers_nothing_and_says_so(self) -> None:
        pr = payload([review(body=summarized(["a.py"], covers=""))], files=["a.py", "b.md"])
        self.assertEqual(0, self.status(pr))
        out = self.out.getvalue()
        self.assertIn("coverage=none ", out)
        self.assertIn("NO COVERAGE IS SHOWN", out)
        self.assertIn("note: no statement, file table, or attested local pass", out)

    def test_an_earlier_round_s_table_carries_to_a_head_round_with_none(self) -> None:
        rounds = [
            review(oid=OLD, body=summarized(["a.py"], covers=""), rid="A"),
            review(body=OVERVIEW, rid="B"),
        ]
        self.assertEqual(0, self.status(payload(rounds, files=["a.py"])))
        self.assertIn("coverage=carried:table ", self.out.getvalue())

    def test_a_cut_review_history_keeps_a_table_from_standing_in(self) -> None:
        pr = payload(
            [review(body=summarized(["a.py"], covers=""))], files=["a.py"], older_reviews=True
        )
        self.assertEqual(0, self.status(pr))
        self.assertIn("coverage=none ", self.out.getvalue())
        self.assertIn("review history runs past the window", self.out.getvalue())

    def test_a_glob_row_matches_one_segment_at_a_time(self) -> None:
        for files, want in (
            (["src/a.py", "src/b.py"], "table"),
            (["src/a.py", "src/sub/b.py"], "none"),
        ):
            with self.subTest(files=files):
                self.out.seek(0)
                self.out.truncate()
                self.status(payload([review(body=summarized(["src/*"], covers=""))], files=files))
                self.assertIn(f"coverage={want} ", self.out.getvalue())

    def test_an_unbelievable_marker_reads_unknown_and_leaves_a_note(self) -> None:
        odd = f"{OVERVIEW}\n<!-- fleet-review: reviewed=4 changed=3 findings=0 -->"
        self.assertEqual(0, self.status(payload([review(body=odd)], files=["a.py"])))
        out = self.out.getvalue()
        self.assertIn("coverage=UNKNOWN ", out)
        self.assertIn("note: no statement, file table, or attested local pass", out)

    def test_an_earlier_partial_outranks_an_unknown_head_reading(self) -> None:
        part = f"{OVERVIEW}\n<!-- fleet-review: reviewed=1 changed=3 findings=0 -->"
        odd = f"{OVERVIEW}\n<!-- fleet-review: reviewed=4 changed=3 findings=0 -->"
        rounds = [review(oid=OLD, body=part, rid="A"), review(body=odd, rid="B")]
        self.assertEqual(42, self.status(payload(rounds, files=["a.py"])))
        out = self.out.getvalue()
        self.assertIn("coverage=PARTIAL ", out)
        self.assertNotIn("coverage=UNKNOWN", out)
        self.assertIn("a round on the head states a reading this cannot believe", out)

    def test_a_head_stating_full_still_beats_an_earlier_partial(self) -> None:
        part = f"{OVERVIEW}\n<!-- fleet-review: reviewed=1 changed=3 findings=0 -->"
        full = f"{OVERVIEW}\n<!-- fleet-review: reviewed=3 changed=3 findings=0 -->"
        rounds = [review(oid=OLD, body=part, rid="A"), review(body=full, rid="B")]
        self.assertEqual(0, self.status(payload(rounds, files=["a.py"])))
        self.assertIn("coverage=full ", self.out.getvalue())

    def test_a_head_with_a_full_and_an_unknown_round_beside_a_partial_is_partial(self) -> None:
        part = f"{OVERVIEW}\n<!-- fleet-review: reviewed=1 changed=3 findings=0 -->"
        full = f"{OVERVIEW}\n<!-- fleet-review: reviewed=3 changed=3 findings=0 -->"
        odd = f"{OVERVIEW}\n<!-- fleet-review: reviewed=4 changed=3 findings=0 -->"
        rounds = [
            review(oid=OLD, body=part, rid="A"),
            review(body=full, rid="B"),
            review(body=odd, rid="C"),
        ]
        self.assertEqual(42, self.status(payload(rounds, files=["a.py"])))
        out = self.out.getvalue()
        self.assertIn("status=COVERAGE_IS_PARTIAL", out)
        self.assertIn("COVERAGE IS PARTIAL:", out)

    def test_a_note_on_an_earlier_round_still_reaches_the_digest(self) -> None:
        odd = "## Pull request overview\n\nThe change is narrow.\n"
        rounds = [review(oid=OLD, body=odd, rid="A"), review(rid="B")]
        self.assertEqual(0, self.status(payload(rounds, files=["a.py"])))
        self.assertIn("shapes=NOTED ", self.out.getvalue())
        self.assertIn(f"round {OLD[:8]}: the body carries no", self.out.getvalue())

    def test_a_body_the_reader_trips_on_holds_and_is_never_a_refusal(self) -> None:
        with mock.patch.object(pr_review_rounds.adapter, "_read", side_effect=ValueError("boom")):
            self.assertEqual(50, self.status(payload([review(body="odd body")], files=["a.py"])))
        out = self.out.getvalue()
        self.assertIn("review_on_head=yes ", out)
        self.assertIn("refusal=no ", out)
        self.assertIn("shapes=HELD ", out)
        self.assertIn("the reader failed on this body (ValueError)", out)
        self.assertIn("      odd body", out)
        self.assertIn("COVERAGE IS UNKNOWN: the reader failed on this body (ValueError)", out)

    def test_a_flagging_verdict_that_counts_nothing_exits_fifty(self) -> None:
        text = overview_v2(
            open_count="0 open findings", entries=0, verdict="### Needs a closer look"
        )
        self.assertEqual(50, self.status(payload([review(body=text)], files=["a.py"])))
        out = self.out.getvalue()
        self.assertIn("VERDICT WITH NO COUNTED FINDING", out)
        self.assertIn("The change is narrow.", out)

    def test_an_open_thread_defers_the_uncounted_verdict(self) -> None:
        text = overview_v2(
            open_count="0 open findings", entries=0, verdict="### Needs a closer look"
        )
        pr = payload([review(body=text)], threads=[thread("T1")], files=["a.py"])
        self.assertEqual(0, self.status(pr))

    def test_a_clean_verdict_s_headline_is_advisory(self) -> None:
        text = overview_v2(
            open_count="0 open findings", entries=0, verdict="### Approval recommended"
        )
        self.assertEqual(0, self.status(payload([review(body=text)], files=["a.py"])))
        self.assertIn("CLEAN VERDICT HEADLINE", self.out.getvalue())

    def test_a_stated_count_beyond_the_round_s_threads_is_printed(self) -> None:
        threads = [thread("T1", rid="PRR_one", cid="4000000001")]
        pr = payload([review(body=overview_v2())], threads=threads, files=["a.py"])
        self.assertEqual(0, self.status(pr))
        out = self.out.getvalue()
        self.assertIn("overview=2/1 ", out)
        self.assertIn("FINDINGS WITH NO THREAD (1)", out)

    def test_a_previously_missed_finding_is_counted_by_round_and_printed(self) -> None:
        missed = (
            f"{OVERVIEW}\n<details>\n<summary><strong>1 previously missed issue</strong></summary>"
            "\n\nIn code that has not changed since last review\n\n[P2] Guard the write.\n</details>"
        )
        rounds = [review(oid=OLD, body=missed, rid="A"), review(rid="B")]
        self.assertEqual(0, self.status(payload(rounds, files=["a.py"])))
        out = self.out.getvalue()
        self.assertIn("previously_missed=1 (on_head=0 earlier=1)", out)
        self.assertIn(f"PREVIOUSLY MISSED (raised on {OLD[:8]}, earlier round)", out)
        self.assertIn("Guard the write.", out)
        self.assertIn("suppressed=0 (on_head=0 earlier=0)", out)

    def test_an_unbelievable_previously_missed_count_tallies_as_unknown(self) -> None:
        missed = (
            f"{OVERVIEW}\n<details>\n<summary><strong>Previously missed (99999)</strong></summary>"
            "\n\nIn code that has not changed since last review\n\n[P2] Guard the write.\n</details>"
        )
        rounds = [review(oid=OLD, body=missed, rid="A"), review(rid="B")]
        self.assertEqual(0, self.status(payload(rounds, files=["a.py"])))
        out = self.out.getvalue()
        self.assertIn("previously_missed=? (on_head=0 earlier=?)", out)
        self.assertIn("a finding count this reader cannot believe", out)

    def test_an_unbelievable_open_count_reads_unknown_and_holds_the_round(self) -> None:
        text = overview_v2(open_count="99999 open findings", entries=0)
        self.assertEqual(50, self.status(payload([review(body=text)], files=["a.py"])))
        out = self.out.getvalue()
        self.assertIn("overview=?/0 ", out)
        self.assertIn("a finding count this reader cannot believe", out)

    def test_coderabbit_s_outside_diff_findings_are_its_own_field(self) -> None:
        cr = {
            "author": {"login": pr_review_rounds.CODERABBIT_LOGIN},
            "state": "COMMENTED",
            "commit": {"oid": HEAD},
            "body": cr_outside_diff_body(count=2),
            "submittedAt": LATE,
        }
        self.assertEqual(0, self.status(payload([review(), cr], files=["a.py"])))
        out = self.out.getvalue()
        self.assertIn("cr_outside_diff=2 (on_head=2 earlier=0)", out)
        self.assertIn("CODERABBIT OUTSIDE-DIFF (on head)", out)
        self.assertNotIn("previously_missed=", out)

    def test_qodo_s_open_findings_are_read_from_its_findings_comment(self) -> None:
        def qodo(badge: str) -> dict:
            text = (
                "<h3>Code Review by Qodo</h3>\n\n<details>\n<summary>  1.  Bad naming"
                f"{badge} <code>Bug</code></summary>\n\nThe name misleads.\n</details>\n"
            )
            return comment(login=pr_review_rounds.QODO_LOGIN, body=text)

        for badge, want in (
            ("", "qodo_open=1 "),
            (" <code>\u2713 Resolved</code>", "qodo_open=0 "),
        ):
            with self.subTest(badge=badge):
                self.out.seek(0)
                self.out.truncate()
                self.status(payload([review()], comments=[qodo(badge)], files=["a.py"]))
                self.assertIn(want, self.out.getvalue())
        summary = comment(
            login=pr_review_rounds.QODO_LOGIN, body="<h3>PR Summary by Qodo</h3>\n\nAdds."
        )
        self.out.seek(0)
        self.out.truncate()
        self.status(payload([review()], comments=[summary], files=["a.py"]))
        self.assertNotIn("qodo_open", self.out.getvalue())

    def test_an_unrecognized_shape_on_the_head_holds_with_its_text_and_fingerprint(self) -> None:
        pr = payload([review(body=unplaced())], files=["a.py"])
        self.assertEqual(50, self.status(pr))
        out = self.out.getvalue()
        hold = pr_review_rounds.shape_hold(pr)
        assert hold is not None
        self.assertRegex(hold[0], r"^[0-9a-f]{12}$")
        self.assertIn("shapes=HELD ", out)
        self.assertIn(f"UNPLACED ON THE HEAD (fingerprint={hold[0]})", out)
        self.assertIn("a collapsed section this reader has no spelling for", out)
        self.assertIn("      The retry bound is off by one.", out)
        self.assertIn(f"--fingerprint {hold[0]} --body <outcomes>", out)
        self.assertIn("status=UNPLACED_PARTS_UNACKNOWLEDGED", out)
        self.assertNotIn("REVIEWER OUTPUT NOTED", out)

    def test_an_acknowledgment_by_an_owner_member_or_collaborator_clears_the_hold(self) -> None:
        fp = self.fingerprint(unplaced())
        for association in ("OWNER", "MEMBER", "COLLABORATOR"):
            with self.subTest(association=association):
                self.out.seek(0)
                self.out.truncate()
                pr = payload(
                    [review(body=unplaced())], comments=[ack(fp, association)], files=["a.py"]
                )
                self.assertEqual(0, self.status(pr))
                out = self.out.getvalue()
                self.assertIn("shapes=ACKNOWLEDGED ", out)
                self.assertIn(f"UNPLACED ON THE HEAD (fingerprint={fp})", out)
                self.assertNotIn("The retry bound is off by one.", out)
                self.assertNotIn("status=UNPLACED_PARTS_UNACKNOWLEDGED", out)

    def test_an_acknowledgment_by_a_non_member_does_not_clear_the_hold(self) -> None:
        fp = self.fingerprint(unplaced())
        for association in ("CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", ""):
            with self.subTest(association=association):
                pr = payload(
                    [review(body=unplaced())], comments=[ack(fp, association)], files=["a.py"]
                )
                self.assertEqual(50, self.status(pr))
                self.assertIn("shapes=HELD ", self.out.getvalue())

    def test_a_marker_with_no_outcome_or_inside_a_fence_does_not_clear_the_hold(self) -> None:
        marker = pr_review_rounds.ACK_MARKER.format(self.fingerprint(unplaced()))
        for text in (marker, f"Fixed in abc1234.\n\n```text\n{marker}\n```"):
            with self.subTest(text=text):
                pr = payload(
                    [review(body=unplaced())],
                    comments=[ack("", "OWNER", text=text)],
                    files=["a.py"],
                )
                self.assertEqual(50, self.status(pr))

    def test_a_new_head_with_new_unplaced_text_holds_again(self) -> None:
        old = unplaced("The retry bound is off by one.")
        new = unplaced("The timeout is read before it is set.")
        rounds = [review(oid=OLD, body=old, rid="A"), review(body=new, rid="B")]
        pr = payload(rounds, comments=[ack(self.fingerprint(old), "OWNER")], files=["a.py"])
        self.assertEqual(50, self.status(pr))
        out = self.out.getvalue()
        self.assertIn("shapes=HELD ", out)
        self.assertIn("      The timeout is read before it is set.", out)
        self.assertIn(f"round {OLD[:8]}: a collapsed section", out)
        self.assertNotEqual(self.fingerprint(old), self.fingerprint(new))

    def test_the_same_unplaced_text_on_a_new_head_keeps_its_acknowledgment(self) -> None:
        def trailing(effort: str) -> str:
            return f"{OVERVIEW}\n**Risk level:**\n\nLow.\n\n<sub>Review effort: {effort}</sub>"

        old, new = trailing("Lite"), trailing("Balanced")
        rounds = [review(oid=OLD, body=old, rid="A"), review(body=new, rid="B")]
        pr = payload(rounds, comments=[ack(self.fingerprint(old), "OWNER")], files=["a.py"])
        self.status(pr)
        self.assertIn("shapes=ACKNOWLEDGED ", self.out.getvalue())

    def test_a_hidden_acknowledgment_does_not_clear_the_hold(self) -> None:
        hidden = {**ack(self.fingerprint(unplaced()), "OWNER"), "isMinimized": True}
        pr = payload([review(body=unplaced())], comments=[hidden], files=["a.py"])
        self.assertEqual(50, self.status(pr))

    def fingerprint(self, text: str) -> str:
        hold = pr_review_rounds.shape_hold(payload([review(body=text)]))
        assert hold is not None
        return hold[0]

    def test_a_wait_on_a_drifted_login_ends_on_forty(self) -> None:
        drifted = review(login="copilot-pull-request-reviewer-v2")
        self.answer(payload([drifted], files=["a.py"]))
        with mock.patch.object(pr_review.time, "sleep"):
            code = pr_review.main(["wait", "7", "--repo", "o/r", "--timeout", "0"])
        self.assertEqual(40, code)
        out = self.out.getvalue()
        self.assertIn("status=REVIEWER_LOGIN_DRIFT", out)
        self.assertIn("copilot-pull-request-reviewer-v2", out)


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
        super().setUp()
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

    def test_wait_does_not_take_forty_four_on_a_superseded_runs_failed_aggregator(self) -> None:
        """A closed and reopened pull request cancels its first run, whose aggregator then fails.

        The reopen's run on the same head is still going and has not posted its own aggregator.
        So the failure is not a verdict on the head, and the digest names the newer run instead.
        """
        gate = "Check pull request workflow status job"
        rollup = [
            check(
                name=gate,
                conclusion="FAILURE",
                suite=10,
                workflow=7,
                suite_status="COMPLETED",
                run=100,
            ),
            check(
                name="lint",
                status="IN_PROGRESS",
                conclusion="",
                started=real_ago(60),
                suite=20,
                workflow=7,
                suite_status="IN_PROGRESS",
                run=200,
            ),
        ]
        self.answer(payload([review()], merge="BLOCKED", checks=rollup))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7"]))
        out = self.out.getvalue()
        self.assertNotIn("status=CHECKS_NOT_MERGEABLE", out)
        self.assertIn(
            f"CHECK SUPERSEDED ({gate!r}, COMPLETED/FAILURE in an older run): run 200", out
        )

    def test_a_superseded_check_that_passed_prints_no_superseded_line(self) -> None:
        """An older run's green check is no failure to explain away, so nothing names it."""
        rollup = [
            check(name="gate", suite=10, workflow=7, run=100),
            check(
                name="lint",
                status="IN_PROGRESS",
                conclusion="",
                started=real_ago(60),
                suite=20,
                workflow=7,
                suite_status="IN_PROGRESS",
                run=200,
            ),
        ]
        pr = payload([review()], merge="BLOCKED", checks=rollup)
        gate = next(n for n in pr_review_checks.check_nodes(pr) if n["name"] == "gate")
        self.assertTrue(gate.get("superseded"))
        self.answer(pr)
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertNotIn("CHECK SUPERSEDED", self.out.getvalue())

    def test_a_stuck_check_on_a_clean_merge_does_not_take_forty_four(self) -> None:
        """`CLEAN` proves no required gate is outstanding, whatever else the rollup is doing.

        The node carries no `isRequired`, which reads as required, so this pins the `BLOCKED`
        half of the condition. The digest still names the check, so the narrower code costs the
        reader nothing.
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

    def test_only_a_required_stuck_check_on_a_blocked_merge_takes_forty_four(self) -> None:
        """`BLOCKED` is also worn by an open thread, so it cannot say a stuck check is required.

        The rollup's `isRequired` can, so a failed check it marks optional exits 0 on a merge
        blocked for another reason, and the same check marked required still exits 44.
        """
        for required, code in ((False, 0), (True, 44)):
            with self.subTest(required=required):
                self.out.seek(0)
                self.out.truncate()
                failed = {**check(name="scan", conclusion="FAILURE"), "isRequired": required}
                self.answer(payload([review()], merge="BLOCKED", checks=[failed]))
                with mock.patch.object(pr_review.time, "sleep"):
                    self.assertEqual(code, self.cli(["wait", "7"]))
                out = self.out.getvalue()
                self.assertIn("stuck=FAILED", out)
                self.assertEqual(code == 44, "status=CHECKS_NOT_MERGEABLE" in out)

    def test_the_forty_four_line_names_only_the_required_stuck_check(self) -> None:
        """The block above prints every stuck check, so the 44 line says which one is required.

        Otherwise a reader fixes the optional failure the block lists first and meets 44 again.
        """
        optional = {**check(name="scan", conclusion="FAILURE"), "isRequired": False}
        gate = {**check(name="gate", conclusion="FAILURE", suite=2), "isRequired": True}
        self.answer(payload([review()], merge="BLOCKED", checks=[optional, gate]))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(44, self.cli(["wait", "7"]))
        status = next(
            line
            for line in self.out.getvalue().splitlines()
            if line.startswith("status=CHECKS_NOT_MERGEABLE")
        )
        self.assertIn("required stuck 'gate':", status)
        self.assertNotIn("'scan'", status)

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

    def test_min_rounds_holds_out_past_a_round_status_already_reported(self) -> None:
        """`status`'s guidance for unstated coverage is to re-request on the same head, which
        leaves an earlier round covering it while the fresh one this wait was called to watch
        for is still outstanding. Naming that earlier `rounds=` count on `--min-rounds` is what
        `wait` needs to hold out for one beyond it, rather than reading `reviewed_head` alone and
        reporting the earlier round as done at once without ever waiting for the fresh one the
        re-request was for. The first poll here carries that shape: one review already on the
        head, a request still pending. Without the flag,
        `wait` returns on it unpolled, per `test_wait_returns_zero_once_the_review_lands_on_the_head`.
        """
        self.answer(
            payload([review()], pending=True),
            payload([review(), review(rid="PRR_two", at=LATE)]),
        )
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7", "--min-rounds", "1"]))
        slept.assert_called_once()

    def test_min_rounds_times_out_rather_than_crediting_the_round_it_was_told_to_skip(self) -> None:
        """A round no higher than `--min-rounds` must not report success at the timeout either,
        or the exit code repeats the bug the polling half of this fix already closes."""
        self.answer(payload([review()], pending=True))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--min-rounds", "1", "--timeout", "0"]))
        self.assertIn("\nstatus=PENDING\n", self.out.getvalue())

    def test_min_rounds_names_the_baseline_when_a_round_lands_mid_wait_below_it(self) -> None:
        """Three rounds already sit on the head, a `status` read of `rounds=3`, and a fourth
        lands while the wait polls. N is the count already seen, so `--min-rounds 3` returns on
        that fourth round. One above it, `--min-rounds 4`, waits for a fifth and reaches its
        timeout, which a bare `status=PENDING` reported exactly like a round that never landed.
        The line names the position it waits for instead, so a caller can tell the two apart.
        Each clock read advances 100 seconds, so one poll lands the fourth round and the read
        after it is past the 150-second timeout.
        """
        seen = [review(rid=f"PRR_{i}", at=f"2026-08-02T10:0{i}:00Z") for i in range(3)]
        fourth = review(rid="PRR_fourth", at=LATE)
        for baseline, code in ((3, 0), (4, 30)):
            with self.subTest(min_rounds=baseline):
                self.out.seek(0)
                self.out.truncate()
                self.answer(payload(seen, pending=True), payload([*seen, fourth]))
                clock = itertools.count(0, 100).__next__
                with (
                    mock.patch.object(pr_review.time, "sleep") as slept,
                    mock.patch.object(pr_review.time, "monotonic", clock),
                ):
                    argv = ["wait", "7", "--min-rounds", str(baseline), "--timeout", "150"]
                    self.assertEqual(code, self.cli(argv))
                slept.assert_called_once()
                status = [
                    line for line in self.out.getvalue().splitlines() if line.startswith("status=")
                ]
                if code:
                    self.assertEqual(1, len(status))
                    self.assertIn("now has 4 rounds, up from 3", status[0])
                    self.assertIn("--min-rounds 4 waits for round 5 or later", status[0])
                else:
                    self.assertEqual([], status)

    def test_min_rounds_keeps_the_bare_pending_line_unless_a_new_round_covers_head(self) -> None:
        """The line naming the baseline says a round landing during the wait covers the head,
        which holds only where one does. Here a round lands mid-wait, raising the count, but on a
        superseded head, so it covers nothing. N sits above the count the wait began with, the
        only case the line can fire in, so the coverage clause alone keeps it bare. That holds
        whether or not an earlier round covers the head. Each clock read advances 100 seconds,
        so one poll lands the round and the read after it is past the 150-second timeout.
        """
        for label, before in (("uncovered", [review(oid=OLD)]), ("covered", [review()])):
            with self.subTest(label):
                self.out.seek(0)
                self.out.truncate()
                self.answer(
                    payload(before, pending=True),
                    payload([*before, review(oid=OLD, rid="PRR_two", at=LATE)]),
                )
                clock = itertools.count(0, 100).__next__
                with (
                    mock.patch.object(pr_review.time, "sleep") as slept,
                    mock.patch.object(pr_review.time, "monotonic", clock),
                ):
                    argv = ["wait", "7", "--min-rounds", "2", "--timeout", "150"]
                    self.assertEqual(30, self.cli(argv))
                slept.assert_called_once()
                self.assertIn("\nstatus=PENDING\n", self.out.getvalue())

    def test_min_rounds_counts_a_stale_off_head_round_the_way_status_does(self) -> None:
        """`min_rounds` must be the same count `status` prints as `rounds=`: every one of the
        reviewer's own reviews on this pull request, on any head. A count narrowed to the
        current head disagrees with that whenever a stale, off-head round sits in the history,
        and undercounts every round after it forever, since the off-head round it excludes never
        ages out. Two rounds already exist here, one off the current head, matching a `status`
        read of `rounds=2` before the re-request. The one that lands after it is the third, on
        the head, and is what `--min-rounds 2` was asked to hold out for.
        """
        self.answer(
            payload([review(oid=OLD, rid="PRR_stale"), review(rid="PRR_head_one")], pending=True),
            payload(
                [
                    review(oid=OLD, rid="PRR_stale"),
                    review(rid="PRR_head_one"),
                    review(rid="PRR_head_two", at=LATE),
                ]
            ),
        )
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7", "--min-rounds", "2"]))
        slept.assert_called_once()

    def test_min_rounds_counts_a_head_refusal_the_way_status_does(self) -> None:
        """A refusal sits in `status`'s `rounds=` but not in a head-scoped count, since a refusal
        covers nothing and `reviewed_head` excludes it. A caller reading `rounds=1` off one
        refusal and reaching `--min-rounds 1` once a genuine round then lands beside it must see
        that genuine round as the fresh one it asked for, not read the head-scoped count, still 1
        because it never counted the refusal to begin with, as still short of the baseline.

        The first poll is bodyless, the shape the liveness query itself carries: `refusal_of`
        reads nothing there and counts the refusal as ordinary coverage, same as
        `test_the_liveness_reading_ends_the_wait_and_the_full_read_refuses_it` above. The full
        read that follows carries the body and is where the refusal is actually read as one,
        alongside the genuine round that answers it.
        """
        bodyless_refusal = {k: v for k, v in review(rid="PRR_refusal").items() if k != "body"}
        self.answer(
            payload([bodyless_refusal], pending=True),
            payload([review(body=REFUSED, rid="PRR_refusal"), review(rid="PRR_genuine", at=LATE)]),
        )
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7", "--min-rounds", "1"]))
        slept.assert_called_once()

    def test_min_rounds_does_not_credit_a_refusal_as_the_fresh_round(self) -> None:
        """The round satisfying `min_rounds` and the round satisfying coverage must be the same
        round, or a refusal answering the re-request reads as done off an untouched, older
        genuine round instead. One genuine round already sits on the head, matching a `status`
        read of `rounds=1`. The caller re-requests and runs `wait --min-rounds 1`, and the round
        that comes back is a refusal, not the fresh genuine round the re-request was for.
        Checking `reviewed_head` and the count as two independent clauses would read this as
        done, since the untouched round0 still satisfies `reviewed_head` and the refusal at
        round1 still pushes the count past 1, returning 0 on a round that declined. Walking one
        list for a single round satisfying both instead falls through to the refusal this newest
        round actually is, which is the correct, narrower failure `refusing_review` names.
        """
        self.answer(payload([review(rid="PRR_one"), review(body=REFUSED, rid="PRR_ref", at=LATE)]))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(41, self.cli(["wait", "7", "--min-rounds", "1", "--timeout", "0"]))
        self.assertIn("status=REVIEW_IS_A_REFUSAL", self.out.getvalue())

    def test_min_rounds_survives_a_push_moving_the_head(self) -> None:
        """A head-scoped count resets to zero the moment a push moves the head, so it reads a
        brand new head's first genuine round as still short of a baseline that was measured
        against a head that no longer exists. The unscoped count `status` prints never resets:
        one round already sits on the old head, matching a `status` read of `rounds=1`, and the
        round that lands on the head after the push is the second, which is what
        `--min-rounds 1` was asked to hold out for.
        """
        stale = payload([review(oid=OLD, rid="PRR_stale")], pending=True)
        stale["headRefOid"] = OLD
        stale["commits"]["nodes"][0]["commit"]["oid"] = OLD
        self.answer(
            stale, payload([review(oid=OLD, rid="PRR_stale"), review(rid="PRR_two", at=LATE)])
        )
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7", "--min-rounds", "1"]))
        slept.assert_called_once()

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
        """One observed lifecycle reaches a review without `copilot_work_started`."""
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

    def wire_history(
        self, prs: list[dict], states: tuple[dict, dict] = RECORDED
    ) -> list[tuple[str, dict]]:
        """Route `gh_graphql` calls after `self.answer(...)`, answering the bot-id/history query
        with `prs` verbatim as `pullRequests.nodes`, and recording every call made.

        `self.answer(...)` already installs a default returning no history at all, which is what
        every other wait test relies on to skip the auto-request and the repo-wide quota
        reading both, untouched. This overrides that default for the tests below that exercise
        either. `wire_bot` is the narrower case, an id and nothing else, and the tests further
        down need a review's own body and `submittedAt` too, which `hist_review` builds.
        """
        calls: list[tuple[str, dict]] = []

        def fake(query: str, **variables: object) -> dict:
            calls.append((query, variables))
            if "pullRequests(first:$prs" in query:
                return {"repository": {"pullRequests": {"nodes": prs}}}
            answered = answer_request(query, variables, states)
            if answered is not None:
                return answered
            raise AssertionError(f"unexpected document: {query[:60]}")

        self.enterContext(mock.patch.object(pr_review, "gh_graphql", side_effect=fake))
        return calls

    def wire_bot(
        self, bot_id: str | None, states: tuple[dict, dict] = RECORDED
    ) -> list[tuple[str, dict]]:
        """`wire_history` narrowed to just an id, for the tests that need nothing else."""
        if bot_id is None:
            return self.wire_history([], states)
        node = {
            "number": 1,
            "reviews": {
                "nodes": [
                    {
                        "author": {
                            "__typename": "Bot",
                            "login": pr_review_rounds.REVIEWER,
                            "id": bot_id,
                        }
                    }
                ]
            },
        }
        return self.wire_history([node], states)

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

    def test_wait_widens_the_lookback_when_the_narrow_window_is_empty(self) -> None:
        """The end-to-end path for a widened lookback: `wait`'s own auto-request still finds a
        bot id once the narrow window has aged out, rather than falling back to polling only."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        calls: list[tuple[str, dict]] = []
        states = RECORDED

        def fake(query: str, **variables: object) -> dict:
            calls.append((query, variables))
            if "pullRequests(first:$prs" in query:
                if variables.get("prs") == pr_review.HISTORY_PRS:
                    return {"repository": {"pullRequests": {"nodes": []}}}
                node = {
                    "number": 1,
                    "reviews": {
                        "nodes": [
                            {
                                "author": {
                                    "__typename": "Bot",
                                    "login": pr_review_rounds.REVIEWER,
                                    "id": "BOT_999",
                                }
                            }
                        ]
                    },
                }
                return {"repository": {"pullRequests": {"nodes": [node]}}}
            answered = answer_request(query, variables, states)
            if answered is not None:
                return answered
            raise AssertionError(f"unexpected document: {query[:60]}")

        self.enterContext(mock.patch.object(pr_review, "gh_graphql", side_effect=fake))
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertEqual(1, slept.call_count)
        mutations = [(q, v) for q, v in calls if "requestReviews" in q]
        self.assertEqual(1, len(mutations))
        self.assertEqual("BOT_999", mutations[0][1].get("bot"))

    def test_an_already_requested_review_is_not_re_requested(self) -> None:
        """Calling `wait` twice on the same pending request never double-requests it."""
        self.answer(payload([review()], pending=True))
        calls = self.wire_bot("BOT_123")
        self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertFalse(calls)
        self.assertNotIn("auto-request:", self.out.getvalue())

    def test_a_request_that_records_nothing_ends_the_wait_after_one_interval(self) -> None:
        """The shape an exhausted allowance left: the mutation succeeds and leaves nothing behind.

        No pending reviewer and no new review-request event, so no review is coming, and polling
        --timeout out only reports that as patience 45 minutes later. The one wait is the first
        poll delay, which gives a request GitHub is slow to record the time to show up.
        """
        self.answer(payload([review(oid=OLD)]))
        unchanged = request_state(events=("RRE_1",))
        calls = self.wire_bot("BOT_123", (unchanged, unchanged))
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(40, self.cli(["wait", "7"]))
        slept.assert_called_once_with(15)
        self.assertEqual(1, len([c for c in calls if "requestReviews" in c[0]]))
        out = self.out.getvalue()
        self.assertIn("the request recorded nothing on the pull request", out)
        self.assertIn("status=REQUEST_NOT_RECORDED", out)
        self.assertNotIn("status=PENDING", out)

    def test_a_first_request_that_records_nothing_is_read_the_same(self) -> None:
        """A pull request with no request event before it has no id to compare against."""
        self.answer(payload([review(oid=OLD)]))
        self.wire_bot("BOT_123", (request_state(), request_state()))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(40, self.cli(["wait", "7", "--timeout", "0"]))

    def test_a_new_event_alone_counts_as_recorded(self) -> None:
        """A request the reviewer already left the pending set for still added its event."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        self.wire_bot("BOT_123", (request_state(), request_state(events=("RRE_1",))))
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertEqual(1, slept.call_count)
        self.assertNotIn("REQUEST_NOT_RECORDED", self.out.getvalue())

    def test_a_pending_reviewer_alone_counts_as_recorded(self) -> None:
        """The pending set is read as well, so an event past the window does not decide it."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        same = ("RRE_1",)
        self.wire_bot("BOT_123", (request_state(events=same), request_state(True, same)))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertNotIn("REQUEST_NOT_RECORDED", self.out.getvalue())

    def test_a_review_on_the_final_read_outranks_an_unrecorded_request(self) -> None:
        """A review covering the head is concrete, so it decides the code over the inference."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        unchanged = request_state(events=("RRE_1",))
        self.wire_bot("BOT_123", (unchanged, unchanged))
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7"]))

    def test_a_reviewer_pending_on_the_confirming_read_is_polled_for(self) -> None:
        """An answer lagging the request it reports is corrected by the next read, so the wait polls."""
        self.answer(
            payload([review(oid=OLD)]),
            payload([review(oid=OLD)]),
            payload([review(oid=OLD)], pending=True),
            payload([review()]),
        )
        unchanged = request_state(events=("RRE_1",))
        self.wire_bot("BOT_123", (unchanged, unchanged))
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertEqual(2, slept.call_count)
        out = self.out.getvalue()
        self.assertNotIn("recorded nothing", out)
        self.assertNotIn("status=REQUEST_NOT_RECORDED", out)

    def test_a_drifted_login_in_the_answer_is_not_read_as_unrecorded(self) -> None:
        """A renamed reviewer is what the poll reports, so the request is not judged on its spelling."""
        drifted = {"__typename": "Bot", "login": "copilot-pull-request-reviewer-v2"}
        before = request_state(events=("RRE_1",))
        after = {
            "reviewRequests": {"nodes": [{"requestedReviewer": drifted}]},
            "timelineItems": {"nodes": [{"id": "RRE_1", "requestedReviewer": drifted}]},
        }
        self.assertIsNone(pr_review_rounds.request_recorded(before, after))

    def test_an_answer_carrying_no_request_state_is_not_read_as_unrecorded(self) -> None:
        """An unread state is not a reading of one, so the wait polls as it did before."""
        before = request_state(events=("RRE_1",))
        self.assertIsNone(pr_review_rounds.request_recorded(before, {"id": "PR_test"}))
        self.assertIsNone(
            pr_review_rounds.request_recorded(before, {"reviewRequests": {"nodes": []}})
        )

    def test_a_null_answer_is_not_read_as_unrecorded(self) -> None:
        """A field answered null was not read, the same as one left out."""
        before = request_state(events=("RRE_1",))
        read: dict = {"nodes": []}
        self.assertIsNone(
            pr_review_rounds.request_recorded(
                before, {"reviewRequests": None, "timelineItems": read}
            )
        )
        self.assertIsNone(
            pr_review_rounds.request_recorded(
                before, {"reviewRequests": read, "timelineItems": None}
            )
        )

        def fake(query: str, **variables: object) -> dict:
            if "requestReviews" in query:
                return {"requestReviews": {"pullRequest": None}}
            answered = answer_request(query, variables, (before, before))
            assert answered is not None
            return answered

        with (
            mock.patch.object(pr_review, "gh_graphql", side_effect=fake),
            mock.patch.object(pr_review.time, "sleep") as slept,
        ):
            _, recorded = pr_review.request_copilot_review("o", "r", 7, "PR_test", "BOT_123", 15)
        self.assertIsNone(recorded)
        slept.assert_not_called()

    def test_a_lagging_answer_is_read_again_by_the_same_predicate(self) -> None:
        """A request whose event lands after the answer is recorded, not blamed on the account.

        The second read waits out the settle delay first, since a read straight after the answer
        sees the same lag the answer did.
        """
        before = request_state(events=("RRE_1",))
        reads = [before, request_state(events=("RRE_1", "RRE_2"))]
        order: list[str] = []

        def fake(query: str, **variables: object) -> dict:
            if "requestReviews" in query:
                order.append("request")
                return {"requestReviews": {"pullRequest": {"id": "PR_test", **before}}}
            order.append("read")
            return {"repository": {"pullRequest": {"id": "PR_test", **reads.pop(0)}}}

        with (
            mock.patch.object(pr_review, "gh_graphql", side_effect=fake),
            mock.patch.object(
                pr_review.time, "sleep", side_effect=lambda s: order.append(f"sleep {s}")
            ),
        ):
            _, recorded = pr_review.request_copilot_review("o", "r", 7, "PR_test", "BOT_123", 15)
        self.assertIs(True, recorded)
        self.assertFalse(reads)
        self.assertEqual(["read", "request", "sleep 15", "read"], order)

    def test_a_recorded_answer_is_not_waited_on(self) -> None:
        """Only an answer reading as unrecorded pays the settle delay."""
        before, after = RECORDED

        def fake(query: str, **variables: object) -> dict:
            answered = answer_request(query, variables, (before, after))
            assert answered is not None
            return answered

        with (
            mock.patch.object(pr_review, "gh_graphql", side_effect=fake),
            mock.patch.object(pr_review.time, "sleep") as slept,
        ):
            _, recorded = pr_review.request_copilot_review("o", "r", 7, "PR_test", "BOT_123", 15)
        self.assertIs(True, recorded)
        slept.assert_not_called()

    def test_a_request_past_the_tenth_pending_reviewer_is_read(self) -> None:
        """A busy pull request still shows the reviewer the request added."""
        self.assertIn("reviewRequests(first:100)", pr_review.REQUEST_STATE)

    def test_a_silent_head_short_circuits_on_the_repo_wide_quota_signal(self) -> None:
        """The shape observed live on two consecutive pull requests: no Copilot activity at all on
        this pull request, while the reviewer's own most recent word anywhere in the repository
        is the account quota. Polling this pull request's silence for the full timeout would
        only relearn that same account state a call late, so the wait stops here instead."""
        self.answer(payload([]))
        calls = self.wire_history([hist_review(962, QUOTA_REFUSED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(41, self.cli(["wait", "7"]))
        slept.assert_not_called()
        out = self.out.getvalue()
        self.assertIn("status=COPILOT_QUOTA_EXHAUSTED_REPO_WIDE", out)
        self.assertIn("#962", out)
        self.assertIn("note: the reviewer's own most recent activity", out)
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))

    def test_the_current_pull_requests_own_bot_id_still_seeds_the_auto_request(self) -> None:
        """An earlier round on this very pull request is still a valid id to request with."""
        self.answer(payload([review(oid=OLD)]))
        calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED, at=EARLY)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        slept.assert_not_called()
        mutations = [(q, v) for q, v in calls if "requestReviews" in q]
        self.assertEqual(1, len(mutations))
        self.assertEqual("BOT_1", mutations[0][1].get("bot"))
        out = self.out.getvalue()
        self.assertNotIn("COPILOT_QUOTA_EXHAUSTED_REPO_WIDE", out)
        self.assertIn("status=PENDING", out)

    def test_a_genuine_review_on_this_pull_requests_own_earlier_head_clears_the_signal(
        self,
    ) -> None:
        """A pull request number is not what makes an entry irrelevant to the repo-wide signal.

        An earlier round posted before this pull request's current head is real evidence about
        the account, not a self-reference to discard, and it correctly outranks a strictly
        older refusal elsewhere: excluding this pull request's own history entirely, as an
        earlier fix here did, would have hidden that evidence and let the older refusal read
        as the newest activity instead.
        """
        self.answer(payload([review(oid=OLD)]))
        calls = self.wire_history(
            [
                hist_review(900, QUOTA_REFUSED, at=EARLY),
                hist_review(7, OVERVIEW + "\n" + COVERED, at=LATE),
            ]
        )
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        slept.assert_not_called()
        mutations = [(q, v) for q, v in calls if "requestReviews" in q]
        self.assertEqual(1, len(mutations))
        out = self.out.getvalue()
        self.assertNotIn("COPILOT_QUOTA_EXHAUSTED_REPO_WIDE", out)
        self.assertIn("status=PENDING", out)

    def test_a_stale_refusal_on_this_pull_requests_own_earlier_head_is_41(self) -> None:
        """This pull request's own older refusal is read from its own reviews, ahead of the repo-wide reading."""
        self.answer(payload([review(oid=OLD, body=QUOTA_REFUSED, at=EARLY)]))
        self.wire_history([hist_review(7, QUOTA_REFUSED, at=EARLY)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(41, self.cli(["wait", "7", "--timeout", "0"]))
        slept.assert_not_called()
        self.assertIn("status=COPILOT_QUOTA_EXHAUSTED", self.out.getvalue())
        self.assertNotIn("REPO_WIDE", self.out.getvalue())

    def test_a_genuine_review_since_the_refusal_clears_the_signal(self) -> None:
        """The most recent record settles it: a working round after the refusal spends it."""
        self.answer(payload([]))
        self.wire_history(
            [
                hist_review(962, QUOTA_REFUSED, at=EARLY),
                hist_review(965, OVERVIEW + "\n" + COVERED, at=LATE),
            ]
        )
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        slept.assert_not_called()
        out = self.out.getvalue()
        self.assertNotIn("COPILOT_QUOTA_EXHAUSTED_REPO_WIDE", out)
        self.assertIn("status=PENDING", out)

    def test_ignore_quota_signal_restores_plain_polling(self) -> None:
        """The override: skip the reading entirely and poll --timeout, once quota might have
        reset. The auto-request still needs the same history, so `--ignore-quota-signal` only
        changes whether the signal is acted on, not whether it is read."""
        self.answer(payload([]))
        self.wire_history([hist_review(962, QUOTA_REFUSED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            code = self.cli(["wait", "7", "--timeout", "0", "--ignore-quota-signal"])
        self.assertEqual(30, code)
        slept.assert_not_called()
        out = self.out.getvalue()
        self.assertNotIn("COPILOT_QUOTA_EXHAUSTED_REPO_WIDE", out)
        self.assertNotIn("note: the reviewer's own most recent activity", out)
        self.assertIn("status=PENDING", out)

    def bodyless(self, oid: str) -> dict:
        """A liveness payload, whose reviews carry no body, as `Q_LIVE` asks for none."""
        return payload([{k: v for k, v in review(oid=oid).items() if k != "body"}])

    def test_the_first_stop_reading_and_every_poll_share_one_helper(self) -> None:
        """A stop reading added to one site and not the other splits the first decision off."""
        self.answer(
            payload([review(oid=OLD)], pending=True),
            payload([review(oid=OLD)], pending=True),
            payload([review(oid=OLD), review(rid="PRR_new")]),
        )
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        spy = self.enterContext(mock.patch.object(pr_review, "liveness", wraps=pr_review.liveness))
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertEqual(slept.call_count + 1, spy.call_count)

    def test_a_pending_request_past_an_error_round_is_still_polled_for(self) -> None:
        """The stop withholds a request, and a review already on its way still lands."""
        self.answer(
            payload([review(oid=OLD)], pending=True),
            payload([review(oid=OLD, body=ERROR_REFUSED)], pending=True),
            payload([review(oid=OLD, body=ERROR_REFUSED), review(rid="PRR_new")]),
        )
        calls = self.wire_history([hist_review(7, ERROR_REFUSED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        slept.assert_called()
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))

    def test_a_pending_request_past_a_quota_round_is_still_polled_for(self) -> None:
        """The repo-wide signal holds back a request too, and a pending one still lands."""
        self.answer(
            payload([review(oid=OLD)], pending=True),
            payload([review(oid=OLD, body=QUOTA_REFUSED)], pending=True),
            payload([review(oid=OLD, body=QUOTA_REFUSED), review(rid="PRR_new")]),
        )
        calls = self.wire_history([hist_review(7, QUOTA_REFUSED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        slept.assert_called()
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))

    def test_ignore_quota_signal_requests_past_a_current_head_refusal(self) -> None:
        """The body-less liveness read counts a head refusal as done, so the override re-reads it."""
        refused = review(body=QUOTA_REFUSED, at=EARLY)
        self.answer(
            payload([{k: v for k, v in refused.items() if k != "body"}]),
            payload([refused]),
            payload([refused, review(at=LATE, rid="PRR_new")]),
        )
        calls = self.wire_history([hist_review(7, QUOTA_REFUSED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7", "--ignore-quota-signal"]))
        self.assertEqual(1, len([c for c in calls if "requestReviews" in c[0]]))

    def into(self, pr: dict, base: str = "develop", attest: bool = False) -> dict:
        """A pull request into `base` on a repository whose default branch is main."""
        pr = {**pr, "baseRefName": base, "baseRepository": {"defaultBranchRef": {"name": "main"}}}
        if attest:
            marker = {
                "author": {"login": "maintainer"},
                "authorAssociation": "OWNER",
                "createdAt": LATE,
                "body": f"Attested.\n\n<!-- fleet-local-review: head={HEAD} base={base} findings=0 -->",
            }
            pr["comments"] = {"nodes": [marker], "pageInfo": {"hasPreviousPage": False}}
        return pr

    def test_a_newer_quota_refusal_outranks_older_coverage_of_the_head(self) -> None:
        """A re-request answered by the limit is the newest word, whatever the head already had."""
        pr = payload(
            [
                review(at=EARLY, rid="PRR_a"),
                review(body=QUOTA_REFUSED, at=LATE, rid="PRR_b"),
            ]
        )
        self.answer(pr)
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("refusal=QUOTA", out)
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(41, self.cli(["wait", "7", "--timeout", "0"]))

    def test_an_older_file_count_refusal_still_yields_to_coverage(self) -> None:
        pr = payload([review(body=REFUSED, at=EARLY, rid="PRR_a"), review(at=LATE, rid="PRR_b")])
        self.answer(pr)
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("refusal=no", out)

    def test_a_later_round_on_the_head_spends_a_quota_refusal_there(self) -> None:
        """A quota refusal on the head, then a genuine round on that same head stating no
        coverage. The genuine round is the newer word, so the refusal is spent and nothing reads
        it as the account state. `--min-rounds 2` waits past the genuine round here, so the wait
        names that baseline rather than the refusal. `--min-rounds 1` and `status` both reach
        the coverage gap itself.
        """
        refused = review(body=QUOTA_REFUSED, at=EARLY, rid="PRR_refused")
        both = payload([refused, review(body=OVERVIEW, at=LATE, rid="PRR_genuine")])
        before = payload([refused], pending=True)
        wait = ["wait", "7", "--ignore-quota-signal", "--timeout", "0", "--min-rounds"]
        cases = (
            ([*wait, "2"], (before, both), 30, "--min-rounds 2 waits for round 3"),
            ([*wait, "1"], (before, both), 0, "coverage=none"),
            (["status", "7"], (both,), 0, "coverage=none"),
        )
        for argv, reads, code, said in cases:
            with self.subTest(argv=argv):
                self.out.seek(0)
                self.out.truncate()
                self.answer(*reads)
                self.wire_history([])
                with mock.patch.object(pr_review.time, "sleep"):
                    self.assertEqual(code, self.cli(argv))
                out = self.out.getvalue()
                self.assertIn(said, out)
                self.assertIn("refusal=no", out)
                self.assertNotIn("COPILOT_QUOTA_EXHAUSTED", out)

    def test_a_fix_push_after_the_first_round_is_not_requested(self) -> None:
        self.answer(self.into(payload([review(oid=OLD)])))
        calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(49, self.cli(["wait", "7", "--timeout", "0"]))
        slept.assert_not_called()
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))
        self.assertIn("status=AWAITING_LOCAL_PASS", self.out.getvalue())

    def test_an_attested_fix_push_closes_the_wait(self) -> None:
        self.answer(self.into(payload([review(oid=OLD)]), attest=True))
        calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))
        out = self.out.getvalue()
        self.assertIn("review_on_head=local", out)
        self.assertIn("coverage=local", out)

    def table_less(self, first: str = OVERVIEW, attest: bool = True) -> dict:
        """A first round on an earlier head, then a round on this head stating nothing."""
        rounds = [review(oid=OLD, body=first, rid="PRR_a"), review(body=OVERVIEW, rid="PRR_b")]
        return self.into(payload(rounds, files=["a.md"]), attest=attest)

    def test_an_attested_head_beside_a_table_less_round_is_covered(self) -> None:
        """The round on the head states nothing and carries no table, so the attestation covers it."""
        for cmd in (["status", "7"], ["wait", "7", "--timeout", "0"]):
            with self.subTest(cmd=cmd[0]):
                self.out.seek(0)
                self.out.truncate()
                self.answer(self.table_less())
                calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
                with mock.patch.object(pr_review.time, "sleep"):
                    self.assertEqual(0, self.cli(cmd))
                self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))
                out = self.out.getvalue()
                self.assertIn("review_on_head=yes", out)
                self.assertIn("coverage=local", out)
                self.assertIn("COVERAGE IS LOCAL", out)
                self.assertNotIn("coverage=none", out)

    def test_an_unattested_head_beside_a_table_less_round_stays_unstated(self) -> None:
        self.answer(self.table_less(attest=False))
        self.assertEqual(0, self.cli(["status", "7"]))
        self.assertIn("coverage=none", self.out.getvalue())

    def test_an_attested_head_beside_its_only_round_is_covered_on_a_feature_pull_request(
        self,
    ) -> None:
        """A table-less round on the head reads the attestation, whether or not it is the first."""
        self.answer(self.into(payload([review(body=OVERVIEW)], files=["a.md"]), attest=True))
        self.assertEqual(0, self.cli(["status", "7"]))
        out = self.out.getvalue()
        self.assertIn("review_on_head=yes", out)
        self.assertIn("coverage=local", out)
        self.assertNotIn("coverage=none", out)

    def test_the_same_head_on_a_promotion_stays_unstated(self) -> None:
        """A promotion keeps its own Copilot round, so its attestation gives `status` no cover."""
        pr = self.into(payload([review(body=OVERVIEW)], files=["a.md"]), base="main", attest=True)
        self.answer(pr)
        self.assertEqual(0, self.cli(["status", "7"]))
        out = self.out.getvalue()
        self.assertIn("coverage=none", out)
        self.assertNotIn("coverage=local", out)

    def test_a_second_round_on_a_feature_pull_request_is_not_requested(self) -> None:
        """Waiting for a second round holds for the local pass rather than requesting one."""
        self.answer(self.into(payload([review(body=OVERVIEW)], files=["a.md"]), attest=True))
        calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.cli(["wait", "7", "--timeout", "0", "--min-rounds", "2"])
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))

    def test_an_attested_head_beside_a_round_whose_table_skips_a_file_is_not_covered(self) -> None:
        """A table naming fewer files than the diff is the round's own word that it skipped one."""
        table = overview_v2(
            open_count="0 open findings",
            covers="",
            entries=0,
            verdict="### Approval recommended",
            paths=("a.md",),
        )
        rounds = [review(oid=OLD, body=OVERVIEW, rid="PRR_a"), review(body=table, rid="PRR_b")]
        self.answer(self.into(payload(rounds, files=["a.md", "b.md"]), attest=True))
        self.assertEqual(0, self.cli(["status", "7"]))
        out = self.out.getvalue()
        self.assertIn("coverage=none", out)
        self.assertNotIn("coverage=local", out)

    def test_a_partial_on_record_beside_a_table_less_round_is_not_covered(self) -> None:
        """A round that read part of a diff sends the pull request to the maintainer."""
        part = OVERVIEW + "\n<!-- fleet-review: reviewed=1 changed=2 findings=0 -->"
        for cmd in (["status", "7"], ["wait", "7", "--timeout", "0"]):
            with self.subTest(cmd=cmd[0]):
                self.out.seek(0)
                self.out.truncate()
                self.answer(self.table_less(first=part))
                self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
                with mock.patch.object(pr_review.time, "sleep"):
                    self.assertNotEqual(0, self.cli(cmd))
                self.assertNotIn("coverage=local", self.out.getvalue())

    def test_an_attested_head_with_a_check_running_at_the_timeout_is_pending(self) -> None:
        """Covered at once, the wait ended while CI ran and exit 0 read as a mergeable head."""
        running = check(status="IN_PROGRESS", conclusion="", started=real_ago(60))
        pr = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[running]), attest=True)
        self.answer(pr)
        calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))
        out = self.out.getvalue()
        self.assertIn("coverage=local", out)
        self.assertIn("status=CHECKS_PENDING", out)

    def test_an_attested_head_polls_its_checks_until_they_settle(self) -> None:
        running = check(status="IN_PROGRESS", conclusion="", started=real_ago(60))
        pending = self.into(
            payload([review(oid=OLD)], merge="BLOCKED", checks=[running]), attest=True
        )
        green = self.into(payload([review(oid=OLD)], checks=[check()]), attest=True)
        self.answer(pending, pending, green)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        slept.assert_called_once()
        self.assertNotIn("CHECKS_PENDING", self.out.getvalue())

    def fresh_head(self, run_status: str) -> dict:
        """A CLEAN head whose first-stage jobs passed while its workflow run has more to start.

        A matrix job's children enter the rollup only once the jobs they need finish. The rollup
        can therefore be all green while the run itself is not completed.
        """
        detect = check(name="changes", suite=20, workflow=7, suite_status=run_status, run=200)
        pr = self.into(payload([review(oid=OLD)], checks=[detect]), attest=True)
        suite = {
            "databaseId": 20,
            "status": run_status,
            "workflowRun": {"databaseId": 200, "workflow": {"databaseId": 7}},
        }
        pr["commits"]["nodes"][0]["commit"]["checkSuites"] = {"nodes": [suite]}
        return pr

    def test_a_fresh_head_with_its_run_in_progress_is_pending_at_the_timeout(self) -> None:
        """Read right after a push, a CLEAN merge and a green rollup exited 0 with jobs unstarted."""
        self.answer(self.fresh_head("IN_PROGRESS"))
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        out = self.out.getvalue()
        self.assertIn("status=CHECKS_PENDING", out)
        self.assertIn("not completed: run 200", out)

    def test_a_fresh_head_polls_until_its_run_completes(self) -> None:
        running = self.fresh_head("IN_PROGRESS")
        done = self.fresh_head("COMPLETED")
        self.answer(running, running, done)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        slept.assert_called_once()
        self.assertNotIn("CHECKS_PENDING", self.out.getvalue())

    def test_a_fresh_head_waiting_on_its_environment_is_pending_at_the_timeout(self) -> None:
        """A gated run's later jobs have not run either, so it holds like any other open run."""
        self.answer(self.fresh_head("WAITING"))
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertIn("not completed: run 200", self.out.getvalue())

    def test_a_stuck_check_on_an_attested_head_ends_the_poll(self) -> None:
        """A shape no wait clears is 44 at once, rather than the timeout polled out against it."""
        starved = check(status="QUEUED", conclusion="", started=real_ago(900))
        pr = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[starved]), attest=True)
        self.answer(pr)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(44, self.cli(["wait", "7"]))
        slept.assert_not_called()

    def test_a_failed_check_ends_the_poll_while_another_still_runs(self) -> None:
        """A BLOCKED merge carrying a stuck check is the 44 the review path returns too."""
        running = check(name="test", status="IN_PROGRESS", conclusion="", started=real_ago(60))
        failed = check(name="lint", conclusion="FAILURE", started=real_ago(60))
        pr = self.into(
            payload([review(oid=OLD)], merge="BLOCKED", checks=[failed, running]), attest=True
        )
        self.answer(pr)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(44, self.cli(["wait", "7", "--timeout", "1"]))
        slept.assert_not_called()

    def test_an_optional_failure_does_not_end_the_poll_on_a_required_check(self) -> None:
        optional = {**check(name="lint", conclusion="FAILURE"), "isRequired": False}
        gate = check(name="gate", status="IN_PROGRESS", conclusion="", started=real_ago(60))
        blocked = self.into(
            payload([review(oid=OLD)], merge="BLOCKED", checks=[optional, gate]), attest=True
        )
        passed = self.into(
            payload([review(oid=OLD)], merge="UNSTABLE", checks=[optional, check(name="gate")]),
            attest=True,
        )
        self.answer(blocked, blocked, passed)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        slept.assert_called_once()

    def test_a_required_check_running_at_the_timeout_outranks_an_optional_failure(self) -> None:
        """Waiting can still clear the merge, so the code is 30 rather than 44."""
        optional = {**check(name="coverage", conclusion="FAILURE"), "isRequired": False}
        gate = check(name="gate", status="IN_PROGRESS", conclusion="", started=real_ago(60))
        pr = self.into(
            payload([review(oid=OLD)], merge="BLOCKED", checks=[optional, gate]), attest=True
        )
        self.answer(pr)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertIn("status=CHECKS_PENDING", self.out.getvalue())

    def test_an_empty_rollup_holds_any_merge_but_a_conflicted_one(self) -> None:
        """A conflicted pull request runs no `pull_request` workflow, so none is coming.

        Its release names that state rather than exiting 0 over checks that never ran.
        """
        for merge, code in (
            ("BLOCKED", 30),
            ("BEHIND", 30),
            ("DRAFT", 30),
            ("UNKNOWN", 30),
            ("DIRTY", 45),
        ):
            with self.subTest(merge=merge):
                pr = self.into(payload([review(oid=OLD)], merge=merge, checks=[]), attest=True)
                self.answer(pr)
                self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
                self.out.seek(0)
                self.out.truncate()
                with mock.patch.object(pr_review.time, "sleep"):
                    self.assertEqual(code, self.cli(["wait", "7", "--timeout", "0"]))
                if code == 45:
                    self.assertIn("status=CHECKS_MISSING", self.out.getvalue())
                    self.assertIn(
                        "but no check has posted, so resolve the head's conflict",
                        self.out.getvalue(),
                    )

    def test_an_optional_check_does_not_hold_once_the_required_gate_concluded(self) -> None:
        """A merge BLOCKED on a thread is not waiting on a check nothing requires."""
        optional = {
            **check(name="coverage", status="IN_PROGRESS", conclusion="", started=real_ago(60)),
            "isRequired": False,
        }
        pr = self.into(
            payload([review(oid=OLD)], merge="BLOCKED", checks=[check(name="gate"), optional]),
            attest=True,
        )
        self.answer(pr)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7", "--timeout", "1"]))
        slept.assert_not_called()

    def test_optional_checks_hold_until_a_required_one_posts(self) -> None:
        """A required aggregator behind `needs:` is absent until the jobs it waits on finish."""
        optional = {
            **check(name="lint", status="IN_PROGRESS", conclusion="", started=real_ago(60)),
            "isRequired": False,
        }
        pr = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[optional]), attest=True)
        self.answer(pr)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertIn("status=CHECKS_PENDING", self.out.getvalue())

    def test_an_unattested_held_head_is_not_polled_for_its_checks(self) -> None:
        running = check(status="IN_PROGRESS", conclusion="", started=real_ago(60))
        self.answer(self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[running])))
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(49, self.cli(["wait", "7", "--timeout", "1"]))
        slept.assert_not_called()

    def rules(self, *contexts: str) -> mock.MagicMock:
        """Patch `gh_rest` to answer the base branch's rules read with these required contexts."""
        out = json.dumps({"n": 2, "c": [[c, None] for c in contexts]})
        return self.enterContext(
            mock.patch.object(
                pr_review, "gh_rest", return_value=subprocess.CompletedProcess([], 0, out, "")
            )
        )

    def test_a_required_check_not_yet_registered_holds_an_attested_head(self) -> None:
        """Five of five passed and BLOCKED is a push whose other workflows have not registered."""
        self.rules("Check pull request workflow status job", "Later workflow")
        short = self.into(
            payload([review(oid=OLD)], merge="BLOCKED", checks=[check()]), attest=True
        )
        full = self.into(
            payload([review(oid=OLD)], checks=[check(), check(name="Later workflow")]), attest=True
        )
        self.answer(short, short, full)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        slept.assert_called_once()
        self.out.seek(0)
        self.out.truncate()
        self.answer(short)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertIn("status=CHECKS_PENDING", self.out.getvalue())
        self.assertIn("required, unmet: Later workflow", self.out.getvalue())

    def test_every_required_check_registered_does_not_hold_a_blocked_head(self) -> None:
        self.rules("Check pull request workflow status job")
        pr = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[check()]), attest=True)
        self.answer(pr)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertNotEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        slept.assert_not_called()

    def test_a_required_check_unregistered_under_a_released_merge_exits_45(self) -> None:
        """A BEHIND or DIRTY merge is not held on an unregistered required check.

        Its release names the check rather than exiting 0 as though the required checks settled.
        """
        self.rules("Check pull request workflow status job", "Later workflow")
        for merge in ("BEHIND", "DIRTY"):
            with self.subTest(merge=merge):
                self.out.seek(0)
                self.out.truncate()
                pr = self.into(
                    payload([review(oid=OLD)], merge=merge, checks=[check()]), attest=True
                )
                self.answer(pr)
                self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
                with mock.patch.object(pr_review.time, "sleep") as slept:
                    self.assertEqual(45, self.cli(["wait", "7", "--timeout", "1"]))
                slept.assert_not_called()
                out = self.out.getvalue()
                self.assertIn("status=CHECKS_MISSING", out)
                self.assertIn(f"The merge reads {merge}", out)
                self.assertIn("came from another app, Later workflow, so update a BEHIND head", out)

    def test_a_released_head_exits_by_what_its_read_shows(self) -> None:
        """Another app's check, an unread payload, an ungated merge, and a closed pull request."""
        name = "Check pull request workflow status job"
        other_app = json.dumps({"n": 1, "c": [[name, 999]]})
        unread = payload([review(oid=OLD)], merge="DIRTY", checks=[check()], rollup_oid="d" * 40)
        closed = payload([review(oid=OLD)], merge="DIRTY", checks=[check()])
        cases = [
            (
                "another app",
                payload([review(oid=OLD)], merge="BEHIND", checks=[check()]),
                45,
                "came from another app, " + name,
            ),
            ("unread payload", unread, 45, "no commit matching the head"),
            (
                "partly unread rollup",
                payload(
                    [review(oid=OLD)],
                    merge="DIRTY",
                    checks=[check(name="lint"), {"__typename": "SomethingNew", "label": "x"}],
                ),
                45,
                "has not registered, sits in the rollup's unread part, or came from another app, "
                + name
                + ", so read the head commit's checks and statuses first, then update a BEHIND head",
            ),
            ("closed", {**closed, "state": "CLOSED"}, 0, ""),
        ] + [
            (merge, payload([review(oid=OLD)], merge=merge, checks=[]), 0, "")
            for merge in ("CLEAN", "UNSTABLE", "HAS_HOOKS")
        ]
        self.enterContext(
            mock.patch.object(
                pr_review,
                "gh_rest",
                return_value=subprocess.CompletedProcess([], 0, other_app, ""),
            )
        )
        for label, pr, code, reason in cases:
            with self.subTest(label):
                pr_review.required_contexts.cache_clear()
                self.out.seek(0)
                self.out.truncate()
                self.answer(self.into(pr, attest=True))
                self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
                with mock.patch.object(pr_review.time, "sleep"):
                    self.assertEqual(code, self.cli(["wait", "7", "--timeout", "1"]))
                if reason:
                    self.assertIn(reason, self.out.getvalue())
                if label == "another app":
                    self.assertNotIn("checks and statuses first", self.out.getvalue())
                if code == 0:
                    self.assertNotIn("status=CHECKS_MISSING", self.out.getvalue())

    def test_a_head_no_attestation_covers_reads_no_rules_for_exit_45(self) -> None:
        """A Copilot round covering the head ends the wait on its own verdict, judging no check."""
        self.answer(payload([review()], merge="DIRTY", checks=[]))
        with mock.patch.object(pr_review, "required_contexts") as read:
            self.assertEqual(0, self.cli(["wait", "7"]))
        read.assert_not_called()

    def test_a_released_merge_exits_0_where_the_unregistered_check_is_unproven(self) -> None:
        """Unread rules, a cut rollup, and a DRAFT merge each leave registration unjudged."""
        cut = self.into(payload([review(oid=OLD)], merge="BEHIND", checks=[check()]), attest=True)
        cut["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["pageInfo"] = {
            "hasNextPage": True
        }
        cases = {
            "unread": (
                self.into(
                    payload([review(oid=OLD)], merge="BEHIND", checks=[check()]), attest=True
                ),
                False,
            ),
            "truncated": (cut, True),
            "draft": (
                self.into(payload([review(oid=OLD)], merge="DRAFT", checks=[check()]), attest=True),
                True,
            ),
        }
        for label, (pr, rules) in cases.items():
            with self.subTest(label):
                pr_review.required_contexts.cache_clear()
                if rules:
                    self.rules("Check pull request workflow status job", "Later workflow")
                self.answer(pr)
                self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
                with mock.patch.object(pr_review.time, "sleep"):
                    self.assertEqual(0, self.cli(["wait", "7", "--timeout", "1"]))

    def test_a_released_merge_with_every_required_check_registered_exits_0(self) -> None:
        self.rules("Check pull request workflow status job")
        for merge in ("BEHIND", "DIRTY"):
            with self.subTest(merge=merge):
                pr = self.into(
                    payload([review(oid=OLD)], merge=merge, checks=[check()]), attest=True
                )
                self.answer(pr)
                self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
                with mock.patch.object(pr_review.time, "sleep"):
                    self.assertEqual(0, self.cli(["wait", "7", "--timeout", "1"]))

    def test_an_unread_rules_read_keeps_the_old_result_and_says_so(self) -> None:
        """The suite's default `gh_rest` fails, which must never read as nothing being required."""
        pr = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[check()]), attest=True)
        self.answer(pr)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertNotEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertIn("rules for develop were unread", self.out.getvalue())

    def test_a_check_nothing_requires_does_not_hold_an_attested_head(self) -> None:
        """A merge reading CLEAN, UNSTABLE, or HAS_HOOKS has no required check outstanding."""
        running = check(status="IN_PROGRESS", conclusion="", started=real_ago(60))
        for merge in ("CLEAN", "UNSTABLE", "HAS_HOOKS"):
            with self.subTest(merge=merge):
                pr = self.into(
                    payload([review(oid=OLD)], merge=merge, checks=[running]), attest=True
                )
                self.answer(pr)
                self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
                with mock.patch.object(pr_review.time, "sleep") as slept:
                    self.assertEqual(0, self.cli(["wait", "7", "--timeout", "0"]))
                slept.assert_not_called()

    def test_the_held_poll_reads_the_narrow_query_and_the_verdict_the_full_one(self) -> None:
        """The digest needs the threads and files the poll leaves out, so it reads `Q_FULL`.

        A poll that never sleeps grades its first read, so the narrow query costs it no call.
        """
        empty = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[]), attest=True)
        green = self.into(payload([review(oid=OLD)], checks=[check()]), attest=True)
        self.answer(empty, empty, green)
        gql = self.enterContext(mock.patch.object(pr_review, "gql", side_effect=pr_review.gql))
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7"]))
        queries = [c.args[0] for c in gql.call_args_list]
        self.assertEqual(
            [pr_review.Q_LIVE, pr_review.Q_FULL, pr_review.Q_HELD, pr_review.Q_FULL],
            queries,
        )
        self.out.seek(0)
        self.out.truncate()
        self.answer(green)
        gql = self.enterContext(mock.patch.object(pr_review, "gql", side_effect=pr_review.gql))
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        slept.assert_not_called()
        self.assertEqual(
            [pr_review.Q_LIVE, pr_review.Q_FULL], [c.args[0] for c in gql.call_args_list]
        )

    def test_a_held_poll_stops_only_on_a_full_read_that_agrees(self) -> None:
        """A narrow read ending the poll is confirmed by the full read the verdict grades.

        A merge reading UNKNOWN on that full read keeps the poll going, rather than returning 30
        with time left on a payload the poll never judged.
        """
        empty = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[]), attest=True)
        green = self.into(payload([review(oid=OLD)], checks=[check()]), attest=True)
        unknown = self.into(
            payload([review(oid=OLD)], merge="UNKNOWN", checks=[check()]), attest=True
        )
        self.answer(empty, empty, green, unknown, green)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        self.assertEqual(2, slept.call_count)

    def test_a_held_poll_s_own_full_read_is_read_again_where_its_file_list_came_back_empty(
        self,
    ) -> None:
        """A round landing mid-poll ends the hold, and the poll's own read becomes the verdict's."""
        running = check(status="IN_PROGRESS", conclusion="", started=real_ago(60))
        held = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[running]), attest=True)
        rounds = [
            review(oid=OLD, body=summarized(["a.py"], covers=""), at=EARLY, rid="A"),
            review(oid=HEAD, body=OVERVIEW + "\n**Findings:** None", at=LATE, rid="B"),
        ]
        landed = self.into(payload(rounds, checks=[check()], files=[]))
        served = self.answer(held, held, landed, landed, self.into(payload(rounds, files=["a.py"])))
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        compare = subprocess.CompletedProcess([], 0, json.dumps({"files": ["a.py"]}), "")
        with (
            mock.patch.object(pr_review, "READ_RETRY_DELAYS", (0,)),
            mock.patch.object(pr_review, "gh_rest", return_value=compare),
            mock.patch.object(pr_review.time, "sleep"),
        ):
            self.assertEqual(0, self.cli(["wait", "7", "--timeout", "600"]))
        self.assertIn("coverage=carried:table ", self.out.getvalue())
        self.assertEqual(5, served.call_count)

    def test_a_held_poll_timing_out_on_a_narrow_read_grades_a_full_one(self) -> None:
        """`Q_HELD` carries no threads or files, so the verdict at the timeout reads `Q_FULL`."""
        running = check(status="IN_PROGRESS", conclusion="", started=real_ago(60))
        held = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[running]), attest=True)
        self.answer(held)
        served = pr_review.gql

        def narrow(query: str, owner: str, repo: str, num: int) -> dict:
            pr = served(query, owner, repo, num)
            if query is pr_review.Q_HELD:
                return {k: v for k, v in pr.items() if k not in ("reviewThreads", "files")}
            return pr

        self.enterContext(mock.patch.object(pr_review, "gql", side_effect=narrow))
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        clock = iter([0.0, 0.0])
        with (
            mock.patch.object(pr_review.time, "monotonic", side_effect=lambda: next(clock, 1e9)),
            mock.patch.object(pr_review.time, "sleep") as slept,
        ):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "60"]))
        slept.assert_called_once()
        self.assertIn("status=CHECKS_PENDING", self.out.getvalue())

    def test_an_attested_head_with_no_checks_registered_yet_is_polled(self) -> None:
        """A wait run right after the push reads an empty rollup, which is CI not started."""
        empty = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[]), attest=True)
        green = self.into(payload([review(oid=OLD)], checks=[check()]), attest=True)
        self.answer(empty, empty, green)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7"]))
        slept.assert_called_once()
        self.out.seek(0)
        self.out.truncate()
        self.answer(empty)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertIn("status=CHECKS_PENDING", self.out.getvalue())

    def test_a_review_requested_during_a_held_wait_ends_the_poll(self) -> None:
        """A requested round is no longer held, so its checks are not what the wait is for."""
        running = check(status="IN_PROGRESS", conclusion="", started=real_ago(60))
        held = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[running]), attest=True)
        requested = self.into(
            payload([review(oid=OLD)], merge="BLOCKED", checks=[running], pending=True),
            attest=True,
        )
        self.answer(held, held, requested)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "1"]))
        slept.assert_called_once()
        self.assertNotIn("CHECKS_PENDING", self.out.getvalue())
        self.assertIn("status=PENDING", self.out.getvalue())

    def test_a_quota_reading_outranks_pending_after_a_mid_poll_request(self) -> None:
        running = check(status="IN_PROGRESS", conclusion="", started=real_ago(60))
        held = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[running]), attest=True)
        requested = self.into(
            payload([review(oid=OLD)], merge="BLOCKED", checks=[running], pending=True),
            attest=True,
        )
        self.answer(held, held, requested)
        self.wire_history([hist_review(962, QUOTA_REFUSED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(41, self.cli(["wait", "7", "--timeout", "1"]))
        self.assertNotIn("status=PENDING", self.out.getvalue())

    def test_a_round_on_the_head_outranks_the_repo_wide_quota_reading(self) -> None:
        """The repo-wide quota line says the head carries no Copilot review of its own, so it cannot fire
        where the final read shows one. Three rounds sit on the pull request with a re-request
        pending, the reviewer's newest word elsewhere is a quota refusal, and a fourth genuine
        round lands on the head mid-wait at a position `--min-rounds 4` does not credit. The
        three sit on the head in the issue's example, and on a superseded head in the case only
        the final read can catch. Each clock read advances 100 seconds, so one poll lands it and
        the read after it is past the 150-second timeout. The opening shape is answered twice,
        since the wait's liveness read and its snapshot each take one. The fourth round is stamped
        before the refusal, so that the head alone is what rules the repo-wide reading out.
        """
        for label, oid in (("on head", HEAD), ("superseded", OLD)):
            with self.subTest(label):
                self.out.seek(0)
                self.out.truncate()
                seen = [
                    review(oid=oid, rid=f"PRR_{i}", at=f"2026-08-02T10:0{i}:00Z") for i in range(3)
                ]
                fourth = review(rid="PRR_fourth", at="2026-08-02T10:20:00Z")
                start = payload(seen, pending=True)
                self.answer(start, start, payload([*seen, fourth]))
                self.wire_history([hist_review(962, QUOTA_REFUSED, at="2026-08-02T10:30:00Z")])
                clock = itertools.count(0, 100).__next__
                with (
                    mock.patch.object(pr_review.time, "sleep"),
                    mock.patch.object(pr_review.time, "monotonic", clock),
                ):
                    argv = ["wait", "7", "--min-rounds", "4", "--timeout", "150"]
                    self.assertEqual(30, self.cli(argv))
                out = self.out.getvalue()
                self.assertNotIn("COPILOT_QUOTA_EXHAUSTED_REPO_WIDE", out)
                self.assertIn("now has 4 rounds, up from 3", out)

    def test_a_round_already_on_the_head_still_requests_and_polls(self) -> None:
        """The reading assumes a head with no Copilot activity of its own, so a genuine round
        there when the wait begins must not skip the request and the poll on it either. Skipped,
        the wait ended at once on a bare pending line that no later wait would ever change.
        """
        self.answer(payload([review(at="2026-08-02T10:00:00Z")]))
        self.wire_history([hist_review(962, QUOTA_REFUSED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--min-rounds", "1", "--timeout", "0"]))
        out = self.out.getvalue()
        self.assertNotIn("COPILOT_QUOTA_EXHAUSTED_REPO_WIDE", out)
        self.assertNotIn("note: the reviewer's own most recent activity", out)
        self.assertIn("auto-request:", out)

    def test_a_push_off_the_reviewed_head_keeps_the_repo_wide_quota_reading(self) -> None:
        """A round on the head when the wait begins lets the request and poll run, but the
        reading itself stands, since a push during the wait can leave the final head with no
        Copilot activity of its own, which is exactly what the repo-wide reading reports. The opening shape is
        answered twice, since the wait's liveness read and its snapshot each take one.
        """
        start = payload([review(at="2026-08-02T10:00:00Z")], pending=True)
        moved = payload([review(oid=OLD, at="2026-08-02T10:00:00Z")], pending=True)
        self.answer(start, start, moved)
        self.wire_history([hist_review(962, QUOTA_REFUSED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(41, self.cli(["wait", "7", "--min-rounds", "1", "--timeout", "0"]))
        self.assertIn("status=COPILOT_QUOTA_EXHAUSTED_REPO_WIDE", self.out.getvalue())

    def test_a_round_newer_than_the_refusal_answers_the_repo_wide_quota_reading(self) -> None:
        """A review on this pull request newer than the refusal elsewhere answers it, the way
        `quota_signal` reads one anywhere, even where a push left that review off the head. That
        holds for a refusal other than the quota's too, since the reviewer ran to post it.
        """
        for label, body in (("genuine", None), ("file-count refusal", REFUSED)):
            with self.subTest(label):
                self.out.seek(0)
                self.out.truncate()
                start = payload([review(at="2026-08-02T10:00:00Z")], pending=True)
                newer = review(oid=OLD, rid="PRR_new", at=LATE)
                if body is not None:
                    newer = review(oid=OLD, rid="PRR_new", at=LATE, body=body)
                moved = payload([review(oid=OLD, at="2026-08-02T10:00:00Z"), newer])
                self.answer(start, start, moved)
                self.wire_history([hist_review(962, QUOTA_REFUSED, at="2026-08-02T10:30:00Z")])
                argv = ["wait", "7", "--min-rounds", "1", "--timeout", "0"]
                with mock.patch.object(pr_review.time, "sleep"):
                    self.assertEqual(30, self.cli(argv))
                self.assertNotIn("COPILOT_QUOTA_EXHAUSTED_REPO_WIDE", self.out.getvalue())

    def test_an_off_head_round_newer_than_the_refusal_opens_no_quota_stop(self) -> None:
        """The opening read answers the refusal the way the final one does, from this pull
        request's own reviews, since the history window can leave them out. Read only from the
        history, the wait skipped the request and the poll, then ended at once on a bare 30.
        """
        self.answer(payload([review(oid=OLD, at=LATE)]))
        self.wire_history([hist_review(962, QUOTA_REFUSED, at="2026-08-02T10:30:00Z")])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        out = self.out.getvalue()
        self.assertNotIn("note: the reviewer's own most recent activity", out)
        self.assertIn("auto-request:", out)

    def test_a_required_check_running_long_holds_the_poll_rather_than_ending_it(self) -> None:
        """Duration alone cannot tell a stalled job from a slow one, so waiting may clear it."""
        slow = check(name="gate", status="IN_PROGRESS", conclusion="", started=real_ago(2000))
        pr = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[slow]), attest=True)
        self.answer(pr)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertIn("status=CHECKS_PENDING", self.out.getvalue())

    def test_an_unreadable_rollup_is_not_held_as_one_not_yet_registered(self) -> None:
        """A rollup read off another commit is empty here, and waiting does not clear that.

        The release names the unread payload rather than exiting 0 over checks it never read.
        """
        pr = payload([review(oid=OLD)], merge="BLOCKED", checks=[check()], rollup_oid="d" * 40)
        self.answer(self.into(pr, attest=True))
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(45, self.cli(["wait", "7", "--timeout", "1"]))
        slept.assert_not_called()
        self.assertIn("status=CHECKS_MISSING", self.out.getvalue())
        self.assertIn("no commit matching the head", self.out.getvalue())

    def test_an_outside_answer_outranks_pending_after_a_mid_poll_request(self) -> None:
        running = check(status="IN_PROGRESS", conclusion="", started=real_ago(60))
        rounds = payload(
            [review(oid=OLD, at=EARLY)],
            merge="BLOCKED",
            checks=[running],
            comments=[comment(at=LATE)],
        )
        held = self.into(rounds, attest=True)
        held["comments"]["nodes"].append(comment(at=LATE))
        requested = {**held, "reviewRequests": payload([], pending=True)["reviewRequests"]}
        self.answer(held, held, requested)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(40, self.cli(["wait", "7", "--timeout", "1"]))
        self.assertNotIn("status=PENDING", self.out.getvalue())

    def test_a_merged_pull_request_is_not_held_on_its_unknown_merge(self) -> None:
        """GitHub reads UNKNOWN on a merged pull request for good, so holding it never ends."""
        pr = self.into(payload([review(oid=OLD)], merge="UNKNOWN", checks=[check()]), attest=True)
        self.answer({**pr, "state": "MERGED"})
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(0, self.cli(["wait", "7", "--timeout", "1"]))
        slept.assert_not_called()

    def test_a_push_during_the_held_poll_ends_it_on_the_unattested_head(self) -> None:
        running = check(status="IN_PROGRESS", conclusion="", started=real_ago(60))
        held = self.into(payload([review(oid=OLD)], merge="BLOCKED", checks=[running]), attest=True)
        moved = self.into(
            payload([review(oid=OLD)], merge="BLOCKED", checks=[running]), attest=True
        )
        moved["headRefOid"] = "c" * 40
        self.answer(held, held, moved)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(49, self.cli(["wait", "7", "--timeout", "1"]))
        slept.assert_called_once()

    def test_an_unknown_merge_holds_the_poll_over_a_failed_required_check(self) -> None:
        """GitHub recomputes the word after a check concludes, and the code is chosen by it."""
        failed = check(name="gate", conclusion="FAILURE", started=real_ago(60))
        unknown = self.into(
            payload([review(oid=OLD)], merge="UNKNOWN", checks=[failed]), attest=True
        )
        blocked = self.into(
            payload([review(oid=OLD)], merge="BLOCKED", checks=[failed]), attest=True
        )
        self.answer(unknown, unknown, blocked)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(44, self.cli(["wait", "7"]))
        slept.assert_called_once()

    def test_settling_is_a_check_waiting_can_still_conclude(self) -> None:
        nodes = pr_review_checks.check_nodes(
            payload(
                [review()],
                checks=[
                    check(name="running", status="IN_PROGRESS", conclusion=""),
                    check(name="queued", status="QUEUED", conclusion=""),
                    check(name="concluding", status="COMPLETED", conclusion=""),
                    check(name="starved", status="QUEUED", conclusion="", started=ago(900)),
                    check(name="long", status="IN_PROGRESS", conclusion="", started=ago(2000)),
                    check(name="passed"),
                    check(name="failed", conclusion="FAILURE"),
                    status_context("ci/building", state="PENDING"),
                    status_context("ci/expected", state="EXPECTED"),
                    status_context("ci/done"),
                    {"__typename": "UnknownContext"},
                ],
            )
        )
        settling = pr_review_checks.checks_settling(nodes, NOW, 300, 1800)
        self.assertEqual(
            ["running", "queued", "concluding", "long", "ci/building", "ci/expected"],
            [n["name"] for n in settling],
        )

    def test_request_asks_for_a_round_on_a_fix_push(self) -> None:
        self.answer(self.into(payload([review(oid=OLD)])))
        calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.cli(["wait", "7", "--timeout", "0", "--request"])
        self.assertEqual(1, len([c for c in calls if "requestReviews" in c[0]]))

    def test_a_feature_pull_request_with_no_round_is_never_requested(self) -> None:
        """Into a branch other than the default, the attested local pass alone covers the head."""
        covered = "its head is covered by an attested local pass"
        for attest, code, said, unsaid in (
            (False, 49, "No attested local pass covers this head yet", covered),
            (True, 0, covered, "No attested local pass covers this head yet"),
        ):
            with self.subTest(attest=attest):
                self.out.seek(0)
                self.out.truncate()
                self.answer(self.into(payload([]), attest=attest))
                calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
                with mock.patch.object(pr_review.time, "sleep"):
                    self.assertEqual(code, self.cli(["wait", "7", "--timeout", "0"]))
                self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))
                out = self.out.getvalue()
                self.assertIn(said, out)
                self.assertNotIn(unsaid, out)
                self.assertEqual(not attest, "Run attest once" in out)
                self.assertIn("status=AWAITING_LOCAL_PASS" if code else "coverage=local", out)

    def test_an_attested_head_copilot_refused_is_not_called_covered(self) -> None:
        """The refusal defeats the local cover, so the note matches the verdict rather than the attestation."""
        self.answer(self.into(payload([review(body=REFUSED)]), attest=True))
        self.wire_history([])
        with mock.patch.object(pr_review.time, "sleep"):
            code = self.cli(["wait", "7", "--ignore-quota-signal", "--timeout", "0"])
        out = self.out.getvalue()
        self.assertEqual(41, code)
        self.assertIn("No attested local pass covers this head yet", out)
        self.assertIn("a Copilot refusal on this head overrules it", out)
        self.assertNotIn("Run attest once", out)
        self.assertNotIn("its head is covered by an attested local pass", out)
        self.assertNotIn("coverage=local", out)

    def test_status_reads_a_feature_pull_request_with_no_round_from_its_attestation(
        self,
    ) -> None:
        self.answer(self.into(payload([]), attest=True))
        self.assertEqual(0, self.cli(["status", "7"]))
        out = self.out.getvalue()
        self.assertIn("review_on_head=local", out)
        self.assertIn("coverage=local", out)
        self.assertIn("COVERAGE IS LOCAL", out)

    def test_status_reads_no_local_cover_on_a_promotion_with_no_round(self) -> None:
        self.answer(self.into(payload([]), base="main", attest=True))
        self.assertEqual(0, self.cli(["status", "7"]))
        out = self.out.getvalue()
        self.assertIn("review_on_head=NO", out)
        self.assertNotIn("coverage=local", out)

    def test_a_promotion_is_still_requested(self) -> None:
        for name, pr in (
            ("earlier round", self.into(payload([review(oid=OLD)]), base="main", attest=True)),
            ("no round", self.into(payload([]), base="main")),
            ("attested, no round", self.into(payload([]), base="main", attest=True)),
        ):
            with self.subTest(case=name):
                self.out.seek(0)
                self.out.truncate()
                self.answer(pr)
                calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
                with mock.patch.object(pr_review.time, "sleep"):
                    self.cli(["wait", "7", "--timeout", "0"])
                self.assertEqual(1, len([c for c in calls if "requestReviews" in c[0]]))

    def test_an_attested_head_behind_a_stale_quota_refusal_is_covered(self) -> None:
        rounds = [
            review(oid="a" * 39 + "1", at=EARLY, rid="PRR_a"),
            review(oid=OLD, body=QUOTA_REFUSED, at=LATE, rid="PRR_b"),
        ]
        self.answer(self.into(payload(rounds), attest=True))
        self.wire_history([hist_review(7, QUOTA_REFUSED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7", "--timeout", "0"]))

    def test_a_partial_on_record_requests_a_round_rather_than_holding(self) -> None:
        part = OVERVIEW + "\n<!-- fleet-review: reviewed=1 changed=2 findings=0 -->"
        self.answer(self.into(payload([review(oid=OLD, body=part)]), attest=True))
        calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.cli(["wait", "7", "--timeout", "0"])
        self.assertEqual(1, len([c for c in calls if "requestReviews" in c[0]]))
        self.assertNotIn("AWAITING_LOCAL_PASS", self.out.getvalue())

    def test_an_attested_head_past_an_old_plain_answer_is_covered(self) -> None:
        pr = self.into(payload([review(oid=OLD, at=EARLY)], comments=[comment(at=LATE)]))
        attested = self.into(pr, attest=True)
        attested["comments"]["nodes"].append(comment(at=LATE))
        self.answer(attested)
        self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(0, self.cli(["wait", "7", "--timeout", "0"]))

    def test_a_pending_request_on_a_fix_push_is_polled_for(self) -> None:
        self.answer(self.into(payload([review(oid=OLD)], pending=True), attest=True))
        calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))

    def test_a_hold_decided_on_a_pushed_unattested_head_grades_that_head(self) -> None:
        """A push before the snapshot read moves the head the hold is decided for.

        No local pass attests the moved head, so the verdict is 49 and nothing is requested.
        """
        first = self.into(payload([review(oid=OLD)]), attest=True)
        moved = self.into(payload([review(oid=OLD)]), attest=True)
        moved["headRefOid"] = "c" * 40
        self.answer(first, moved)
        calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(49, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))

    def test_a_truncated_review_history_requests_a_round_rather_than_holding(self) -> None:
        self.answer(self.into(payload([review(oid=OLD)], older_reviews=True), attest=True))
        calls = self.wire_history([hist_review(7, OVERVIEW + "\n" + COVERED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.cli(["wait", "7", "--timeout", "0"])
        self.assertEqual(1, len([c for c in calls if "requestReviews" in c[0]]))

    def test_a_truncated_review_history_is_no_local_cover(self) -> None:
        pr = self.into(payload([review(oid=OLD)], older_reviews=True), attest=True)
        self.assertFalse(pr_review_rounds.local_cover(pr))

    def test_checkout_belongs_to_attest_alone(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pr_review.main(["status", "7", "--repo", "o/r", "--checkout", "."])

    def test_request_belongs_to_wait_alone(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pr_review.main(["status", "7", "--repo", "o/r", "--request"])

    def test_a_refusal_on_the_head_is_not_overruled_by_an_attestation(self) -> None:
        rounds = [
            review(oid=OLD, at=EARLY, rid="PRR_a"),
            review(body=REFUSED, at=LATE, rid="PRR_b"),
        ]
        pr = self.into(payload(rounds), attest=True)
        self.assertFalse(pr_review_rounds.local_cover(pr))

    def test_a_later_round_on_the_head_lets_an_attestation_past_a_spent_refusal(self) -> None:
        rounds = [
            review(oid=OLD, at=EARLY, rid="PRR_a"),
            review(body=QUOTA_REFUSED, at="2026-08-02T10:30:00Z", rid="PRR_b"),
            review(body=OVERVIEW, at=LATE, rid="PRR_c"),
        ]
        pr = self.into(payload(rounds), attest=True)
        self.assertTrue(pr_review_rounds.local_cover(pr))

    def test_status_reads_an_attested_head_unless_a_partial_is_on_record(self) -> None:
        part = OVERVIEW + "\n<!-- fleet-review: reviewed=1 changed=2 findings=0 -->"
        for rounds, field in (
            ([review(oid=OLD)], "review_on_head=local"),
            ([review(oid=OLD, body=part)], "review_on_head=NO"),
        ):
            with self.subTest(field=field):
                self.answer(self.into(payload(rounds), attest=True))
                out, _ = pr_review.digest("o", "r", 7)
                self.assertIn(field, out)

    def test_ignore_quota_signal_requests_past_the_repo_wide_signal(self) -> None:
        self.answer(payload([]))
        calls = self.wire_history([hist_review(962, QUOTA_REFUSED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.cli(["wait", "7", "--timeout", "0", "--ignore-quota-signal"])
        self.assertEqual(1, len([c for c in calls if "requestReviews" in c[0]]))

    def test_a_newer_comment_spends_an_earlier_head_refusal(self) -> None:
        pr = payload(
            [review(oid=OLD, body=ERROR_REFUSED, at=EARLY)],
            comments=[comment(at=LATE)],
        )
        self.assertIsNone(pr_review_rounds.stopping_refusal(pr))
        self.answer(pr)
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("refusal=no", out)

    def test_status_reports_an_error_round_on_an_earlier_head(self) -> None:
        self.answer(payload([review(oid=OLD, body=ERROR_REFUSED)]))
        out, _ = pr_review.digest("o", "r", 7)
        self.assertIn("review_on_head=NO", out)
        self.assertIn("refusal=ERROR", out)
        self.assertIn("COPILOT REFUSED THIS ROUND", out)

    def test_the_run_read_is_bounded_to_the_reviewer_s_failed_runs_before_the_round(self) -> None:
        seen: list[str] = []

        def rest(
            path: str, jq: str | None = None, raw: bool = False
        ) -> subprocess.CompletedProcess:
            seen.append(jq or "")
            return subprocess.CompletedProcess([], 1, "", "not read")

        with mock.patch.object(pr_review, "gh_rest", side_effect=rest):
            pr_review.run_cause("o", "r", HEAD, LATE)
        self.assertIn(f'.path == "{pr_review.COPILOT_RUN_PATH}"', seen[0])
        self.assertIn('.conclusion == "failure"', seen[0])
        self.assertIn(f'.created_at <= "{LATE}"', seen[0])

    def test_any_rate_limit_error_in_the_log_is_the_cause(self) -> None:
        log = "x errorType: 'network',\n" + RATE_LIMIT_LOG + "y errorType: 'internal',\n"
        self.run_log(log)
        cause = pr_review.run_cause("o", "r", HEAD, LATE)
        self.assertEqual("rate_limit", cause[0] if cause else None)

    def run_log(self, log: str | None) -> None:
        """Answer the three reads `run_cause` makes, or fail all three where `log` is None."""

        def rest(
            path: str, jq: str | None = None, raw: bool = False
        ) -> subprocess.CompletedProcess:
            if log is None:
                return subprocess.CompletedProcess([], 1, "", "not read")
            if "/actions/runs?" in path:
                return subprocess.CompletedProcess([], 0, "101\n", "")
            if path.endswith("/jobs"):
                return subprocess.CompletedProcess([], 0, "202\n", "")
            if path.endswith("/actions/jobs/202/logs") and raw:
                return subprocess.CompletedProcess([], 0, "\x1b[36;1m" + log, "")
            return subprocess.CompletedProcess([], 1, "", "unexpected read")

        self.enterContext(mock.patch.object(pr_review, "gh_rest", side_effect=rest))

    def test_an_error_round_on_an_earlier_head_stops_the_auto_request(self) -> None:
        """The weekly limit does not move with a push, so the next head is not requested into it.

        The liveness read carries no bodies, as the real query does not, so the stop is read
        from the full payload that follows it.
        """
        self.answer(self.bodyless(OLD), payload([review(oid=OLD, body=ERROR_REFUSED)]))
        calls = self.wire_history([hist_review(7, ERROR_REFUSED)])
        self.run_log(None)
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(41, self.cli(["wait", "7", "--timeout", "0"]))
        slept.assert_not_called()
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))
        self.assertIn("status=COPILOT_ERROR_POSSIBLE_QUOTA", self.out.getvalue())

    def test_a_rate_limit_in_the_run_log_confirms_the_quota_and_names_the_reset(self) -> None:
        self.answer(self.bodyless(OLD), payload([review(oid=OLD, body=ERROR_REFUSED)]))
        self.wire_history([hist_review(7, ERROR_REFUSED)])
        self.run_log(RATE_LIMIT_LOG)
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(41, self.cli(["wait", "7"]))
        out = self.out.getvalue()
        self.assertIn("status=COPILOT_QUOTA_EXHAUSTED", out)
        self.assertIn("reset on August 9, 2026 at 12:00 AM. Do not re-request", out)

    def test_ignore_quota_signal_requests_past_an_error_round(self) -> None:
        self.answer(payload([review(oid=OLD, body=ERROR_REFUSED)]))
        calls = self.wire_history([hist_review(7, ERROR_REFUSED)])
        with mock.patch.object(pr_review.time, "sleep"):
            self.cli(["wait", "7", "--timeout", "0", "--ignore-quota-signal"])
        self.assertEqual(1, len([c for c in calls if "requestReviews" in c[0]]))

    def test_a_genuine_round_since_the_error_spends_it(self) -> None:
        pr = payload(
            [
                review(oid=OLD, body=ERROR_REFUSED, at=EARLY, rid="PRR_a"),
                review(oid=OLD, body=OVERVIEW + "\n" + COVERED, at=LATE, rid="PRR_b"),
            ]
        )
        self.assertIsNone(pr_review_rounds.stopping_refusal(pr))

    def test_the_digest_reads_an_error_round_by_what_its_run_logged(self) -> None:
        for log, field, line in (
            (None, "refusal=ERROR", False),
            (RATE_LIMIT_LOG, "refusal=QUOTA", True),
            ("2026-08-02T10:59:00Z   errorType: 'internal',\n", "refusal=ERROR", False),
        ):
            with self.subTest(field=field, line=line):
                pr_review.run_cause.cache_clear()
                self.answer(payload([review(body=ERROR_REFUSED)]))
                self.run_log(log)
                out, _ = pr_review.digest("o", "r", 7)
                self.assertIn(field, out)
                self.assertEqual(line, "RATE LIMIT FROM THE REVIEWER'S RUN LOG" in out)

    def test_a_transient_fetch_failure_in_the_run_log_is_its_own_cause(self) -> None:
        for log, want in (
            (TRANSIENT_LOG, ("", "", TRANSIENT_LINE)),
            ("x errorType: 'internal',\n" + TRANSIENT_LOG, ("internal", "", TRANSIENT_LINE)),
            ("x errorType: 'internal',\n", ("internal", "", "")),
            ("nothing the reader names\n", None),
        ):
            with self.subTest(want=want):
                pr_review.run_cause.cache_clear()
                self.run_log(log)
                self.assertEqual(want, pr_review.run_cause("o", "r", HEAD, LATE))

    def test_a_logged_rate_limit_outranks_a_transient_line(self) -> None:
        self.run_log(TRANSIENT_LOG + RATE_LIMIT_LOG)
        node = review(body=ERROR_REFUSED, at=LATE)
        self.assertEqual("", pr_review.transient_error("o", "r", node))
        self.assertIn("reset on August 9", pr_review.confirmed_quota("o", "r", node))

    def test_a_transient_error_round_is_re_requested_once_and_reviewed(self) -> None:
        """The refusal on the current head is the issue's own case, and an earlier head behaves alike."""
        for oid in (HEAD, OLD):
            with self.subTest(head=oid == HEAD):
                pr_review.run_cause.cache_clear()
                self.out.seek(0)
                self.out.truncate()
                refused = review(oid=oid, body=ERROR_REFUSED, at=EARLY, rid="PRR_a")
                answered = payload([refused, review(at=LATE, rid="PRR_b")])
                self.answer(self.bodyless(OLD), payload([refused]), answered)
                calls = self.wire_history([hist_review(7, ERROR_REFUSED)])
                self.run_log(TRANSIENT_LOG)
                with mock.patch.object(pr_review.time, "sleep") as slept:
                    self.assertEqual(0, self.cli(["wait", "7"]))
                self.assertEqual(1, slept.call_count)
                self.assertEqual(1, len([c for c in calls if "requestReviews" in c[0]]))
                out = self.out.getvalue()
                self.assertIn("so this is its one re-request: " + TRANSIENT_LINE, out)
                self.assertNotIn("COPILOT_ERROR_POSSIBLE_QUOTA", out)

    def test_a_second_error_round_after_the_re_request_ends_the_poll_as_quota(self) -> None:
        """The second refusal is no head review, so only the round count ends the poll early."""
        first = review(oid=OLD, body=ERROR_REFUSED, at=EARLY, rid="PRR_a")
        again = payload([first, review(body=ERROR_REFUSED, at=LATE, rid="PRR_b")])
        self.answer(self.bodyless(OLD), payload([first]), again)
        calls = self.wire_history([hist_review(7, ERROR_REFUSED)])
        self.run_log(TRANSIENT_LOG)
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(41, self.cli(["wait", "7"]))
        self.assertEqual(1, slept.call_count)
        self.assertEqual(1, len([c for c in calls if "requestReviews" in c[0]]))
        out = self.out.getvalue()
        self.assertIn("status=COPILOT_ERROR_POSSIBLE_QUOTA", out)
        self.assertIn("but the round before it was an error refusal too", out)

    def test_two_error_rounds_in_a_row_request_nothing_whatever_the_log_says(self) -> None:
        rounds = [
            review(oid=OLD, body=ERROR_REFUSED, at=EARLY, rid="PRR_a"),
            review(oid=OLD, body=ERROR_REFUSED, at=LATE, rid="PRR_b"),
        ]
        self.answer(self.bodyless(OLD), payload(rounds))
        calls = self.wire_history([hist_review(7, ERROR_REFUSED)])
        self.run_log(TRANSIENT_LOG)
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(41, self.cli(["wait", "7", "--timeout", "0"]))
        slept.assert_not_called()
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))
        self.assertIn("but the round before it was an error refusal too", self.out.getvalue())

    def test_a_pending_re_request_with_no_round_by_the_timeout_is_pending(self) -> None:
        refused = [review(oid=OLD, body=ERROR_REFUSED, at=EARLY, rid="PRR_a")]
        self.answer(self.bodyless(OLD), payload(refused), payload(refused, pending=True))
        calls = self.wire_history([hist_review(7, ERROR_REFUSED)])
        self.run_log(TRANSIENT_LOG)
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(30, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertEqual(1, len([c for c in calls if "requestReviews" in c[0]]))
        reads = [q for q, _ in calls if "REVIEW_REQUESTED_EVENT" in q and "requestReviews" not in q]
        self.assertEqual(3, len(reads), "the first payload, the request, then one the last shares")
        out = self.out.getvalue()
        self.assertIn("status=PENDING the newest Copilot review is an error refusal", out)
        self.assertNotIn("COPILOT_ERROR_POSSIBLE_QUOTA", out)

    def test_a_re_request_already_sent_is_not_sent_again(self) -> None:
        """The bound counts requests rather than rounds, so a request that vanished still spends it."""
        sent = SENT_AFTER
        self.answer(self.bodyless(OLD), payload([review(oid=OLD, body=ERROR_REFUSED, at=EARLY)]))
        calls = self.wire_history([hist_review(7, ERROR_REFUSED)], (sent, sent))
        self.run_log(TRANSIENT_LOG)
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(41, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))
        self.assertIn("one re-request was sent with no round answering it", self.out.getvalue())

    def test_a_re_request_recording_nothing_is_reported_as_unrecorded(self) -> None:
        refused = payload([review(oid=OLD, body=ERROR_REFUSED, at=EARLY)])
        self.answer(self.bodyless(OLD), refused)
        self.wire_history([hist_review(7, ERROR_REFUSED)], (request_state(), request_state()))
        self.run_log(TRANSIENT_LOG)
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(40, self.cli(["wait", "7", "--timeout", "0"]))
        out = self.out.getvalue()
        self.assertIn("status=REQUEST_NOT_RECORDED", out)
        self.assertNotIn("this is its one re-request", out)
        self.assertNotIn("COPILOT_ERROR_TRANSIENT", out)

    def test_a_wait_on_a_pending_re_request_ends_on_the_next_round(self) -> None:
        """A second refusal is no head review, so the round count is what ends this poll early."""
        first = review(oid=OLD, body=ERROR_REFUSED, at=EARLY, rid="PRR_a")
        live = payload([{k: v for k, v in first.items() if k != "body"}], pending=True)
        again = payload([first, review(body=ERROR_REFUSED, at=LATE, rid="PRR_b")])
        self.answer(live, payload([first], pending=True), again)
        calls = self.wire_history([hist_review(7, ERROR_REFUSED)], (SENT_AFTER, SENT_AFTER))
        self.run_log(TRANSIENT_LOG)
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual(41, self.cli(["wait", "7"]))
        self.assertEqual(1, slept.call_count)
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))
        self.assertIn("but the round before it was an error refusal too", self.out.getvalue())

    def test_a_held_head_names_the_owed_re_request_rather_than_quota(self) -> None:
        self.answer(self.into(payload([review(oid=OLD, body=ERROR_REFUSED)])))
        calls = self.wire_history([hist_review(7, ERROR_REFUSED)])
        self.run_log(TRANSIENT_LOG)
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(41, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))
        out = self.out.getvalue()
        self.assertIn("status=COPILOT_ERROR_TRANSIENT", out)
        self.assertIn("pass --request to send it", out)

    def test_an_unread_run_log_keeps_the_quota_reading_and_says_so(self) -> None:
        self.answer(self.bodyless(OLD), payload([review(oid=OLD, body=ERROR_REFUSED)]))
        calls = self.wire_history([hist_review(7, ERROR_REFUSED)])
        self.run_log(None)
        with mock.patch.object(pr_review.time, "sleep"):
            self.assertEqual(41, self.cli(["wait", "7", "--timeout", "0"]))
        self.assertEqual(0, len([c for c in calls if "requestReviews" in c[0]]))
        self.assertIn("Its run log was unread or names neither", self.out.getvalue())

    def test_the_digest_names_the_run_log_reading_it_took(self) -> None:
        one = [review(body=ERROR_REFUSED, at=LATE, rid="PRR_a")]
        two = [review(oid=OLD, body=ERROR_REFUSED, at=EARLY, rid="PRR_z"), *one]
        early = [review(body=ERROR_REFUSED, at=EARLY, rid="PRR_a")]
        transient = "RUN LOG READING: transient. The run log names"
        spent = "possible quota. The run log names a fetch failure, but the round"
        sent = "possible quota. The one re-request it was owed was sent"
        unread = "RUN LOG READING: possible quota. The run log was unread"
        for rounds, log, states, line in (
            (one, TRANSIENT_LOG, RECORDED, transient),
            (two, TRANSIENT_LOG, RECORDED, spent),
            (early, TRANSIENT_LOG, (SENT_AFTER, SENT_AFTER), sent),
            (one, None, RECORDED, unread),
            (one, RATE_LIMIT_LOG, RECORDED, None),
        ):
            with self.subTest(line=line):
                pr_review.run_cause.cache_clear()
                self.answer(payload(rounds))
                self.wire_history([], states)
                self.run_log(log)
                out, _ = pr_review.digest("o", "r", 7)
                self.assertIn("refusal=QUOTA" if line is None else "refusal=ERROR", out)
                if line is None:
                    self.assertNotIn("RUN LOG READING", out)
                else:
                    self.assertIn(line, out)

    def test_the_run_log_line_sits_inside_the_refusal_block_for_either_reading(self) -> None:
        """A line printed after the block reads as advice of its own, apart from the refusal.

        An open thread prints after the block, so a line appended to the digest's end lands past it.
        Only the refusal's own body, indented under the marker, may sit between the two.
        """
        open_thread = [thread("PRRT_after", login="someone", body="A finding after the block.")]
        for body, log, field, want in (
            (ERROR_REFUSED, TRANSIENT_LOG, "refusal=ERROR", "  RUN LOG READING: transient."),
            (ERROR_REFUSED, None, "refusal=ERROR", "  RUN LOG READING: possible quota."),
            (ERROR_REFUSED, RATE_LIMIT_LOG, "refusal=QUOTA", "  RATE LIMIT FROM THE REVIEWER'S"),
            (QUOTA_REFUSED, None, "refusal=QUOTA", None),
        ):
            with self.subTest(field=field, want=want):
                pr_review.run_cause.cache_clear()
                self.answer(payload([review(body=body, at=LATE)], threads=open_thread))
                self.wire_history([], RECORDED)
                self.run_log(log)
                out, _ = pr_review.digest("o", "r", 7)
                self.assertIn(field, out)
                self.assertNotIn("re-requesting repeats it", out)
                self.assertIn("say whether a re-request is owed or repeats the refusal", out)
                lines = out.splitlines()
                start = next(i for i, ln in enumerate(lines) if "COPILOT REFUSED" in ln)
                after = next(i for i, ln in enumerate(lines) if "PRRT_after" in ln)
                if want is None:
                    self.assertNotIn("RUN LOG READING", out)
                    self.assertNotIn("RATE LIMIT FROM THE REVIEWER'S RUN LOG", out)
                    continue
                at = next(i for i, ln in enumerate(lines) if ln.startswith(want))
                self.assertTrue(all(ln.startswith("    ") for ln in lines[start + 1 : at]))
                self.assertLess(at, after)


class TestCopilotHistoryReadings(GqlCase):
    """The readings built from the reviewer's own review history across the repository."""

    def test_history_sorts_by_timestamp_regardless_of_pull_request_order(self) -> None:
        """The query orders pull requests by their own update time, not by when each round
        landed, and the sort key is that timestamp specifically rather than the pull request
        number or the query's own order.

        Three pull requests whose number order, timestamp order, and mocked query order are
        each different from the other two: ascending by number is 900, 950, 962, the query
        lists them 950, 962, 900, and only descending by `_at` gives the expected 962, 900, 950.
        A reader trusting the query's own order, or sorting by number in either direction,
        produces a different sequence than the one this asserts.
        """
        self.answer(payload([]))
        self.enterContext(
            mock.patch.object(
                pr_review,
                "gh_graphql",
                return_value={
                    "repository": {
                        "pullRequests": {
                            "nodes": [
                                hist_review(950, QUOTA_REFUSED, at=EARLY),
                                hist_review(962, QUOTA_REFUSED, at=LATE),
                                hist_review(900, QUOTA_REFUSED, at="2026-08-02T10:30:00Z"),
                            ]
                        }
                    }
                },
            )
        )
        history = pr_review.copilot_history("o", "r")
        self.assertEqual([962, 900, 950], [number for number, _ in history])

    def test_a_later_comment_outranks_an_earlier_review_in_the_merged_history(self) -> None:
        """A plain comment answers the same way `answered_outside_review` reads one per pull request."""
        self.answer(payload([]))
        self.enterContext(
            mock.patch.object(
                pr_review,
                "gh_graphql",
                return_value={
                    "repository": {
                        "pullRequests": {
                            "nodes": [
                                {
                                    "number": 962,
                                    "reviews": {
                                        "nodes": [
                                            {
                                                "author": {
                                                    "__typename": "Bot",
                                                    "login": pr_review_rounds.REVIEWER,
                                                    "id": "BOT_1",
                                                },
                                                "state": "COMMENTED",
                                                "body": QUOTA_REFUSED,
                                                "submittedAt": EARLY,
                                            }
                                        ]
                                    },
                                    "comments": {
                                        "nodes": [
                                            {
                                                "author": {"login": pr_review_rounds.REVIEWER},
                                                "body": "Reviewed and it looks fine.",
                                                "createdAt": LATE,
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                },
            )
        )
        history = pr_review.copilot_history("o", "r")
        self.assertEqual("comment", history[0][1]["_kind"])
        self.assertIsNone(pr_review_rounds.quota_signal(history))

    def test_copilot_history_carries_the_callers_own_pull_request_too(self) -> None:
        """No pull request is special-cased out of the fetch, the caller's own included: an
        earlier round on it is a valid id for `copilot_bot_id` and real evidence for
        `quota_signal` alike, neither of which this function decides on its own."""
        self.answer(payload([]))
        self.enterContext(
            mock.patch.object(
                pr_review,
                "gh_graphql",
                return_value={
                    "repository": {
                        "pullRequests": {
                            "nodes": [
                                hist_review(969, "Ordinary prose.", at=LATE),
                                hist_review(962, QUOTA_REFUSED, at=EARLY),
                            ]
                        }
                    }
                },
            )
        )
        history = pr_review.copilot_history("o", "r")
        self.assertEqual([969, 962], [number for number, _ in history])

    def test_an_empty_narrow_window_widens_before_giving_up(self) -> None:
        """The gap the wider window fixes: an outage outlasting HISTORY_PRS pull requests must
        not silently revert every caller to blind polling for the rest of the outage."""
        self.answer(payload([]))
        seen: list[object] = []

        def fake(_query: str, **variables: object) -> dict:
            seen.append(variables["prs"])
            if variables["prs"] == pr_review.HISTORY_PRS:
                return {"repository": {"pullRequests": {"nodes": []}}}
            return {"repository": {"pullRequests": {"nodes": [hist_review(900, QUOTA_REFUSED)]}}}

        self.enterContext(mock.patch.object(pr_review, "gh_graphql", side_effect=fake))
        history = pr_review.copilot_history("o", "r")
        self.assertEqual([pr_review.HISTORY_PRS, pr_review.HISTORY_PRS_WIDE], seen)
        self.assertEqual([900], [number for number, _ in history])

    def test_a_narrow_window_carrying_history_never_widens(self) -> None:
        """The ordinary case costs one call, not two."""
        self.answer(payload([]))
        seen: list[object] = []

        def fake(_query: str, **variables: object) -> dict:
            seen.append(variables["prs"])
            return {"repository": {"pullRequests": {"nodes": [hist_review(900, QUOTA_REFUSED)]}}}

        self.enterContext(mock.patch.object(pr_review, "gh_graphql", side_effect=fake))
        pr_review.copilot_history("o", "r")
        self.assertEqual([pr_review.HISTORY_PRS], seen)

    def test_a_comment_only_narrow_window_still_widens(self) -> None:
        """A narrow window carrying activity is not itself a usable bot id: a comment carries
        none, per `copilot_bot_id`, so this must widen the same as an empty window would rather
        than returning a history no caller can read a bot id from."""
        self.answer(payload([]))
        seen: list[object] = []
        narrow_node = {
            "number": 970,
            "reviews": {"nodes": []},
            "comments": {"nodes": [comment(at=LATE)]},
        }

        def fake(_query: str, **variables: object) -> dict:
            seen.append(variables["prs"])
            if variables["prs"] == pr_review.HISTORY_PRS:
                return {"repository": {"pullRequests": {"nodes": [narrow_node]}}}
            return {"repository": {"pullRequests": {"nodes": [hist_review(900, QUOTA_REFUSED)]}}}

        self.enterContext(mock.patch.object(pr_review, "gh_graphql", side_effect=fake))
        history = pr_review.copilot_history("o", "r")
        self.assertEqual([pr_review.HISTORY_PRS, pr_review.HISTORY_PRS_WIDE], seen)
        self.assertEqual("BOT_1", pr_review_rounds.copilot_bot_id(history))

    def test_both_windows_empty_still_carries_no_signal_and_no_bot_id(self) -> None:
        """An outage wide enough to empty HISTORY_PRS_WIDE too is a real, if rarer, case: still
        no id to request with and no fabricated one, rather than a crash on the second call."""
        self.answer(payload([]))
        self.enterContext(
            mock.patch.object(
                pr_review,
                "gh_graphql",
                return_value={"repository": {"pullRequests": {"nodes": []}}},
            )
        )
        history = pr_review.copilot_history("o", "r")
        self.assertEqual([], history)
        self.assertIsNone(pr_review_rounds.copilot_bot_id(history))

    def test_an_empty_history_carries_no_signal_and_no_bot_id(self) -> None:
        self.assertIsNone(pr_review_rounds.quota_signal([]))
        self.assertIsNone(pr_review_rounds.copilot_bot_id([]))

    def test_the_bot_id_is_read_from_the_first_review_entry_that_carries_one(self) -> None:
        history: list[tuple[int, dict]] = [
            (1, {"_kind": "review", "author": {"id": None}}),
            (2, {"_kind": "review", "author": {"id": "BOT_9"}}),
        ]
        self.assertEqual("BOT_9", pr_review_rounds.copilot_bot_id(history))

    def test_a_comment_entry_carries_no_id_to_read_even_if_it_had_one(self) -> None:
        """The id field belongs to the review connection's `... on Bot{ id }` selection only."""
        history: list[tuple[int, dict]] = [(1, {"_kind": "comment", "author": {"id": "BOT_9"}})]
        self.assertIsNone(pr_review_rounds.copilot_bot_id(history))

    def test_the_signal_names_the_pull_request_the_refusal_came_from(self) -> None:
        history = [(962, {"_kind": "review", "_at": LATE, "body": QUOTA_REFUSED})]
        signal = pr_review_rounds.quota_signal(history)
        self.assertIsNotNone(signal)
        assert signal is not None
        number, node = signal
        self.assertEqual(962, number)
        self.assertEqual(LATE, node.get("_at"))

    def test_a_comment_sourced_signal_names_the_same_way(self) -> None:
        """`_at` is normalized from `createdAt` for a comment, the same as `submittedAt` for a
        review, so the caller printing it needs no branch on which connection it came from."""
        history = [(962, {"_kind": "comment", "_at": LATE, "body": QUOTA_REFUSED})]
        signal = pr_review_rounds.quota_signal(history)
        self.assertIsNotNone(signal)
        assert signal is not None
        number, node = signal
        self.assertEqual(962, number)
        self.assertEqual(LATE, node.get("_at"))

    def test_a_generic_refusal_is_not_a_quota_signal(self) -> None:
        history = [(1, {"_kind": "review", "_at": LATE, "body": REFUSED})]
        self.assertIsNone(pr_review_rounds.quota_signal(history))


def rthread(
    tid: str,
    body: str = "The retry count is off by one.",
    path: str = "a.py",
    line: int = 12,
    resolved: bool = False,
    login: str = pr_review_rounds.REVIEWER,
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


class TestCommentAcknowledgesAnUnplacedSet(CommentCase):
    def test_the_outcomes_post_with_the_marker_the_gate_reads(self) -> None:
        held = payload([review(body=unplaced())], files=["a.py"])
        hold = pr_review_rounds.shape_hold(held)
        assert hold is not None
        sent = f"{COMMENT_BODY}\n\n<!-- fleet-shape-ack: fingerprint={hold[0]} -->"
        self.wire({"id": "PR_from_query", "url": COMMENT["url"]}, {**COMMENT, "body": sent})
        argv = ["comment", "7", "--repo", "o/r", "--fingerprint", hold[0], "--body", COMMENT_BODY]
        self.assertEqual(0, pr_review.main(argv))
        mutation = next((v for q, v in self.calls if "addComment" in q))
        self.assertEqual(sent, mutation["body"])
        answered = {**held, "comments": {"nodes": [ack(hold[0], "OWNER", text=sent)]}}
        self.assertEqual((hold[0], hold[1], True), pr_review_rounds.shape_hold(answered))

    def test_the_fingerprint_belongs_to_comment_alone(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pr_review.main(["status", "7", "--repo", "o/r", "--fingerprint", "0" * 12])


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

    def test_a_finding_posted_twice_refuses_and_names_the_flag(self) -> None:
        """No wording selects one of two identical threads, so the refusal names the way out."""
        self.wire(page([rthread("t1", line=118), rthread("t2", line=399)]))
        self.assertEqual(61, self.run_reply("--resolve"))
        self.assertFalse(self.wrote())
        self.assertIn("--all-identical", self.out.getvalue())

    def test_all_identical_answers_and_resolves_every_copy(self) -> None:
        ids = []

        def capture(query: str, **variables: object) -> dict:
            if "reviewThreads" in query:
                threads = [rthread("t1", line=118), rthread("t2", line=399)]
                return {"repository": {"pullRequest": {"reviewThreads": page(threads)}}}
            ids.append(variables.get("threadId"))
            if "addPullRequestReviewThreadReply" in query:
                return {"addPullRequestReviewThreadReply": {"comment": LANDED}}
            return {"resolveReviewThread": {"thread": {"isResolved": True}}}

        with mock.patch.object(pr_review, "gh_graphql", side_effect=capture):
            self.assertEqual(0, self.run_reply("--resolve", "--all-identical"))
        self.assertEqual(["t1", "t1", "t2", "t2"], ids)
        self.assertIn("REPLIED_AND_RESOLVED (2 identical threads)", self.out.getvalue())

    def test_all_identical_without_resolve_names_the_count_and_leaves_both_open(self) -> None:
        self.wire(page([rthread("t1", line=118), rthread("t2", line=399)]))
        self.assertEqual(0, self.run_reply("--all-identical"))
        self.assertFalse(self.resolved_a_thread())
        self.assertIn("status=REPLIED (2 identical threads)", self.out.getvalue())

    def test_all_identical_refuses_the_same_text_on_two_files(self) -> None:
        """Generic text on two files is two findings, and `--path` already selects between them."""
        self.wire(page([rthread("t1", path="a.py"), rthread("t2", path="b.py")]))
        self.assertEqual(61, self.run_reply("--resolve", "--all-identical"))
        self.assertFalse(self.wrote())
        self.assertIn("add --path", self.out.getvalue())
        self.assertNotIn("pass --all-identical", self.out.getvalue())

    def test_all_identical_compares_in_the_folded_form_match_reads(self) -> None:
        """Bodies differing only where `--match` cannot see are still one finding to it."""
        self.wire(
            page(
                [
                    rthread("t1", body="The retry count\nis off \u2014 by one."),
                    rthread("t2", body="The Retry Count is off - by one."),
                ]
            )
        )
        self.assertEqual(0, self.run_reply("--resolve", "--all-identical"))
        self.assertIn("REPLIED_AND_RESOLVED", self.out.getvalue())

    def test_all_identical_still_refuses_findings_that_only_share_words(self) -> None:
        """Two findings quoting the same words are two findings, and one body answers neither."""
        self.wire(
            page(
                [
                    rthread("t1", body="The retry count is off by one."),
                    rthread("t2", body="The retry count is never logged."),
                ]
            )
        )
        self.assertEqual(61, self.run_reply("--resolve", "--all-identical"))
        self.assertFalse(self.wrote())
        self.assertIn("these differ", self.out.getvalue())

    def test_all_identical_says_none_was_attempted_after_a_first_copy_fails(self) -> None:
        """A failure on the first copy still leaves the others unanswered, and the output says so."""
        self.wire(page([rthread("t1"), rthread("t2")]), reply={"id": "c1", "url": None, "body": ""})
        self.assertEqual(62, self.run_reply("--resolve", "--all-identical"))
        self.assertFalse(self.resolved_a_thread())
        self.assertIn(
            "0 of 2 identical threads are confirmed to carry this reply", self.out.getvalue()
        )

    def test_all_identical_stops_at_the_first_unconfirmed_reply(self) -> None:
        """A later copy is not attempted past a failure, and the count says what already landed."""
        replies = [LANDED, {"id": "c2", "url": None, "body": ""}]
        ids = []

        def fake(query: str, **variables: object) -> dict:
            if "reviewThreads" in query:
                threads = [rthread("t1"), rthread("t2"), rthread("t3")]
                return {"repository": {"pullRequest": {"reviewThreads": page(threads)}}}
            ids.append(variables.get("threadId"))
            if "addPullRequestReviewThreadReply" in query:
                return {"addPullRequestReviewThreadReply": {"comment": replies.pop(0)}}
            return {"resolveReviewThread": {"thread": {"isResolved": True}}}

        with mock.patch.object(pr_review, "gh_graphql", side_effect=fake):
            self.assertEqual(62, self.run_reply("--resolve", "--all-identical"))
        self.assertEqual(["t1", "t1", "t2"], ids)
        self.assertIn(
            "1 of 3 identical threads are confirmed to carry this reply", self.out.getvalue()
        )

    def test_all_identical_counts_the_reply_under_a_failed_resolve(self) -> None:
        """A resolve that fails comes after its own reply landed, so that copy carries the answer."""
        resolves = [True, False]
        ids = []

        def fake(query: str, **variables: object) -> dict:
            if "reviewThreads" in query:
                threads = [rthread("t1"), rthread("t2"), rthread("t3")]
                return {"repository": {"pullRequest": {"reviewThreads": page(threads)}}}
            ids.append(variables.get("threadId"))
            if "addPullRequestReviewThreadReply" in query:
                return {"addPullRequestReviewThreadReply": {"comment": LANDED}}
            return {"resolveReviewThread": {"thread": {"isResolved": resolves.pop(0)}}}

        with mock.patch.object(pr_review, "gh_graphql", side_effect=fake):
            self.assertEqual(63, self.run_reply("--resolve", "--all-identical"))
        self.assertEqual(["t1", "t1", "t2", "t2"], ids)
        self.assertIn(
            "2 of 3 identical threads are confirmed to carry this reply", self.out.getvalue()
        )

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

    def test_an_ascii_pattern_selects_a_body_written_with_typographic_punctuation(self) -> None:
        """A `--match` string copied from a rendered finding is ASCII, and the body may not be."""
        body = (
            "The helper wasn\u2019t clear on \u201cwidening the words\u201d \u2014 it just "
            "refuses\u2026"
        )
        self.wire(page([rthread("t1", body=body)]))
        self.assertEqual(
            0,
            self.run_reply(
                "--resolve", "--match", 'wasn\'t clear on "widening the words" - it just refuses'
            ),
        )
        self.assertIn("REPLIED_AND_RESOLVED", self.out.getvalue())

    def test_a_typographic_pattern_selects_a_body_written_in_ascii(self) -> None:
        """The fold runs both ways: a pasted pattern can carry the typographic form too."""
        self.wire(page([rthread("t1", body='The helper says "widen the words" - try again.')]))
        self.assertEqual(
            0,
            self.run_reply("--resolve", "--match", "\u201cwiden the words\u201d \u2014 try again"),
        )
        self.assertIn("REPLIED_AND_RESOLVED", self.out.getvalue())

    def test_a_pattern_copied_from_the_printed_unresolved_line_selects_its_own_thread(self) -> None:
        """`describe()` collapses whitespace before printing an `unresolved:` line, so a pattern
        copied verbatim from that line must still select the thread it was copied from, through
        the line break and the run of spaces the raw body still carries."""
        body = "The helper\nneeds to fold   whitespace before comparing bodies."
        copied = pr_review_rounds.describe(rthread("t1", body=body)).split(": ", 1)[1]
        self.wire(page([rthread("t1", body=body)]))
        self.assertEqual(0, self.run_reply("--resolve", "--match", copied))
        self.assertIn("REPLIED_AND_RESOLVED", self.out.getvalue())

    def test_every_documented_character_selects_across_the_ascii_boundary(self) -> None:
        """Each of the issue's seven characters folds on its own, an expectation independent of
        `_TYPOGRAPHIC_FOLD` itself, so dropping one from that table still fails this."""
        expected_folds = {
            0x2018: "'",  # left single quotation mark
            0x2019: "'",  # right single quotation mark
            0x201C: '"',  # left double quotation mark
            0x201D: '"',  # right double quotation mark
            0x2013: "-",  # en dash
            0x2014: "-",  # em dash
            0x2026: "...",  # horizontal ellipsis
        }
        for code_point, ascii_form in expected_folds.items():
            char = chr(code_point)
            with self.subTest(char=repr(char)):
                self.out.seek(0)
                self.out.truncate(0)
                self.wire(page([rthread("t1", body=f"The finding reads foo{char}bar plainly.")]))
                self.assertEqual(0, self.run_reply("--resolve", "--match", f"foo{ascii_form}bar"))
                self.assertIn("REPLIED_AND_RESOLVED", self.out.getvalue())

    def test_no_match_names_how_many_unresolved_threads_there_are(self) -> None:
        """Zero and several unresolved threads otherwise read the same, with nothing to tell them apart."""
        self.wire(
            page(
                [
                    rthread("t1", body="An unrelated finding about naming."),
                    rthread("t2", body="A second, also unrelated finding."),
                ]
            )
        )
        self.assertEqual(60, self.run_reply("--resolve"))
        self.assertIn("of 2 unresolved thread(s) total", self.out.getvalue())


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
        for flag in (["--match", "x"], ["--resolve"], ["--path", "a.py"], ["--all-identical"]):
            with self.subTest(flag=flag[0]):
                self.assertIn(
                    flag[0],
                    self.err(["comment", "7", "--repo", "o/r", "--body", "Fixed.", *flag]),
                )

    def test_a_writing_option_on_a_reading_command_is_an_error(self) -> None:
        """Silently ignored, it reads as an option that took effect on a run that wrote nothing."""
        for flag in (
            ["--body", "Fixed."],
            ["--match", "x"],
            ["--resolve"],
            ["--path", "a.py"],
            ["--all-identical"],
        ):
            with self.subTest(flag=flag[0]):
                self.assertIn(flag[0], self.err(["status", "7", "--repo", "o/r", *flag]))

    def test_a_command_line_with_two_bad_flags_names_the_same_one_first(self) -> None:
        """Each expected line was printed before the ownership checks became `FLAG_OWNERS`."""
        cases = (
            (
                ["status", "7", "--repo", "o/r", "--body", "b", "--match", "x"],
                "--match belongs to `reply`, not `status`",
            ),
            (
                ["status", "--repo", "o/r", "--fingerprint", "f", "--checkout", "."],
                "--checkout belongs to `attest` or `cleanup`, not `status`",
            ),
            (
                ["status", "--repo", "o/r", "--fingerprint", "f", "--body", "b"],
                "the following arguments are required: number",
            ),
            (
                [
                    "--timeout",
                    "5",
                    "cleanup",
                    "7",
                    "--repo",
                    "o/r",
                    "--checkout",
                    ".",
                    "--body",
                    "b",
                ],
                "`cleanup` takes its arguments after it, as `cleanup NUMBER --repo OWNER/NAME`",
            ),
            (
                ["wait", "7", "--repo", "o/r", "--min-rounds", "-1", "--pickup-grace", "-1"],
                "--pickup-grace cannot be negative",
            ),
            (
                ["wait", "7", "--repo", "o/r", "--check-stall", "-1", "--request", "--body", "b"],
                "--body belongs to `comment` or `reply`, not `wait`",
            ),
        )
        for argv, first in cases:
            with self.subTest(argv=argv):
                self.assertTrue(self.err(argv).strip().endswith(f"error: {first}"), argv)

    def test_every_owner_in_the_table_is_a_subcommand(self) -> None:
        """A misspelled owner would refuse its flag on the one command it belongs to."""
        commands = set(pr_review.COMMANDS)
        # A floor, since an emptied table would pass the subset check below.
        self.assertGreaterEqual(len(pr_review.FLAG_OWNERS), 8)
        for flag, owners, _ in pr_review.FLAG_OWNERS:
            with self.subTest(flag=flag):
                self.assertLessEqual(set(owners), commands)


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
        """The observed shape: a `develop` commit named as history, correct and not on this head."""
        self.assertEqual(
            [],
            refs(
                '`9d85941` merged "Reaching the Hub" whole, so the cluster '
                "is deleted rather than annotated."
            )[1],
        )

    def test_a_sha_inside_quoted_tool_output_is_not_a_claim(self) -> None:
        """The observed shape: a digest pasted to show what the tool prints."""
        self.assertEqual(
            [], refs("pr=108 head=9f56a472 rounds=1 review_on_head=yes threads=0 merge=CLEAN")[1]
        )

    def test_a_commit_in_another_repository_is_not_a_claim(self) -> None:
        """Two observed rounds' shape, neither carrying a URL that would mark it as elsewhere."""
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
        self.assertIn("n in blob", flowed_text(source))

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
        doc = flowed_text(pr_review.__doc__ or "")
        self.assertIn("Exit 0 = no Copilot review covers the head yet", doc)

    def test_status_documents_review_on_head_as_copilot_scoped(self) -> None:
        """`review_on_head=NO` alongside a genuine `other_reviewed` head is a scoping fact, not
        a coverage gap, the exact confusion that reached this docstring as a filed issue.
        """
        doc = flowed_text(pr_review.__doc__ or "")
        self.assertIn('never "no review of any kind covers this head"', doc)
        self.assertIn("not a gap", doc)

    def test_status_documents_the_coderabbit_and_qodo_finding_fields(self) -> None:
        doc = flowed_text(pr_review.__doc__ or "")
        self.assertIn("cr_outside_diff=N (on_head=X earlier=Y)", doc)
        self.assertIn("qodo_open=N", doc)

    def test_the_runbook_bootstraps_the_review_skill(self) -> None:
        """Copilot reaches the provider-independent review contract from its always-on file."""
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn(".github/skills/fleet-code-review/SKILL.md", text)

    def test_the_runbook_forbids_suppressed_findings(self) -> None:
        """A finding without a thread cannot participate in the ordinary reply loop."""
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("Never suppress a finding", text)

    def test_the_runbook_publishes_the_machine_readable_coverage_marker(self) -> None:
        """The instructed shape is the one the adapter reads as a statement of full coverage."""
        marker = "<!-- fleet-review: reviewed=N changed=N findings=N -->"
        # Raw, since a marker split over two lines is not the one-line shape the skill asks for.
        self.assertIn(marker, CODE_REVIEW_SKILL.read_text(encoding="utf-8"))
        body = f"{OVERVIEW}\n{marker.replace('N', '1')}"
        self.assertEqual("full", pr_review_rounds.adapter.read(body).coverage)

    def test_the_runbook_names_partial_coverage_as_a_state_that_blocks_a_merge(self) -> None:
        """A verify step reading `commit.oid` alone is what let five partial rounds merge.

        The absent case is named as a head nothing covers rather than as a statement absent from
        the round covering it, since a file table or an attested local pass stands in for one.
        """
        text = " ".join(RUNBOOK.read_text(encoding="utf-8").split())
        # Pinned as whole sentences rather than as the fragment `partial coverage`.
        # Any rewording of the paragraph would still satisfy that fragment while saying more.
        self.assertIn("A round reporting partial coverage of the diff blocks the merge", text)
        self.assertIn(
            "a head that no coverage statement, file table, or attested local pass covers", text
        )

    def test_the_only_writes_are_the_five_named_here(self) -> None:
        """Every write these scripts make is one of five, and each arrived as a reviewed change.

        The read subcommands are the bulk of it and a mutation reaching them is a digest that
        writes, so the whole-source guard stays and is narrowed to the documents the script
        actually needs rather than dropped when the first of them arrived. `requestReviews`
        closes the gap where `wait` only polled and never asked. `addComment` owns body-only
        finding responses. The reply and resolve pair owns inline threads. `deleteRef` is
        `pr_cleanup.py`'s, by an id read in the same run, so no REST verb enters for it. `union:true` only
        adds to the request set, so it cannot drop a requested human reviewer.
        `docs/pr-reviewer-reference.md` keeps the `union:false` clear-and-recover form as a
        manual operation, the runbook carrying no mutation of its own. A sixth document arriving
        is a write nobody reviewed as one rather than a style drift.
        """
        source = "".join(
            (REPO / "scripts" / name).read_text(encoding="utf-8")
            for name in (*GATE_SOURCES, "pr_cleanup.py")
        )
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
            "the additive form is the only one this script issues, since the "
            "replacing form runs by hand and only where no human or team "
            "reviewer is pending, which no script can decide",
        )
        # `mutation(` opens a document, so the count is the number of documents.
        self.assertEqual(5, source.count("mutation("))
        self.assertIn("deleteRef", source)
        self.assertIn("addComment", source)
        self.assertIn("addPullRequestReviewThreadReply", source)
        self.assertIn("resolveReviewThread", source)
        self.assertIn("requestReviews", source)

    def test_the_reviewer_reference_holds_the_manual_clear(self) -> None:
        """The three surfaces routing a stalled driver there are pinned to something that exists.

        The guard above proves only that this script does not issue the replacing form. Nothing
        proved the file the same docstring names still holds it, so deleting that code block left
        the skill, the runbook, and the README each pointing at a recovery nobody could run, with
        every gate green. Whitespace is collapsed because the document writes the argument with a
        space where the guard above writes the token without one.
        """
        text = flowed_text(REVIEWER_REFERENCE.read_text(encoding="utf-8"))
        self.assertIn(
            "requestReviews(input: { pullRequestId: $pr, botIds: [], union: false })", text
        )

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
        for name in GATE_SOURCES:
            with self.subTest(module=name):
                self.assertNotIn("PRRT_", (REPO / "scripts" / name).read_text(encoding="utf-8"))
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
        source = "".join(
            (REPO / "scripts" / name).read_text(encoding="utf-8") for name in GATE_SOURCES
        )
        for tail in (">/dev/null", "2>/dev/null", "&>/dev/null", "|| true", "|| :", "shell=True"):
            with self.subTest(tail=tail):
                self.assertNotIn(tail, source)

    def test_the_readers_import_nothing_that_reaches_out(self) -> None:
        """A reader importing the gate loads a second copy of it, and one importing a transport can call."""
        for name in GATE_SOURCES[1:]:
            tree = ast.parse((REPO / "scripts" / name).read_text(encoding="utf-8"))
            imported = {
                alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            } | {
                (node.module or "").split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            }
            module = name.removesuffix(".py")
            with self.subTest(module=name):
                self.assertLessEqual(imported, READER_IMPORTS, f"{name} imports {imported}")
                # A floor, since a collector that matched nothing would read as a pass.
                self.assertIn(READER_FLOORS[module], imported)
                # The adapter's own imports count too, so the gate and a transport stay unloaded.
                before, after = import_delta(REPO / "scripts", module, REACHING_OUT)
                # Clean before the import, or a module loaded at startup reads as never loaded.
                self.assertEqual([], before)
                self.assertEqual([], after, f"importing {module} loads {after}")

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

    def test_a_negative_min_rounds_is_rejected_rather_than_read_as_any_round(self) -> None:
        """A negative count is satisfied by the same `rounds=0` a caller naming none would see."""
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            pr_review.main(["wait", "7", "--repo", "o/r", "--min-rounds", "-1"])
        self.assertIn("min-rounds", err.getvalue())

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
        doc = flowed_text(pr_review.__doc__ or "")
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
        self.assertLess(pr_review_checks.CHECK_GRACE, pr_review_checks.CHECK_STALL)

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
        delays = list(pr_review.POLL_DELAYS)
        self.assertGreaterEqual(len(delays), 3)
        self.assertTrue(all(d > 0 for d in delays))
        self.assertEqual(delays, sorted(delays))

    def test_the_backoff_sleeps_the_schedule_until_the_timeout(self) -> None:
        """The last delay repeats, and the bound is read before each sleep rather than after.

        Six sleeps reach 290 seconds and a seventh 410, so with a 400-second bound the eighth is
        never taken.
        """
        clock = [0.0]
        slept: list[int] = []

        def sleep(seconds: int) -> None:
            slept.append(seconds)
            clock[0] += seconds

        reads = iter(range(1, 100))
        with (
            mock.patch.object(pr_review.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(pr_review.time, "sleep", side_effect=sleep),
        ):
            last = pr_review.backoff(0, lambda: next(reads), lambda _: True, 0.0, 400)
        self.assertEqual([15, 20, 30, 45, 60, 120, 120], slept)
        self.assertEqual(7, last)

    def test_the_backoff_returns_the_first_value_it_does_not_keep(self) -> None:
        """A met condition ends the poll without a sleep, and a later one ends it on that read."""
        with mock.patch.object(pr_review.time, "sleep") as slept:
            self.assertEqual("done", pr_review.backoff("done", str, lambda v: v != "done", 0.0, 9))
        slept.assert_not_called()
        reads = iter(["more", "done", "never"])
        with mock.patch.object(pr_review.time, "sleep") as slept:
            last = pr_review.backoff("start", lambda: next(reads), lambda v: v != "done", 0.0, 1e9)
        self.assertEqual("done", last)
        self.assertEqual([mock.call(15), mock.call(20)], slept.call_args_list)

    def test_the_held_poll_reads_neither_threads_nor_files(self) -> None:
        """The poll re-reads every iteration, and only the final digest read needs the two."""
        poll = " ".join(pr_review.Q_HELD.split())
        self.assertNotIn("reviewThreads", poll)
        self.assertNotIn("files(", poll)
        self.assertIn("headRefOid baseRefName state mergeable mergeStateStatus", poll)
        self.assertIn("conclusion startedAt isRequired(pullRequestNumber:$n)", poll)
        self.assertIn("authorAssociation createdAt body", poll)


class TestScopeRefusalNamesTheDirectoryItProbed(unittest.TestCase):
    """These refusals name the directory probed, the reader's own being a different one."""

    ANCHOR = Path("/anchor-this-script-sits-in")

    def test_the_probe_reads_the_directory_the_refusal_names(self) -> None:
        """A message naming a directory the probe never read sends the reader at the wrong tree."""
        self.enterContext(mock.patch.object(pr_review, "HERE", self.ANCHOR))
        with mock.patch.object(pr_review.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="https://github.com/o/r.git\n")
            self.assertEqual("o", pr_review.origin_owner())
        argv = run.call_args.args[0]
        self.assertEqual(str(self.ANCHOR), argv[argv.index("-C") + 1])

    def test_an_unreadable_origin_names_that_directory_and_a_remedy(self) -> None:
        """A claim naming no directory is one the operator disproves in the wrong tree."""
        self.enterContext(mock.patch.object(pr_review, "HERE", self.ANCHOR))
        with mock.patch.object(pr_review, "origin_owner", return_value=None):
            ok, why = pr_review.in_scope("o")
        self.assertFalse(ok)
        self.assertIn(str(self.ANCHOR), why)
        self.assertIn("hub checkout", why)
        self.assertIn("instead of this one", why)
        self.assertIn("git remote get-url origin", why)
        self.assertNotIn("this checkout", why)

    def test_a_cross_owner_refusal_names_that_directory_too(self) -> None:
        """The same ambiguity, on the branch that reports both owners."""
        self.enterContext(mock.patch.object(pr_review, "HERE", self.ANCHOR))
        with mock.patch.object(pr_review, "origin_owner", return_value="o"):
            ok, why = pr_review.in_scope("someone-else")
        self.assertFalse(ok)
        self.assertIn(str(self.ANCHOR), why)
        self.assertNotIn("this checkout", why)


class TestOriginOwnerIgnoresInheritedEnvironment(unittest.TestCase):
    """An inherited environment must not redirect the probe, by either mechanism.

    `GIT_DIR` and `GIT_COMMON_DIR` each override repository discovery on their own. `GIT_WORK_TREE`
    and `GIT_OBJECT_DIRECTORY` are stripped as part of the same discovery set but demonstrate no
    redirect of their own here, and `GIT_OBJECT_DIRECTORY` overrides the object store rather than
    discovery. A `GIT_CONFIG*` name injects config into the call instead, so both mechanisms are
    covered here.
    """

    def _clean_env(self) -> dict[str, str]:
        """The caller's environment minus every channel the probe itself drops.

        A fixture that left them in place would build its repositories under the same influence
        the cases exist to rule out, so it drops at least what the probe drops, and every other
        repository-local variable git names besides.
        """
        return {k: v for k, v in without_git_location().items() if not k.startswith("GIT_CONFIG")}

    def make_repo(self, parent: Path, name: str, owner_slash_repo: str) -> Path:
        """Build one throwaway repo under `parent`, immune to the caller's own git environment.

        `git init` and `git remote add` both honor an inherited GIT_DIR the same way the probe
        does, so without this strip an ambient one sends `git remote add` writing into whatever
        repository that variable names instead of the throwaway repo this helper just created.
        """
        env = self._clean_env()
        repo = parent / name
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True, env=env)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                f"https://github.com/{owner_slash_repo}.git",
            ],
            check=True,
            env=env,
        )
        return repo

    def test_a_git_dir_inherited_from_the_caller_does_not_redirect_the_probe(self) -> None:
        """An inherited GIT_DIR overrides `-C` for repository discovery.

        So an inherited one that names a different repository must not make this probe answer
        for that repository instead.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            anchor = self.make_repo(tmp_path, "anchor", "acme/anchor-repo")
            other = self.make_repo(tmp_path, "other", "unrelated-owner/other-repo")
            self.enterContext(mock.patch.object(pr_review, "HERE", anchor))
            with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
                self.assertEqual("acme", pr_review.origin_owner())

    def test_a_git_common_dir_inherited_from_the_caller_does_not_redirect_the_probe(self) -> None:
        """An inherited GIT_COMMON_DIR redirects the probe on its own, the same way GIT_DIR does.

        Config is read from the common dir, so this one needs no other inherited name alongside
        it to make the probe answer for the wrong repository.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            anchor = self.make_repo(tmp_path, "anchor", "acme/anchor-repo")
            other = self.make_repo(tmp_path, "other", "unrelated-owner/other-repo")
            self.enterContext(mock.patch.object(pr_review, "HERE", anchor))
            with mock.patch.dict(os.environ, {"GIT_COMMON_DIR": str(other / ".git")}):
                self.assertEqual("acme", pr_review.origin_owner())

    def test_the_whole_inherited_discovery_set_does_not_redirect_the_probe(self) -> None:
        """GIT_DIR and GIT_COMMON_DIR each redirect the probe on their own, and each has its own case above.

        GIT_WORK_TREE and GIT_OBJECT_DIRECTORY are stripped as part of the same documented
        discovery set, with no redirect of their own demonstrated for this call. What this holds
        is that all four inherited at once still leave the probe answering for the directory the
        script sits in.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            anchor = self.make_repo(tmp_path, "anchor", "acme/anchor-repo")
            other = self.make_repo(tmp_path, "other", "unrelated-owner/other-repo")
            self.enterContext(mock.patch.object(pr_review, "HERE", anchor))
            env = {
                "GIT_DIR": str(other / ".git"),
                "GIT_WORK_TREE": str(other),
                "GIT_COMMON_DIR": str(other / ".git"),
                "GIT_OBJECT_DIRECTORY": str(other / ".git" / "objects"),
            }
            with mock.patch.dict(os.environ, env):
                self.assertEqual("acme", pr_review.origin_owner())

    def test_an_instead_of_rewrite_injected_via_git_config_count_does_not_redirect_the_probe(
        self,
    ) -> None:
        """`remote get-url` applies `insteadOf` rewriting, so an unstripped injection here would rewrite the answer.

        The injected config rewrites this checkout's real `acme` origin to read as
        `unrelated-owner`. The `GIT_CONFIG` prefix strip removes `GIT_CONFIG_COUNT`,
        `GIT_CONFIG_KEY_0`, and `GIT_CONFIG_VALUE_0` before the call runs, so the real config
        governs and the probe still answers for the real owner.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            anchor = self.make_repo(tmp_path, "anchor", "acme/anchor-repo")
            self.enterContext(mock.patch.object(pr_review, "HERE", anchor))
            env = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url.https://github.com/unrelated-owner/.insteadof",
                "GIT_CONFIG_VALUE_0": "https://github.com/acme/",
            }
            with mock.patch.dict(os.environ, env):
                self.assertEqual("acme", pr_review.origin_owner())

    def test_the_same_instead_of_rewrite_delivered_via_a_git_config_global_file_does_not_redirect_the_probe(
        self,
    ) -> None:
        """Same rewrite as above, delivered through `GIT_CONFIG_GLOBAL` instead of the count/key/value form.

        `remote get-url` merges the global config into what it resolves, so an unstripped
        `GIT_CONFIG_GLOBAL` pointing at a file carrying this rewrite would still rewrite the
        answer. `GIT_CONFIG_GLOBAL` matches the same `GIT_CONFIG` prefix, so it is stripped
        before the call runs and the probe still answers for the real owner.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            anchor = self.make_repo(tmp_path, "anchor", "acme/anchor-repo")
            global_config = tmp_path / "injected-global-gitconfig"
            global_config.write_text(
                '[url "https://github.com/unrelated-owner/"]\n'
                "\tinsteadOf = https://github.com/acme/\n",
                encoding="utf-8",
            )
            self.enterContext(mock.patch.object(pr_review, "HERE", anchor))
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config)}):
                self.assertEqual("acme", pr_review.origin_owner())

    def test_a_direct_remote_origin_url_override_injected_via_git_config_count_does_not_redirect_the_probe(
        self,
    ) -> None:
        """A `remote.origin.url` override injected the same count/key/value way as the rewrite above.

        Not a regression guard for the current probe: measured directly, `git remote get-url`
        ignores a config-injected `remote.origin.url` value regardless of whether the
        `GIT_CONFIG` prefix is stripped, so this case passes with or without that strip. It stays
        in place for a future change that switches the probe to a command that does honor this
        vector, such as `git config --get remote.origin.url`, which the strip does guard against.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            anchor = self.make_repo(tmp_path, "anchor", "acme/anchor-repo")
            self.enterContext(mock.patch.object(pr_review, "HERE", anchor))
            env = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "remote.origin.url",
                "GIT_CONFIG_VALUE_0": "https://github.com/unrelated-owner/other-repo.git",
            }
            with mock.patch.dict(os.environ, env):
                self.assertEqual("acme", pr_review.origin_owner())

    def test_two_origin_urls_resolve_to_the_first_one(self) -> None:
        """`remote.origin.url` may carry more than one value; `remote get-url` returns the first.

        `git config --get` instead returns the last: with two urls, that command names a
        different owner than the one a write should stay within, the opposite of what this probe
        needs. Both commands are run against the same repository here so the difference is
        the proof, rather than reverting and restoring the production probe.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            anchor = self.make_repo(tmp_path, "anchor", "acme/anchor-repo")
            env = self._clean_env()
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(anchor),
                    "config",
                    "--local",
                    "--add",
                    "remote.origin.url",
                    "https://github.com/second-owner/anchor-repo.git",
                ],
                check=True,
                env=env,
            )
            self.enterContext(mock.patch.object(pr_review, "HERE", anchor))
            self.assertEqual("acme", pr_review.origin_owner())
            reverted = subprocess.run(
                ["git", "-C", str(anchor), "config", "--local", "--get", "remote.origin.url"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                env=env,
            ).stdout.strip()
            self.assertIn("second-owner", reverted)

    def test_an_origin_url_delivered_via_include_path_still_resolves(self) -> None:
        """A checkout whose `remote.origin.url` arrives through `include.path` rather than a literal value.

        `git config --local` does not follow `include.path`/`includeIf`, so a probe built on that
        command instead would read no owner here and refuse every write. `remote get-url` does
        follow includes, so the probe in this script still resolves the real owner. Both commands
        are run against the same repository here so the difference is the proof, rather than
        reverting and restoring the production probe.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            anchor = self.make_repo(tmp_path, "anchor", "acme/anchor-repo")
            env = self._clean_env()
            subprocess.run(
                ["git", "-C", str(anchor), "config", "--local", "--unset", "remote.origin.url"],
                check=True,
                env=env,
            )
            included = tmp_path / "origin-include.gitconfig"
            included.write_text(
                '[remote "origin"]\n\turl = https://github.com/acme/anchor-repo.git\n',
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(anchor),
                    "config",
                    "--local",
                    "--add",
                    "include.path",
                    str(included),
                ],
                check=True,
                env=env,
            )
            self.enterContext(mock.patch.object(pr_review, "HERE", anchor))
            self.assertEqual("acme", pr_review.origin_owner())
            reverted = subprocess.run(
                ["git", "-C", str(anchor), "config", "--local", "--get", "remote.origin.url"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env=env,
            )
            self.assertEqual(1, reverted.returncode)


class TestWaitStaysInScope(unittest.TestCase):
    """`wait` mutates via the auto-request, so it must refuse cross-owner outright too."""

    def test_a_cross_owner_target_is_refused_before_the_wait_reads_or_writes_anything(self) -> None:
        """Both transports raise on any call, so a reverted check fails fast rather than
        falling into the real, minutes-long backoff loop with a mocked GraphQL response."""
        out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(mock.patch.object(pr_review, "origin_owner", return_value="acme"))
        boom = AssertionError("no network call should be reached for an out-of-scope target")
        with (
            mock.patch.object(pr_review, "gql", side_effect=boom) as gql,
            mock.patch.object(pr_review, "gh_graphql", side_effect=boom) as gh_graphql,
        ):
            code = pr_review.main(["wait", "7", "--repo", "someone-else/r"])
        self.assertEqual(64, code)
        self.assertIn("status=OUT_OF_SCOPE", out.getvalue())
        gql.assert_not_called()
        gh_graphql.assert_not_called()


# `wait` was already in the parser's `cmd` choices when it became a write, by acquiring a `requestReviews` mutation, with no subcommand added.
# Update this table whenever a command's write status changes, not only when a new one is added.
WRITE_COMMANDS: dict[str, list[str]] = {
    "comment": ["comment", "7", "--repo", "someone-else/r", "--body", "Fixed."],
    "reply": [
        "reply",
        "7",
        "--repo",
        "someone-else/r",
        "--match",
        "retry count",
        "--body",
        "Fixed.",
    ],
    "wait": ["wait", "7", "--repo", "someone-else/r"],
    "attest": ["attest", "7", "--repo", "someone-else/r"],
    "cleanup": ["cleanup", "7", "--repo", "someone-else/r"],
}

# The parser's remaining `cmd` choices, none of which write.
# The partition test below checks that this set and WRITE_COMMANDS together match the parser's choices.
READ_ONLY_COMMANDS = ["claims", "status"]


class TestEveryWriteCommandRefusesCrossOwner(unittest.TestCase):
    """`requestReviews` once sat outside `in_scope` for its whole life with a green suite.

    The whole-source check in TestContract counts which mutation documents exist, which held
    that regression invisible since a document can exist and still be reachable without the
    owner check running first. This pins the command-level invariant instead: every write
    subcommand refuses a cross-owner target before either transport is touched. It does not
    pin the static property TestContract's count implied, that every mutation document in the
    source sits behind `in_scope`; a write reachable by some path this table does not drive
    would still slip past it.
    """

    def test_each_write_command_refuses_before_reaching_either_transport(self) -> None:
        for cmd, argv in WRITE_COMMANDS.items():
            with self.subTest(cmd=cmd):
                boom = AssertionError(f"{cmd}: no network call should be reached out of scope")
                with (
                    contextlib.redirect_stdout(io.StringIO()) as out,
                    mock.patch.object(pr_review, "origin_owner", return_value="acme"),
                    mock.patch.object(pr_review, "gql", side_effect=boom) as gql,
                    mock.patch.object(pr_review, "gh_graphql", side_effect=boom) as gh_graphql,
                    mock.patch.object(pr_review.shared, "request", side_effect=boom) as request,
                    mock.patch.object(pr_review, "gh_rest", side_effect=boom) as rest,
                ):
                    code = pr_review.main(argv)
                self.assertEqual(64, code)
                self.assertIn("status=OUT_OF_SCOPE", out.getvalue())
                gql.assert_not_called()
                gh_graphql.assert_not_called()
                request.assert_not_called()
                rest.assert_not_called()


class TestRequiredContexts(unittest.TestCase):
    """The rules read names what is required, and the hold arm compares it with the rollup."""

    def setUp(self) -> None:
        pr_review.required_contexts.cache_clear()
        self.addCleanup(pr_review.required_contexts.cache_clear)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def read(self, out: str, code: int = 0) -> frozenset[tuple[str, int | None]] | None:
        proc = subprocess.CompletedProcess([], code, out, "")
        with mock.patch.object(pr_review, "gh_rest", return_value=proc):
            return pr_review.required_contexts("o", "r", "develop")

    def test_the_contexts_are_read_from_the_rules(self) -> None:
        self.assertEqual(
            frozenset({("a", None), ("b", 7)}),
            self.read('{"n": 3, "c": [["a", null], ["b", 7]]}'),
        )

    def test_a_slash_in_the_base_is_one_path_segment(self) -> None:
        proc = subprocess.CompletedProcess([], 0, '{"n": 1, "c": []}', "")
        with mock.patch.object(pr_review, "gh_rest", return_value=proc) as rest:
            pr_review.required_contexts("o", "r", "release/1.0")
        self.assertIn("/rules/branches/release%2F1.0?", rest.call_args.args[0])

    def test_a_read_that_did_not_happen_is_none_not_empty(self) -> None:
        for out, code in (
            ("", 1),
            ("not json", 0),
            ('{"n": 1}', 0),
            ('{"n": 100, "c": []}', 0),
            ('{"n": 1, "c": [["a", "15368"]]}', 0),
            ('{"n": 1, "c": [["a", 0]]}', 0),
            ('{"n": 1, "c": [["a", true]]}', 0),
        ):
            with self.subTest(out=out, code=code):
                pr_review.required_contexts.cache_clear()
                self.assertIsNone(self.read(out, code))

    def test_blocked_with_a_required_context_absent_holds(self) -> None:
        pr = payload([], merge="BLOCKED", checks=[check()])
        nodes = pr_review_checks.check_nodes(pr)
        now = datetime.now(UTC)
        want = frozenset({("Check pull request workflow status job", None), ("Later", None)})
        self.assertTrue(pr_review_checks.held_checks_open(pr, nodes, now, 600, 3600, want))
        self.assertFalse(
            pr_review_checks.held_checks_open(pr, nodes, now, 600, 3600, want - {("Later", None)})
        )
        self.assertFalse(pr_review_checks.held_checks_open(pr, nodes, now, 600, 3600))
        clean = {**pr, "mergeStateStatus": "CLEAN"}
        self.assertFalse(pr_review_checks.held_checks_open(clean, nodes, now, 600, 3600, want))

    def test_checks_never_ran_judges_only_an_open_gated_merge(self) -> None:
        pr = payload([], merge="DIRTY", checks=[])
        self.assertIn("no check has posted", pr_review_checks.checks_never_ran(pr, [], frozenset()))
        nodes = pr_review_checks.check_nodes(payload([], merge="DIRTY", checks=[check()]))
        self.assertEqual("", pr_review_checks.checks_never_ran(pr, nodes, frozenset()))
        later = pr_review_checks.checks_never_ran(pr, nodes, frozenset({("Later", None)}))
        self.assertIn("has not registered or came from another app, Later", later)
        for ungated in ({**pr, "mergeStateStatus": "CLEAN"}, {**pr, "state": "MERGED"}):
            self.assertEqual("", pr_review_checks.checks_never_ran(ungated, [], frozenset()))
        other = payload([], merge="BLOCKED", checks=[check()], rollup_oid="f" * 40)
        self.assertIn(
            "no commit matching", pr_review_checks.checks_never_ran(other, [], frozenset())
        )

    def test_required_unregistered_judges_only_a_rollup_read_whole(self) -> None:
        """A cut rollup can hold the absent context past its window, so it proves nothing."""
        want = frozenset({("Later", None)})
        pr = payload([], merge="BEHIND", checks=[check()])
        nodes = pr_review_checks.check_nodes(pr)
        self.assertEqual(["Later"], pr_review_checks.required_unregistered(pr, nodes, want))
        self.assertEqual([], pr_review_checks.required_unregistered(pr, nodes, frozenset()))
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["pageInfo"] = {
            "hasNextPage": True
        }
        self.assertEqual([], pr_review_checks.required_unregistered(pr, nodes, want))

    def test_a_rollup_not_read_whole_never_holds_on_an_absent_context(self) -> None:
        now = datetime.now(UTC)
        want = frozenset({("Later", None)})
        other = payload([], merge="BLOCKED", checks=[check()], rollup_oid="f" * 40)
        self.assertFalse(
            pr_review_checks.held_checks_open(
                other, pr_review_checks.check_nodes(other), now, 600, 3600, want
            )
        )
        cut = payload([], merge="BLOCKED", checks=[check()])
        cut["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["pageInfo"] = {
            "hasNextPage": True
        }
        self.assertFalse(
            pr_review_checks.held_checks_open(
                cut, pr_review_checks.check_nodes(cut), now, 600, 3600, want
            )
        )

    def test_the_rules_filter_projects_the_context_and_integration_id(self) -> None:
        proc = subprocess.CompletedProcess([], 0, '{"n": 0, "c": []}', "")
        with mock.patch.object(pr_review, "gh_rest", return_value=proc) as rest:
            pr_review.required_contexts("o", "r", "develop")
        jq = rest.call_args.args[1]
        self.assertIn(".context", jq)
        self.assertIn(".integration_id", jq)

    def test_a_check_run_with_no_app_does_not_meet_an_app_bound_entry(self) -> None:
        node = check(name="Gate")
        node["checkSuite"]["app"] = None
        self.assertTrue(self.held([node], ("Gate", 222)))

    def held(self, nodes: list[dict], entry: tuple[str, int | None]) -> bool:
        pr = payload([], merge="BLOCKED", checks=nodes)
        return pr_review_checks.held_checks_open(
            pr, pr_review_checks.check_nodes(pr), datetime.now(UTC), 600, 3600, frozenset({entry})
        )

    def test_an_entry_bound_to_an_app_is_not_met_by_another_apps_check(self) -> None:
        self.assertTrue(self.held([check(name="Gate", app_id=111)], ("Gate", 222)))

    def test_an_entry_bound_to_an_app_is_met_by_that_apps_check(self) -> None:
        self.assertFalse(self.held([check(name="Gate", app_id=222)], ("Gate", 222)))

    def test_an_entry_bound_to_an_app_is_not_met_by_a_status_context(self) -> None:
        self.assertTrue(self.held([status_context("Gate")], ("Gate", 222)))

    def test_an_unbound_entry_is_met_by_a_same_name_node_of_either_type(self) -> None:
        self.assertFalse(self.held([check(name="Gate", app_id=111)], ("Gate", None)))
        self.assertFalse(self.held([status_context("Gate")], ("Gate", None)))

    def test_the_unmet_context_is_named(self) -> None:
        pr = payload([], merge="BLOCKED", checks=[check(name="Gate")])
        nodes = pr_review_checks.check_nodes(pr)
        self.assertEqual("", pr_review_checks._unmet_note(pr, frozenset({("Gate", None)}), nodes))
        self.assertIn("Gate", pr_review_checks._unmet_note(pr, frozenset({("Gate", 9)}), nodes))
        note = pr_review_checks._unmet_note(pr, frozenset({("Gate", 9)}), nodes)
        self.assertNotIn("unread part", note)
        unread = nodes + pr_review_checks.check_nodes(
            payload([], merge="BLOCKED", checks=[{"__typename": "SomethingNew", "label": "x"}])
        )
        note = pr_review_checks._unmet_note(pr, frozenset({("Gate", 9)}), unread)
        self.assertIn("Gate, which may sit in the rollup's unread part", note)
        rollup = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
        rollup["contexts"]["pageInfo"] = {"hasNextPage": True}
        self.assertEqual("", pr_review_checks._unmet_note(pr, frozenset({("Gate", 9)}), nodes))

    def test_the_rules_are_read_only_for_a_blocked_merge(self) -> None:
        with mock.patch.object(pr_review, "gh_rest") as rest:
            clean = payload([], merge="CLEAN", checks=[check()])
            self.assertEqual(frozenset(), pr_review.required_for(clean, "o", "r"))
        rest.assert_not_called()

    def test_the_rules_for_exit_45_are_read_only_for_a_dirty_or_behind_merge(self) -> None:
        gated = frozenset({"DIRTY", "BEHIND"})
        with mock.patch.object(pr_review, "gh_rest") as rest:
            for merge in ("CLEAN", "UNSTABLE", "HAS_HOOKS", "BLOCKED", "DRAFT"):
                pr = payload([], merge=merge, checks=[check()])
                self.assertEqual(frozenset(), pr_review.required_for(pr, "o", "r", gated))
            closed = {**payload([], merge="DIRTY", checks=[check()]), "state": "CLOSED"}
            self.assertEqual(frozenset(), pr_review.required_for(closed, "o", "r", gated))
        rest.assert_not_called()


class TestRunsOpen(unittest.TestCase):
    """A head's workflow run that has not completed holds a held wait whatever the merge reads."""

    @staticmethod
    def suite(status: str, run: int | None = 200, sid: int = 20) -> dict:
        workflow_run = {"databaseId": run, "workflow": {"databaseId": 7}} if run else None
        return {"databaseId": sid, "status": status, "workflowRun": workflow_run}

    def held(self, merge: str, checks: list[dict], *suites: dict) -> dict:
        pr = payload([], merge=merge, checks=checks)
        pr["commits"]["nodes"][0]["commit"]["checkSuites"] = {"nodes": list(suites)}
        return pr

    def is_open(self, pr: dict) -> bool:
        return pr_review_checks.held_checks_open(
            pr, pr_review_checks.check_nodes(pr), NOW, 300, 1800
        )

    def test_a_run_not_completed_holds_a_clean_merge(self) -> None:
        for status in ("QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"):
            with self.subTest(status=status):
                pr = self.held("CLEAN", [check()], self.suite(status))
                self.assertEqual(["run 200"], pr_review_checks.runs_open(pr))
                self.assertTrue(self.is_open(pr))

    def test_a_completed_run_releases_a_clean_merge(self) -> None:
        pr = self.held("CLEAN", [check()], self.suite("COMPLETED"))
        self.assertEqual([], pr_review_checks.runs_open(pr))
        self.assertFalse(self.is_open(pr))

    def test_a_suite_with_no_workflow_run_does_not_hold(self) -> None:
        """An installed app's suite can sit queued forever with nothing ever reporting."""
        pr = self.held("CLEAN", [check()], self.suite("QUEUED", run=None))
        self.assertEqual([], pr_review_checks.runs_open(pr))
        self.assertFalse(self.is_open(pr))

    def test_an_unrequired_checks_open_run_holds_a_clean_merge(self) -> None:
        """CLEAN releases an unrequired check, but not the run it belongs to while that is open."""
        optional = {
            **check(status="IN_PROGRESS", conclusion="", workflow=7, suite_status="IN_PROGRESS"),
            "isRequired": False,
        }
        optional["checkSuite"]["workflowRun"]["databaseId"] = 200
        pr = self.held("CLEAN", [optional], self.suite("IN_PROGRESS"))
        self.assertEqual(["run 200"], pr_review_checks.runs_open(pr))
        self.assertTrue(self.is_open(pr))

    def test_a_run_read_only_through_the_rollup_holds(self) -> None:
        """A run's suite can fall outside the suites window while its jobs sit in the rollup."""
        running = check(status="IN_PROGRESS", conclusion="", workflow=7, suite_status="IN_PROGRESS")
        running["checkSuite"]["workflowRun"]["databaseId"] = 200
        pr = self.held("CLEAN", [running])
        self.assertEqual(["run 200"], pr_review_checks.runs_open(pr))
        self.assertTrue(self.is_open(pr))

    def test_an_actions_suite_stands_in_where_the_run_is_unreadable(self) -> None:
        running = check(status="IN_PROGRESS", conclusion="", suite=20, suite_status="IN_PROGRESS")
        pr = {**self.held("CLEAN", [running]), pr_review_checks.WORKFLOW_KEYS_UNAVAILABLE: True}
        self.assertEqual(["suite 20"], pr_review_checks.runs_open(pr))
        self.assertTrue(self.is_open(pr))
        self.assertEqual([], pr_review_checks.runs_open(self.held("CLEAN", [running])))

    def test_a_fresh_actions_suite_stands_in_with_no_job_registered(self) -> None:
        """Read right after a push, the suite exists before any job does, and no rollup node carries it."""
        suite = {"databaseId": 20, "status": "QUEUED", "app": {"slug": "github-actions"}}
        pr = {**self.held("CLEAN", [], suite), pr_review_checks.WORKFLOW_KEYS_UNAVAILABLE: True}
        self.assertEqual(["suite 20"], pr_review_checks.runs_open(pr))
        self.assertTrue(self.is_open(pr))

    def test_another_apps_queued_suite_holds_nothing_where_runs_are_unreadable(self) -> None:
        """Other apps leave suites queued with no job for good, so only an Actions suite stands in."""
        suite = {"databaseId": 30, "status": "QUEUED", "app": {"slug": "coderabbitai"}}
        pr = {
            **self.held("CLEAN", [check()], suite),
            pr_review_checks.WORKFLOW_KEYS_UNAVAILABLE: True,
        }
        self.assertEqual([], pr_review_checks.runs_open(pr))
        self.assertFalse(self.is_open(pr))

    def test_a_gated_required_check_holds_a_blocked_poll_rather_than_ending_it(self) -> None:
        """A timer clears by itself and an approval may land, so a gate is not the 44 shape."""
        gated = check(status="WAITING", conclusion="", started=ago(900))
        pr = self.held("BLOCKED", [gated], self.suite("WAITING"))
        self.assertTrue(self.is_open(pr))

    def test_a_job_waiting_on_its_environment_reads_gated_not_starved(self) -> None:
        """The starved line's re-run is the wrong remedy for a job a protection rule holds."""
        node = {"name": "deploy", "state": "WAITING", "conclusion": "", "since": ago(900)}
        self.assertEqual("GATED", pr_review_checks.check_shape(node, NOW, 300, 1800))
        line = pr_review_checks.check_lines({}, [node], [(node, "GATED")], NOW)[0]
        self.assertIn("CHECK WAITING ON ITS ENVIRONMENT ('deploy', waiting 15m)", line)
        inside = {**node, "since": ago(60)}
        self.assertEqual("", pr_review_checks.check_shape(inside, NOW, 300, 1800))

    def test_a_job_no_runner_picked_up_still_holds_its_open_run(self) -> None:
        """A queue past the grace often starts, so releasing on it would exit 0 with jobs unstarted."""
        starved = {
            **check(status="QUEUED", conclusion="", started=ago(900)),
            "isRequired": False,
        }
        pr = self.held("CLEAN", [starved], self.suite("IN_PROGRESS"))
        self.assertEqual(["run 200"], pr_review_checks.runs_open(pr))
        self.assertTrue(self.is_open(pr))

    def test_a_stuck_required_check_still_ends_a_blocked_poll(self) -> None:
        """The 44 a failed required check decides is not delayed by a sibling run."""
        failed = check(conclusion="FAILURE")
        pr = self.held("BLOCKED", [failed], self.suite("IN_PROGRESS"))
        self.assertFalse(self.is_open(pr))

    def test_a_closed_pull_request_holds_nothing(self) -> None:
        pr = {**self.held("CLEAN", [check()], self.suite("IN_PROGRESS")), "state": "MERGED"}
        self.assertFalse(self.is_open(pr))

    def test_each_run_is_named_once(self) -> None:
        pr = self.held(
            "CLEAN",
            [check()],
            self.suite("IN_PROGRESS", run=300, sid=30),
            self.suite("QUEUED", run=200),
            self.suite("IN_PROGRESS", run=300, sid=30),
        )
        self.assertEqual(["run 200", "run 300"], pr_review_checks.runs_open(pr))


class TestAttest(unittest.TestCase):
    """A local pass is published only for the content the pull request's head carries."""

    def setUp(self) -> None:
        self.dir = self.enterContext(tempfile.TemporaryDirectory())
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        for args in (
            ["init", "-q", "-b", "feature"],
            [
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.test",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "base",
            ],
        ):
            subprocess.run(["git", "-C", self.dir, *args], check=True, env=env)
        subprocess.run(
            ["git", "-C", self.dir, "update-ref", "refs/remotes/origin/develop", "HEAD"],
            check=True,
            env=env,
        )
        self.head = subprocess.run(
            ["git", "-C", self.dir, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            env=env,
        ).stdout.strip()
        self.enterContext(mock.patch.object(pr_review, "in_scope", return_value=(True, "")))
        self.posted: list[str] = []

        def post(_o: str, _r: str, _n: int, body: str) -> int:
            self.posted.append(body)
            return 0

        self.enterContext(mock.patch.object(pr_review, "comment_on_pr", side_effect=post))
        self.out = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))

    def run_attest(self, head: str, check_exit: int = 0, base: str | None = None) -> int:
        """Run `attest` with a stub `local_review.py` that records how it was called."""
        record = Path(self.dir).parent / f"calls-{check_exit}.txt"
        stub = Path(self.dir).parent / f"stub-{check_exit}.py"
        stub.write_text(
            "import json, os, sys\n"
            f"open({str(record)!r}, 'a').write(os.getcwd() + '|' + ' '.join(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1] == 'status':\n"
            f"    print(json.dumps({{'covered': {check_exit == 0}, 'receiptProblems': [],"
            " 'findings': {'agent-skill': 2, 'coderabbit-cli': 1}}))\n"
            f"sys.exit({check_exit})\n"
        )
        self.addCleanup(stub.unlink)
        self.addCleanup(lambda: record.unlink(missing_ok=True))
        self.record = record
        target = {"headRefOid": head, "baseRefName": "develop"}
        merge_base = subprocess.CompletedProcess([], 0, (base or self.head) + "\n", "")
        with (
            mock.patch.object(pr_review, "gql", return_value=target),
            mock.patch.object(pr_review, "gh_rest", return_value=merge_base),
            mock.patch.object(pr_review, "LOCAL_REVIEW", stub),
        ):
            return pr_review.attest("o", "r", 7, self.dir)

    def test_a_covered_head_is_attested_by_its_full_commit(self) -> None:
        self.assertEqual(0, self.run_attest(self.head))
        self.assertEqual(1, len(self.posted))
        self.assertIn(
            f"\n<!-- fleet-local-review: head={self.head} base=develop findings=3 -->",
            self.posted[0],
        )
        calls = [line.split("|") for line in self.record.read_text().splitlines()]
        self.assertEqual(["status --target develop"], [c[1] for c in calls])
        for cwd, _ in calls:
            self.assertEqual(os.path.realpath(self.dir), os.path.realpath(cwd))

    def lagging(self, tip: str, heads: list[str]) -> int:
        """Run `attest` while the pull request's head reads each of `heads` in turn."""
        replies = iter(heads)
        target = {"headRefOid": heads[0], "baseRefName": "develop"}

        def gql(query: str, *_: object) -> dict:
            if query == pr_review.Q_ATTEST_BRANCH:
                return {
                    "state": "OPEN",
                    "headRefName": "feature",
                    "headRepository": {"url": "https://x.test/o/r"},
                }
            return {**target, "headRefOid": next(replies, heads[-1])}

        self.slept: list[float] = []
        remote = subprocess.CompletedProcess([], 0, f"{tip}\trefs/heads/feature\n", "")
        original = pr_review._git

        def git(checkout: str, *args: str) -> subprocess.CompletedProcess:
            return remote if args[0] == "ls-remote" else original(checkout, *args)

        merge_base = subprocess.CompletedProcess([], 0, self.head + "\n", "")
        stub = Path(self.dir).parent / "stub-lag.py"
        stub.write_text(
            "import json, sys\n"
            "print(json.dumps({'covered': True, 'receiptProblems': [], 'findings': {'a': 0}}))\n"
        )
        self.addCleanup(stub.unlink)
        with (
            mock.patch.object(pr_review, "gql", side_effect=gql),
            mock.patch.object(pr_review, "_git", side_effect=git),
            mock.patch.object(pr_review, "gh_rest", return_value=merge_base),
            mock.patch.object(pr_review, "LOCAL_REVIEW", stub),
            mock.patch.object(pr_review, "_attest_sleep", side_effect=self.slept.append),
        ):
            return pr_review.attest("o", "r", 7, self.dir)

    def test_a_head_that_catches_up_within_the_bound_is_attested(self) -> None:
        old = "d" * 40
        self.assertEqual(0, self.lagging(self.head, [old, old, self.head]))
        self.assertEqual(2, len(self.slept))
        self.assertEqual(1, len(self.posted))

    def test_a_head_that_never_catches_up_is_refused_after_the_bound(self) -> None:
        old = "d" * 40
        self.assertEqual(67, self.lagging(self.head, [old]))
        self.assertLessEqual(sum(self.slept), 60)
        self.assertGreater(sum(self.slept), 0)
        self.assertIn("had not registered it yet", self.out.getvalue())
        self.assertEqual([], self.posted)

    def test_a_closed_pull_request_is_refused_without_waiting(self) -> None:
        old = "d" * 40
        replies = {"state": "MERGED", "headRefName": "feature", "headRepository": {"url": "u"}}
        with mock.patch.object(pr_review, "gql", return_value=replies):
            head, registered = pr_review.await_registered_head(
                "o", "r", 7, self.dir, self.head, old
            )
        self.assertEqual((old, True), (head, registered))

    def test_a_head_that_moves_to_a_third_commit_is_not_called_unregistered(self) -> None:
        old, other = "d" * 40, "c" * 40
        self.assertEqual(67, self.lagging(self.head, [old, other]))
        self.assertEqual(1, len(self.slept))
        self.assertNotIn("had not registered", self.out.getvalue())

    def test_a_remote_tip_that_is_not_the_checkout_is_refused_at_once(self) -> None:
        old = "d" * 40
        self.assertEqual(67, self.lagging("e" * 40, [old]))
        self.assertEqual([], self.slept)
        self.assertNotIn("had not registered", self.out.getvalue())

    def test_a_checkout_measuring_another_merge_base_is_refused(self) -> None:
        self.assertEqual(67, self.run_attest(self.head, base="e" * 40))
        self.assertEqual([], self.posted)

    def test_a_checkout_that_moves_during_the_check_is_refused(self) -> None:
        original = pr_review._git
        reads: list[str] = []

        def moved(checkout: str, *args: str) -> subprocess.CompletedProcess:
            proc = original(checkout, *args)
            if args == ("rev-parse", "HEAD"):
                reads.append("HEAD")
                if len(reads) > 1:
                    return subprocess.CompletedProcess([], 0, "f" * 40 + "\n", "")
            return proc

        with mock.patch.object(pr_review, "_git", side_effect=moved):
            self.assertEqual(67, self.run_attest(self.head))
        self.assertEqual([], self.posted)

    def test_a_base_that_would_change_the_compare_path_is_refused(self) -> None:
        target = {"headRefOid": self.head, "baseRefName": "develop#frag"}
        with (
            mock.patch.object(pr_review, "gql", return_value=target),
            mock.patch.object(pr_review, "gh_rest") as rest,
        ):
            self.assertEqual(67, pr_review.attest("o", "r", 7, self.dir))
        rest.assert_not_called()
        self.assertEqual([], self.posted)

    def test_an_unread_pull_request_is_refused(self) -> None:
        with mock.patch.object(pr_review, "gql", return_value={}):
            self.assertEqual(65, pr_review.attest("o", "r", 7, self.dir))
        self.assertEqual([], self.posted)

    def test_a_checkout_at_another_commit_is_refused(self) -> None:
        self.assertEqual(67, self.run_attest("f" * 40))
        self.assertEqual([], self.posted)

    def test_a_checkout_holding_changes_is_refused(self) -> None:
        (Path(self.dir) / "extra.txt").write_text("not in the head\n")
        self.assertEqual(67, self.run_attest(self.head))
        self.assertEqual([], self.posted)

    def test_no_current_local_pass_is_refused(self) -> None:
        self.assertEqual(68, self.run_attest(self.head, check_exit=1))
        self.assertEqual([], self.posted)


class TestAttestationReadings(unittest.TestCase):
    """What the gate reads an attestation, a promotion, and a hold from."""

    def comment(self, body: str, association: str = "OWNER") -> dict:
        return {"body": body, "authorAssociation": association, "author": {"login": "someone"}}

    def test_the_attestation_carries_the_findings_count(self) -> None:
        for line, expected in (
            (f"<!-- fleet-local-review: head={HEAD} base=develop findings=4 -->", "4"),
            (f"<!-- fleet-local-review: head={HEAD} base=develop findings=unknown -->", "unknown"),
            (f"<!-- fleet-local-review: head={HEAD} base=feature findings=4 -->", None),
            (f"<!-- fleet-local-review: head={HEAD} -->", None),
        ):
            with self.subTest(line=line):
                pr = {
                    "headRefOid": HEAD,
                    "baseRefName": "develop",
                    "comments": {"nodes": [self.comment(line)]},
                }
                self.assertEqual(expected, pr_review_rounds.attestation(pr))

    def test_only_a_writer_s_marker_for_the_current_head_attests(self) -> None:
        marker = f"<!-- fleet-local-review: head={HEAD} base=develop findings=0 -->"
        for nodes, expected in (
            ([self.comment(marker)], True),
            ([self.comment(marker, "COLLABORATOR")], True),
            ([self.comment(marker, "NONE")], False),
            ([self.comment(marker, "CONTRIBUTOR")], False),
            (
                [self.comment(f"<!-- fleet-local-review: head={OLD} base=develop findings=0 -->")],
                False,
            ),
            ([self.comment(f"Attest posts `{marker}` as its last line.")], False),
            ([self.comment(f"Quoted:\n\n```\n{marker}\n```\n")], False),
            ([self.comment(f"A tick `\n{marker}\n` on each side.")], True),
            ([], False),
        ):
            with self.subTest(nodes=nodes):
                pr = {"headRefOid": HEAD, "baseRefName": "develop", "comments": {"nodes": nodes}}
                self.assertIs(expected, pr_review_rounds.attested(pr))

    def test_a_promotion_is_a_pull_request_into_the_default_branch(self) -> None:
        for base, default, expected in (
            ("main", "main", True),
            ("develop", "main", False),
            ("develop", None, True),
        ):
            with self.subTest(base=base, default=default):
                pr = {
                    "baseRefName": base,
                    "baseRepository": {"defaultBranchRef": {"name": default} if default else None},
                }
                self.assertIs(expected, pr_review_rounds.promotion(pr))

    def test_the_base_branch_alone_decides_the_hold_and_the_local_cover(self) -> None:
        """No Copilot round is needed first, so an attested head with none is covered off main."""
        marker = f"<!-- fleet-local-review: head={HEAD} base=develop findings=0 -->"
        for base, expected in (("develop", True), ("main", False)):
            with self.subTest(base=base):
                pr = {
                    **payload([]),
                    "baseRefName": base,
                    "baseRepository": {"defaultBranchRef": {"name": "main"}},
                    "comments": {"nodes": [self.comment(marker.replace("develop", base))]},
                }
                self.assertIs(expected, pr_review_rounds.holds(pr))
                self.assertIs(expected, pr_review_rounds.local_cover(pr))


class TestWriteCommandsPartitionParserChoices(unittest.TestCase):
    """WRITE_COMMANDS and READ_ONLY_COMMANDS must together account for every parser choice.

    `wait` became a write without a subcommand being added, so nothing prompted the table to be
    checked. This ties the two hand-maintained sets to the parser's own `cmd` choices instead of
    trusting them to stay in step: a subcommand added to the parser and left out of both sets
    fails this test until someone classifies it as a write or a read.
    """

    def test_write_and_read_only_commands_together_equal_the_parsers_cmd_choices(self) -> None:
        original_add_argument = argparse.ArgumentParser.add_argument
        captured_choices = []

        def capture(self, *args, **kwargs):
            action = original_add_argument(self, *args, **kwargs)
            if args and args[0] == "cmd":
                captured_choices.append(action.choices)
            return action

        with (
            mock.patch.object(argparse.ArgumentParser, "add_argument", capture),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            pr_review.main([])
        self.assertEqual(set(captured_choices[0]), set(WRITE_COMMANDS) | set(READ_ONLY_COMMANDS))


class TestConsoleIsUtf8(unittest.TestCase):
    """Printing reviewer text must not depend on the host code page."""

    SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")

    def run_child(self, code: str) -> subprocess.CompletedProcess[bytes]:
        """Run code in a child whose stdout defaults to cp1252, as on a Windows host."""
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env["PYTHONIOENCODING"] = "cp1252"
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            check=False,
            env=env,
            timeout=60,
        )

    def test_a_character_outside_the_code_page_prints(self) -> None:
        """A character cp1252 cannot map prints as UTF-8 once main starts."""
        code = (
            f"import sys; sys.path.insert(0, {self.SCRIPTS!r}); import pr_review; "
            "pr_review.utf8_console(); print('\\u014d\\U0001f3af')"
        )
        got = self.run_child(code)
        self.assertEqual(got.returncode, 0, got.stderr.decode("utf-8", "replace"))
        self.assertEqual(got.stdout.decode("utf-8").strip(), "\u014d\U0001f3af")

    def test_main_reconfigures_before_parsing(self) -> None:
        """The entry point applies the reconfigure, so the helper is not dead code."""
        with (
            mock.patch.object(pr_review, "utf8_console") as called,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            pr_review.main([])
        called.assert_called_once_with()

    def test_a_stream_without_reconfigure_is_left_alone(self) -> None:
        """A harness may substitute a stream with no reconfigure, which must not raise."""
        with mock.patch.object(sys, "stdout", io.StringIO()):
            pr_review.utf8_console()


class TestHarness(unittest.TestCase):
    def test_this_module_collects_a_plausible_number_of_cases(self) -> None:
        """A module whose cases fail to load still reports OK, which is a pass proving nothing."""
        loaded = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertGreaterEqual(loaded.countTestCases(), 48)


class TestWorkflowRunErrorTolerance(unittest.TestCase):
    """`gh_graphql` tolerates a per-field `workflowRun` error and no other."""

    WORKFLOW_RUN_PATH: ClassVar[list[str]] = [
        "repository",
        "pullRequest",
        "commits",
        "checkSuite",
        "workflowRun",
    ]

    def read(self, payload_in: dict, code: int = 1) -> dict:
        """Run `gh_graphql` against a constructed response, as `gh api graphql` emits it."""
        done = subprocess.CompletedProcess([], code, json.dumps(payload_in), "")
        with mock.patch.object(pr_review.subprocess, "run", return_value=done):
            return pr_review.gh_graphql("query{ x }")

    def test_a_workflow_run_only_error_is_tolerated_with_the_data_kept(self) -> None:
        data = {"repository": {"name": "r"}}
        errors = [{"message": "forbidden", "path": self.WORKFLOW_RUN_PATH}]
        with contextlib.redirect_stderr(io.StringIO()):
            got = self.read({"data": data, "errors": errors})
        self.assertEqual({**data, pr_review_checks.WORKFLOW_KEYS_UNAVAILABLE: True}, got)

    def test_tolerated_errors_with_null_data_abort(self) -> None:
        errors = [{"message": "forbidden", "path": self.WORKFLOW_RUN_PATH}]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.read({"data": None, "errors": errors})
        self.assertIn("returned no data", str(raised.exception))

    def test_a_mixed_error_set_still_aborts(self) -> None:
        errors = [
            {"message": "forbidden", "path": self.WORKFLOW_RUN_PATH},
            {"message": "other", "path": ["repository", "pullRequest", "reviews"]},
        ]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.read({"data": {"repository": {}}, "errors": errors})

    def test_an_error_on_another_path_or_with_no_path_still_aborts(self) -> None:
        for error in (
            {"message": "x", "path": ["repository", "workflowRunX"]},
            {"message": "x", "path": []},
            {"message": "x"},
        ):
            with (
                self.subTest(error=error),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                self.read({"data": {"repository": {}}, "errors": [error]})

    def test_a_failed_call_with_no_json_errors_still_aborts(self) -> None:
        done = subprocess.CompletedProcess([], 1, "", "HTTP 502")
        with (
            mock.patch.object(pr_review.subprocess, "run", return_value=done),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            pr_review.gh_graphql("query{ x }")

    def test_the_dedupe_falls_back_to_the_app_slug_for_a_null_workflow_run(self) -> None:
        """A null `workflowRun` groups by app slug, so two suites of one app still supersede."""
        old = check(
            name="lint", conclusion="FAILURE", suite=10, workflow=None, slug="github-actions"
        )
        new = check(
            name="lint", conclusion="SUCCESS", suite=20, workflow=None, slug="github-actions"
        )
        other = check(name="lint", conclusion="FAILURE", suite=5, workflow=None, slug="other-app")
        nodes = pr_review_checks.check_nodes(payload([review()], checks=[old, new, other]))
        kept = sorted((n["name"], n["conclusion"]) for n in nodes)
        self.assertEqual([("lint", "FAILURE"), ("lint", "SUCCESS")], kept)


if __name__ == "__main__":
    unittest.main(verbosity=2)
