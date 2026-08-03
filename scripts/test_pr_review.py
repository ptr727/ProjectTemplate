#!/usr/bin/env python3
"""Drive pr_review's digest and wait loop against responses they must read correctly.

The script exists to collapse a poll cycle into one invocation, so its failure mode is a wrong
answer rather than a crash: a review attributed to the wrong login, a review counted against a
stale head, or a wait that returns success while nothing landed. Each case below feeds a crafted
GraphQL payload and asserts the reading, with `gql` replaced so no case reaches the network.

Run as `python3 scripts/test_pr_review.py`, or under `python3 -m unittest discover -s scripts`.
"""
from __future__ import annotations
import contextlib, io, json, re, subprocess, sys, unittest
from itertools import count
from pathlib import Path
from unittest import mock

import pr_review

REPO = Path(__file__).resolve().parent.parent
RUNBOOK = REPO / '.github' / 'copilot-instructions.md'

HEAD = 'a' * 40
OLD = 'b' * 40


EARLY = '2026-08-02T10:00:00Z'
LATE = '2026-08-02T11:00:00Z'


def review(login: str = pr_review.REVIEWER, oid: str = HEAD, body: str = '',
           at: str = EARLY) -> dict:
    return {'author': {'login': login}, 'state': 'COMMENTED', 'commit': {'oid': oid},
            'body': body, 'submittedAt': at}


def comment(login: str = pr_review.REVIEWER, at: str = LATE,
            body: str = 'I have reached my quota limit and cannot review this now.') -> dict:
    return {'author': {'login': login}, 'createdAt': at, 'body': body}


def collapsed(heading: str = 'Comments suppressed due to low confidence (1)',
              finding: str = 'a.py:12 The retry count is off by one.') -> str:
    return (f'Reviewed 3 of 3 changed files.\n\n<details>\n<summary>{heading}</summary>\n\n'
            f'{finding}\n\n</details>\n')


def thread(tid: str, resolved: bool = False, login: str = pr_review.REVIEWER,
           body: str = 'A finding.', path: str = 'a.py', line: int = 1) -> dict:
    return {'id': tid, 'isResolved': resolved,
            'comments': {'nodes': [{'author': {'login': login}, 'path': path,
                                    'line': line, 'body': body}]}}


def payload(reviews: list[dict], threads: list[dict] | None = None,
            merge: str = 'CLEAN', comments: list[dict] | None = None,
            older: bool = False, older_reviews: bool = False, pending: bool = False) -> dict:
    requested = ([{'requestedReviewer': {'__typename': 'Bot', 'login': pr_review.REVIEWER}}]
                 if pending else [])
    return {'headRefOid': HEAD, 'mergeable': 'MERGEABLE', 'mergeStateStatus': merge,
            'reviews': {'nodes': reviews, 'pageInfo': {'hasPreviousPage': older_reviews}},
            'reviewThreads': {'nodes': threads or []},
            'comments': {'nodes': comments or [], 'pageInfo': {'hasPreviousPage': older}},
            'reviewRequests': {'nodes': requested}}


class GqlCase(unittest.TestCase):
    """Base that answers every `gql` call from a queue, so no case reaches the network."""

    def answer(self, *responses: dict) -> mock._patch:
        """Patch `gql` to return each response in turn, repeating the last one."""
        queue = list(responses)

        def fake(_query, _owner, _repo, _num):
            return queue.pop(0) if len(queue) > 1 else queue[0]

        return self.enterContext(mock.patch.object(pr_review, 'gql', side_effect=fake))


class TestLiveState(GqlCase):
    def test_the_review_must_be_the_reviewer_and_on_the_current_head(self) -> None:
        """A stale review is the failure the whole wait exists to avoid reporting as done."""
        for label, reviews, want in (
            ('reviewer on head', [review()], True),
            ('reviewer on an older commit', [review(oid=OLD)], False),
            ('a human on head', [review(login='ptr727')], False),
            ('no reviews at all', [], False),
            ('stale round plus a current one', [review(oid=OLD), review()], True),
        ):
            with self.subTest(case=label):
                self.answer(payload(reviews))
                self.assertEqual((HEAD, want, None), pr_review.live_state('o', 'r', 1))

    def test_a_null_author_or_commit_does_not_raise(self) -> None:
        """GraphQL returns null for a deleted account, and a crash there stalls the whole wait."""
        self.answer(payload([{'author': None, 'state': 'COMMENTED', 'commit': None}]))
        self.assertEqual((HEAD, False, None), pr_review.live_state('o', 'r', 1))


class TestAnsweredOutsideReview(unittest.TestCase):
    """A refusal answers the request without covering the head, so a wait cannot read it as pending."""

    def test_a_reviewer_comment_newer_than_every_review_is_the_answer(self) -> None:
        answer = pr_review.answered_outside_review(
            payload([review(oid=OLD, at=EARLY)], comments=[comment(at=LATE)]))
        self.assertIsNotNone(answer)
        self.assertEqual(LATE, (answer or {}).get('createdAt'))

    def test_an_answer_the_reviewer_then_superseded_is_spent(self) -> None:
        """The review it preceded did land, so the comment is history rather than a stop signal."""
        self.assertIsNone(pr_review.answered_outside_review(
            payload([review(at=LATE)], comments=[comment(at=EARLY)])))

    def test_another_account_s_comment_is_not_the_reviewer_answering(self) -> None:
        """A maintainer note and a codecov post both postdate the review and mean nothing here."""
        for login in ('ptr727', 'codecov[bot]', 'copilot-swe-agent'):
            with self.subTest(login=login):
                self.assertIsNone(pr_review.answered_outside_review(
                    payload([review(oid=OLD)], comments=[comment(login=login)])))

    def test_no_comments_at_all_reads_as_no_answer(self) -> None:
        self.assertIsNone(pr_review.answered_outside_review(payload([review(oid=OLD)])))

    def test_ordinary_discussion_does_not_push_the_answer_out_of_the_window(self) -> None:
        """The window reads the newest comments, not the reviewer's, so others crowd it."""
        chatter = [comment(login='ptr727', at=LATE) for _ in range(pr_review.WINDOW - 1)]
        found = pr_review.answered_outside_review(
            payload([review(oid=OLD, at=EARLY)], comments=[comment(at=LATE)] + chatter))
        self.assertIsNotNone(found)

    def test_comments_behind_the_window_are_unknown_rather_than_no_answer(self) -> None:
        """Finding nothing and having nothing to find are one reading once an answer can hide."""
        full = [comment(login='ptr727') for _ in range(pr_review.WINDOW)]
        self.assertTrue(
            pr_review.window_blind(payload([review()], comments=full, older=True), 'comments'))

    def test_a_window_holding_every_comment_is_not_a_gap(self) -> None:
        """A full window and a window holding the lot are the same length, so length cannot say."""
        full = [comment(login='ptr727') for _ in range(pr_review.WINDOW)]
        self.assertFalse(
            pr_review.window_blind(payload([review()], comments=full, older=False), 'comments'))

    def test_reviews_behind_the_window_report_nothing_rather_than_a_false_answer(self) -> None:
        """No reviewer review in view dates every comment as newer, so each reads as an answer.

        Reporting nothing keeps the wait polling, where a wrong answer ends it outright on a
        pull request whose review landed and simply sits behind a busier review history.
        """
        pr = payload([review(login='ptr727') for _ in range(pr_review.WINDOW)],
                     comments=[comment(at=LATE)], older_reviews=True)
        self.assertTrue(pr_review.window_blind(pr, 'reviews'))
        self.assertIsNone(pr_review.answered_outside_review(pr))

    def test_one_reviewer_review_in_view_is_a_baseline_the_answer_can_be_dated_against(self) -> None:
        """Reviews arrive in creation order too, so a hidden one is older than the one in view."""
        pr = payload([review(at=EARLY, oid=OLD)]
                     + [review(login='ptr727') for _ in range(pr_review.WINDOW - 1)],
                     comments=[comment(at=LATE)], older_reviews=True)
        self.assertFalse(pr_review.window_blind(pr, 'reviews'))
        self.assertIsNotNone(pr_review.answered_outside_review(pr))

    def test_one_spent_reviewer_comment_in_view_settles_the_question(self) -> None:
        """Comments arrive in creation order, so a hidden one is older than the spent one in view."""
        full = ([comment(at=EARLY)]
                + [comment(login='ptr727') for _ in range(pr_review.WINDOW - 1)])
        pr = payload([review(at=LATE)], comments=full, older=True)
        self.assertIsNone(pr_review.answered_outside_review(pr))
        self.assertFalse(pr_review.window_blind(pr, 'comments'))


class TestPickup(unittest.TestCase):
    """A request nothing acted on and a review being worked on are one reading from the reviews."""

    def test_a_request_with_no_pickup_after_it_is_named_by_its_timestamp(self) -> None:
        """The shape that sat thirteen hours reading as pending: requested, never started."""
        events = [('review_requested', '2026-08-02T22:58:15Z'),
                  ('copilot_work_started', '2026-08-02T22:58:45Z'),
                  ('review_requested', '2026-08-03T00:15:00Z')]
        self.assertEqual('2026-08-03T00:15:00Z', pr_review.never_picked_up(events))

    def test_a_request_the_reviewer_took_up_is_not_stalled(self) -> None:
        """Slow is not stuck, and only the pickup event tells them apart."""
        events = [('review_requested', '2026-08-03T13:09:19Z'),
                  ('copilot_work_started', '2026-08-03T13:09:54Z')]
        self.assertEqual('', pr_review.never_picked_up(events))

    def test_an_earlier_pickup_does_not_cover_a_later_request(self) -> None:
        """Answering the last request is not answering this one, and order is what says so."""
        events = [('copilot_work_started', '2026-08-02T22:58:45Z'),
                  ('review_requested', '2026-08-02T23:31:44Z')]
        self.assertEqual('2026-08-02T23:31:44Z', pr_review.never_picked_up(events))

    def test_no_request_at_all_is_not_a_stall(self) -> None:
        self.assertEqual('', pr_review.never_picked_up(
            [('copilot_work_started', '2026-08-02T22:58:45Z')]))
        self.assertEqual('', pr_review.never_picked_up([]))

    def test_the_pending_set_is_read_where_a_bot_reviewer_is_visible(self) -> None:
        """`gh pr view --json reviewRequests` omits a Bot outright and reports an empty set."""
        pending = {'reviewRequests': {'nodes': [
            {'requestedReviewer': {'__typename': 'Bot', 'login': pr_review.REVIEWER}}]}}
        self.assertTrue(pr_review.reviewer_requested(pending))
        human = {'reviewRequests': {'nodes': [
            {'requestedReviewer': {'__typename': 'User', 'login': 'ptr727'}}]}}
        self.assertFalse(pr_review.reviewer_requested(human))
        self.assertFalse(pr_review.reviewer_requested({'reviewRequests': {'nodes': []}}))
        # A null reviewer is what a deleted account leaves behind, and it must not raise.
        self.assertFalse(pr_review.reviewer_requested(
            {'reviewRequests': {'nodes': [{'requestedReviewer': None}]}}))


class TestDigest(GqlCase):
    def test_the_summary_line_counts_what_it_names(self) -> None:
        self.answer(payload([review(), review(oid=OLD)],
                            [thread('T1'), thread('T2', resolved=True)]))
        out, unresolved = pr_review.digest('o', 'r', 7)
        self.assertEqual(1, unresolved)
        self.assertIn('pr=7', out)
        self.assertIn(f'head={HEAD[:8]}', out)
        self.assertIn('rounds=2', out)
        self.assertIn('review_on_head=yes', out)
        self.assertIn('threads=2', out)
        self.assertIn('unresolved=1', out)
        self.assertIn('merge=CLEAN', out)

    def test_review_on_head_reports_no_when_every_round_is_stale(self) -> None:
        """`NO` is upper-case on purpose, so the one state that blocks a merge is not skimmed past."""
        self.answer(payload([review(oid=OLD)]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('review_on_head=NO', out)

    def test_a_thread_from_a_deleted_account_does_not_crash_the_digest(self) -> None:
        """GraphQL sends `author` present and null, which a defaulted lookup returns as None."""
        orphan = thread('T1')
        orphan['comments']['nodes'][0]['author'] = None
        self.answer(payload([review()], [orphan, thread('T2')]))
        out, unresolved = pr_review.digest('o', 'r', 7)
        self.assertEqual(1, unresolved)
        self.assertIn('T2', out)

    def test_only_the_reviewer_s_own_unresolved_threads_are_listed(self) -> None:
        """A maintainer's own open thread is not a review finding to answer."""
        self.answer(payload([review()], [thread('T1', login='ptr727'), thread('T2')]))
        out, unresolved = pr_review.digest('o', 'r', 7)
        self.assertEqual(1, unresolved)
        self.assertIn('T2', out)
        self.assertNotIn('T1', out)

    def test_a_seen_set_marks_each_thread_new_exactly_once(self) -> None:
        """The seen set is what tells a second round's findings from the ones already answered."""
        seen: set[str] = set()
        self.answer(payload([review()], [thread('T1')]),
                    payload([review()], [thread('T1'), thread('T2')]))
        first, _ = pr_review.digest('o', 'r', 7, seen)
        self.assertIn('NEW T1', first)
        self.assertIn('new=1', first)
        second, unresolved = pr_review.digest('o', 'r', 7, seen)
        self.assertNotIn('T1', second)
        self.assertIn('NEW T2', second)
        self.assertIn('new=1', second)
        # The count still reports every open thread, not only the newly seen ones.
        self.assertEqual(2, unresolved)

    def test_a_body_is_flattened_and_bounded(self) -> None:
        """A multi-line finding on one digest line keeps the digest a few hundred bytes."""
        self.answer(payload([review()], [thread('T1', body='one\n  two\t\tthree ' + 'x' * 400)]))
        out, _ = pr_review.digest('o', 'r', 7)
        body = out.split('\n')[1]
        self.assertIn('one two three', body)
        self.assertLessEqual(len(body.split('a.py:1 ')[1]), 160)


class TestSuppressed(GqlCase):
    """The findings collapsed in a review body, which reach no thread and so no thread poll."""

    def test_either_documented_heading_counts_and_a_clean_body_does_not(self) -> None:
        """One phrasing alone reports zero on a review that has them, the false clean once more."""
        for label, body, want in (
            ('the current heading', collapsed(), 1),
            ('the earlier heading', collapsed(heading='Suppressed comments (2)'), 2),
            ('a heading with no count', collapsed(heading='Suppressed comments'), 1),
            ('a clean pass', 'Reviewed 3 of 3 changed files and generated no comments.', 0),
            ('no body at all', '', 0),
        ):
            with self.subTest(case=label):
                self.answer(payload([review(body=body)]))
                out, _ = pr_review.digest('o', 'r', 7)
                self.assertIn(f'suppressed={want}', out)

    def test_a_block_on_a_review_with_no_commit_names_that_rather_than_an_empty_sha(self) -> None:
        """GraphQL returns a null commit for a pending review, and the sha is what traces it.

        Rendered from an empty string it read "raised on , earlier round", which loses the round
        and reads as a formatting glitch rather than as a finding that still needs an answer.
        """
        self.answer(payload([review(body=collapsed()) | {'commit': None}]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('commit unknown, treat as outstanding', out)
        self.assertNotIn('raised on ,', out)
        # It still counts, since an unknown round is not a reason to drop a finding.
        self.assertIn('suppressed=1', out)

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
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('suppressed=1', out)
        self.assertIn('earlier=1', out)
        self.assertIn('earlier round', out)

    def test_the_finding_prints_whole_under_a_marker_naming_the_answer(self) -> None:
        """A thread can be re-read at its id and truncates for that reason, and this cannot."""
        finding = 'a.py:12 ' + ('the same clause repeated. ' * 20).strip()
        self.answer(payload([review(body=collapsed(finding=finding))]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('no thread to resolve, answer it in the PR conversation', out)
        self.assertIn(finding, out)
        # The `<details>` wrapper is markup around the finding, not part of it.
        self.assertNotIn('<summary>', out)
        self.assertNotIn('<details>', out)

    def test_a_body_naming_the_block_outside_a_details_wrapper_still_reports(self) -> None:
        """Reporting zero because the markup moved is the failure the whole case guards."""
        self.answer(payload([review(body='Suppressed comments (1)\n\na.py:12 Off by one.')]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('suppressed=1', out)
        self.assertIn('a.py:12 Off by one.', out)

    def test_the_per_file_summary_block_beside_it_is_not_a_finding(self) -> None:
        """Every real body collapses a file table too, and reporting that is noise, not a finding."""
        body = ('<details>\n<summary>Show a summary per file</summary>\n\n'
                '| File | Description |\n\n</details>\n' + collapsed())
        self.answer(payload([review(body=body)]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('suppressed=1', out)
        self.assertNotIn('Show a summary per file', out)
        self.assertIn('The retry count is off by one.', out)

    def test_the_count_is_findings_rather_than_blocks(self) -> None:
        """A body holds one block per round, so counting blocks reports two findings as one."""
        self.answer(payload([review(body=collapsed(heading='Suppressed comments (3)')),
                             review(body=collapsed(heading='Suppressed comments (2)'))]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('suppressed=5', out)

    def test_prose_that_merely_discusses_the_phrase_is_not_a_block(self) -> None:
        """This PR's own review body was the false positive, discussing the phrase and carrying none."""
        body = ('## Pull request overview\n\nThis PR reports the suppressed and low confidence '
                'findings that reach no thread (and are therefore invisible to a thread poll).\n\n'
                '<details>\n<summary>Show a summary per file</summary>\n\n| File |\n\n</details>\n')
        self.answer(payload([review(body=body)]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('suppressed=0', out)
        self.assertNotIn('SUPPRESSED', out)

    def test_a_human_review_carrying_the_phrase_is_not_a_copilot_finding(self) -> None:
        self.answer(payload([review(login='ptr727', body=collapsed()), review()]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('suppressed=0', out)


class TestDigestReportsTheAnswer(GqlCase):
    def test_the_comment_prints_whole_under_a_marker_naming_it_terminal(self) -> None:
        """Its wording is what separates a refusal from a remark, so it is not truncated."""
        text = 'Copilot has reached its quota limit.\nTry again after the window resets.'
        self.answer(payload([review(oid=OLD)], comments=[comment(body=text)]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('answered_outside_review=yes', out)
        self.assertIn('COPILOT COMMENT', out)
        for line in text.splitlines():
            self.assertIn(line, out)

    def test_a_pull_request_with_no_such_answer_says_so_rather_than_staying_silent(self) -> None:
        self.answer(payload([review()]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('answered_outside_review=no', out)

    def test_an_unreadable_window_reports_unknown_and_names_why(self) -> None:
        """Reporting `no` off a window an answer can hide behind is the false clean to avoid."""
        self.answer(payload([review()], older=True,
                            comments=[comment(login='ptr727')
                                      for _ in range(pr_review.WINDOW)]))
        out, _ = pr_review.digest('o', 'r', 7)
        self.assertIn('answered_outside_review=unknown', out)
        self.assertIn('BEHIND THE WINDOW (comments)', out)


class TestGqlTransport(unittest.TestCase):
    def test_a_failed_call_raises_rather_than_returning_an_empty_reading(self) -> None:
        """Returning nothing on failure would read as a PR with no reviews and no threads."""
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout='', stderr='boom')
        with mock.patch.object(pr_review.subprocess, 'run', return_value=failed), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                pr_review.gql('query', 'o', 'r', 1)
        self.assertIn('boom', err.getvalue())

    def test_a_successful_call_unwraps_to_the_pull_request(self) -> None:
        body = json.dumps({'data': {'repository': {'pullRequest': {'headRefOid': HEAD}}}})
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=body, stderr='')
        with mock.patch.object(pr_review.subprocess, 'run', return_value=done) as run:
            self.assertEqual({'headRefOid': HEAD}, pr_review.gql('query', 'o', 'r', 1))
        # Read-only: the transport shells out to `gh api graphql` and nothing else.
        self.assertEqual(['gh', 'api', 'graphql'], run.call_args.args[0][:3])


class TestCli(GqlCase):
    def setUp(self) -> None:
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_status_prints_the_digest_and_exits_zero(self) -> None:
        self.answer(payload([review()]))
        self.assertEqual(0, pr_review.main(['status', '7']))
        self.assertIn('pr=7', self.out.getvalue())

    def test_wait_returns_zero_once_the_review_lands_on_the_head(self) -> None:
        """The first poll already sees it, so the loop body never runs."""
        self.answer(payload([review()]))
        with mock.patch.object(pr_review.time, 'sleep') as slept:
            self.assertEqual(0, pr_review.main(['wait', '7']))
        slept.assert_not_called()
        self.assertIn('waited=', self.out.getvalue())

    def test_wait_polls_again_after_a_pending_round(self) -> None:
        """Each iteration re-reads the head, since a push during the wait moves it."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        with mock.patch.object(pr_review.time, 'sleep') as slept:
            self.assertEqual(0, pr_review.main(['wait', '7']))
        self.assertEqual(1, slept.call_count)

    def test_wait_exits_thirty_at_the_timeout_rather_than_reporting_success(self) -> None:
        """Pending is not failure and not success, so it takes a code of its own."""
        self.answer(payload([review(oid=OLD)]))
        with mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(30, pr_review.main(['wait', '7', '--timeout', '0']))
        self.assertIn('status=PENDING', self.out.getvalue())

    def test_the_timeout_carries_the_digest_rather_than_a_bare_pending_line(self) -> None:
        """A wait that ends with no evidence reports a slow reviewer and a broken poll alike."""
        self.answer(payload([review(oid=OLD)], [thread('T1')]))
        with mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(30, pr_review.main(['wait', '7', '--timeout', '0']))
        out = self.out.getvalue()
        self.assertIn('review_on_head=NO', out)
        self.assertIn('unresolved=1', out)

    def test_wait_ends_on_an_answer_outside_a_review_instead_of_waiting_it_out(self) -> None:
        """A refusal covers no head, so polling on for the timeout waits for nothing.

        The zero timeout is what this case fails on rather than hangs on: an answer read as
        pending spins the loop for the whole default wait, and a case that hangs gates nothing.
        """
        self.answer(payload([review(oid=OLD)], comments=[comment()]))
        with mock.patch.object(pr_review.time, 'sleep') as slept:
            self.assertEqual(40, pr_review.main(['wait', '7', '--timeout', '0']))
        slept.assert_not_called()
        out = self.out.getvalue()
        self.assertIn('status=ANSWERED_OUTSIDE_REVIEW', out)
        self.assertIn('quota', out)

    def test_a_landed_review_wins_over_an_older_answer(self) -> None:
        """Coverage is the success case, and a spent comment does not downgrade it to 40."""
        self.answer(payload([review(at=LATE)], comments=[comment(at=EARLY)]))
        with mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(0, pr_review.main(['wait', '7']))

    def test_wait_stops_on_a_request_nothing_picked_up(self) -> None:
        """Waiting on cannot start a request nothing is acting on, so the wait says so and ends.

        The zero timeout is what this fails on rather than hangs on, and it also pins the order:
        the pickup is read before the clock, so the stall reports as itself instead of as PENDING.
        """
        self.answer(payload([review(oid=OLD)], pending=True))
        with mock.patch.object(pr_review, 'timeline',
                               return_value=[('review_requested', LATE)]), \
                mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(50, pr_review.main(
                ['wait', '7', '--pickup-grace', '0', '--timeout', '0']))
        out = self.out.getvalue()
        self.assertIn('status=REQUEST_NOT_PICKED_UP', out)
        self.assertIn(LATE, out)

    def test_a_request_being_worked_on_is_not_stopped_on(self) -> None:
        """A slow round is the case the grace exists for, and stopping on it loses the review."""
        self.answer(payload([review(oid=OLD)], pending=True), payload([review()], pending=True))
        with mock.patch.object(pr_review, 'timeline',
                               return_value=[('review_requested', EARLY),
                                             ('copilot_work_started', LATE)]), \
                mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(0, pr_review.main(
                ['wait', '7', '--pickup-grace', '0', '--timeout', '600']))

    def test_the_pickup_read_waits_out_the_grace_rather_than_running_per_poll(self) -> None:
        """It costs a second call, and inside the grace a pending request is just work in flight."""
        self.answer(payload([review(oid=OLD)], pending=True), payload([review()], pending=True))
        with mock.patch.object(pr_review, 'timeline', return_value=[]) as seen, \
                mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(0, pr_review.main(
                ['wait', '7', '--pickup-grace', '9999', '--timeout', '600']))
        seen.assert_not_called()

    def test_the_pickup_read_runs_on_its_own_interval_once_the_grace_is_out(self) -> None:
        """Every poll past the grace is what the comment ruled out and the code did anyway.

        The clock advances a fixed step per reading, so the interval is counted rather than
        waited: a long wait must not turn one REST reader into one per poll.
        """
        picked_up = [('review_requested', EARLY), ('copilot_work_started', LATE)]
        self.answer(payload([review(oid=OLD)], pending=True))
        with mock.patch.object(pr_review.time, 'monotonic', side_effect=count(0, 30)), \
                mock.patch.object(pr_review, 'timeline', return_value=picked_up) as seen, \
                mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(30, pr_review.main(
                ['wait', '7', '--pickup-grace', '300', '--timeout', '1200']))
        # Roughly one read per grace interval over the wait, never one per poll.
        self.assertGreaterEqual(seen.call_count, 1)
        self.assertLessEqual(seen.call_count, 1200 // 300 + 1)

    def test_a_review_landing_during_the_last_read_wins_over_the_stalled_code(self) -> None:
        """The digest and the exit code come from one payload, or they describe different PRs.

        An automated reader resolves a digest saying covered against a code saying stalled by
        believing the code, so the review it just printed is the thing that gets dropped.
        """
        self.answer(payload([review(oid=OLD)], pending=True), payload([review()], pending=True))
        with mock.patch.object(pr_review, 'timeline',
                               return_value=[('review_requested', LATE)]), \
                mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(0, pr_review.main(
                ['wait', '7', '--pickup-grace', '0', '--timeout', '0']))
        out = self.out.getvalue()
        self.assertIn('review_on_head=yes', out)
        self.assertNotIn('status=REQUEST_NOT_PICKED_UP', out)

    def test_a_review_landing_during_the_last_read_wins_over_the_timeout(self) -> None:
        """Same disagreement at the other exit: printing coverage and returning PENDING."""
        self.answer(payload([review(oid=OLD)]), payload([review()]))
        with mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(0, pr_review.main(['wait', '7', '--timeout', '0']))
        out = self.out.getvalue()
        self.assertIn('review_on_head=yes', out)
        self.assertNotIn('status=PENDING', out)

    def test_a_request_picked_up_after_the_loop_read_it_is_not_reported_as_stalled(self) -> None:
        """The stall is re-read at the end, or a request taken up since still reports as dead."""
        self.answer(payload([review(oid=OLD)], pending=True))
        picked_up = [('review_requested', EARLY), ('copilot_work_started', LATE)]
        with mock.patch.object(pr_review, 'timeline',
                               side_effect=[[('review_requested', LATE)], picked_up]), \
                mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(30, pr_review.main(
                ['wait', '7', '--pickup-grace', '0', '--timeout', '0']))
        out = self.out.getvalue()
        self.assertNotIn('status=REQUEST_NOT_PICKED_UP', out)
        self.assertNotIn('REQUEST NOT PICKED UP', out)

    def test_an_answer_outranks_a_stall_when_both_are_true(self) -> None:
        """The reviewer saying something outranks it saying nothing, and the digest shows both."""
        self.answer(payload([review(oid=OLD)], comments=[comment()], pending=True))
        with mock.patch.object(pr_review, 'timeline',
                               return_value=[('review_requested', LATE)]), \
                mock.patch.object(pr_review.time, 'sleep'):
            self.assertEqual(40, pr_review.main(
                ['wait', '7', '--pickup-grace', '0', '--timeout', '0']))

    def test_the_repo_argument_splits_into_owner_and_name(self) -> None:
        self.answer(payload([review()]))
        with mock.patch.object(pr_review, 'digest', return_value=('x', 0)) as dig:
            self.assertEqual(0, pr_review.main(['status', '7', '--repo', 'owner/name']))
        self.assertEqual(('owner', 'name', 7), dig.call_args.args)


class TestContract(unittest.TestCase):
    def test_the_reviewer_login_matches_the_runbook_graphql_form(self) -> None:
        """GraphQL drops the `[bot]` suffix REST carries, and this script is GraphQL-only.

        The runbook is the source, so this reads it rather than restating the string.
        """
        text = RUNBOOK.read_text(encoding='utf-8')
        self.assertIn(f'`{pr_review.REVIEWER}`, with **no `[bot]` suffix**', text)
        self.assertFalse(pr_review.REVIEWER.endswith('[bot]'))

    def test_the_suppressed_pattern_is_the_runbook_alternation(self) -> None:
        """The heading wording has changed once, so the pattern tracks the runbook, not a memory."""
        text = RUNBOOK.read_text(encoding='utf-8')
        self.assertIn(f'test("{pr_review.SUPPRESSED.pattern}")', text)

    def test_no_mutation_reaches_this_script(self) -> None:
        """Mutations stay as explicit `gh` calls so the write-guard hook and review still see them."""
        source = (REPO / 'scripts' / 'pr_review.py').read_text(encoding='utf-8')
        # The GraphQL keyword is matched with its opening token, so naming the runbook is not a hit.
        for verb in ('mutation(', 'mutation{', 'mutation {', '-X POST', '-X PATCH', '-X PUT',
                     '-X DELETE', 'gh pr merge', 'gh pr review'):
            with self.subTest(verb=verb):
                self.assertFalse(verb in source, f'{verb!r} is a state-changing call in a read-only script')

    def test_the_guard_tests_the_window_the_queries_actually_read(self) -> None:
        """A guard measuring one number while the query fetches another reads clean on drift."""
        source = (REPO / 'scripts' / 'pr_review.py').read_text(encoding='utf-8')
        windows = set(re.findall(r'(?:comments|reviews)\(last:(\d+)\)', source))
        self.assertEqual({str(pr_review.WINDOW)}, windows)
        # The guard reads `hasPreviousPage`, so a connection that stops asking reports no.
        # That is the silent narrowing this holds every window against.
        # Four: reviews and comments, in each of the two queries.
        self.assertEqual(4, source.count('pageInfo{ hasPreviousPage }'))
        self.assertEqual(4, len(re.findall(r'(?:comments|reviews)\(last:\d+\)', source)))

    def test_the_timeline_reader_asks_for_the_largest_page(self) -> None:
        """The page size is what pagination costs, and the default of 30 triples the requests."""
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')
        with mock.patch.object(pr_review.subprocess, 'run', return_value=done) as run:
            pr_review.timeline('o', 'r', 7)
        argv = run.call_args.args[0]
        self.assertIn('repos/o/r/issues/7/timeline?per_page=100', argv)
        self.assertIn('--paginate', argv)
        # A read, and the guard against a write creeping into the one REST call here.
        self.assertEqual(['gh', 'api'], argv[:2])
        self.assertFalse({'-X', '--method'} & set(argv))

    def test_the_timeline_filter_takes_the_reviewer_s_own_requests_only(self) -> None:
        """A human requested later is not this request, and reading it as one reports a stall.

        The filter runs inside gh, so this drives the real `jq` over a crafted timeline rather
        than asserting on the filter's text, which would pass on a filter that matches nothing.
        The timeline spells the reviewer `Copilot` with type `Bot`, a third form after GraphQL's
        `copilot-pull-request-reviewer` and REST's `[bot]` suffix on that, so a filter keyed to
        either of those two selects nothing here and the whole state reads as no request at all.
        """
        events = [
            {'event': 'review_requested', 'created_at': '01', 'requested_reviewer':
             {'login': 'Copilot', 'type': 'Bot'}},
            {'event': 'copilot_work_started', 'created_at': '02'},
            {'event': 'review_requested', 'created_at': '03', 'requested_reviewer':
             {'login': 'ptr727', 'type': 'User'}},
            {'event': 'review_requested', 'created_at': '04', 'requested_reviewer':
             {'login': 'some-other-bot', 'type': 'Bot'}},
            {'event': 'commented', 'created_at': '05'},
        ]
        run = subprocess.run(['jq', '-r', pr_review.TIMELINE_JQ],
                             input=json.dumps(events), capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(['review_requested 01', 'copilot_work_started 02'],
                         run.stdout.split('\n')[:-1])
        # The reading that matters: the human request must not become the newest request.
        parsed = [(ln.split(' ', 1)[0], ln.split(' ', 1)[1]) for ln in run.stdout.splitlines()]
        self.assertEqual('', pr_review.never_picked_up(parsed))

    def test_a_negative_pickup_grace_is_rejected_rather_than_read_as_every_poll(self) -> None:
        """It leaves the next reading behind the clock, which is the per-poll pattern returning."""
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                pr_review.main(['wait', '7', '--pickup-grace', '-1'])

    def test_a_failed_timeline_read_raises_rather_than_reading_as_no_events(self) -> None:
        """An empty list reads as no request pending, which is the false clean one level up."""
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout='', stderr='boom')
        with mock.patch.object(pr_review.subprocess, 'run', return_value=failed), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                pr_review.timeline('o', 'r', 7)
        self.assertIn('boom', err.getvalue())

    def test_the_backoff_is_bounded_and_non_decreasing(self) -> None:
        """A wait that sleeps zero seconds is a busy loop, and one that shrinks polls harder later."""
        source = (REPO / 'scripts' / 'pr_review.py').read_text(encoding='utf-8')
        delays = [int(n) for n in
                  source.split('delays = [')[1].split(']')[0].replace(' ', '').split(',')]
        self.assertGreaterEqual(len(delays), 3)
        self.assertTrue(all(d > 0 for d in delays))
        self.assertEqual(delays, sorted(delays))


class TestHarness(unittest.TestCase):
    def test_this_module_collects_a_plausible_number_of_cases(self) -> None:
        """A module whose cases fail to load still reports OK, which is a pass proving nothing."""
        loaded = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertGreaterEqual(loaded.countTestCases(), 20)


if __name__ == '__main__':
    unittest.main(verbosity=2)
